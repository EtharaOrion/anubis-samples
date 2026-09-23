#!/usr/bin/env python3
"""TrainMix allocation compiler.

Derives the training mixture certificate from the shipped manifests following the
fourteen stage chain in the specification.  All arithmetic is integer or exact
rational; no floating point value is produced, compared or serialized.

Usage: python3 plan.py <DATA_DIR> <OUT_DIR>
"""

import csv
import hashlib
import heapq
import json
import os
import sys
from fractions import Fraction

# Stage 4 domain to phase map, fixed by the specification.
DOMAIN_PHASES = {
    "maint": ("PH1",),
    "manual": ("PH1", "PH2"),
    "incident": ("PH2",),
    "qa": ("PH2", "PH3"),
    "translated": ("PH1", "PH3"),
    "synthetic": ("PH3",),
    "reg": ("PH2",),
}

HASHED_FILES = [
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

# Quiet branch codes in stage activation order.
BRANCH_ORDER = [
    "B_UNIT_ROUND_TIE",
    "B_FLOOR_BIND",
    "B_CAP_BIND",
    "B_FLOOR_OVER_CAP",
    "B_PRECEDENCE_OVERTURN",
    "B_CYCLE_BREAK",
    "B_CARRY_DEBT",
    "B_GLOBAL_CLAWBACK",
    "B_EMPTY_GROUP",
]


# ---------------------------------------------------------------- arithmetic

def rhe(p, d):
    """round_half_even(p, d) over a positive denominator d."""
    if d == 0:
        raise ZeroDivisionError("quantization over a zero denominator")
    if d < 0:
        p, d = -p, -d
    q, r = divmod(p, d)
    two_r = 2 * r
    if two_r > d:
        return q + 1
    if two_r == d:
        return q if q % 2 == 0 else q + 1
    return q


def is_tie(p, d):
    if d == 0:
        return False
    if d < 0:
        p, d = -p, -d
    _, r = divmod(p, d)
    return 2 * r == d


def largest_remainder(total, weights, keys):
    """Split an integer total across keys in proportion to positive weights.

    Leftover units go to keys in descending remainder order, ties by ascending
    key.  Returns (base, award) dicts.
    """
    wsum = sum(weights[k] for k in keys)
    base = {k: 0 for k in keys}
    award = {k: 0 for k in keys}
    if wsum <= 0 or total <= 0:
        return base, award
    rem = {}
    for k in keys:
        q, r = divmod(total * weights[k], wsum)
        base[k] = q
        rem[k] = r
    leftover = total - sum(base.values())
    order = sorted(keys, key=lambda k: (-rem[k], k))
    for i in range(leftover):
        award[order[i % len(order)]] += 1
    return base, award


# ------------------------------------------------------------------- loading

def read_csv(path):
    with open(path, "r", encoding="utf-8", newline="") as f:
        return [dict(row) for row in csv.DictReader(f)]


def read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ------------------------------------------------------------ serialization

def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row, sort_keys=True, separators=(",", ":")))
            f.write("\n")


def write_json(path, obj):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(obj, sort_keys=True, indent=2))
        f.write("\n")


def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


# --------------------------------------------------------------- the compiler

def compile_plan(data_dir, out_dir):
    sources = read_csv(os.path.join(data_dir, "sources.csv"))
    quality = {r["source_id"]: r for r in read_csv(os.path.join(data_dir, "quality.csv"))}
    freshness = {r["source_id"]: r for r in read_csv(os.path.join(data_dir, "freshness.csv"))}
    examples = read_csv(os.path.join(data_dir, "examples.csv"))
    capacity = read_csv(os.path.join(data_dir, "capacity.csv"))
    carry_init = {r["phase"]: r for r in read_csv(os.path.join(data_dir, "carry_state.csv"))}
    unit_rules = read_json(os.path.join(data_dir, "unit_rules.json"))
    scoring_weights = read_json(os.path.join(data_dir, "scoring_weights.json"))
    language_policy = read_json(os.path.join(data_dir, "language_policy.json"))["languages"]
    safety_policy = read_json(os.path.join(data_dir, "safety_policy.json"))["strata"]
    quality_policy = read_json(os.path.join(data_dir, "quality_policy.json"))
    priority_order = read_json(os.path.join(data_dir, "priority_order.json"))
    curriculum = read_json(os.path.join(data_dir, "curriculum.json"))
    batch_rules = read_json(os.path.join(data_dir, "batch_rules.json"))

    scale = unit_rules["scale"]
    score_scale = scoring_weights["scale"]
    half_life = scoring_weights["half_life_days"]
    priority_weights = scoring_weights["priority_weights"]
    blocked_domains = set(quality_policy["blocked_domains"])
    min_q_num = quality_policy["min_quality"]["num"]
    min_q_den = quality_policy["min_quality"]["den"]
    disp_rank = priority_order["stratum_displacement_rank"]

    sids = [r["source_id"] for r in sources]
    src = {r["source_id"]: r for r in sources}
    src_index = {s: i for i, s in enumerate(sids)}

    phases = [r["phase"] for r in capacity]
    budget = {r["phase"]: int(r["budget_tokens"]) for r in capacity}
    cap = {r["phase"]: int(r["cap_tokens"]) for r in capacity}

    branches = {code: [] for code in BRANCH_ORDER}

    # -------------------------------------------- stage 1, canonicalization
    canonical_rows = []
    canonical = {}
    for sid in sids:
        row = src[sid]
        amount = int(row["raw_amount"])
        unit = row["raw_unit"]
        rule = unit_rules["units"][unit]
        p = amount * rule["num"]
        den = rule["den"]
        tokens = rhe(p, den)
        tie = is_tie(p, den)
        canonical[sid] = tokens
        canonical_rows.append({
            "source_id": sid,
            "raw_amount": amount,
            "raw_unit": unit,
            "canonical_tokens": tokens,
            "rounding_tie": tie,
        })
        if tie:
            branches["B_UNIT_ROUND_TIE"].append(sid)

    # ------------------------------------------------ stage 2, eligibility
    eligibility_rows = []
    eligible = []
    for sid in sids:
        row = src[sid]
        q = quality.get(sid)
        q_num = int(q["q_num"]) if q else 0
        q_den = int(q["q_den"]) if q else 0
        code = "E_OK"
        if q_den == 0 or q_num * min_q_den < min_q_num * q_den:
            code = "E_LOW_QUALITY"
        elif row["language"] not in language_policy:
            code = "E_LANG_EXCLUDED"
        elif row["safety_stratum"] not in safety_policy:
            code = "E_STRATUM_UNKNOWN"
        elif row["domain"] in blocked_domains:
            code = "E_DOMAIN_BLOCKED"
        elif not canonical[sid] > 0:
            code = "E_EMPTY_TOKENS"
        ok = code == "E_OK"
        if ok:
            eligible.append(sid)
        eligibility_rows.append({"source_id": sid, "eligible": ok, "code": code})

    # ---------------------------------------------- stage 3, demand scoring
    demand_rows = []
    demand = {}
    for sid in eligible:
        row = src[sid]
        q = quality[sid]
        quality_bp = rhe(int(q["q_num"]) * score_scale, int(q["q_den"]))
        age = int(freshness[sid]["age_days"]) if sid in freshness else 0
        fresh_den = half_life + age
        fresh_bp = rhe(half_life * score_scale, fresh_den) if fresh_den != 0 else 0
        priority_factor = int(priority_weights[str(int(row["priority"]))])
        balance_bp = int(language_policy[row["language"]]["balance_bp"])
        s1 = rhe(canonical[sid] * quality_bp, score_scale)
        s2 = rhe(s1 * fresh_bp, score_scale)
        s3 = s2 * priority_factor
        score = rhe(s3 * balance_bp, score_scale)
        demand[sid] = score
        demand_rows.append({
            "source_id": sid,
            "quality_bp": quality_bp,
            "fresh_bp": fresh_bp,
            "priority_factor": priority_factor,
            "lang_balance_bp": balance_bp,
            "demand_score": score,
        })

    # ----------------------------------------------- stage 4, phase membership
    members = {p: [] for p in phases}
    for sid in eligible:
        for p in DOMAIN_PHASES.get(src[sid]["domain"], ()):
            if p in members:
                members[p].append(sid)
    for p in phases:
        members[p].sort(key=lambda s: src_index[s])
    member_set = {p: set(members[p]) for p in phases}
    membership_rows = [{"phase": p, "source_id": s} for p in phases for s in members[p]]

    # ----------------------------------- stage 4, proportional target per phase
    target = {}
    prop_rows = []
    for p in phases:
        dsum = sum(demand[s] for s in members[p] if demand[s] > 0)
        for s in members[p]:
            share_num = demand[s] * budget[p]
            t = 0 if (dsum <= 0 or demand[s] <= 0) else rhe(share_num, dsum)
            target[(p, s)] = t
            prop_rows.append({
                "phase": p,
                "source_id": s,
                "target_tokens": t,
                "share_num": share_num,
                "share_den": dsum,
            })

    # ----------------------------------------------- stage 5, protected floors
    floor = {}
    floor_rows = []
    for p in phases:
        for s in members[p]:
            row = src[s]
            sfloor = int(safety_policy[row["safety_stratum"]]["floor_tokens"])
            lfloor = int(language_policy[row["language"]]["floor_tokens"])
            f = sfloor if sfloor >= lfloor else lfloor
            floor[(p, s)] = f
            floor_rows.append({
                "phase": p,
                "source_id": s,
                "safety_floor": sfloor,
                "language_floor": lfloor,
                "floor": f,
            })

    # ------------------------------------------- stage 7, precedence closure
    edges = curriculum["edges"]
    adj = {p: set() for p in phases}
    for e in edges:
        if e["from"] in adj and e["to"] in adj:
            adj[e["from"]].add(e["to"])
    reach = {}
    for p in phases:
        seen = set()
        stack = list(adj[p])
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            stack.extend(adj[n])
        reach[p] = seen
    requires = {p: set() for p in phases}
    for p in phases:
        for t in reach[p]:
            requires[t].add(p)
    closure_rows = [
        {"phase": p, "requires": r}
        for p in sorted(phases)
        for r in sorted(requires[p])
    ]

    # ------------------------------------------------- stage 8, cycle break
    cycle_nodes = sorted(p for p in phases if p in reach[p])
    cycle_rows = [{"cycle_nodes": cycle_nodes}] if cycle_nodes else []
    cycle_break_rows = []
    removed = set()
    if cycle_nodes:
        cyc = set(cycle_nodes)
        candidates = [e for e in edges if e["from"] in cyc and e["to"] in cyc]
        if candidates:
            chosen = min(candidates, key=lambda e: (
                0 if e["hardness"] == "soft" else 1,
                e["from"],
                e["to"],
                e["edge_id"],
            ))
            removed.add(chosen["edge_id"])
            cycle_break_rows.append({
                "edge_id": chosen["edge_id"],
                "from": chosen["from"],
                "to": chosen["to"],
                "hardness": chosen["hardness"],
            })
            branches["B_CYCLE_BREAK"].append(chosen["edge_id"])

    # topological order of the surviving edges, ties by phase name
    succ = {p: [] for p in phases}
    indeg = {p: 0 for p in phases}
    for e in edges:
        if e["edge_id"] in removed or e["from"] not in indeg or e["to"] not in indeg:
            continue
        succ[e["from"]].append(e["to"])
        indeg[e["to"]] += 1
    heap = [p for p in phases if indeg[p] == 0]
    heapq.heapify(heap)
    topo = []
    placed = set()
    remaining = set(phases)
    while remaining:
        if heap:
            p = heapq.heappop(heap)
            if p in placed:
                continue
        else:
            p = min(remaining)
        topo.append(p)
        placed.add(p)
        remaining.discard(p)
        for t in succ[p]:
            indeg[t] -= 1
            if indeg[t] <= 0 and t not in placed:
                heapq.heappush(heap, t)
    topo_rank = {p: i for i, p in enumerate(topo)}

    # ------------------ stage 6 binding and stage 9 carry, in topological order
    alloc = {}
    bind_rows = []
    carry_rows = []
    credit_carry = 0
    debt_carry = 0
    eff_cap = {}
    for p in topo:
        credit_in = int(carry_init[p]["initial_credit"]) if p in carry_init else 0
        debt_in = int(carry_init[p]["initial_debt"]) if p in carry_init else 0
        credit_in += credit_carry
        debt_in += debt_carry
        capacity_eff = cap[p] + credit_in - debt_in
        eff_cap[p] = capacity_eff

        cur = {}
        raised = False
        for s in members[p]:
            t = target[(p, s)]
            f = floor[(p, s)]
            if f > t:
                raised = True
            cur[s] = f if f > t else t
        if raised:
            branches["B_FLOOR_BIND"].append(p)

        total = sum(cur.values())
        if total > capacity_eff:
            branches["B_CAP_BIND"].append(p)
            order = sorted(members[p], key=lambda s: (
                disp_rank.get(src[s]["safety_stratum"], 0),
                -int(src[s]["priority"]),
                s,
            ))
            excess = total - capacity_eff
            for s in order:
                if excess <= 0:
                    break
                room = cur[s] - floor[(p, s)]
                if room <= 0:
                    continue
                take = room if room < excess else excess
                bind_rows.append({
                    "phase": p,
                    "source_id": s,
                    "before": cur[s],
                    "after": cur[s] - take,
                    "displaced": take,
                    "kind": "above_floor",
                })
                cur[s] -= take
                excess -= take
            if excess > 0:
                branches["B_FLOOR_OVER_CAP"].append(p)
                for s in order:
                    if excess <= 0:
                        break
                    room = cur[s]
                    if room <= 0:
                        continue
                    take = room if room < excess else excess
                    bind_rows.append({
                        "phase": p,
                        "source_id": s,
                        "before": cur[s],
                        "after": cur[s] - take,
                        "displaced": take,
                        "kind": "floor_cut",
                    })
                    cur[s] -= take
                    excess -= take
        for s in members[p]:
            alloc[(p, s)] = cur[s]

        allocated = sum(cur.values())
        credit_out = capacity_eff - allocated
        if credit_out < 0:
            credit_out = 0
        debt_out = 0
        for s in members[p]:
            deferred = floor[(p, s)] - cur[s]
            if deferred > 0:
                debt_out += deferred
        carry_rows.append({
            "phase": p,
            "credit_in": credit_in,
            "debt_in": debt_in,
            "credit_out": credit_out,
            "debt_out": debt_out,
        })
        if debt_out > 0:
            branches["B_CARRY_DEBT"].append(p)
        credit_carry = credit_out
        debt_carry = debt_out

    # -------------------------------------------- stage 8, precedence overturn
    overturn_rows = []
    for p in phases:
        for q in sorted(requires[p]):
            if q == p:
                continue
            for s in members[p]:
                if s in member_set[q] and alloc[(p, s)] > 0 and alloc[(q, s)] == 0:
                    overturn_rows.append({
                        "phase": p,
                        "prereq_phase": q,
                        "source_id": s,
                    })
                    branches["B_PRECEDENCE_OVERTURN"].append(p)

    pre_rows = [
        {"phase": p, "source_id": s, "tokens": alloc[(p, s)]}
        for p in phases for s in members[p]
    ]

    # ------------------------------------------- stage 10, global reconciliation
    final = dict(alloc)
    reconcile_rows = []
    for sid in sids:
        phases_of = [p for p in phases if sid in member_set[p]]
        if not phases_of:
            continue
        total = sum(final[(p, sid)] for p in phases_of)
        supply = canonical[sid]
        if total <= supply:
            continue
        excess = total - supply
        branches["B_GLOBAL_CLAWBACK"].append(sid)
        for p in sorted(phases_of, key=lambda x: -topo_rank[x]):
            if excess <= 0:
                break
            have = final[(p, sid)]
            if have <= 0:
                continue
            take = have if have < excess else excess
            reconcile_rows.append({
                "source_id": sid,
                "phase": p,
                "before": have,
                "after": have - take,
                "clawed": take,
            })
            final[(p, sid)] = have - take
            excess -= take

    final_rows = [
        {"phase": p, "source_id": s, "tokens": final[(p, s)]}
        for p in phases for s in members[p]
    ]

    source_total = {s: 0 for s in sids}
    for p in phases:
        for s in members[p]:
            source_total[s] += final[(p, s)]
    total_tokens = sum(source_total.values())

    # -------------------------------------------------------------- summaries
    lang_rows = []
    for lang in sorted(language_policy):
        tok = sum(source_total[s] for s in sids if src[s]["language"] == lang)
        lang_rows.append({
            "language": lang,
            "tokens": tok,
            "min_required": rhe(int(language_policy[lang]["min_share_bp"]) * total_tokens, scale),
            "max_allowed": rhe(int(language_policy[lang]["max_share_bp"]) * total_tokens, scale),
        })

    safety_rows = []
    for stratum in sorted(safety_policy):
        tok = sum(source_total[s] for s in sids if src[s]["safety_stratum"] == stratum)
        safety_rows.append({
            "stratum": stratum,
            "tokens": tok,
            "floor": int(safety_policy[stratum]["floor_tokens"]),
        })

    source_rows = [
        {"source_id": s, "tokens": source_total[s], "canonical_tokens": canonical[s]}
        for s in sids
    ]

    phase_rows = [
        {
            "phase": p,
            "allocated": sum(final[(p, s)] for s in members[p]),
            "cap": cap[p],
            "budget": budget[p],
        }
        for p in phases
    ]

    # ------------------------------------------------ stages 11 and 12, weights
    by_source = {s: [] for s in sids}
    for e in examples:
        if e["source_id"] in by_source:
            by_source[e["source_id"]].append(e)
    for s in sids:
        by_source[s].sort(key=lambda e: e["example_id"])

    rational_rows = []
    fixed_rows = []
    residual_rows = []
    materialized = {}
    for s in sids:
        exs = by_source[s]
        if not exs:
            continue
        keys = [e["example_id"] for e in exs]
        lengths = {e["example_id"]: int(e["token_length"]) for e in exs}
        group_den = sum(lengths[k] for k in keys)
        total = source_total[s]
        if group_den == 0:
            branches["B_EMPTY_GROUP"].append(s)
            for k in keys:
                materialized[k] = 0
                fixed_rows.append({
                    "example_id": k,
                    "source_id": s,
                    "materialized_tokens": 0,
                })
            continue
        for k in keys:
            w = Fraction(total * lengths[k], group_den)
            rational_rows.append({
                "example_id": k,
                "source_id": s,
                "w_num": w.numerator,
                "w_den": w.denominator,
            })
        base, award = largest_remainder(total, lengths, keys)
        for k in keys:
            materialized[k] = base[k] + award[k]
            fixed_rows.append({
                "example_id": k,
                "source_id": s,
                "materialized_tokens": base[k] + award[k],
            })
            if award[k] > 0:
                residual_rows.append({
                    "example_id": k,
                    "source_id": s,
                    "base_tokens": base[k],
                    "residual_unit": award[k],
                })

    # ------------------------------------------------- stage 13, microbatches
    max_seq = batch_rules["max_seq_tokens"]
    adjacency_limit = batch_rules["adjacency_limit"]
    batch_rows = []
    batch_no = 0
    for p in phases:
        seq = []
        for s in members[p]:
            if final[(p, s)] <= 0:
                continue
            for e in by_source[s]:
                k = e["example_id"]
                if materialized.get(k, 0) > 0:
                    seq.append((k, s, materialized[k]))
        cur_members = []
        cur_sources = []
        cur_total = 0
        for (k, s, tok) in seq:
            start_new = False
            if cur_members:
                if cur_total + tok > max_seq:
                    start_new = True
                else:
                    run = 0
                    for prev in reversed(cur_sources):
                        if prev != s:
                            break
                        run += 1
                    if run >= adjacency_limit:
                        start_new = True
            if start_new:
                batch_no += 1
                batch_rows.append({
                    "batch_id": "BATCH%04d" % batch_no,
                    "phase": p,
                    "members": cur_members,
                    "seq_tokens": cur_total,
                })
                cur_members, cur_sources, cur_total = [], [], 0
            cur_members.append(k)
            cur_sources.append(s)
            cur_total += tok
        if cur_members:
            batch_no += 1
            batch_rows.append({
                "batch_id": "BATCH%04d" % batch_no,
                "phase": p,
                "members": cur_members,
                "seq_tokens": cur_total,
            })

    # ------------------------------------------------------------- invariants
    phase_ok = all(sum(final[(p, s)] for s in members[p]) <= eff_cap[p] for p in phases)
    supply_ok = all(source_total[s] <= canonical[s] for s in sids)
    mat_ok = True
    for s in sids:
        exs = by_source[s]
        if not exs or sum(int(e["token_length"]) for e in exs) == 0:
            continue
        if sum(materialized[e["example_id"]] for e in exs) != source_total[s]:
            mat_ok = False
    batch_ok = all(b["seq_tokens"] <= max_seq or len(b["members"]) == 1 for b in batch_rows)
    invariant_rows = [
        {"invariant": "phase_within_cap", "holds": phase_ok},
        {"invariant": "source_within_supply", "holds": supply_ok},
        {"invariant": "materialized_sums_match", "holds": mat_ok},
        {"invariant": "batches_respect_seq_cap", "holds": batch_ok},
    ]
    invariants_hold = phase_ok and supply_ok and mat_ok and batch_ok

    # -------------------------------------------------------- exception trace
    exception_rows = [
        {"code": code, "ref": ref}
        for code in BRANCH_ORDER
        for ref in branches[code]
    ]

    # ----------------------------------------------------------- the objective
    objective = [
        total_tokens,
        len(reconcile_rows),
        len(cycle_break_rows),
        len(bind_rows),
        len(batch_rows),
    ]
    order_names = [
        "total_tokens",
        "global_clawbacks",
        "cycle_edges_removed",
        "capacity_displacements",
        "microbatches",
    ]

    # --------------------------------------------------------------- emission
    os.makedirs(out_dir, exist_ok=True)

    def out(name):
        return os.path.join(out_dir, name)

    write_jsonl(out("canonical_sources.jsonl"), canonical_rows)
    write_jsonl(out("eligibility_ledger.jsonl"), eligibility_rows)
    write_jsonl(out("demand_score_ledger.jsonl"), demand_rows)
    write_jsonl(out("phase_membership.jsonl"), membership_rows)
    write_jsonl(out("proportional_allocation.jsonl"), prop_rows)
    write_jsonl(out("protected_floor_ledger.jsonl"), floor_rows)
    write_jsonl(out("capacity_binding_trace.jsonl"), bind_rows)
    write_jsonl(out("precedence_closure.jsonl"), closure_rows)
    write_jsonl(out("cycle_report.jsonl"), cycle_rows)
    write_jsonl(out("cycle_break_decisions.jsonl"), cycle_break_rows)
    write_jsonl(out("precedence_overturn_trace.jsonl"), overturn_rows)
    write_jsonl(out("carry_state_ledger.jsonl"), carry_rows)
    write_jsonl(out("pre_reconciliation_ledger.jsonl"), pre_rows)
    write_jsonl(out("global_reconciliation_trace.jsonl"), reconcile_rows)
    write_jsonl(out("final_allocation.jsonl"), final_rows)
    write_jsonl(out("language_allocation_summary.jsonl"), lang_rows)
    write_jsonl(out("safety_allocation_summary.jsonl"), safety_rows)
    write_jsonl(out("source_allocation_summary.jsonl"), source_rows)
    write_jsonl(out("rational_example_weights.jsonl"), rational_rows)
    write_jsonl(out("fixed_point_weights.jsonl"), fixed_rows)
    write_jsonl(out("rounding_residual_ledger.jsonl"), residual_rows)
    write_jsonl(out("microbatch_manifest.jsonl"), batch_rows)
    write_jsonl(out("phase_token_totals.jsonl"), phase_rows)
    write_jsonl(out("invariant_report.jsonl"), invariant_rows)
    write_jsonl(out("exception_trace.jsonl"), exception_rows)
    write_json(out("objective_tuple.json"), {"objective": objective, "order": order_names})

    with open(os.path.abspath(__file__), "rb") as f:
        own_source = f.read()
    with open(out("plan.py"), "wb") as f:
        f.write(own_source)

    manifest = {name: digest(out(name)) for name in HASHED_FILES}
    write_json(out("integrity_manifest.json"), manifest)

    chain = "0" * 64
    for name in sorted(manifest):
        chain = hashlib.sha256((chain + manifest[name]).encode("utf-8")).hexdigest()

    write_json(out("final_state.json"), {
        "chain": chain,
        "total_tokens": total_tokens,
        "global_clawbacks": len(reconcile_rows),
        "capacity_displacements": len(bind_rows),
        "cycle_edges_removed": len(cycle_break_rows),
        "precedence_overturns": len(overturn_rows),
        "microbatches": len(batch_rows),
        "examples_placed": sum(len(b["members"]) for b in batch_rows),
        "quiet_branches": len(exception_rows),
        "objective": objective,
        "invariants_hold": invariants_hold,
    })


def main(argv):
    if len(argv) != 3:
        sys.stderr.write("usage: plan.py <DATA_DIR> <OUT_DIR>\n")
        return 2
    compile_plan(argv[1], argv[2])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
