#!/usr/bin/env python3
"""TrainMix allocation compiler.

Derives the deterministic training plan certificate from the manifest set in a
data directory into an output directory. All arithmetic is integer or exact
rational; no float is produced, compared or serialized anywhere.

Usage: python3 plan.py <DATA_DIR> <OUT_DIR>
"""

import csv
import hashlib
import json
import os
import sys
from math import gcd

# Fixed domain to phase mapping from compiler_spec.md stage 4.
DOMAIN_PHASES = {
    "maint": ("PH1",),
    "manual": ("PH1", "PH2"),
    "incident": ("PH2",),
    "qa": ("PH2", "PH3"),
    "translated": ("PH1", "PH3"),
    "synthetic": ("PH3",),
    "reg": ("PH2",),
}

INVARIANT_ORDER = (
    "phase_within_cap",
    "source_within_supply",
    "materialized_sums_match",
    "batches_respect_seq_cap",
)

OBJECTIVE_ORDER = (
    "total_tokens",
    "global_clawbacks",
    "cycle_edges_removed",
    "capacity_displacements",
    "microbatches",
)

CHAIN_START = "0" * 64


# --------------------------------------------------------------------------
# exact arithmetic
# --------------------------------------------------------------------------
def rhe(p, d):
    """round_half_even(p, d) for integer p over positive denominator d."""
    q, r = divmod(p, d)
    if 2 * r > d:
        return q + 1
    if 2 * r == d:
        return q if q % 2 == 0 else q + 1
    return q


def reduced_fraction(num, den):
    """Fully reduced fraction with a positive denominator."""
    if den < 0:
        num, den = -num, -den
    g = gcd(abs(num), den) or 1
    return num // g, den // g


# --------------------------------------------------------------------------
# readers
# --------------------------------------------------------------------------
def read_csv_rows(path):
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return [dict(row) for row in csv.DictReader(fh)]


def read_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def as_int(text):
    return int(str(text).strip())


# --------------------------------------------------------------------------
# writers
# --------------------------------------------------------------------------
def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True, separators=(",", ":")))
            fh.write("\n")


def write_json(path, obj):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(json.dumps(obj, sort_keys=True, indent=2))
        fh.write("\n")


def sha256_file(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


# --------------------------------------------------------------------------
# stage 1 and 2
# --------------------------------------------------------------------------
def canonicalize(sources, unit_rules):
    rows = []
    tie = {}
    canon = {}
    for s in sources:
        unit = s["raw_unit"]
        num = as_int(unit_rules["units"][unit]["num"])
        den = as_int(unit_rules["units"][unit]["den"])
        p = s["raw_amount"] * num
        is_tie = den > 0 and 2 * (p % den) == den
        tokens = rhe(p, den)
        tie[s["source_id"]] = is_tie
        canon[s["source_id"]] = tokens
        rows.append({
            "source_id": s["source_id"],
            "raw_amount": s["raw_amount"],
            "raw_unit": unit,
            "canonical_tokens": tokens,
            "rounding_tie": is_tie,
        })
    return rows, canon, tie


def project_eligibility(sources, quality, lang_policy, safety_policy,
                        quality_policy, canon):
    rows = []
    eligible = {}
    blocked = set(quality_policy["blocked_domains"])
    min_num = as_int(quality_policy["min_quality"]["num"])
    min_den = as_int(quality_policy["min_quality"]["den"])
    for s in sources:
        sid = s["source_id"]
        qn, qd = quality.get(sid, (0, 0))
        if qd == 0 or qn * min_den < min_num * qd:
            code = "E_LOW_QUALITY"
        elif s["language"] not in lang_policy:
            code = "E_LANG_EXCLUDED"
        elif s["safety_stratum"] not in safety_policy:
            code = "E_STRATUM_UNKNOWN"
        elif s["domain"] in blocked:
            code = "E_DOMAIN_BLOCKED"
        elif canon[sid] <= 0:
            code = "E_EMPTY_TOKENS"
        else:
            code = "E_OK"
        eligible[sid] = code == "E_OK"
        rows.append({"source_id": sid, "eligible": eligible[sid], "code": code})
    return rows, eligible


# --------------------------------------------------------------------------
# stage 3
# --------------------------------------------------------------------------
def score_demand(sources, eligible, quality, freshness, lang_policy, canon,
                 scoring_weights):
    scale = as_int(scoring_weights["scale"])
    half_life = as_int(scoring_weights["half_life_days"])
    rows = []
    scores = {}
    for s in sources:
        sid = s["source_id"]
        if not eligible[sid]:
            continue
        qn, qd = quality[sid]
        quality_bp = rhe(qn * scale, qd)
        age = freshness.get(sid, 0)
        fresh_bp = rhe(half_life * scale, half_life + age) if half_life + age > 0 else 0
        priority_factor = as_int(
            scoring_weights["priority_weights"][str(s["priority"])])
        balance_bp = as_int(lang_policy[s["language"]]["balance_bp"])
        s1 = rhe(canon[sid] * quality_bp, scale)
        s2 = rhe(s1 * fresh_bp, scale)
        s3 = s2 * priority_factor
        score = rhe(s3 * balance_bp, scale)
        scores[sid] = score
        rows.append({
            "source_id": sid,
            "quality_bp": quality_bp,
            "fresh_bp": fresh_bp,
            "priority_factor": priority_factor,
            "lang_balance_bp": balance_bp,
            "demand_score": score,
        })
    return rows, scores


# --------------------------------------------------------------------------
# stage 7 and 8, closure, cycles, topological order
# --------------------------------------------------------------------------
def transitive_closure(phases, edges):
    reach = {p: set() for p in phases}
    for edge in edges:
        reach[edge["from"]].add(edge["to"])
    for k in phases:
        for i in phases:
            if k in reach[i]:
                reach[i] |= reach[k]
    return reach


def find_cycle_nodes(reach):
    return sorted(p for p in reach if p in reach[p])


def break_cycle(edges, cycle_nodes):
    """Rank edges with both endpoints on a cycle: soft below hard, then from,
    then to, then edge id; remove the single lowest ranked edge."""
    cyc = set(cycle_nodes)
    candidates = [e for e in edges if e["from"] in cyc and e["to"] in cyc]
    if not candidates:
        return None, []
    hardness_rank = {"soft": 0, "hard": 1}

    def key(edge):
        return (hardness_rank.get(edge["hardness"], 0), edge["from"],
                edge["to"], edge["edge_id"])

    chosen = min(candidates, key=key)
    remaining = [e for e in edges if e["edge_id"] != chosen["edge_id"]]
    return chosen, remaining


def topological_order(phases, edges):
    indeg = {p: 0 for p in phases}
    succ = {p: [] for p in phases}
    for edge in edges:
        indeg[edge["to"]] += 1
        succ[edge["from"]].append(edge["to"])
    ready = sorted(p for p in phases if indeg[p] == 0)
    order = []
    while ready:
        node = ready.pop(0)
        order.append(node)
        for nxt in succ[node]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                ready.append(nxt)
        ready.sort()
    tail = sorted(p for p in phases if p not in order)
    return order + tail


def largest_remainder_split(total, weights, keys):
    """Split integer total across keys in proportion to weights[key]."""
    wsum = sum(weights[k] for k in keys)
    if wsum == 0 or total == 0:
        return {k: 0 for k in keys}, {k: (0, 0) for k in keys}
    base = {}
    rem = {}
    for k in keys:
        p = total * weights[k]
        base[k] = p // wsum
        rem[k] = p % wsum
    out = dict(base)
    leftover = total - sum(base.values())
    if leftover > 0:
        order = sorted(keys, key=lambda k: (-rem[k], k))
        for k in order[:leftover]:
            out[k] += 1
    residuals = {k: (base[k], out[k] - base[k]) for k in keys}
    return out, residuals


# --------------------------------------------------------------------------
# main compile
# --------------------------------------------------------------------------
def compile_plan(data_dir, out_dir):
    sources_rows = read_csv_rows(os.path.join(data_dir, "sources.csv"))
    quality_rows = read_csv_rows(os.path.join(data_dir, "quality.csv"))
    freshness_rows = read_csv_rows(os.path.join(data_dir, "freshness.csv"))
    examples_rows = read_csv_rows(os.path.join(data_dir, "examples.csv"))
    capacity_rows = read_csv_rows(os.path.join(data_dir, "capacity.csv"))
    carry_rows = read_csv_rows(os.path.join(data_dir, "carry_state.csv"))
    curriculum = read_json(os.path.join(data_dir, "curriculum.json"))
    lang_policy_doc = read_json(os.path.join(data_dir, "language_policy.json"))
    safety_policy = read_json(
        os.path.join(data_dir, "safety_policy.json"))["strata"]
    quality_policy = read_json(os.path.join(data_dir, "quality_policy.json"))
    scoring_weights = read_json(os.path.join(data_dir, "scoring_weights.json"))
    priority_order = read_json(os.path.join(data_dir, "priority_order.json"))
    batch_rules = read_json(os.path.join(data_dir, "batch_rules.json"))
    unit_rules = read_json(os.path.join(data_dir, "unit_rules.json"))

    lang_policy = lang_policy_doc["languages"]
    scale = as_int(unit_rules["scale"])
    exception_trace = []

    # canonical source order: order of first appearance in sources.csv
    sources = []
    seen = set()
    for row in sources_rows:
        sid = row["source_id"]
        if sid in seen:
            continue
        seen.add(sid)
        sources.append({
            "source_id": sid,
            "language": row["language"],
            "domain": row["domain"],
            "source_class": row["source_class"],
            "safety_stratum": row["safety_stratum"],
            "priority": as_int(row["priority"]),
            "raw_amount": as_int(row["raw_amount"]),
            "raw_unit": row["raw_unit"],
        })
    source_ids = [s["source_id"] for s in sources]
    by_source = {s["source_id"]: s for s in sources}

    quality = {}
    for row in quality_rows:
        quality[row["source_id"]] = (as_int(row["q_num"]), as_int(row["q_den"]))
    freshness = {}
    for row in freshness_rows:
        freshness[row["source_id"]] = as_int(row["age_days"])

    examples_by_source = {sid: [] for sid in source_ids}
    for row in examples_rows:
        rec = {
            "example_id": row["example_id"],
            "source_id": row["source_id"],
            "stratum": row["stratum"],
            "token_length": as_int(row["token_length"]),
        }
        if rec["source_id"] in examples_by_source:
            examples_by_source[rec["source_id"]].append(rec)
    for sid in examples_by_source:
        examples_by_source[sid].sort(key=lambda r: r["example_id"])

    phases = [row["phase"] for row in capacity_rows]
    phases_set = set(phases)
    budget = {row["phase"]: as_int(row["budget_tokens"]) for row in capacity_rows}
    cap = {row["phase"]: as_int(row["cap_tokens"]) for row in capacity_rows}
    initial_credit = {row["phase"]: as_int(row["initial_credit"])
                      for row in carry_rows}
    initial_debt = {row["phase"]: as_int(row["initial_debt"])
                    for row in carry_rows}
    for p in phases:
        initial_credit.setdefault(p, 0)
        initial_debt.setdefault(p, 0)

    # stage 1 -------------------------------------------------------------
    canonical_rows, canon, unit_tie = canonicalize(sources, unit_rules)
    for s in sources:
        if unit_tie[s["source_id"]]:
            exception_trace.append({"code": "B_UNIT_ROUND_TIE",
                                    "ref": s["source_id"]})

    # stage 2 -------------------------------------------------------------
    eligibility_rows, eligible = project_eligibility(
        sources, quality, lang_policy, safety_policy, quality_policy, canon)

    # stage 3 -------------------------------------------------------------
    demand_rows, demand = score_demand(
        sources, eligible, quality, freshness, lang_policy, canon, scoring_weights)

    # stage 4, membership and proportional targets -------------------------
    membership = {p: [] for p in phases}
    for s in sources:
        for p in DOMAIN_PHASES.get(s["domain"], ()):
            if p in membership:
                membership[p].append(s["source_id"])
    for p in phases:
        membership[p].sort()

    membership_rows = [{"phase": p, "source_id": sid}
                       for p in phases for sid in membership[p]]

    proportional_rows = []
    target = {p: {} for p in phases}
    share_num = {p: {} for p in phases}
    share_den = {p: {} for p in phases}
    for p in phases:
        members = membership[p]
        active = [sid for sid in members
                  if eligible[sid] and demand.get(sid, 0) > 0]
        dsum = sum(demand[sid] for sid in active)
        for sid in members:
            num = demand[sid] * budget[p] if eligible[sid] else 0
            den = dsum
            share_num[p][sid] = num
            share_den[p][sid] = den
            tgt = rhe(num, den) if (den > 0 and num > 0) else 0
            target[p][sid] = tgt
            proportional_rows.append({
                "phase": p,
                "source_id": sid,
                "target_tokens": tgt,
                "share_num": num,
                "share_den": den,
            })

    # stage 5, protected floors -------------------------------------------
    # A lookup key can only be absent for a source that stage 2 already
    # rejected; such a source is never allocated, so the absent floor is 0.
    floor_rows = []
    floor = {p: {} for p in phases}
    for p in phases:
        for sid in membership[p]:
            s = by_source[sid]
            stratum = safety_policy.get(s["safety_stratum"])
            language = lang_policy.get(s["language"])
            sfloor = as_int(stratum["floor_tokens"]) if stratum else 0
            lfloor = as_int(language["floor_tokens"]) if language else 0
            floor[p][sid] = max(sfloor, lfloor)
            floor_rows.append({
                "phase": p,
                "source_id": sid,
                "safety_floor": sfloor,
                "language_floor": lfloor,
                "floor": floor[p][sid],
            })

    # stage 7, closure -----------------------------------------------------
    edges = [{"edge_id": e["edge_id"], "from": e["from"], "to": e["to"],
              "hardness": e["hardness"]} for e in curriculum["edges"]
             if e["from"] in phases_set and e["to"] in phases_set]
    reach = transitive_closure(phases, edges)
    closure_rows = [{"phase": p, "requires": r}
                    for p in sorted(reach) for r in sorted(reach[p])]

    # stage 8, cycle break -------------------------------------------------
    cycle_nodes = find_cycle_nodes(reach)
    cycle_rows = [{"cycle_nodes": cycle_nodes}] if cycle_nodes else []
    removed_edge, surviving_edges = break_cycle(edges, cycle_nodes)
    cycle_break_rows = []
    if removed_edge is not None:
        cycle_break_rows.append({
            "edge_id": removed_edge["edge_id"],
            "from": removed_edge["from"],
            "to": removed_edge["to"],
            "hardness": removed_edge["hardness"],
        })
        exception_trace.append({"code": "B_CYCLE_BREAK",
                                "ref": removed_edge["edge_id"]})

    topo = topological_order(phases, surviving_edges)

    # stages 4, 5, 6 and 9, per phase in topological order ------------------
    rank = priority_order["stratum_displacement_rank"]
    carry_credit = dict(initial_credit)
    carry_debt = dict(initial_debt)
    binding_rows = []
    carry_rows_out = []
    pre_reconciliation = {p: {} for p in phases}
    eff_cap = {}

    for idx, p in enumerate(topo):
        members = membership[p]
        credit_in = carry_credit[p]
        debt_in = carry_debt[p]
        eff = cap[p] + credit_in - debt_in
        eff_cap[p] = eff

        alloc = {}
        raised = False
        for sid in members:
            tgt = target[p][sid] if eligible[sid] else 0
            fl = floor[p][sid] if eligible[sid] else 0
            alloc[sid] = tgt if tgt > fl else fl
            if fl > tgt:
                raised = True

        excess = sum(alloc.values()) - eff
        if excess > 0:
            # cut into the floors themselves, in displacement order, when the
            # reductions above the floor cannot absorb the whole excess
            def displacement_key(sid):
                s = by_source[sid]
                return (as_int(rank[s["safety_stratum"]]), -s["priority"], sid)

            order = sorted(members, key=displacement_key)
            exception_trace.append({"code": "B_CAP_BIND", "ref": p})
            for sid in order:
                if excess <= 0:
                    break
                room = alloc[sid] - floor[p][sid]
                if room > 0:
                    take = room if room < excess else excess
                    before = alloc[sid]
                    alloc[sid] = before - take
                    excess -= take
                    binding_rows.append({
                        "phase": p,
                        "source_id": sid,
                        "before": before,
                        "after": alloc[sid],
                        "displaced": take,
                        "kind": "above_floor",
                    })
            if excess > 0:
                exception_trace.append({"code": "B_FLOOR_OVER_CAP", "ref": p})
                for sid in order:
                    if excess <= 0:
                        break
                    room = alloc[sid]
                    if room > 0:
                        take = room if room < excess else excess
                        before = alloc[sid]
                        alloc[sid] = before - take
                        excess -= take
                        binding_rows.append({
                            "phase": p,
                            "source_id": sid,
                            "before": before,
                            "after": alloc[sid],
                            "displaced": take,
                            "kind": "floor_cut",
                        })

        pre_reconciliation[p] = alloc

        credit_out = eff - sum(alloc.values())
        if credit_out < 0:
            credit_out = 0
        debt_out = 0
        for sid in members:
            if eligible[sid]:
                shortfall = floor[p][sid] - alloc[sid]
                if shortfall > 0:
                    debt_out += shortfall

        if raised:
            exception_trace.append({"code": "B_FLOOR_BIND", "ref": p})
        if debt_out > 0:
            exception_trace.append({"code": "B_CARRY_DEBT", "ref": p})

        carry_rows_out.append({
            "phase": p,
            "credit_in": credit_in,
            "debt_in": debt_in,
            "credit_out": credit_out,
            "debt_out": debt_out,
        })

        # successor in the surviving order takes this phase's carry forward
        if idx + 1 < len(topo):
            nxt = topo[idx + 1]
            carry_credit[nxt] += credit_out
            carry_debt[nxt] += debt_out

    # stage 8, precedence overturn (uses the stage 7 closure) --------------
    overturn_rows = []
    for p in phases:
        for sid in membership[p]:
            if pre_reconciliation[p][sid] <= 0:
                continue
            for req in sorted(reach[p]):
                if req == p or req not in membership:
                    continue
                if sid in membership[req] and pre_reconciliation[req][sid] == 0:
                    overturn_rows.append({
                        "phase": p,
                        "prereq_phase": req,
                        "source_id": sid,
                    })
    overturn_phases = []
    for row in overturn_rows:
        if row["phase"] not in overturn_phases:
            overturn_phases.append(row["phase"])
    for p in overturn_phases:
        exception_trace.append({"code": "B_PRECEDENCE_OVERTURN", "ref": p})

    # stage 10, global reconciliation ---------------------------------------
    final = {p: dict(pre_reconciliation[p]) for p in phases}
    recon_rows = []
    claw_sources = []
    for sid in source_ids:
        total = sum(final[p][sid] for p in phases if sid in final[p])
        supply = canon[sid]
        if total <= supply:
            continue
        excess = total - supply
        claw_sources.append(sid)
        for p in reversed(topo):
            if excess <= 0:
                break
            if sid in final[p] and final[p][sid] > 0:
                take = final[p][sid] if final[p][sid] < excess else excess
                before = final[p][sid]
                final[p][sid] = before - take
                excess -= take
                recon_rows.append({
                    "source_id": sid,
                    "phase": p,
                    "before": before,
                    "after": final[p][sid],
                    "clawed": take,
                })
    for sid in claw_sources:
        exception_trace.append({"code": "B_GLOBAL_CLAWBACK", "ref": sid})

    final_allocation_rows = [{"phase": p, "source_id": sid, "tokens": final[p][sid]}
                             for p in phases for sid in membership[p]]
    pre_reconciliation_rows = [{"phase": p, "source_id": sid,
                                "tokens": pre_reconciliation[p][sid]}
                               for p in phases for sid in membership[p]]

    source_total = {sid: sum(final[p][sid] for p in phases if sid in final[p])
                    for sid in source_ids}
    total_tokens = sum(source_total.values())

    # stage 11, rational example weights ------------------------------------
    rational_rows = []
    group_den = {}
    for sid in source_ids:
        gden = sum(r["token_length"] for r in examples_by_source[sid])
        group_den[sid] = gden
        if gden == 0:
            if examples_by_source[sid]:
                exception_trace.append({"code": "B_EMPTY_GROUP", "ref": sid})
            continue
        for rec in examples_by_source[sid]:
            num, den = reduced_fraction(source_total[sid] * rec["token_length"],
                                        gden)
            rational_rows.append({
                "example_id": rec["example_id"],
                "source_id": sid,
                "w_num": num,
                "w_den": den,
            })

    # stage 12, integer materialization -------------------------------------
    fixed_rows = []
    residual_rows = []
    materialized = {}
    for sid in source_ids:
        recs = examples_by_source[sid]
        keys = [r["example_id"] for r in recs]
        weights = {r["example_id"]: r["token_length"] for r in recs}
        total = source_total[sid]
        if not recs or group_den[sid] == 0 or total == 0:
            split = {k: 0 for k in keys}
            base = {k: (0, 0) for k in keys}
        else:
            split, base = largest_remainder_split(total, weights, keys)
        materialized[sid] = split
        for rec in recs:
            eid = rec["example_id"]
            fixed_rows.append({
                "example_id": eid,
                "source_id": sid,
                "materialized_tokens": split[eid],
            })
            b, resid = base[eid]
            if resid > 0:
                residual_rows.append({
                    "example_id": eid,
                    "source_id": sid,
                    "base_tokens": b,
                    "residual_unit": resid,
                })

    # stage 13, microbatches ------------------------------------------------
    max_seq = as_int(batch_rules["max_seq_tokens"])
    adjacency = as_int(batch_rules["adjacency_limit"])
    batch_rows = []
    examples_placed = 0
    current = []
    current_total = 0
    run_source = None
    run_len = 0

    for p in phases:
        for sid in membership[p]:
            if final[p][sid] <= 0:
                continue
            for rec in examples_by_source[sid]:
                eid = rec["example_id"]
                tok = materialized[sid].get(eid, 0)
                if tok <= 0:
                    continue
                if current and (current_total + tok > max_seq or
                                (run_source == sid and run_len >= adjacency)):
                    batch_rows.append({
                        "batch_id": "BATCH%04d" % (len(batch_rows) + 1),
                        "phase": p,
                        "members": list(current),
                        "seq_tokens": current_total,
                    })
                    current = []
                    current_total = 0
                    run_source = None
                    run_len = 0
                current.append(eid)
                current_total += tok
                examples_placed += 1
                if run_source == sid:
                    run_len += 1
                else:
                    run_source = sid
                    run_len = 1
        if current:
            batch_rows.append({
                "batch_id": "BATCH%04d" % (len(batch_rows) + 1),
                "phase": p,
                "members": list(current),
                "seq_tokens": current_total,
            })
            current = []
            current_total = 0
            run_source = None
            run_len = 0

    # summaries -------------------------------------------------------------
    language_summary = []
    for lang in sorted(lang_policy):
        tokens = sum(source_total[s["source_id"]] for s in sources
                     if s["language"] == lang)
        language_summary.append({
            "language": lang,
            "tokens": tokens,
            "min_required": rhe(
                as_int(lang_policy[lang]["min_share_bp"]) * total_tokens, scale),
            "max_allowed": rhe(
                as_int(lang_policy[lang]["max_share_bp"]) * total_tokens, scale),
        })

    safety_summary = []
    for stratum in sorted(safety_policy):
        tokens = sum(source_total[s["source_id"]] for s in sources
                     if s["safety_stratum"] == stratum)
        safety_summary.append({
            "stratum": stratum,
            "tokens": tokens,
            "floor": as_int(safety_policy[stratum]["floor_tokens"]),
        })

    source_summary = [{"source_id": sid,
                       "tokens": source_total[sid],
                       "canonical_tokens": canon[sid]} for sid in source_ids]

    phase_totals = [{"phase": p,
                     "allocated": sum(final[p].values()),
                     "cap": cap[p],
                     "budget": budget[p]} for p in phases]

    # invariants ------------------------------------------------------------
    inv = {}
    inv["phase_within_cap"] = all(sum(final[p].values()) <= eff_cap[p]
                                  for p in phases)
    inv["source_within_supply"] = all(source_total[sid] <= canon[sid]
                                      for sid in source_ids)
    mat_ok = True
    for sid in source_ids:
        if not examples_by_source[sid]:
            continue
        if sum(materialized[sid].values()) != source_total[sid]:
            mat_ok = False
    inv["materialized_sums_match"] = mat_ok
    inv["batches_respect_seq_cap"] = all(
        r["seq_tokens"] <= max_seq or len(r["members"]) == 1 for r in batch_rows)
    invariant_rows = [{"invariant": name, "holds": inv[name]}
                      for name in INVARIANT_ORDER]

    # objective -------------------------------------------------------------
    objective = [
        total_tokens,
        len(recon_rows),
        len(cycle_break_rows),
        len(binding_rows),
        len(batch_rows),
    ]

    # write artifacts -------------------------------------------------------
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)

    write_jsonl(os.path.join(out_dir, "canonical_sources.jsonl"), canonical_rows)
    write_jsonl(os.path.join(out_dir, "eligibility_ledger.jsonl"), eligibility_rows)
    write_jsonl(os.path.join(out_dir, "demand_score_ledger.jsonl"), demand_rows)
    write_jsonl(os.path.join(out_dir, "phase_membership.jsonl"), membership_rows)
    write_jsonl(os.path.join(out_dir, "proportional_allocation.jsonl"),
                proportional_rows)
    write_jsonl(os.path.join(out_dir, "protected_floor_ledger.jsonl"), floor_rows)
    write_jsonl(os.path.join(out_dir, "capacity_binding_trace.jsonl"),
                binding_rows)
    write_jsonl(os.path.join(out_dir, "precedence_closure.jsonl"), closure_rows)
    write_jsonl(os.path.join(out_dir, "cycle_report.jsonl"), cycle_rows)
    write_jsonl(os.path.join(out_dir, "cycle_break_decisions.jsonl"),
                cycle_break_rows)
    write_jsonl(os.path.join(out_dir, "precedence_overturn_trace.jsonl"),
                overturn_rows)
    write_jsonl(os.path.join(out_dir, "carry_state_ledger.jsonl"), carry_rows_out)
    write_jsonl(os.path.join(out_dir, "pre_reconciliation_ledger.jsonl"),
                pre_reconciliation_rows)
    write_jsonl(os.path.join(out_dir, "global_reconciliation_trace.jsonl"),
                recon_rows)
    write_jsonl(os.path.join(out_dir, "final_allocation.jsonl"),
                final_allocation_rows)
    write_jsonl(os.path.join(out_dir, "language_allocation_summary.jsonl"),
                language_summary)
    write_jsonl(os.path.join(out_dir, "safety_allocation_summary.jsonl"),
                safety_summary)
    write_jsonl(os.path.join(out_dir, "source_allocation_summary.jsonl"),
                source_summary)
    write_jsonl(os.path.join(out_dir, "rational_example_weights.jsonl"),
                rational_rows)
    write_jsonl(os.path.join(out_dir, "fixed_point_weights.jsonl"), fixed_rows)
    write_jsonl(os.path.join(out_dir, "rounding_residual_ledger.jsonl"),
                residual_rows)
    write_jsonl(os.path.join(out_dir, "microbatch_manifest.jsonl"), batch_rows)
    write_jsonl(os.path.join(out_dir, "phase_token_totals.jsonl"), phase_totals)
    write_jsonl(os.path.join(out_dir, "invariant_report.jsonl"), invariant_rows)
    write_jsonl(os.path.join(out_dir, "exception_trace.jsonl"), exception_trace)

    write_json(os.path.join(out_dir, "objective_tuple.json"), {
        "objective": objective,
        "order": list(OBJECTIVE_ORDER),
    })

    # the program writes its own source into the output directory
    program_path = os.path.abspath(__file__)
    with open(program_path, "rb") as fh:
        program_bytes = fh.read()
    target_program = os.path.abspath(os.path.join(out_dir, "plan.py"))
    if target_program != program_path:
        with open(target_program, "wb") as fh:
            fh.write(program_bytes)

    manifest_names = [
        "canonical_sources.jsonl",
        "eligibility_ledger.jsonl",
        "demand_score_ledger.jsonl",
        "phase_membership.jsonl",
        "proportional_allocation.jsonl",
        "protected_floor_ledger.jsonl",
        "capacity_binding_trace.jsonl",
        "precedence_closure.jsonl",
        "cycle_report.jsonl",
        "cycle_break_decisions.jsonl",
        "precedence_overturn_trace.jsonl",
        "carry_state_ledger.jsonl",
        "pre_reconciliation_ledger.jsonl",
        "global_reconciliation_trace.jsonl",
        "final_allocation.jsonl",
        "language_allocation_summary.jsonl",
        "safety_allocation_summary.jsonl",
        "source_allocation_summary.jsonl",
        "rational_example_weights.jsonl",
        "fixed_point_weights.jsonl",
        "rounding_residual_ledger.jsonl",
        "microbatch_manifest.jsonl",
        "phase_token_totals.jsonl",
        "invariant_report.jsonl",
        "exception_trace.jsonl",
        "objective_tuple.json",
        "plan.py",
    ]
    manifest = {name: sha256_file(os.path.join(out_dir, name))
                for name in manifest_names}
    write_json(os.path.join(out_dir, "integrity_manifest.json"), manifest)

    chain = CHAIN_START
    for name in sorted(manifest):
        chain = hashlib.sha256(
            (chain + manifest[name]).encode("utf-8")).hexdigest()

    write_json(os.path.join(out_dir, "final_state.json"), {
        "chain": chain,
        "total_tokens": total_tokens,
        "global_clawbacks": len(recon_rows),
        "capacity_displacements": len(binding_rows),
        "cycle_edges_removed": len(cycle_break_rows),
        "precedence_overturns": len(overturn_rows),
        "microbatches": len(batch_rows),
        "examples_placed": examples_placed,
        "quiet_branches": len(exception_trace),
        "objective": objective,
        "invariants_hold": all(inv[name] for name in INVARIANT_ORDER),
    })


def main(argv):
    if len(argv) != 3:
        sys.stderr.write("usage: plan.py <DATA_DIR> <OUT_DIR>\n")
        return 2
    compile_plan(argv[1], argv[2])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
