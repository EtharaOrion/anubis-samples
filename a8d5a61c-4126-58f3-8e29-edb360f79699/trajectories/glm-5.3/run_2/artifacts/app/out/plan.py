#!/usr/bin/env python3
"""TrainMix allocation compiler.

Derives the weighted training plan, phase allocations, batch manifests and the
allocation certificate from the manifest set in the data directory given on the
command line. Every number is an integer or an exact rational; no floating point
value is computed, compared or serialized. Standard library only.

Usage: python3 plan.py <DATA_DIR> <OUT_DIR>
"""

import csv
import hashlib
import json
import os
import sys
from fractions import Fraction

PHASES = ["PH1", "PH2", "PH3"]
DOMAIN_PHASES = {
    "maint": ["PH1"],
    "manual": ["PH1", "PH2"],
    "incident": ["PH2"],
    "qa": ["PH2", "PH3"],
    "translated": ["PH1", "PH3"],
    "synthetic": ["PH3"],
    "reg": ["PH2"],
}
HARDNESS_RANK = {"soft": 0, "hard": 1}


# ------------------------------------------------------------------ arithmetic

def r_half_even(p, d):
    """round_half_even(p, d) for an integer numerator and positive denominator."""
    if d <= 0:
        raise ValueError("non positive denominator")
    neg = p < 0
    q, r = divmod(abs(p), d)
    if 2 * r > d or (2 * r == d and q % 2 == 1):
        q += 1
    return -q if neg else q


def is_exact_half(p, d):
    return (abs(p) % d) * 2 == d


def largest_remainder(total, keys, weight):
    """Split an integer total across keys in proportion to a positive weight."""
    wsum = sum(weight[k] for k in keys)
    if wsum == 0 or total == 0:
        return {k: 0 for k in keys}
    base = {}
    order = []
    for k in keys:
        q, r = divmod(total * weight[k], wsum)
        base[k] = q
        order.append((-r, k))
    order.sort()
    left = total - sum(base.values())
    for i in range(left):
        base[order[i][1]] += 1
    return base


# ----------------------------------------------------------------------- input

def read_csv(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def read_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------- output

class Out:
    """Holds derived documents in memory so each is written exactly once."""

    def __init__(self):
        self.docs = []

    def jsonl(self, name, rows):
        text = "".join(
            json.dumps(r, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False) + "\n" for r in rows)
        self.docs.append((name, text.encode("utf-8")))

    def json_doc(self, name, obj):
        text = json.dumps(obj, sort_keys=True, indent=2,
                          ensure_ascii=False) + "\n"
        self.docs.append((name, text.encode("utf-8")))

    def replace(self, name, data):
        self.docs = [(n, d) for n, d in self.docs if n != name]
        self.docs.append((name, data))

    def write(self, outdir):
        for name, data in self.docs:
            with open(os.path.join(outdir, name), "wb") as fh:
                fh.write(data)


# --------------------------------------------------------------------- stages

def stage1_canonical(sources, units):
    canon, rows, ties = {}, [], []
    for s in sources:
        sid = s["source_id"]
        amt = int(s["raw_amount"])
        u = units[s["raw_unit"]]
        num, den = int(u["num"]), int(u["den"])
        tie = is_exact_half(amt * num, den)
        tok = r_half_even(amt * num, den)
        canon[sid] = tok
        rows.append({"source_id": sid, "raw_amount": amt,
                     "raw_unit": s["raw_unit"], "canonical_tokens": tok,
                     "rounding_tie": tie})
        if tie:
            ties.append(sid)
    return canon, rows, ties


def stage2_eligibility(sources, canon, quality, qpol, langpol, spol):
    minq = Fraction(qpol["min_quality"]["num"], qpol["min_quality"]["den"])
    blocked = qpol["blocked_domains"]
    rows, elig = [], {}
    for s in sources:
        sid = s["source_id"]
        qn, qd = quality[sid]["q_num"], quality[sid]["q_den"]
        if qd == 0 or Fraction(qn, qd) < minq:
            code = "E_LOW_QUALITY"
        elif s["language"] not in langpol["languages"]:
            code = "E_LANG_EXCLUDED"
        elif s["safety_stratum"] not in spol["strata"]:
            code = "E_STRATUM_UNKNOWN"
        elif s["domain"] in blocked:
            code = "E_DOMAIN_BLOCKED"
        elif canon[sid] <= 0:
            code = "E_EMPTY_TOKENS"
        else:
            code = "E_OK"
        elig[sid] = code == "E_OK"
        rows.append({"source_id": sid, "eligible": elig[sid], "code": code})
    return elig, rows


def stage3_demand(sources, elig, canon, quality, freshness, sw, langpol):
    scale = int(sw["scale"])
    half_life = int(sw["half_life_days"])
    rows, scores = [], {}
    for s in sources:
        sid = s["source_id"]
        if not elig[sid]:
            continue
        qn, qd = quality[sid]["q_num"], quality[sid]["q_den"]
        quality_bp = r_half_even(qn * scale, qd)
        fresh_bp = r_half_even(half_life * scale,
                               half_life + freshness[sid])
        priority_factor = int(sw["priority_weights"][str(s["priority"])])
        lang_balance_bp = int(
            langpol["languages"][s["language"]]["balance_bp"])
        s1 = r_half_even(canon[sid] * quality_bp, scale)
        s2 = r_half_even(s1 * fresh_bp, scale)
        s3 = s2 * priority_factor
        score = r_half_even(s3 * lang_balance_bp, scale)
        scores[sid] = score
        rows.append({"source_id": sid, "quality_bp": quality_bp,
                     "fresh_bp": fresh_bp, "priority_factor": priority_factor,
                     "lang_balance_bp": lang_balance_bp,
                     "demand_score": score})
    return scores, rows


def transitive_closure(nodes, edges):
    """reach[a] is every node reachable from a by one or more edges."""
    succ = {n: set() for n in nodes}
    for e in edges:
        succ[e["from"]].add(e["to"])
    reach = {n: set(succ[n]) for n in nodes}
    for _ in range(len(nodes)):
        changed = False
        for n in nodes:
            new = set()
            for b in reach[n]:
                new |= reach[b]
            if not new <= reach[n]:
                reach[n] |= new
                changed = True
        if not changed:
            break
    return reach


def topo_order(nodes, edges):
    """Kahn order over surviving edges, ties broken by ascending name."""
    remaining = list(nodes)
    order = []
    while remaining:
        ready = sorted(n for n in remaining
                       if not any(e["to"] == n and e["from"] in remaining
                                  for e in edges))
        order.append(ready[0])
        remaining.remove(ready[0])
    return order


def main():
    if len(sys.argv) != 3:
        sys.stderr.write("usage: plan.py <DATA_DIR> <OUT_DIR>\n")
        return 2
    data_dir, out_dir = sys.argv[1], sys.argv[2]
    os.makedirs(out_dir, exist_ok=True)

    sources = read_csv(os.path.join(data_dir, "sources.csv"))
    quality = {r["source_id"]: {"q_num": int(r["q_num"]),
                                "q_den": int(r["q_den"])}
               for r in read_csv(os.path.join(data_dir, "quality.csv"))}
    freshness = {r["source_id"]: int(r["age_days"])
                 for r in read_csv(os.path.join(data_dir, "freshness.csv"))}
    examples = read_csv(os.path.join(data_dir, "examples.csv"))
    capacity = {r["phase"]: {"budget_tokens": int(r["budget_tokens"]),
                             "cap_tokens": int(r["cap_tokens"])}
                for r in read_csv(os.path.join(data_dir, "capacity.csv"))}
    carry0 = {r["phase"]: {"credit": int(r["initial_credit"]),
                           "debt": int(r["initial_debt"])}
              for r in read_csv(os.path.join(data_dir, "carry_state.csv"))}
    units = read_json(os.path.join(data_dir, "unit_rules.json"))["units"]
    qpol = read_json(os.path.join(data_dir, "quality_policy.json"))
    langpol = read_json(os.path.join(data_dir, "language_policy.json"))
    spol = read_json(os.path.join(data_dir, "safety_policy.json"))
    prio = read_json(os.path.join(data_dir, "priority_order.json"))
    sw = read_json(os.path.join(data_dir, "scoring_weights.json"))
    brules = read_json(os.path.join(data_dir, "batch_rules.json"))
    curriculum = read_json(os.path.join(data_dir, "curriculum.json"))

    for ph in PHASES:
        capacity.setdefault(ph, {"budget_tokens": 0, "cap_tokens": 0})
        carry0.setdefault(ph, {"credit": 0, "debt": 0})
    src_order = [s["source_id"] for s in sources]
    src = {s["source_id"]: s for s in sources}
    phases = list(PHASES)
    nodes = set(phases)
    for e in curriculum["edges"]:
        nodes.add(e["from"])
        nodes.add(e["to"])
    ex = {}
    for r in examples:
        ex.setdefault(r["source_id"], []).append(
            (r["example_id"], int(r["token_length"])))
    for sid in ex:
        ex[sid].sort()

    out = Out()
    quiet = []

    # stage 1, manifest canonicalization
    canon, canon_rows, ties = stage1_canonical(sources, units)
    for sid in ties:
        quiet.append(("B_UNIT_ROUND_TIE", sid))
    out.jsonl("canonical_sources.jsonl", canon_rows)

    # stage 2, eligibility projection
    elig, elig_rows = stage2_eligibility(sources, canon, quality, qpol,
                                         langpol, spol)
    out.jsonl("eligibility_ledger.jsonl", elig_rows)

    # stage 3, demand scoring
    scores, score_rows = stage3_demand(sources, elig, canon, quality,
                                       freshness, sw, langpol)
    out.jsonl("demand_score_ledger.jsonl", score_rows)

    # stage 4, phase membership and proportional allocation
    members = {p: [] for p in PHASES}
    for sid in src_order:
        for p in DOMAIN_PHASES.get(src[sid]["domain"], []):
            members[p].append(sid)
    for p in PHASES:
        members[p].sort()
    for e in curriculum["edges"]:
        members.setdefault(e["from"], [])
        members.setdefault(e["to"], [])
        capacity.setdefault(e["from"], {"budget_tokens": 0, "cap_tokens": 0})
        capacity.setdefault(e["to"], {"budget_tokens": 0, "cap_tokens": 0})
        carry0.setdefault(e["from"], {"credit": 0, "debt": 0})
        carry0.setdefault(e["to"], {"credit": 0, "debt": 0})
    out.jsonl("phase_membership.jsonl",
              [{"phase": p, "source_id": sid}
               for p in PHASES for sid in members[p]])

    prop = {}
    prop_rows = []
    for p in PHASES:
        budget = capacity[p]["budget_tokens"]
        dsum = sum(scores[sid] for sid in members[p]
                   if elig[sid] and scores[sid] > 0)
        for sid in members[p]:
            sc = scores.get(sid, 0)
            if dsum > 0 and sc > 0:
                num = sc * budget
                target = r_half_even(num, dsum)
            else:
                num = 0
                target = 0
            prop[(p, sid)] = target
            prop_rows.append({"phase": p, "source_id": sid,
                              "target_tokens": target, "share_num": num,
                              "share_den": dsum})
    out.jsonl("proportional_allocation.jsonl", prop_rows)

    # stage 5, protected floors
    floor = {}
    floor_rows = []
    for p in PHASES:
        for sid in members[p]:
            st = src[sid]["safety_stratum"]
            lg = src[sid]["language"]
            sf = int(spol["strata"][st]["floor_tokens"]) \
                if st in spol["strata"] else 0
            lf = int(langpol["languages"][lg]["floor_tokens"]) \
                if lg in langpol["languages"] else 0
            floor[(p, sid)] = max(sf, lf)
            floor_rows.append({"phase": p, "source_id": sid,
                               "safety_floor": sf, "language_floor": lf,
                               "floor": max(sf, lf)})
    out.jsonl("protected_floor_ledger.jsonl", floor_rows)

    # stage 7, precedence closure over the shipped graph
    edges = list(curriculum["edges"])
    reach = transitive_closure(sorted(nodes), edges)
    out.jsonl("precedence_closure.jsonl",
              [{"phase": a, "requires": b} for a in sorted(nodes)
               for b in sorted(x for x in reach[a] if x != a)])

    # stage 8, cycle break
    surviving = list(edges)
    break_rows = []
    while True:
        live = transitive_closure(sorted(nodes), surviving)
        cyc = {n for n in nodes if n in live[n]}
        if not cyc:
            break
        cands = sorted((e for e in surviving
                        if e["from"] in cyc and e["to"] in cyc),
                       key=lambda e: (HARDNESS_RANK[e["hardness"]],
                                      e["from"], e["to"], e["edge_id"]))
        drop = cands[0]
        surviving.remove(drop)
        break_rows.append({"edge_id": drop["edge_id"], "from": drop["from"],
                           "to": drop["to"], "hardness": drop["hardness"]})
        quiet.append(("B_CYCLE_BREAK", drop["edge_id"]))
    out.jsonl("cycle_report.jsonl",
              [{"cycle_nodes": sorted({n for n in nodes if n in reach[n]})}])
    out.jsonl("cycle_break_decisions.jsonl", break_rows)

    order = topo_order(sorted(nodes), surviving)
    rank = {p: i for i, p in enumerate(order)}

    # stage 6 capacity binding inside the stage 9 topological carry walk
    alloc = {}
    bind_rows = []
    carry_rows = []
    credit = {p: int(carry0[p]["credit"]) for p in phases}
    debt = {p: int(carry0[p]["debt"]) for p in phases}
    eff_cap = {}
    for p in order:
        ec = capacity[p]["cap_tokens"] + credit[p] - debt[p]
        eff_cap[p] = ec
        vals = {}
        raised = False
        for sid in members[p]:
            if not elig[sid]:
                vals[sid] = 0
                continue
            vals[sid] = max(prop[(p, sid)], floor[(p, sid)])
            if vals[sid] > prop[(p, sid)]:
                raised = True
        if raised:
            quiet.append(("B_FLOOR_BIND", p))
        excess = sum(vals.values()) - ec
        if excess > 0:
            rank_map = prio["stratum_displacement_rank"]
            seq = sorted([sid for sid in members[p] if elig[sid]],
                         key=lambda sid: (rank_map[src[sid]["safety_stratum"]],
                                          -int(src[sid]["priority"]), sid))
            displaced = False
            cut_floor = False
            for sid in seq:
                if excess <= 0:
                    break
                room = vals[sid] - floor[(p, sid)]
                if room > 0:
                    take = min(room, excess)
                    before = vals[sid]
                    vals[sid] -= take
                    excess -= take
                    displaced = True
                    bind_rows.append({"phase": p, "source_id": sid,
                                      "before": before, "after": vals[sid],
                                      "displaced": take,
                                      "kind": "above_floor"})
            if excess > 0:
                for sid in seq:
                    if excess <= 0:
                        break
                    if vals[sid] > 0:
                        take = min(vals[sid], excess)
                        before = vals[sid]
                        vals[sid] -= take
                        excess -= take
                        cut_floor = True
                        bind_rows.append({"phase": p, "source_id": sid,
                                          "before": before, "after": vals[sid],
                                          "displaced": take,
                                          "kind": "floor_cut"})
            if displaced or cut_floor:
                quiet.append(("B_CAP_BIND", p))
            if cut_floor:
                quiet.append(("B_FLOOR_OVER_CAP", p))
        for sid in members[p]:
            alloc[(p, sid)] = vals[sid]
        used = sum(vals.values())
        co = max(ec - used, 0)
        do = sum(max(floor[(p, sid)] - vals[sid], 0)
                 for sid in members[p] if elig[sid])
        if do > 0:
            quiet.append(("B_CARRY_DEBT", p))
        i = order.index(p)
        if i + 1 < len(order):
            credit[order[i + 1]] += co
            debt[order[i + 1]] += do
        carry_rows.append({"phase": p, "credit_in": credit[p],
                           "debt_in": debt[p], "credit_out": co,
                           "debt_out": do})
    out.jsonl("capacity_binding_trace.jsonl", bind_rows)
    out.jsonl("carry_state_ledger.jsonl", carry_rows)

    # stage 8, precedence overturn, judged on the stage 6 matrix
    over_rows = []
    for p in order:
        for sid in members[p]:
            if elig[sid] and alloc[(p, sid)] > 0:
                for req in sorted(x for x in reach[p] if x != p):
                    if sid in members[req] and alloc[(req, sid)] == 0:
                        over_rows.append({"phase": p, "prereq_phase": req,
                                          "source_id": sid})
                        quiet.append(("B_PRECEDENCE_OVERTURN", p))
    out.jsonl("precedence_overturn_trace.jsonl", over_rows)

    out.jsonl("pre_reconciliation_ledger.jsonl",
              [{"phase": p, "source_id": sid, "tokens": alloc[(p, sid)]}
               for p in PHASES for sid in members[p]])

    # stage 10, global reconciliation
    trace_rows = []
    clawbacks = 0
    for sid in src_order:
        if not elig[sid]:
            continue
        excess = (sum(alloc[(p, sid)] for p in PHASES
                      if (p, sid) in alloc) - canon[sid])
        if excess > 0:
            for p in sorted((q for q in PHASES if alloc.get((q, sid), 0) > 0),
                            key=lambda q: -rank[q]):
                if excess <= 0:
                    break
                before = alloc[(p, sid)]
                take = min(before, excess)
                alloc[(p, sid)] = before - take
                excess -= take
                trace_rows.append({"source_id": sid, "phase": p,
                                   "before": before, "after": alloc[(p, sid)],
                                   "clawed": take})
                clawbacks += 1
            quiet.append(("B_GLOBAL_CLAWBACK", sid))
    out.jsonl("global_reconciliation_trace.jsonl", trace_rows)
    out.jsonl("final_allocation.jsonl",
              [{"phase": p, "source_id": sid, "tokens": alloc[(p, sid)]}
               for p in PHASES for sid in members[p]])

    def source_total(sid):
        return sum(alloc[(p, sid)] for p in PHASES if (p, sid) in alloc)

    total_tokens = sum(alloc[(p, sid)] for p in PHASES for sid in members[p])

    # summaries
    scale = int(sw["scale"])
    lang_tokens = {l: 0 for l in langpol["languages"]}
    strat_tokens = {s: 0 for s in spol["strata"]}
    for sid in src_order:
        if elig[sid]:
            lang_tokens[src[sid]["language"]] += source_total(sid)
            strat_tokens[src[sid]["safety_stratum"]] += source_total(sid)
    out.jsonl("language_allocation_summary.jsonl",
              [{"language": l, "tokens": lang_tokens[l],
                "min_required": r_half_even(
                    int(langpol["languages"][l]["min_share_bp"]) * total_tokens,
                    scale),
                "max_allowed": r_half_even(
                    int(langpol["languages"][l]["max_share_bp"]) * total_tokens,
                    scale)} for l in sorted(langpol["languages"])])
    out.jsonl("safety_allocation_summary.jsonl",
              [{"stratum": st, "tokens": strat_tokens[st],
                "floor": int(spol["strata"][st]["floor_tokens"])}
               for st in sorted(spol["strata"])])
    out.jsonl("source_allocation_summary.jsonl",
              [{"source_id": sid, "tokens": source_total(sid),
                "canonical_tokens": canon[sid]} for sid in src_order])
    out.jsonl("phase_token_totals.jsonl",
              [{"phase": p,
                "allocated": sum(alloc[(p, sid)] for sid in members[p]),
                "cap": capacity[p]["cap_tokens"],
                "budget": capacity[p]["budget_tokens"]}
               for p in PHASES])

    # stage 11, rational example weights and stage 12 materialization
    rat_rows = []
    fixed_rows = []
    resid_rows = []
    mat = {}
    for sid in src_order:
        exs = ex.get(sid, [])
        total = source_total(sid)
        gden = sum(t for _, t in exs)
        if exs and gden == 0:
            quiet.append(("B_EMPTY_GROUP", sid))
        split = (largest_remainder(total, [eid for eid, _ in exs],
                                   {eid: t for eid, t in exs})
                 if exs and gden > 0 else {})
        for eid, t in exs:
            if gden > 0:
                fr = Fraction(total * t, gden)
                rat_rows.append({"example_id": eid, "source_id": sid,
                                 "w_num": fr.numerator,
                                 "w_den": fr.denominator})
                mat[(sid, eid)] = split[eid]
            else:
                mat[(sid, eid)] = 0
            fixed_rows.append({"example_id": eid, "source_id": sid,
                               "materialized_tokens": mat[(sid, eid)]})
        if gden > 0:
            for eid, t in exs:
                q, r = divmod(total * t, gden)
                if split[eid] > q:
                    resid_rows.append({"example_id": eid, "source_id": sid,
                                       "base_tokens": q, "residual_unit": 1})
    out.jsonl("rational_example_weights.jsonl", rat_rows)
    out.jsonl("fixed_point_weights.jsonl", fixed_rows)
    out.jsonl("rounding_residual_ledger.jsonl", resid_rows)

    # stage 13, microbatch construction
    max_seq = int(brules["max_seq_tokens"])
    adj = int(brules["adjacency_limit"])
    batch_rows = []
    cur = None
    bno = 0
    examples_placed = 0
    for p in PHASES:
        for sid in [s for s in members[p] if alloc[(p, s)] > 0]:
            for eid, t in [e for e in ex.get(sid, [])
                           if mat.get((sid, e[0]), 0) > 0]:
                tk = mat[(sid, eid)]
                need = cur is None or cur["phase"] != p \
                    or cur["tokens"] + tk > max_seq \
                    or (cur["last"] == sid and cur["run"] >= adj)
                if need:
                    if cur is not None:
                        bno += 1
                        batch_rows.append(
                            {"batch_id": "BATCH%04d" % bno,
                             "phase": cur["phase"], "members": cur["ids"],
                             "seq_tokens": cur["tokens"]})
                    cur = {"phase": p, "ids": [], "tokens": 0, "run": 0,
                           "last": None}
                if cur["last"] == sid:
                    cur["run"] += 1
                else:
                    cur["run"] = 1
                    cur["last"] = sid
                cur["ids"].append(eid)
                cur["tokens"] += tk
                examples_placed += 1
    if cur is not None:
        bno += 1
        batch_rows.append({"batch_id": "BATCH%04d" % bno, "phase": cur["phase"],
                           "members": cur["ids"], "seq_tokens": cur["tokens"]})
    out.jsonl("microbatch_manifest.jsonl", batch_rows)

    # invariants
    phase_ok = all(sum(alloc[(p, sid)] for sid in members[p]) <= eff_cap[p]
                   for p in PHASES)
    supply_ok = all(source_total(sid) <= canon[sid]
                    for sid in src_order if elig[sid])
    mat_ok = all(
        sum(mat[(sid, eid)] for eid, _ in exs) == source_total(sid)
        for sid in src_order for exs in [ex.get(sid, [])]
        if exs and sum(t for _, t in exs) > 0)
    batch_ok = all(b["seq_tokens"] <= max_seq or len(b["members"]) == 1
                   for b in batch_rows)
    inv_rows = [{"invariant": "phase_within_cap", "holds": phase_ok},
                {"invariant": "source_within_supply", "holds": supply_ok},
                {"invariant": "materialized_sums_match", "holds": mat_ok},
                {"invariant": "batches_respect_seq_cap", "holds": batch_ok}]
    out.jsonl("invariant_report.jsonl", inv_rows)
    out.jsonl("exception_trace.jsonl",
              [{"code": c, "ref": r} for c, r in quiet])

    objective = [total_tokens, clawbacks, len(break_rows), len(bind_rows),
                 len(batch_rows)]
    out.json_doc("objective_tuple.json",
                 {"objective": objective,
                  "order": ["total_tokens", "global_clawbacks",
                            "cycle_edges_removed", "capacity_displacements",
                            "microbatches"]})

    with open(__file__, "rb") as fh:
        own = fh.read()
    out.replace("plan.py", own)

    manifest = {name: hashlib.sha256(data).hexdigest()
                for name, data in out.docs}
    out.json_doc("integrity_manifest.json", manifest)
    chain = "0" * 64
    for name in sorted(manifest):
        chain = hashlib.sha256((chain + manifest[name]).encode("utf-8")
                               ).hexdigest()
    out.json_doc("final_state.json", {
        "chain": chain,
        "total_tokens": total_tokens,
        "global_clawbacks": clawbacks,
        "capacity_displacements": len(bind_rows),
        "cycle_edges_removed": len(break_rows),
        "precedence_overturns": len(over_rows),
        "microbatches": len(batch_rows),
        "examples_placed": examples_placed,
        "quiet_branches": len(quiet),
        "objective": objective,
        "invariants_hold": all(r["holds"] for r in inv_rows),
    })
    out.write(out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
