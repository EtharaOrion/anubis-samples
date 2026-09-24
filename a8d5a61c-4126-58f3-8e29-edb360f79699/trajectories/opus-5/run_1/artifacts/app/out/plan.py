#!/usr/bin/env python3
"""TrainMix allocation compiler.

Derives the reproducible training mixture certificate from the shipped manifests
in the data directory handed as the first argument, writing every derived record
into the output directory handed as the second argument.

All arithmetic is exact integer or rational arithmetic; no float is ever built,
compared or serialized.
"""

import csv
import hashlib
import json
import os
import sys
from fractions import Fraction

DOMAIN_PHASES = {
    "maint": ("PH1",),
    "manual": ("PH1", "PH2"),
    "incident": ("PH2",),
    "qa": ("PH2", "PH3"),
    "translated": ("PH1", "PH3"),
    "synthetic": ("PH3",),
    "reg": ("PH2",),
}

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


# ---------------------------------------------------------------- arithmetic

def rhe(p, d):
    """round_half_even(p, d): quantize p over the positive denominator d."""
    if d == 0:
        raise ZeroDivisionError("round_half_even over a zero denominator")
    if d < 0:
        p, d = -p, -d
    q, r = divmod(p, d)
    twice = 2 * r
    if twice > d:
        return q + 1
    if twice < d:
        return q
    return q if q % 2 == 0 else q + 1


def is_half(p, d):
    """True when p over d lands on an exact half."""
    if d == 0:
        return False
    if d < 0:
        p, d = -p, -d
    return 2 * (p % d) == d


def largest_remainder(total, keys, weights):
    """Split total across keys in proportion to weights, ties by ascending key.

    Returns the base quotient map and the final share map.
    """
    wsum = sum(weights[k] for k in keys)
    base = dict((k, 0) for k in keys)
    share = dict((k, 0) for k in keys)
    if wsum <= 0 or total <= 0:
        return base, share
    for k in keys:
        base[k] = (total * weights[k]) // wsum
        share[k] = base[k]
    leftover = total - sum(base.values())
    order = sorted(keys, key=lambda k: (-((total * weights[k]) % wsum), k))
    for k in order[:leftover]:
        share[k] += 1
    return base, share


# ------------------------------------------------------------------- reading

def read_csv(path):
    with open(path, "r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def read_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


# --------------------------------------------------------------- serializing

def write_jsonl(path, rows):
    body = "".join(
        json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows
    )
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(body)


def write_json(path, obj):
    body = json.dumps(obj, sort_keys=True, indent=2) + "\n"
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(body)


def digest(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


# ------------------------------------------------------------------ compiler

def compile_plan(data_dir, out_dir):
    sources = read_csv(os.path.join(data_dir, "sources.csv"))
    quality = dict(
        (r["source_id"], r) for r in read_csv(os.path.join(data_dir, "quality.csv"))
    )
    freshness = dict(
        (r["source_id"], r) for r in read_csv(os.path.join(data_dir, "freshness.csv"))
    )
    examples = read_csv(os.path.join(data_dir, "examples.csv"))
    capacity = read_csv(os.path.join(data_dir, "capacity.csv"))
    carry_seed = dict(
        (r["phase"], r) for r in read_csv(os.path.join(data_dir, "carry_state.csv"))
    )
    batch_rules = read_json(os.path.join(data_dir, "batch_rules.json"))
    curriculum = read_json(os.path.join(data_dir, "curriculum.json"))
    lang_policy = read_json(os.path.join(data_dir, "language_policy.json"))["languages"]
    priority_order = read_json(os.path.join(data_dir, "priority_order.json"))
    quality_policy = read_json(os.path.join(data_dir, "quality_policy.json"))
    safety_policy = read_json(os.path.join(data_dir, "safety_policy.json"))["strata"]
    scoring_weights = read_json(os.path.join(data_dir, "scoring_weights.json"))
    unit_rules = read_json(os.path.join(data_dir, "unit_rules.json"))

    scale = unit_rules["scale"]
    score_scale = scoring_weights["scale"]
    half_life = scoring_weights["half_life_days"]
    priority_weights = scoring_weights["priority_weights"]
    units = unit_rules["units"]
    blocked = set(quality_policy["blocked_domains"])
    min_q = quality_policy["min_quality"]
    disp_rank = priority_order["stratum_displacement_rank"]

    order = [r["source_id"] for r in sources]
    src = dict((r["source_id"], r) for r in sources)
    phases = [r["phase"] for r in capacity]
    budget = dict((r["phase"], int(r["budget_tokens"])) for r in capacity)
    cap = dict((r["phase"], int(r["cap_tokens"])) for r in capacity)

    branches = dict((code, []) for code in BRANCH_ORDER)

    # -- stage 1, manifest canonicalization --------------------------------
    canonical = {}
    canonical_rows = []
    for sid in order:
        row = src[sid]
        raw_amount = int(row["raw_amount"])
        raw_unit = row["raw_unit"]
        unit = units[raw_unit]
        product = raw_amount * unit["num"]
        den = unit["den"]
        tokens = rhe(product, den)
        tie = is_half(product, den)
        canonical[sid] = tokens
        if tie:
            branches["B_UNIT_ROUND_TIE"].append(sid)
        canonical_rows.append(
            {
                "source_id": sid,
                "raw_amount": raw_amount,
                "raw_unit": raw_unit,
                "canonical_tokens": tokens,
                "rounding_tie": tie,
            }
        )

    # -- stage 2, eligibility projection -----------------------------------
    eligibility_rows = []
    eligible = []
    for sid in order:
        row = src[sid]
        qrow = quality.get(sid)
        q_num = int(qrow["q_num"]) if qrow else 0
        q_den = int(qrow["q_den"]) if qrow else 0
        if q_den == 0 or q_num * min_q["den"] < min_q["num"] * q_den:
            code = "E_LOW_QUALITY"
        elif row["language"] not in lang_policy:
            code = "E_LANG_EXCLUDED"
        elif row["safety_stratum"] not in safety_policy:
            code = "E_STRATUM_UNKNOWN"
        elif row["domain"] in blocked:
            code = "E_DOMAIN_BLOCKED"
        elif canonical[sid] <= 0:
            code = "E_EMPTY_TOKENS"
        else:
            code = "E_OK"
        if code == "E_OK":
            eligible.append(sid)
        eligibility_rows.append(
            {"source_id": sid, "eligible": code == "E_OK", "code": code}
        )
    eligible_set = set(eligible)

    # -- stage 3, demand scoring -------------------------------------------
    demand = {}
    demand_rows = []
    for sid in eligible:
        row = src[sid]
        qrow = quality[sid]
        quality_bp = rhe(int(qrow["q_num"]) * score_scale, int(qrow["q_den"]))
        frow = freshness.get(sid)
        age_days = int(frow["age_days"]) if frow else 0
        fresh_den = half_life + age_days
        fresh_bp = rhe(half_life * score_scale, fresh_den) if fresh_den else 0
        priority_factor = priority_weights.get(str(row["priority"]), 0)
        balance_bp = lang_policy[row["language"]]["balance_bp"]
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

    # -- stage 4, phase membership and proportional allocation --------------
    members = dict((ph, []) for ph in phases)
    for sid in eligible:
        for ph in DOMAIN_PHASES.get(src[sid]["domain"], ()):
            if ph in members:
                members[ph].append(sid)

    membership_rows = []
    for ph in phases:
        for sid in members[ph]:
            membership_rows.append({"phase": ph, "source_id": sid})

    target = {}
    proportional_rows = []
    for ph in phases:
        demand_sum = sum(demand[s] for s in members[ph] if demand[s] > 0)
        for sid in members[ph]:
            share_num = demand[sid] * budget[ph]
            if demand[sid] > 0 and demand_sum > 0:
                tokens = rhe(share_num, demand_sum)
            else:
                tokens = 0
            target[(ph, sid)] = tokens
            proportional_rows.append(
                {
                    "phase": ph,
                    "source_id": sid,
                    "target_tokens": tokens,
                    "share_num": share_num,
                    "share_den": demand_sum,
                }
            )

    # -- stage 5, protected floors ------------------------------------------
    floor = {}
    floor_rows = []
    for ph in phases:
        raised = False
        for sid in members[ph]:
            row = src[sid]
            safety_floor = safety_policy[row["safety_stratum"]]["floor_tokens"]
            language_floor = lang_policy[row["language"]]["floor_tokens"]
            value = max(safety_floor, language_floor)
            floor[(ph, sid)] = value
            if value > target[(ph, sid)]:
                raised = True
            floor_rows.append(
                {
                    "phase": ph,
                    "source_id": sid,
                    "safety_floor": safety_floor,
                    "language_floor": language_floor,
                    "floor": value,
                }
            )
        if raised:
            branches["B_FLOOR_BIND"].append(ph)

    # -- stage 7, curriculum precedence closure ------------------------------
    edges = curriculum["edges"]
    nodes = list(curriculum.get("phases", []))
    for edge in edges:
        for key in ("from", "to"):
            if edge[key] not in nodes:
                nodes.append(edge[key])

    successors = dict((n, set()) for n in nodes)
    for edge in edges:
        successors[edge["from"]].add(edge["to"])
    reach = dict((n, set(successors[n])) for n in nodes)
    changed = True
    while changed:
        changed = False
        for n in nodes:
            extra = set()
            for m in reach[n]:
                extra |= reach[m]
            if not extra <= reach[n]:
                reach[n] |= extra
                changed = True

    # an edge from A to B means A precedes B, so B requires A
    requires = dict(
        (n, sorted(m for m in nodes if n in reach[m])) for n in nodes
    )
    closure_rows = []
    for ph in sorted(nodes):
        for prereq in requires[ph]:
            closure_rows.append({"phase": ph, "requires": prereq})

    # -- stage 8, cycle report and cycle break --------------------------------
    cycle_nodes = sorted(n for n in nodes if n in reach[n])
    cycle_rows = []
    cycle_break_rows = []
    removed_edge = None
    if cycle_nodes:
        cycle_rows.append({"cycle_nodes": cycle_nodes})
        cycle_set = set(cycle_nodes)
        candidates = [
            e for e in edges if e["from"] in cycle_set and e["to"] in cycle_set
        ]
        if candidates:
            removed_edge = min(
                candidates,
                key=lambda e: (
                    0 if e["hardness"] == "soft" else 1,
                    e["from"],
                    e["to"],
                    e["edge_id"],
                ),
            )
            cycle_break_rows.append(
                {
                    "edge_id": removed_edge["edge_id"],
                    "from": removed_edge["from"],
                    "to": removed_edge["to"],
                    "hardness": removed_edge["hardness"],
                }
            )
            branches["B_CYCLE_BREAK"].append(removed_edge["edge_id"])

    surviving = [e for e in edges if e is not removed_edge]

    # topological order of the surviving edges, ties by phase name
    phase_set = set(phases)
    indegree = dict((ph, 0) for ph in phases)
    forward = dict((ph, []) for ph in phases)
    for edge in surviving:
        a, b = edge["from"], edge["to"]
        if a in phase_set and b in phase_set and b not in forward[a]:
            forward[a].append(b)
            indegree[b] += 1
    ready = sorted(ph for ph in phases if indegree[ph] == 0)
    topo = []
    while ready:
        node = ready.pop(0)
        topo.append(node)
        for nxt in sorted(forward[node]):
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                ready.append(nxt)
        ready.sort()
    for ph in sorted(phases):
        if ph not in topo:
            topo.append(ph)

    # -- stages 6 and 9, capacity binding and carry state ---------------------
    alloc = {}
    binding_rows = []
    carry_ledger_rows = []
    cap_bound = []
    floor_over_cap = []
    carry_debt = []
    credit_in = dict(
        (ph, int(carry_seed[ph]["initial_credit"]) if ph in carry_seed else 0)
        for ph in phases
    )
    debt_in = dict(
        (ph, int(carry_seed[ph]["initial_debt"]) if ph in carry_seed else 0)
        for ph in phases
    )
    effective = {}
    for index, ph in enumerate(topo):
        eff = cap[ph] + credit_in[ph] - debt_in[ph]
        effective[ph] = eff
        current = {}
        for sid in members[ph]:
            current[sid] = max(target[(ph, sid)], floor[(ph, sid)])
        displacement_order = sorted(
            members[ph],
            key=lambda sid: (
                disp_rank.get(src[sid]["safety_stratum"], 0),
                -int(src[sid]["priority"]),
                sid,
            ),
        )
        excess = sum(current.values()) - eff
        if excess > 0:
            cap_bound.append(ph)
            for sid in displacement_order:
                if excess <= 0:
                    break
                room = current[sid] - floor[(ph, sid)]
                if room <= 0:
                    continue
                cut = min(room, excess)
                before = current[sid]
                current[sid] = before - cut
                excess -= cut
                binding_rows.append(
                    {
                        "phase": ph,
                        "source_id": sid,
                        "before": before,
                        "after": current[sid],
                        "displaced": cut,
                        "kind": "above_floor",
                    }
                )
            if excess > 0:
                floor_over_cap.append(ph)
                for sid in displacement_order:
                    if excess <= 0:
                        break
                    room = current[sid]
                    if room <= 0:
                        continue
                    cut = min(room, excess)
                    before = current[sid]
                    current[sid] = before - cut
                    excess -= cut
                    binding_rows.append(
                        {
                            "phase": ph,
                            "source_id": sid,
                            "before": before,
                            "after": current[sid],
                            "displaced": cut,
                            "kind": "floor_cut",
                        }
                    )
        for sid in members[ph]:
            alloc[(ph, sid)] = current[sid]
        allocated = sum(current.values())
        credit_out = max(0, eff - allocated)
        debt_out = sum(max(0, floor[(ph, sid)] - current[sid]) for sid in members[ph])
        if debt_out > 0:
            carry_debt.append(ph)
        carry_ledger_rows.append(
            {
                "phase": ph,
                "credit_in": credit_in[ph],
                "debt_in": debt_in[ph],
                "credit_out": credit_out,
                "debt_out": debt_out,
            }
        )
        if index + 1 < len(topo):
            nxt = topo[index + 1]
            credit_in[nxt] += credit_out
            debt_in[nxt] += debt_out

    pre_rows = []
    for ph in phases:
        for sid in members[ph]:
            pre_rows.append({"phase": ph, "source_id": sid, "tokens": alloc[(ph, sid)]})

    # -- stage 8, precedence overturn -----------------------------------------
    overturn_rows = []
    for ph in phases:
        for prereq in requires.get(ph, []):
            if prereq == ph or prereq not in members:
                continue
            for sid in members[ph]:
                if sid in members[prereq] and alloc[(prereq, sid)] == 0 and alloc[(ph, sid)] > 0:
                    overturn_rows.append(
                        {"phase": ph, "prereq_phase": prereq, "source_id": sid}
                    )
                    branches["B_PRECEDENCE_OVERTURN"].append(ph)

    # -- stage 10, global reconciliation ---------------------------------------
    final = dict(alloc)
    recon_rows = []
    for sid in order:
        if sid not in eligible_set:
            continue
        mine = [ph for ph in topo if sid in members[ph]]
        total = sum(final[(ph, sid)] for ph in mine)
        supply = canonical[sid]
        if total <= supply:
            continue
        branches["B_GLOBAL_CLAWBACK"].append(sid)
        excess = total - supply
        for ph in reversed(mine):
            if excess <= 0:
                break
            before = final[(ph, sid)]
            if before <= 0:
                continue
            clawed = min(before, excess)
            final[(ph, sid)] = before - clawed
            excess -= clawed
            recon_rows.append(
                {
                    "source_id": sid,
                    "phase": ph,
                    "before": before,
                    "after": final[(ph, sid)],
                    "clawed": clawed,
                }
            )

    final_rows = []
    for ph in phases:
        for sid in members[ph]:
            final_rows.append(
                {"phase": ph, "source_id": sid, "tokens": final[(ph, sid)]}
            )

    source_total = dict((sid, 0) for sid in order)
    for (ph, sid), tokens in final.items():
        source_total[sid] += tokens
    total_tokens = sum(source_total.values())

    # -- summaries --------------------------------------------------------------
    lang_rows = []
    for language in sorted(lang_policy):
        tokens = sum(
            source_total[sid] for sid in order if src[sid]["language"] == language
        )
        lang_rows.append(
            {
                "language": language,
                "tokens": tokens,
                "min_required": rhe(
                    lang_policy[language]["min_share_bp"] * total_tokens, scale
                ),
                "max_allowed": rhe(
                    lang_policy[language]["max_share_bp"] * total_tokens, scale
                ),
            }
        )

    safety_rows = []
    for stratum in sorted(safety_policy):
        tokens = sum(
            source_total[sid] for sid in order if src[sid]["safety_stratum"] == stratum
        )
        safety_rows.append(
            {
                "stratum": stratum,
                "tokens": tokens,
                "floor": safety_policy[stratum]["floor_tokens"],
            }
        )

    source_rows = [
        {
            "source_id": sid,
            "tokens": source_total[sid],
            "canonical_tokens": canonical[sid],
        }
        for sid in order
    ]

    phase_total_rows = [
        {
            "phase": ph,
            "allocated": sum(final[(ph, sid)] for sid in members[ph]),
            "cap": cap[ph],
            "budget": budget[ph],
        }
        for ph in phases
    ]

    # -- stages 11 and 12, example weights and materialization -------------------
    by_source = dict((sid, []) for sid in order)
    for row in examples:
        if row["source_id"] in by_source:
            by_source[row["source_id"]].append(row)
    for sid in order:
        by_source[sid].sort(key=lambda r: r["example_id"])

    rational_rows = []
    fixed_rows = []
    residual_rows = []
    materialized = {}
    for sid in order:
        group = by_source[sid]
        if not group:
            continue
        ids = [r["example_id"] for r in group]
        lengths = dict((r["example_id"], int(r["token_length"])) for r in group)
        group_den = sum(lengths[e] for e in ids)
        total = source_total[sid]
        if group_den == 0:
            branches["B_EMPTY_GROUP"].append(sid)
        else:
            for eid in ids:
                weight = Fraction(total * lengths[eid], group_den)
                rational_rows.append(
                    {
                        "example_id": eid,
                        "source_id": sid,
                        "w_num": weight.numerator,
                        "w_den": weight.denominator,
                    }
                )
        base, share = largest_remainder(total, ids, lengths)
        for eid in ids:
            materialized[eid] = share[eid]
            fixed_rows.append(
                {
                    "example_id": eid,
                    "source_id": sid,
                    "materialized_tokens": share[eid],
                }
            )
            if share[eid] > base[eid]:
                residual_rows.append(
                    {
                        "example_id": eid,
                        "source_id": sid,
                        "base_tokens": base[eid],
                        "residual_unit": 1,
                    }
                )

    # -- stage 13, microbatch construction ----------------------------------------
    max_seq = batch_rules["max_seq_tokens"]
    adjacency_limit = batch_rules["adjacency_limit"]
    batch_rows = []
    batch_number = 0

    def emit(phase, batch, seq_tokens):
        return {
            "batch_id": "BATCH%04d" % batch_number,
            "phase": phase,
            "members": batch,
            "seq_tokens": seq_tokens,
        }

    for ph in phases:
        sequence = []
        for sid in sorted(members[ph]):
            if final[(ph, sid)] <= 0:
                continue
            for row in by_source[sid]:
                eid = row["example_id"]
                if materialized.get(eid, 0) > 0:
                    sequence.append((eid, sid, materialized[eid]))
        batch = []
        batch_sources = []
        seq_tokens = 0
        for eid, sid, tokens in sequence:
            if batch:
                run = 0
                for other in reversed(batch_sources):
                    if other != sid:
                        break
                    run += 1
                if seq_tokens + tokens > max_seq or run + 1 > adjacency_limit:
                    batch_number += 1
                    batch_rows.append(emit(ph, batch, seq_tokens))
                    batch = []
                    batch_sources = []
                    seq_tokens = 0
            batch.append(eid)
            batch_sources.append(sid)
            seq_tokens += tokens
            if tokens > max_seq:
                batch_number += 1
                batch_rows.append(emit(ph, batch, seq_tokens))
                batch = []
                batch_sources = []
                seq_tokens = 0
        if batch:
            batch_number += 1
            batch_rows.append(emit(ph, batch, seq_tokens))

    # -- invariants ------------------------------------------------------------------
    phase_within_cap = all(
        sum(final[(ph, sid)] for sid in members[ph]) <= effective[ph] for ph in phases
    )
    source_within_supply = all(source_total[sid] <= canonical[sid] for sid in eligible)
    materialized_match = True
    for sid in order:
        group = by_source[sid]
        if not group or sum(int(r["token_length"]) for r in group) == 0:
            continue
        if sum(materialized[r["example_id"]] for r in group) != source_total[sid]:
            materialized_match = False
    batches_ok = all(
        row["seq_tokens"] <= max_seq or len(row["members"]) == 1 for row in batch_rows
    )
    invariant_rows = [
        {"invariant": "phase_within_cap", "holds": phase_within_cap},
        {"invariant": "source_within_supply", "holds": source_within_supply},
        {"invariant": "materialized_sums_match", "holds": materialized_match},
        {"invariant": "batches_respect_seq_cap", "holds": batches_ok},
    ]

    # -- quiet branch trace -------------------------------------------------------------
    branches["B_CAP_BIND"] = cap_bound
    branches["B_FLOOR_OVER_CAP"] = floor_over_cap
    branches["B_CARRY_DEBT"] = carry_debt
    exception_rows = []
    for code in BRANCH_ORDER:
        for ref in branches[code]:
            exception_rows.append({"code": code, "ref": ref})

    objective = [
        total_tokens,
        len(recon_rows),
        len(cycle_break_rows),
        len(binding_rows),
        len(batch_rows),
    ]

    # -- emission --------------------------------------------------------------------------
    payload = {
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
        "global_reconciliation_trace.jsonl": recon_rows,
        "final_allocation.jsonl": final_rows,
        "language_allocation_summary.jsonl": lang_rows,
        "safety_allocation_summary.jsonl": safety_rows,
        "source_allocation_summary.jsonl": source_rows,
        "rational_example_weights.jsonl": rational_rows,
        "fixed_point_weights.jsonl": fixed_rows,
        "rounding_residual_ledger.jsonl": residual_rows,
        "microbatch_manifest.jsonl": batch_rows,
        "phase_token_totals.jsonl": phase_total_rows,
        "invariant_report.jsonl": invariant_rows,
        "exception_trace.jsonl": exception_rows,
    }

    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    for name in JSONL_FILES:
        write_jsonl(os.path.join(out_dir, name), payload[name])
    write_json(
        os.path.join(out_dir, "objective_tuple.json"),
        {"objective": objective, "order": list(OBJECTIVE_ORDER)},
    )

    with open(os.path.abspath(__file__), "rb") as handle:
        own_source = handle.read()
    with open(os.path.join(out_dir, "plan.py"), "wb") as handle:
        handle.write(own_source)

    manifest = {}
    for name in JSONL_FILES + ["objective_tuple.json", "plan.py"]:
        manifest[name] = digest(os.path.join(out_dir, name))
    write_json(os.path.join(out_dir, "integrity_manifest.json"), manifest)

    chain = "0" * 64
    for name in sorted(manifest):
        chain = hashlib.sha256((chain + manifest[name]).encode("utf-8")).hexdigest()

    final_state = {
        "chain": chain,
        "total_tokens": total_tokens,
        "global_clawbacks": len(recon_rows),
        "capacity_displacements": len(binding_rows),
        "cycle_edges_removed": len(cycle_break_rows),
        "precedence_overturns": len(overturn_rows),
        "microbatches": len(batch_rows),
        "examples_placed": sum(len(row["members"]) for row in batch_rows),
        "quiet_branches": len(exception_rows),
        "objective": objective,
        "invariants_hold": all(row["holds"] for row in invariant_rows),
    }
    write_json(os.path.join(out_dir, "final_state.json"), final_state)
    return final_state


def main(argv):
    if len(argv) != 3:
        sys.stderr.write("usage: plan.py <DATA_DIR> <OUT_DIR>\n")
        return 2
    compile_plan(argv[1], argv[2])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
