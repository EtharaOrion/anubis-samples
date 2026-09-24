#!/usr/bin/env python3
"""TrainMix allocation compiler.

Derives the deterministic training mixture certificate from a data directory of
manifests and policies, writing every derived artifact into an output directory.

Usage: python3 plan.py <DATA_DIR> <OUT_DIR>
"""

import csv
import hashlib
import json
import os
import sys
from math import gcd

DOMAIN_PHASES = {
    "maint": ["PH1"],
    "manual": ["PH1", "PH2"],
    "incident": ["PH2"],
    "qa": ["PH2", "PH3"],
    "translated": ["PH1", "PH3"],
    "synthetic": ["PH3"],
    "reg": ["PH2"],
}

JSONL_FILES = [
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
]

OBJECTIVE_ORDER = [
    "total_tokens",
    "global_clawbacks",
    "cycle_edges_removed",
    "capacity_displacements",
    "microbatches",
]


# ----------------------------------------------------------------- arithmetic


def rhe(p, d):
    """round_half_even(p, d): quantize p over the positive denominator d."""
    if d <= 0:
        raise ValueError("quantization denominator must be positive")
    q, r = divmod(p, d)
    twice = 2 * r
    if twice > d:
        return q + 1
    if twice < d:
        return q
    return q if q % 2 == 0 else q + 1


def is_tie(p, d):
    return 2 * (p % d) == d


def reduce_fraction(num, den):
    if den < 0:
        num, den = -num, -den
    g = gcd(abs(num), den)
    if g == 0:
        return 0, 1
    return num // g, den // g


def largest_remainder(total, weights, keys):
    """Split total across keys in proportion to weights; tie break ascending key.

    Returns (base_map, residual_set) where residual_set holds the keys handed a
    single leftover unit above their base quotient.
    """
    wsum = sum(weights[k] for k in keys)
    base = {k: 0 for k in keys}
    if wsum <= 0 or total <= 0:
        return base, set()
    rems = []
    assigned = 0
    for k in keys:
        q, r = divmod(total * weights[k], wsum)
        base[k] = q
        assigned += q
        rems.append((-r, k))
    leftover = total - assigned
    rems.sort()
    residual = set()
    for i in range(leftover):
        residual.add(rems[i][1])
    return base, residual


# --------------------------------------------------------------- serializing


def write_jsonl(path, rows):
    parts = []
    for row in rows:
        parts.append(json.dumps(row, sort_keys=True, separators=(",", ":")))
        parts.append("\n")
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write("".join(parts))


def write_json(path, obj):
    data = json.dumps(obj, sort_keys=True, indent=2) + "\n"
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(data)


def read_csv(path):
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return [dict(row) for row in csv.DictReader(fh)]


def read_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------------ topology


def transitive_closure(nodes, edges):
    """requires[x] is the set of nodes y with a path y -> ... -> x."""
    reach = {n: set() for n in nodes}
    for f, t in edges:
        if f in reach and t in reach:
            reach[t].add(f)
    changed = True
    while changed:
        changed = False
        for n in nodes:
            new = set(reach[n])
            for m in list(reach[n]):
                new |= reach[m]
            if new != reach[n]:
                reach[n] = new
                changed = True
    return reach


def topological_order(nodes, edges):
    indeg = {n: 0 for n in nodes}
    adj = {n: [] for n in nodes}
    seen = set()
    for f, t in edges:
        if f not in indeg or t not in indeg or (f, t) in seen:
            continue
        seen.add((f, t))
        adj[f].append(t)
        indeg[t] += 1
    order = []
    placed = set()
    while len(order) < len(nodes):
        ready = sorted(n for n in nodes if n not in placed and indeg[n] == 0)
        if not ready:
            ready = sorted(n for n in nodes if n not in placed)
        pick = ready[0]
        order.append(pick)
        placed.add(pick)
        for t in sorted(adj[pick]):
            indeg[t] -= 1
    return order


# ---------------------------------------------------------------------- main


def compile_plan(data_dir, out_dir):
    sources_rows = read_csv(os.path.join(data_dir, "sources.csv"))
    quality_rows = read_csv(os.path.join(data_dir, "quality.csv"))
    freshness_rows = read_csv(os.path.join(data_dir, "freshness.csv"))
    example_rows = read_csv(os.path.join(data_dir, "examples.csv"))
    capacity_rows = read_csv(os.path.join(data_dir, "capacity.csv"))
    carry_rows = read_csv(os.path.join(data_dir, "carry_state.csv"))
    batch_rules = read_json(os.path.join(data_dir, "batch_rules.json"))
    curriculum = read_json(os.path.join(data_dir, "curriculum.json"))
    language_policy = read_json(os.path.join(data_dir, "language_policy.json"))
    priority_order = read_json(os.path.join(data_dir, "priority_order.json"))
    quality_policy = read_json(os.path.join(data_dir, "quality_policy.json"))
    safety_policy = read_json(os.path.join(data_dir, "safety_policy.json"))
    scoring_weights = read_json(os.path.join(data_dir, "scoring_weights.json"))
    unit_rules = read_json(os.path.join(data_dir, "unit_rules.json"))

    scale = int(unit_rules["scale"])
    score_scale = int(scoring_weights["scale"])
    languages = language_policy["languages"]
    strata = safety_policy["strata"]
    blocked_domains = set(quality_policy["blocked_domains"])
    min_q_num = int(quality_policy["min_quality"]["num"])
    min_q_den = int(quality_policy["min_quality"]["den"])
    units = unit_rules["units"]
    half_life = int(scoring_weights["half_life_days"])
    priority_weights = scoring_weights["priority_weights"]
    disp_rank = priority_order["stratum_displacement_rank"]

    quality = {r["source_id"]: (int(r["q_num"]), int(r["q_den"])) for r in quality_rows}
    freshness = {r["source_id"]: int(r["age_days"]) for r in freshness_rows}

    source_ids = [r["source_id"] for r in sources_rows]
    src = {}
    for r in sources_rows:
        src[r["source_id"]] = {
            "language": r["language"],
            "domain": r["domain"],
            "safety_stratum": r["safety_stratum"],
            "priority": int(r["priority"]),
            "raw_amount": int(r["raw_amount"]),
            "raw_unit": r["raw_unit"],
        }

    phase_order = [r["phase"] for r in capacity_rows]
    budget = {r["phase"]: int(r["budget_tokens"]) for r in capacity_rows}
    cap = {r["phase"]: int(r["cap_tokens"]) for r in capacity_rows}
    init_credit = {r["phase"]: int(r["initial_credit"]) for r in carry_rows}
    init_debt = {r["phase"]: int(r["initial_debt"]) for r in carry_rows}

    # quiet branches are emitted in the order the stages activate them, so each
    # activation is bucketed under the stage that owns it
    branch_stages = [1, 5, 6, 8, 9, 10, 11]
    branches = {stage: [] for stage in branch_stages}

    def note(stage, code, ref):
        branches[stage].append({"code": code, "ref": ref})

    # --- stage 1, manifest canonicalization ----------------------------------
    canonical_rows = []
    canonical_tokens = {}
    for sid in source_ids:
        s = src[sid]
        unit = units[s["raw_unit"]]
        p = s["raw_amount"] * int(unit["num"])
        den = int(unit["den"])
        tokens = rhe(p, den)
        tie = is_tie(p, den)
        canonical_tokens[sid] = tokens
        canonical_rows.append(
            {
                "source_id": sid,
                "raw_amount": s["raw_amount"],
                "raw_unit": s["raw_unit"],
                "canonical_tokens": tokens,
                "rounding_tie": tie,
            }
        )
        if tie:
            note(1, "B_UNIT_ROUND_TIE", sid)

    # --- stage 2, eligibility projection -------------------------------------
    eligibility_rows = []
    eligible = []
    for sid in source_ids:
        s = src[sid]
        q_num, q_den = quality[sid]
        if q_den == 0 or q_num * min_q_den < min_q_num * q_den:
            code = "E_LOW_QUALITY"
        elif s["language"] not in languages:
            code = "E_LANG_EXCLUDED"
        elif s["safety_stratum"] not in strata:
            code = "E_STRATUM_UNKNOWN"
        elif s["domain"] in blocked_domains:
            code = "E_DOMAIN_BLOCKED"
        elif canonical_tokens[sid] <= 0:
            code = "E_EMPTY_TOKENS"
        else:
            code = "E_OK"
        ok = code == "E_OK"
        if ok:
            eligible.append(sid)
        eligibility_rows.append({"source_id": sid, "eligible": ok, "code": code})

    # --- stage 3, demand scoring ---------------------------------------------
    demand_rows = []
    demand = {}
    for sid in eligible:
        s = src[sid]
        q_num, q_den = quality[sid]
        quality_bp = rhe(q_num * score_scale, q_den)
        fresh_bp = rhe(half_life * score_scale, half_life + freshness[sid])
        priority_factor = int(priority_weights[str(s["priority"])])
        lang_bp = int(languages[s["language"]]["balance_bp"])
        s1 = rhe(canonical_tokens[sid] * quality_bp, score_scale)
        s2 = rhe(s1 * fresh_bp, score_scale)
        s3 = s2 * priority_factor
        score = rhe(s3 * lang_bp, score_scale)
        demand[sid] = score
        demand_rows.append(
            {
                "source_id": sid,
                "quality_bp": quality_bp,
                "fresh_bp": fresh_bp,
                "priority_factor": priority_factor,
                "lang_balance_bp": lang_bp,
                "demand_score": score,
            }
        )

    # --- stage 4, phase membership and proportional allocation ---------------
    members = {ph: [] for ph in phase_order}
    membership_rows = []
    for sid in eligible:
        for ph in DOMAIN_PHASES.get(src[sid]["domain"], []):
            if ph in members:
                members[ph].append(sid)
    for ph in phase_order:
        for sid in members[ph]:
            membership_rows.append({"phase": ph, "source_id": sid})

    target = {}
    proportional_rows = []
    for ph in phase_order:
        demand_sum = sum(demand[sid] for sid in members[ph] if demand[sid] > 0)
        for sid in members[ph]:
            share_num = demand[sid] * budget[ph]
            if demand[sid] > 0 and demand_sum > 0:
                tgt = rhe(share_num, demand_sum)
            else:
                tgt = 0
            target[(ph, sid)] = tgt
            proportional_rows.append(
                {
                    "phase": ph,
                    "source_id": sid,
                    "target_tokens": tgt,
                    "share_num": share_num,
                    "share_den": demand_sum,
                }
            )

    # --- stage 5, protected floors -------------------------------------------
    floor = {}
    floor_rows = []
    floored = {}
    for ph in phase_order:
        floor_bind = False
        for sid in members[ph]:
            s = src[sid]
            safety_floor = int(strata[s["safety_stratum"]]["floor_tokens"])
            language_floor = int(languages[s["language"]]["floor_tokens"])
            f = max(safety_floor, language_floor)
            floor[(ph, sid)] = f
            floored[(ph, sid)] = max(target[(ph, sid)], f)
            if f > target[(ph, sid)]:
                floor_bind = True
            floor_rows.append(
                {
                    "phase": ph,
                    "source_id": sid,
                    "safety_floor": safety_floor,
                    "language_floor": language_floor,
                    "floor": f,
                }
            )
        if floor_bind:
            note(5, "B_FLOOR_BIND", ph)

    # --- stage 7, curriculum precedence closure ------------------------------
    edges = [
        {
            "edge_id": e["edge_id"],
            "from": e["from"],
            "to": e["to"],
            "hardness": e["hardness"],
        }
        for e in curriculum["edges"]
    ]
    nodes = list(phase_order)
    for ph in curriculum.get("phases", []):
        if ph not in nodes:
            nodes.append(ph)
    for e in edges:
        for ph in (e["from"], e["to"]):
            if ph not in nodes:
                nodes.append(ph)

    requires = transitive_closure(nodes, [(e["from"], e["to"]) for e in edges])
    closure_rows = []
    for ph in sorted(nodes):
        for req in sorted(requires[ph]):
            closure_rows.append({"phase": ph, "requires": req})

    # --- stage 8a, cycle break ------------------------------------------------
    cycle_nodes = sorted(n for n in nodes if n in requires[n])
    cycle_rows = []
    cycle_break_rows = []
    removed_edge = None
    if cycle_nodes:
        cycle_rows.append({"cycle_nodes": cycle_nodes})
        cycle_set = set(cycle_nodes)
        candidates = [e for e in edges if e["from"] in cycle_set and e["to"] in cycle_set]
        if candidates:
            candidates.sort(
                key=lambda e: (
                    0 if e["hardness"] == "soft" else 1,
                    e["from"],
                    e["to"],
                    e["edge_id"],
                )
            )
            removed_edge = candidates[0]
            cycle_break_rows.append(
                {
                    "edge_id": removed_edge["edge_id"],
                    "from": removed_edge["from"],
                    "to": removed_edge["to"],
                    "hardness": removed_edge["hardness"],
                }
            )

    surviving = [e for e in edges if e is not removed_edge]
    topo = topological_order(nodes, [(e["from"], e["to"]) for e in surviving])
    topo_phases = [ph for ph in topo if ph in members]
    topo_rank = {ph: i for i, ph in enumerate(topo_phases)}

    # --- stages 6 and 9, capacity binding with carry state -------------------
    alloc = {}
    binding_rows = []
    carry_ledger_rows = []
    credit_out_of = {}
    debt_out_of = {}
    eff_cap_of = {}
    prev_phase = None
    for ph in topo_phases:
        credit_in = init_credit.get(ph, 0)
        debt_in = init_debt.get(ph, 0)
        if prev_phase is not None:
            credit_in += credit_out_of[prev_phase]
            debt_in += debt_out_of[prev_phase]
        eff_cap = cap[ph] + credit_in - debt_in
        eff_cap_of[ph] = eff_cap

        order = sorted(
            members[ph],
            key=lambda sid: (
                disp_rank[src[sid]["safety_stratum"]],
                -src[sid]["priority"],
                sid,
            ),
        )
        cur = {sid: floored[(ph, sid)] for sid in members[ph]}
        excess = sum(cur.values()) - eff_cap
        if excess > 0:
            note(6, "B_CAP_BIND", ph)
            for sid in order:
                if excess <= 0:
                    break
                room = cur[sid] - floor[(ph, sid)]
                if room <= 0:
                    continue
                take = min(room, excess)
                binding_rows.append(
                    {
                        "phase": ph,
                        "source_id": sid,
                        "before": cur[sid],
                        "after": cur[sid] - take,
                        "displaced": take,
                        "kind": "above_floor",
                    }
                )
                cur[sid] -= take
                excess -= take
            if excess > 0:
                note(6, "B_FLOOR_OVER_CAP", ph)
                for sid in order:
                    if excess <= 0:
                        break
                    room = cur[sid]
                    if room <= 0:
                        continue
                    take = min(room, excess)
                    binding_rows.append(
                        {
                            "phase": ph,
                            "source_id": sid,
                            "before": cur[sid],
                            "after": cur[sid] - take,
                            "displaced": take,
                            "kind": "floor_cut",
                        }
                    )
                    cur[sid] -= take
                    excess -= take

        allocated = 0
        deferred = 0
        for sid in members[ph]:
            alloc[(ph, sid)] = cur[sid]
            allocated += cur[sid]
            deferred += max(0, floor[(ph, sid)] - cur[sid])
        credit_out_of[ph] = max(0, eff_cap - allocated)
        debt_out_of[ph] = deferred
        carry_ledger_rows.append(
            {
                "phase": ph,
                "credit_in": credit_in,
                "debt_in": debt_in,
                "credit_out": credit_out_of[ph],
                "debt_out": debt_out_of[ph],
            }
        )
        prev_phase = ph

    # --- stage 8b, precedence overturn ---------------------------------------
    overturn_rows = []
    for ph in phase_order:
        for req in sorted(requires.get(ph, set())):
            if req not in members:
                continue
            for sid in members[ph]:
                if (
                    sid in members[req]
                    and alloc[(req, sid)] == 0
                    and alloc[(ph, sid)] > 0
                ):
                    overturn_rows.append(
                        {"phase": ph, "prereq_phase": req, "source_id": sid}
                    )

    if cycle_break_rows:
        note(8, "B_CYCLE_BREAK", cycle_break_rows[0]["edge_id"])
    for row in overturn_rows:
        note(8, "B_PRECEDENCE_OVERTURN", row["phase"])
    for row in carry_ledger_rows:
        if row["debt_out"] > 0:
            note(9, "B_CARRY_DEBT", row["phase"])

    pre_rows = []
    for ph in phase_order:
        for sid in members[ph]:
            pre_rows.append({"phase": ph, "source_id": sid, "tokens": alloc[(ph, sid)]})

    # --- stage 10, global reconciliation -------------------------------------
    final = dict(alloc)
    reconciliation_rows = []
    for sid in source_ids:
        phases_of = [ph for ph in phase_order if sid in members[ph]]
        total = sum(final[(ph, sid)] for ph in phases_of)
        if total <= canonical_tokens[sid]:
            continue
        excess = total - canonical_tokens[sid]
        note(10, "B_GLOBAL_CLAWBACK", sid)
        for ph in sorted(phases_of, key=lambda p: -topo_rank[p]):
            if excess <= 0:
                break
            have = final[(ph, sid)]
            if have <= 0:
                continue
            take = min(have, excess)
            reconciliation_rows.append(
                {
                    "source_id": sid,
                    "phase": ph,
                    "before": have,
                    "after": have - take,
                    "clawed": take,
                }
            )
            final[(ph, sid)] = have - take
            excess -= take

    final_rows = []
    for ph in phase_order:
        for sid in members[ph]:
            final_rows.append({"phase": ph, "source_id": sid, "tokens": final[(ph, sid)]})

    source_total = {
        sid: sum(final[(ph, sid)] for ph in phase_order if sid in members[ph])
        for sid in source_ids
    }
    total_tokens = sum(source_total.values())

    # --- summaries ------------------------------------------------------------
    language_rows = []
    for lang in sorted(languages):
        language_rows.append(
            {
                "language": lang,
                "tokens": sum(
                    source_total[sid]
                    for sid in source_ids
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
                    for sid in source_ids
                    if src[sid]["safety_stratum"] == stratum
                ),
                "floor": int(strata[stratum]["floor_tokens"]),
            }
        )

    source_summary_rows = [
        {
            "source_id": sid,
            "tokens": source_total[sid],
            "canonical_tokens": canonical_tokens[sid],
        }
        for sid in source_ids
    ]

    phase_total_rows = [
        {
            "phase": ph,
            "allocated": sum(final[(ph, sid)] for sid in members[ph]),
            "cap": cap[ph],
            "budget": budget[ph],
        }
        for ph in phase_order
    ]

    # --- stages 11 and 12, example weights -----------------------------------
    examples_by_source = {sid: [] for sid in source_ids}
    example_len = {}
    for r in example_rows:
        sid = r["source_id"]
        eid = r["example_id"]
        example_len[eid] = int(r["token_length"])
        examples_by_source.setdefault(sid, []).append(eid)
    for sid in examples_by_source:
        examples_by_source[sid].sort()

    ordered_sources = source_ids + sorted(
        sid for sid in examples_by_source if sid not in src
    )
    rational_rows = []
    fixed_rows = []
    residual_rows = []
    materialized = {}
    empty_groups = []
    for sid in ordered_sources:
        eids = examples_by_source.get(sid, [])
        if not eids:
            continue
        group_den = sum(example_len[e] for e in eids)
        total = source_total.get(sid, 0)
        if group_den == 0:
            empty_groups.append(sid)
            for eid in eids:
                materialized[eid] = 0
                fixed_rows.append(
                    {"example_id": eid, "source_id": sid, "materialized_tokens": 0}
                )
            continue
        for eid in eids:
            w_num, w_den = reduce_fraction(total * example_len[eid], group_den)
            rational_rows.append(
                {"example_id": eid, "source_id": sid, "w_num": w_num, "w_den": w_den}
            )
        base, residual = largest_remainder(total, example_len, eids)
        for eid in eids:
            amount = base[eid] + (1 if eid in residual else 0)
            materialized[eid] = amount
            fixed_rows.append(
                {
                    "example_id": eid,
                    "source_id": sid,
                    "materialized_tokens": amount,
                }
            )
        for eid in eids:
            if eid in residual:
                residual_rows.append(
                    {
                        "example_id": eid,
                        "source_id": sid,
                        "base_tokens": base[eid],
                        "residual_unit": 1,
                    }
                )
    for sid in empty_groups:
        note(11, "B_EMPTY_GROUP", sid)

    # --- stage 13, microbatch construction -----------------------------------
    max_seq = int(batch_rules["max_seq_tokens"])
    adjacency_limit = int(batch_rules["adjacency_limit"])
    batch_rows = []
    batch_no = 0

    def emit_batch(phase, member_ids, seq_tokens):
        nonlocal batch_no
        batch_no += 1
        batch_rows.append(
            {
                "batch_id": "BATCH%04d" % batch_no,
                "phase": phase,
                "members": list(member_ids),
                "seq_tokens": seq_tokens,
            }
        )

    for ph in phase_order:
        seq = []
        for sid in sorted(members[ph]):
            if final[(ph, sid)] <= 0:
                continue
            for eid in examples_by_source.get(sid, []):
                if materialized.get(eid, 0) > 0:
                    seq.append((eid, sid, materialized[eid]))
        cur_members = []
        cur_sources = []
        cur_total = 0
        for eid, sid, length in seq:
            start_new = False
            if cur_members:
                if cur_total + length > max_seq:
                    start_new = True
                else:
                    run = 0
                    for prev in reversed(cur_sources):
                        if prev != sid:
                            break
                        run += 1
                    if run >= adjacency_limit:
                        start_new = True
            if start_new:
                emit_batch(ph, cur_members, cur_total)
                cur_members = []
                cur_sources = []
                cur_total = 0
            cur_members.append(eid)
            cur_sources.append(sid)
            cur_total += length
        if cur_members:
            emit_batch(ph, cur_members, cur_total)

    # --- invariants -----------------------------------------------------------
    phase_within_cap = all(
        row["allocated"] <= eff_cap_of.get(row["phase"], cap[row["phase"]])
        for row in phase_total_rows
    )
    source_within_supply = all(
        source_total[sid] <= canonical_tokens[sid] for sid in source_ids
    )
    materialized_ok = True
    for sid in ordered_sources:
        eids = examples_by_source.get(sid, [])
        if not eids or sum(example_len[e] for e in eids) == 0:
            continue
        if sum(materialized[e] for e in eids) != source_total.get(sid, 0):
            materialized_ok = False
    batches_ok = all(
        row["seq_tokens"] <= max_seq or len(row["members"]) == 1 for row in batch_rows
    )
    invariant_rows = [
        {"invariant": "phase_within_cap", "holds": phase_within_cap},
        {"invariant": "source_within_supply", "holds": source_within_supply},
        {"invariant": "materialized_sums_match", "holds": materialized_ok},
        {"invariant": "batches_respect_seq_cap", "holds": batches_ok},
    ]

    objective = [
        total_tokens,
        len(reconciliation_rows),
        len(cycle_break_rows),
        len(binding_rows),
        len(batch_rows),
    ]

    exceptions = [row for stage in branch_stages for row in branches[stage]]

    artifacts = {
        "canonical_sources.jsonl": canonical_rows,
        "eligibility_ledger.jsonl": eligibility_rows,
        "demand_score_ledger.jsonl": demand_rows,
        "phase_membership.jsonl": membership_rows,
        "proportional_allocation.jsonl": proportional_rows,
        "protected_floor_ledger.jsonl": floor_rows,
        "capacity_binding_trace.jsonl": binding_rows,
        "precedence_closure.jsonl": closure_rows,
        "cycle_report.jsonl": cycle_rows,
        "cycle_break_decisions.jsonl": cycle_break_rows,
        "precedence_overturn_trace.jsonl": overturn_rows,
        "carry_state_ledger.jsonl": carry_ledger_rows,
        "pre_reconciliation_ledger.jsonl": pre_rows,
        "global_reconciliation_trace.jsonl": reconciliation_rows,
        "final_allocation.jsonl": final_rows,
        "language_allocation_summary.jsonl": language_rows,
        "safety_allocation_summary.jsonl": safety_rows,
        "source_allocation_summary.jsonl": source_summary_rows,
        "rational_example_weights.jsonl": rational_rows,
        "fixed_point_weights.jsonl": fixed_rows,
        "rounding_residual_ledger.jsonl": residual_rows,
        "microbatch_manifest.jsonl": batch_rows,
        "phase_token_totals.jsonl": phase_total_rows,
        "invariant_report.jsonl": invariant_rows,
        "exception_trace.jsonl": exceptions,
    }
    for name in JSONL_FILES:
        write_jsonl(os.path.join(out_dir, name), artifacts[name])

    write_json(
        os.path.join(out_dir, "objective_tuple.json"),
        {"objective": objective, "order": list(OBJECTIVE_ORDER)},
    )

    self_path = os.path.abspath(__file__)
    program_path = os.path.join(out_dir, "plan.py")
    if os.path.abspath(program_path) != self_path:
        with open(self_path, "rb") as fh:
            own_source = fh.read()
        with open(program_path, "wb") as fh:
            fh.write(own_source)

    manifest = {
        name: sha256_file(os.path.join(out_dir, name))
        for name in JSONL_FILES + ["objective_tuple.json", "plan.py"]
    }
    write_json(os.path.join(out_dir, "integrity_manifest.json"), manifest)

    chain = "0" * 64
    for name in sorted(manifest):
        chain = hashlib.sha256((chain + manifest[name]).encode("utf-8")).hexdigest()

    write_json(
        os.path.join(out_dir, "final_state.json"),
        {
            "chain": chain,
            "total_tokens": total_tokens,
            "global_clawbacks": len(reconciliation_rows),
            "capacity_displacements": len(binding_rows),
            "cycle_edges_removed": len(cycle_break_rows),
            "precedence_overturns": len(overturn_rows),
            "microbatches": len(batch_rows),
            "examples_placed": sum(len(row["members"]) for row in batch_rows),
            "quiet_branches": len(exceptions),
            "objective": objective,
            "invariants_hold": all(row["holds"] for row in invariant_rows),
        },
    )


def main(argv):
    if len(argv) != 3:
        sys.stderr.write("usage: plan.py <DATA_DIR> <OUT_DIR>\n")
        return 2
    os.makedirs(argv[2], exist_ok=True)
    compile_plan(argv[1], argv[2])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
