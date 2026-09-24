#!/usr/bin/env python3
"""TrainMix allocation compiler.

Derives the reproducible training mixture certificate from a manifest
directory. All arithmetic is integer or exact rational; no float value is
produced, compared or serialized.

Usage: python3 plan.py <DATA_DIR> <OUT_DIR>
"""

import csv
import hashlib
import json
import os
import sys
from math import gcd

DOMAIN_PHASES = {
    "maint": ("PH1",),
    "manual": ("PH1", "PH2"),
    "incident": ("PH2",),
    "qa": ("PH2", "PH3"),
    "translated": ("PH1", "PH3"),
    "synthetic": ("PH3",),
    "reg": ("PH2",),
}

MANIFEST_FILES = [
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

OBJECTIVE_ORDER = [
    "total_tokens",
    "global_clawbacks",
    "cycle_edges_removed",
    "capacity_displacements",
    "microbatches",
]


# --------------------------------------------------------------------------
# arithmetic and io primitives
# --------------------------------------------------------------------------


def rhe(p, d):
    """round_half_even(p, d): nearest integer to p/d, exact half to even."""
    if d == 0:
        raise ZeroDivisionError("round_half_even over a zero denominator")
    if d < 0:
        p, d = -p, -d
    q, r = divmod(p, d)
    twice = 2 * r
    if twice > d or (twice == d and q % 2 != 0):
        q += 1
    return q


def is_half(p, d):
    if d == 0:
        return False
    if d < 0:
        p, d = -p, -d
    return 2 * (p % d) == d


def read_csv(path):
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return [dict(row) for row in csv.DictReader(fh)]


def read_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def write_jsonl(path, rows):
    text = "".join(
        json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows
    )
    with open(path, "wb") as fh:
        fh.write(text.encode("utf-8"))


def write_json(path, obj):
    text = json.dumps(obj, sort_keys=True, indent=2) + "\n"
    with open(path, "wb") as fh:
        fh.write(text.encode("utf-8"))


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def largest_remainder(total, keys, weights):
    """Split total across keys in proportion to weights, ties by ascending key."""
    wsum = sum(weights[k] for k in keys)
    base = {k: 0 for k in keys}
    extra = {k: 0 for k in keys}
    if wsum <= 0 or total <= 0:
        return base, extra
    rem = {}
    for k in keys:
        q, r = divmod(total * weights[k], wsum)
        base[k] = q
        rem[k] = r
    leftover = total - sum(base.values())
    order = sorted(keys, key=lambda k: (-rem[k], k))
    for k in order[:leftover]:
        extra[k] = 1
    return base, extra


# --------------------------------------------------------------------------
# graph helpers
# --------------------------------------------------------------------------


def transitive_closure(phases, edges):
    """Set of (a, b) pairs meaning b is reachable from a along one or more edges."""
    reach = set((e["from"], e["to"]) for e in edges)
    changed = True
    while changed:
        changed = False
        for a, b in list(reach):
            for c in phases:
                if (b, c) in reach and (a, c) not in reach:
                    reach.add((a, c))
                    changed = True
    return reach


def cycle_groups(phases, edges):
    """Groups of mutually reachable phases that sit on a cycle."""
    reach = transitive_closure(phases, edges)
    on_cycle = [p for p in phases if (p, p) in reach]
    groups = []
    seen = set()
    for p in on_cycle:
        if p in seen:
            continue
        group = sorted(
            q
            for q in on_cycle
            if q == p or ((p, q) in reach and (q, p) in reach)
        )
        seen.update(group)
        groups.append(group)
    return groups


def topological_order(phases, edges):
    """Kahn order over the given edges, ties broken by ascending phase name."""
    indeg = {p: 0 for p in phases}
    succ = {p: [] for p in phases}
    for e in edges:
        succ[e["from"]].append(e["to"])
        indeg[e["to"]] += 1
    ready = sorted(p for p in phases if indeg[p] == 0)
    order = []
    while ready:
        p = ready.pop(0)
        order.append(p)
        for q in succ[p]:
            indeg[q] -= 1
            if indeg[q] == 0:
                ready.append(q)
                ready.sort()
    for p in phases:
        if p not in order:
            order.append(p)
    return order


# --------------------------------------------------------------------------
# the compiler
# --------------------------------------------------------------------------


def compile_plan(data_dir, out_dir):
    sources_rows = read_csv(os.path.join(data_dir, "sources.csv"))
    quality_rows = read_csv(os.path.join(data_dir, "quality.csv"))
    freshness_rows = read_csv(os.path.join(data_dir, "freshness.csv"))
    example_rows = read_csv(os.path.join(data_dir, "examples.csv"))
    capacity_rows = read_csv(os.path.join(data_dir, "capacity.csv"))
    carry_state_rows = read_csv(os.path.join(data_dir, "carry_state.csv"))
    unit_rules = read_json(os.path.join(data_dir, "unit_rules.json"))
    quality_policy = read_json(os.path.join(data_dir, "quality_policy.json"))
    language_policy = read_json(os.path.join(data_dir, "language_policy.json"))
    safety_policy = read_json(os.path.join(data_dir, "safety_policy.json"))
    scoring_weights = read_json(os.path.join(data_dir, "scoring_weights.json"))
    priority_order = read_json(os.path.join(data_dir, "priority_order.json"))
    curriculum = read_json(os.path.join(data_dir, "curriculum.json"))
    batch_rules = read_json(os.path.join(data_dir, "batch_rules.json"))

    scale = int(unit_rules["scale"])
    score_scale = int(scoring_weights["scale"])
    half_life = int(scoring_weights["half_life_days"])
    priority_weights = scoring_weights["priority_weights"]
    languages = language_policy["languages"]
    strata = safety_policy["strata"]
    blocked_domains = set(quality_policy["blocked_domains"])
    min_q_num = int(quality_policy["min_quality"]["num"])
    min_q_den = int(quality_policy["min_quality"]["den"])
    units = unit_rules["units"]
    disp_rank = priority_order["stratum_displacement_rank"]

    src_order = [r["source_id"] for r in sources_rows]
    src = {r["source_id"]: r for r in sources_rows}
    quality = {r["source_id"]: r for r in quality_rows}
    freshness = {r["source_id"]: int(r["age_days"]) for r in freshness_rows}

    phases = [r["phase"] for r in capacity_rows]
    budget = {r["phase"]: int(r["budget_tokens"]) for r in capacity_rows}
    cap = {r["phase"]: int(r["cap_tokens"]) for r in capacity_rows}
    init_credit = {p: 0 for p in phases}
    init_debt = {p: 0 for p in phases}
    for r in carry_state_rows:
        if r["phase"] in init_credit:
            init_credit[r["phase"]] = int(r["initial_credit"])
            init_debt[r["phase"]] = int(r["initial_debt"])

    exc_stage = {n: [] for n in (1, 5, 6, 8, 9, 10, 11)}

    def quiet(stage, code, ref):
        exc_stage[stage].append({"code": code, "ref": ref})

    # -- stage 1, manifest canonicalization --------------------------------
    canonical = {}
    canonical_rows = []
    for sid in src_order:
        row = src[sid]
        raw_amount = int(row["raw_amount"])
        raw_unit = row["raw_unit"]
        unit = units[raw_unit]
        product = raw_amount * int(unit["num"])
        den = int(unit["den"])
        tokens = rhe(product, den)
        tie = is_half(product, den)
        canonical[sid] = tokens
        canonical_rows.append(
            {
                "source_id": sid,
                "raw_amount": raw_amount,
                "raw_unit": raw_unit,
                "canonical_tokens": tokens,
                "rounding_tie": tie,
            }
        )
        if tie:
            quiet(1, "B_UNIT_ROUND_TIE", sid)

    # -- stage 2, eligibility projection -----------------------------------
    eligibility_rows = []
    eligible = []
    for sid in src_order:
        row = src[sid]
        qrow = quality.get(sid)
        q_num = int(qrow["q_num"]) if qrow else 0
        q_den = int(qrow["q_den"]) if qrow else 0
        if q_den == 0 or q_num * min_q_den < min_q_num * q_den:
            code = "E_LOW_QUALITY"
        elif row["language"] not in languages:
            code = "E_LANG_EXCLUDED"
        elif row["safety_stratum"] not in strata:
            code = "E_STRATUM_UNKNOWN"
        elif row["domain"] in blocked_domains:
            code = "E_DOMAIN_BLOCKED"
        elif not canonical[sid] > 0:
            code = "E_EMPTY_TOKENS"
        else:
            code = "E_OK"
        eligibility_rows.append(
            {"source_id": sid, "eligible": code == "E_OK", "code": code}
        )
        if code == "E_OK":
            eligible.append(sid)

    # -- stage 3, demand scoring -------------------------------------------
    demand = {}
    demand_rows = []
    for sid in eligible:
        row = src[sid]
        qrow = quality[sid]
        quality_bp = rhe(int(qrow["q_num"]) * score_scale, int(qrow["q_den"]))
        fresh_bp = rhe(half_life * score_scale, half_life + freshness[sid])
        priority_factor = int(priority_weights[str(int(row["priority"]))])
        balance_bp = int(languages[row["language"]]["balance_bp"])
        s1 = rhe(canonical[sid] * quality_bp, score_scale)
        s2 = rhe(s1 * fresh_bp, score_scale)
        s3 = s2 * priority_factor
        score = rhe(s3 * balance_bp, score_scale)
        demand[sid] = score
        demand_rows.append(
            {
                "source_id": sid,
                "quality_bp": quality_bp,
                "fresh_bp": fresh_bp,
                "priority_factor": priority_factor,
                "lang_balance_bp": balance_bp,
                "demand_score": score,
            }
        )

    # -- stage 4, phase membership and proportional allocation -------------
    members = {p: [] for p in phases}
    for sid in eligible:
        for p in DOMAIN_PHASES.get(src[sid]["domain"], ()):
            if p in members:
                members[p].append(sid)
    membership_rows = []
    for p in phases:
        members[p] = sorted(members[p])
        for sid in members[p]:
            membership_rows.append({"phase": p, "source_id": sid})

    target = {}
    proportional_rows = []
    for p in phases:
        demand_sum = sum(demand[s] for s in members[p] if demand[s] > 0)
        for sid in members[p]:
            share_num = demand[sid] * budget[p]
            if demand[sid] > 0 and demand_sum > 0:
                tokens = rhe(share_num, demand_sum)
            else:
                tokens = 0
            target[(p, sid)] = tokens
            proportional_rows.append(
                {
                    "phase": p,
                    "source_id": sid,
                    "target_tokens": tokens,
                    "share_num": share_num,
                    "share_den": demand_sum,
                }
            )

    # -- stage 5, protected floors -----------------------------------------
    floor = {}
    floor_rows = []
    for p in phases:
        bound = False
        for sid in members[p]:
            row = src[sid]
            safety_floor = int(strata[row["safety_stratum"]]["floor_tokens"])
            language_floor = int(languages[row["language"]]["floor_tokens"])
            fl = max(safety_floor, language_floor)
            floor[(p, sid)] = fl
            floor_rows.append(
                {
                    "phase": p,
                    "source_id": sid,
                    "safety_floor": safety_floor,
                    "language_floor": language_floor,
                    "floor": fl,
                }
            )
            if fl > target[(p, sid)]:
                bound = True
        if bound:
            quiet(5, "B_FLOOR_BIND", p)

    # -- stage 7, curriculum precedence closure ----------------------------
    edges = [e for e in curriculum["edges"] if e["from"] in cap and e["to"] in cap]
    closure = transitive_closure(phases, edges)
    closure_rows = []
    for p in sorted(phases):
        for q in sorted(phases):
            if (q, p) in closure:
                closure_rows.append({"phase": p, "requires": q})

    # -- stage 8a, cycle report and cycle break ----------------------------
    cycle_rows = [{"cycle_nodes": list(g)} for g in cycle_groups(phases, edges)]
    break_rows = []
    surviving = list(edges)
    while True:
        groups = cycle_groups(phases, surviving)
        if not groups:
            break
        group_of = {}
        for idx, group in enumerate(groups):
            for node in group:
                group_of[node] = idx
        candidates = [
            e
            for e in surviving
            if e["from"] in group_of
            and e["to"] in group_of
            and group_of[e["from"]] == group_of[e["to"]]
        ]
        if not candidates:
            break
        victim = min(
            candidates,
            key=lambda e: (
                0 if e["hardness"] == "soft" else 1,
                e["from"],
                e["to"],
                e["edge_id"],
            ),
        )
        break_rows.append(
            {
                "edge_id": victim["edge_id"],
                "from": victim["from"],
                "to": victim["to"],
                "hardness": victim["hardness"],
            }
        )
        quiet(8, "B_CYCLE_BREAK", victim["edge_id"])
        surviving = [e for e in surviving if e is not victim]

    topo = topological_order(phases, surviving)

    # -- stages 6 and 9, capacity binding and carry state ------------------
    alloc = {}
    binding_rows = []
    carry_rows = []
    effective_cap = {}
    credit_carry = 0
    debt_carry = 0
    for p in topo:
        credit_in = init_credit[p] + credit_carry
        debt_in = init_debt[p] + debt_carry
        eff = cap[p] + credit_in - debt_in
        effective_cap[p] = eff
        current = {s: max(target[(p, s)], floor[(p, s)]) for s in members[p]}
        deferred = 0
        if sum(current.values()) > eff:
            quiet(6, "B_CAP_BIND", p)
            excess = sum(current.values()) - eff
            order = sorted(
                members[p],
                key=lambda s: (
                    disp_rank.get(src[s]["safety_stratum"], len(disp_rank)),
                    -int(src[s]["priority"]),
                    s,
                ),
            )
            for sid in order:
                if excess <= 0:
                    break
                take = min(current[sid] - floor[(p, sid)], excess)
                if take > 0:
                    binding_rows.append(
                        {
                            "phase": p,
                            "source_id": sid,
                            "before": current[sid],
                            "after": current[sid] - take,
                            "displaced": take,
                            "kind": "above_floor",
                        }
                    )
                    current[sid] -= take
                    excess -= take
            if excess > 0:
                quiet(6, "B_FLOOR_OVER_CAP", p)
                for sid in order:
                    if excess <= 0:
                        break
                    take = min(current[sid], excess)
                    if take > 0:
                        binding_rows.append(
                            {
                                "phase": p,
                                "source_id": sid,
                                "before": current[sid],
                                "after": current[sid] - take,
                                "displaced": take,
                                "kind": "floor_cut",
                            }
                        )
                        current[sid] -= take
                        excess -= take
                        deferred += take
        for sid in members[p]:
            alloc[(p, sid)] = current[sid]
        credit_out = eff - sum(current.values())
        if credit_out < 0:
            credit_out = 0
        carry_rows.append(
            {
                "phase": p,
                "credit_in": credit_in,
                "debt_in": debt_in,
                "credit_out": credit_out,
                "debt_out": deferred,
            }
        )
        if deferred > 0:
            quiet(9, "B_CARRY_DEBT", p)
        credit_carry = credit_out
        debt_carry = deferred

    pre_rows = []
    for p in phases:
        for sid in members[p]:
            pre_rows.append({"phase": p, "source_id": sid, "tokens": alloc[(p, sid)]})

    # -- stage 8b, precedence overturn -------------------------------------
    overturn_rows = []
    for p in phases:
        for q in phases:
            if p == q or (q, p) not in closure:
                continue
            for sid in members[p]:
                if sid in members[q] and alloc[(p, sid)] > 0 and alloc[(q, sid)] == 0:
                    overturn_rows.append(
                        {"phase": p, "prereq_phase": q, "source_id": sid}
                    )
                    quiet(8, "B_PRECEDENCE_OVERTURN", p)

    # -- stage 10, global reconciliation ------------------------------------
    final = dict(alloc)
    reconcile_rows = []
    for sid in src_order:
        held = [p for p in phases if sid in members[p]]
        excess = sum(final[(p, sid)] for p in held) - canonical[sid]
        if excess <= 0:
            continue
        for p in reversed(topo):
            if excess <= 0:
                break
            if sid not in members[p]:
                continue
            take = min(final[(p, sid)], excess)
            if take > 0:
                reconcile_rows.append(
                    {
                        "source_id": sid,
                        "phase": p,
                        "before": final[(p, sid)],
                        "after": final[(p, sid)] - take,
                        "clawed": take,
                    }
                )
                final[(p, sid)] -= take
                excess -= take
        quiet(10, "B_GLOBAL_CLAWBACK", sid)

    final_rows = []
    for p in phases:
        for sid in members[p]:
            final_rows.append({"phase": p, "source_id": sid, "tokens": final[(p, sid)]})

    source_total = {
        sid: sum(final[(p, sid)] for p in phases if sid in members[p])
        for sid in src_order
    }
    total_tokens = sum(source_total.values())

    # -- summaries ----------------------------------------------------------
    language_rows = []
    for lang in sorted(languages):
        language_rows.append(
            {
                "language": lang,
                "tokens": sum(
                    source_total[sid]
                    for sid in src_order
                    if src[sid]["language"] == lang
                ),
                "min_required": rhe(
                    int(languages[lang]["min_share_bp"]) * total_tokens, scale
                ),
                "max_allowed": rhe(
                    int(languages[lang]["max_share_bp"]) * total_tokens, scale
                ),
            }
        )

    safety_rows = []
    for stratum in sorted(strata):
        safety_rows.append(
            {
                "stratum": stratum,
                "tokens": sum(
                    source_total[sid]
                    for sid in src_order
                    if src[sid]["safety_stratum"] == stratum
                ),
                "floor": int(strata[stratum]["floor_tokens"]),
            }
        )

    source_rows = [
        {
            "source_id": sid,
            "tokens": source_total[sid],
            "canonical_tokens": canonical[sid],
        }
        for sid in src_order
    ]

    phase_total_rows = [
        {
            "phase": p,
            "allocated": sum(final[(p, sid)] for sid in members[p]),
            "cap": cap[p],
            "budget": budget[p],
        }
        for p in phases
    ]

    # -- stages 11 and 12, example weights ----------------------------------
    examples_by_source = {sid: [] for sid in src_order}
    ex_len = {}
    for row in example_rows:
        sid = row["source_id"]
        if sid not in examples_by_source:
            continue
        examples_by_source[sid].append(row["example_id"])
        ex_len[row["example_id"]] = int(row["token_length"])
    for sid in src_order:
        examples_by_source[sid].sort()

    rational_rows = []
    fixed_rows = []
    residual_rows = []
    materialized = {}
    for sid in src_order:
        ids = examples_by_source[sid]
        group_den = sum(ex_len[e] for e in ids)
        total = source_total[sid]
        if ids and group_den == 0:
            quiet(11, "B_EMPTY_GROUP", sid)
        if ids and group_den > 0:
            for eid in ids:
                num = total * ex_len[eid]
                g = gcd(abs(num), group_den) or 1
                rational_rows.append(
                    {
                        "example_id": eid,
                        "source_id": sid,
                        "w_num": num // g,
                        "w_den": group_den // g,
                    }
                )
            base, extra = largest_remainder(total, ids, ex_len)
        else:
            base = {e: 0 for e in ids}
            extra = {e: 0 for e in ids}
        for eid in ids:
            materialized[eid] = base[eid] + extra[eid]
            fixed_rows.append(
                {
                    "example_id": eid,
                    "source_id": sid,
                    "materialized_tokens": materialized[eid],
                }
            )
            if extra[eid] > 0:
                residual_rows.append(
                    {
                        "example_id": eid,
                        "source_id": sid,
                        "base_tokens": base[eid],
                        "residual_unit": extra[eid],
                    }
                )

    # -- stage 13, microbatch construction -----------------------------------
    max_seq = int(batch_rules["max_seq_tokens"])
    adjacency_limit = int(batch_rules["adjacency_limit"])
    batch_rows = []
    batch = {"members": [], "sources": [], "tokens": 0}

    def flush(phase):
        if batch["members"]:
            batch_rows.append(
                {
                    "batch_id": "BATCH%04d" % (len(batch_rows) + 1),
                    "phase": phase,
                    "members": list(batch["members"]),
                    "seq_tokens": batch["tokens"],
                }
            )
        batch["members"] = []
        batch["sources"] = []
        batch["tokens"] = 0

    for p in phases:
        for sid in sorted(members[p]):
            if final[(p, sid)] <= 0:
                continue
            for eid in examples_by_source[sid]:
                tok = materialized[eid]
                if tok <= 0:
                    continue
                if batch["members"]:
                    trailing = 0
                    for prev in reversed(batch["sources"]):
                        if prev != sid:
                            break
                        trailing += 1
                    if batch["tokens"] + tok > max_seq or trailing >= adjacency_limit:
                        flush(p)
                batch["members"].append(eid)
                batch["sources"].append(sid)
                batch["tokens"] += tok
        flush(p)

    # -- invariants ----------------------------------------------------------
    materialized_ok = True
    for sid in src_order:
        ids = examples_by_source[sid]
        if not ids or sum(ex_len[e] for e in ids) == 0:
            continue
        if sum(materialized[e] for e in ids) != source_total[sid]:
            materialized_ok = False
    invariant_rows = [
        {
            "invariant": "phase_within_cap",
            "holds": all(
                sum(final[(p, sid)] for sid in members[p]) <= effective_cap[p]
                for p in phases
            ),
        },
        {
            "invariant": "source_within_supply",
            "holds": all(source_total[sid] <= canonical[sid] for sid in src_order),
        },
        {"invariant": "materialized_sums_match", "holds": materialized_ok},
        {
            "invariant": "batches_respect_seq_cap",
            "holds": all(
                row["seq_tokens"] <= max_seq or len(row["members"]) == 1
                for row in batch_rows
            ),
        },
    ]

    exception_rows = []
    for stage in (1, 5, 6, 8, 9, 10, 11):
        exception_rows.extend(exc_stage[stage])

    objective = [
        total_tokens,
        len(reconcile_rows),
        len(break_rows),
        len(binding_rows),
        len(batch_rows),
    ]

    # -- emission -------------------------------------------------------------
    os.makedirs(out_dir, exist_ok=True)

    def out(name):
        return os.path.join(out_dir, name)

    write_jsonl(out("canonical_sources.jsonl"), canonical_rows)
    write_jsonl(out("eligibility_ledger.jsonl"), eligibility_rows)
    write_jsonl(out("demand_score_ledger.jsonl"), demand_rows)
    write_jsonl(out("phase_membership.jsonl"), membership_rows)
    write_jsonl(out("proportional_allocation.jsonl"), proportional_rows)
    write_jsonl(out("protected_floor_ledger.jsonl"), floor_rows)
    write_jsonl(out("capacity_binding_trace.jsonl"), binding_rows)
    write_jsonl(out("precedence_closure.jsonl"), closure_rows)
    write_jsonl(out("cycle_report.jsonl"), cycle_rows)
    write_jsonl(out("cycle_break_decisions.jsonl"), break_rows)
    write_jsonl(out("precedence_overturn_trace.jsonl"), overturn_rows)
    write_jsonl(out("carry_state_ledger.jsonl"), carry_rows)
    write_jsonl(out("pre_reconciliation_ledger.jsonl"), pre_rows)
    write_jsonl(out("global_reconciliation_trace.jsonl"), reconcile_rows)
    write_jsonl(out("final_allocation.jsonl"), final_rows)
    write_jsonl(out("language_allocation_summary.jsonl"), language_rows)
    write_jsonl(out("safety_allocation_summary.jsonl"), safety_rows)
    write_jsonl(out("source_allocation_summary.jsonl"), source_rows)
    write_jsonl(out("rational_example_weights.jsonl"), rational_rows)
    write_jsonl(out("fixed_point_weights.jsonl"), fixed_rows)
    write_jsonl(out("rounding_residual_ledger.jsonl"), residual_rows)
    write_jsonl(out("microbatch_manifest.jsonl"), batch_rows)
    write_jsonl(out("phase_token_totals.jsonl"), phase_total_rows)
    write_jsonl(out("invariant_report.jsonl"), invariant_rows)
    write_jsonl(out("exception_trace.jsonl"), exception_rows)
    write_json(
        out("objective_tuple.json"),
        {"objective": objective, "order": list(OBJECTIVE_ORDER)},
    )

    with open(os.path.abspath(__file__), "rb") as fh:
        own_source = fh.read()
    with open(out("plan.py"), "wb") as fh:
        fh.write(own_source)

    manifest = {name: sha256_file(out(name)) for name in MANIFEST_FILES}
    write_json(out("integrity_manifest.json"), manifest)

    chain = "0" * 64
    for name in sorted(manifest):
        chain = hashlib.sha256((chain + manifest[name]).encode("utf-8")).hexdigest()

    write_json(
        out("final_state.json"),
        {
            "chain": chain,
            "total_tokens": total_tokens,
            "global_clawbacks": len(reconcile_rows),
            "capacity_displacements": len(binding_rows),
            "cycle_edges_removed": len(break_rows),
            "precedence_overturns": len(overturn_rows),
            "microbatches": len(batch_rows),
            "examples_placed": sum(len(row["members"]) for row in batch_rows),
            "quiet_branches": len(exception_rows),
            "objective": objective,
            "invariants_hold": all(row["holds"] for row in invariant_rows),
        },
    )


def main(argv):
    if len(argv) != 3:
        sys.stderr.write("usage: plan.py <DATA_DIR> <OUT_DIR>\n")
        return 2
    compile_plan(argv[1], argv[2])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
