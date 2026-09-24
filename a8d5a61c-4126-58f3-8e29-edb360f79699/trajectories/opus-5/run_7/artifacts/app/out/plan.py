#!/usr/bin/env python3
"""TrainMix allocation compiler.

Usage: python3 plan.py <DATA_DIR> <OUT_DIR>

Derives the training mixture certificate from the shipped manifests using the
fourteen stage chain in the data directory specification. All arithmetic is
integer or exact rational; no floating point value is produced or serialized.
"""

import csv
import hashlib
import json
import os
import sys
from fractions import Fraction

DOMAIN_PHASES = {
    "maint": ["PH1"],
    "manual": ["PH1", "PH2"],
    "incident": ["PH2"],
    "qa": ["PH2", "PH3"],
    "translated": ["PH1", "PH3"],
    "synthetic": ["PH3"],
    "reg": ["PH2"],
}

INVARIANT_NAMES = [
    "phase_within_cap",
    "source_within_supply",
    "materialized_sums_match",
    "batches_respect_seq_cap",
]

OBJECTIVE_ORDER = [
    "total_tokens",
    "global_clawbacks",
    "cycle_edges_removed",
    "capacity_displacements",
    "microbatches",
]


def round_half_even(p, d):
    """Quantize p over the positive denominator d, half to even."""
    if d == 0:
        raise ZeroDivisionError("round_half_even called with a zero denominator")
    if d < 0:
        p, d = -p, -d
    q, r = divmod(p, d)
    twice = 2 * r
    if twice > d:
        q += 1
    elif twice == d and q % 2 != 0:
        q += 1
    return q


def is_tie(p, d):
    if d == 0:
        return False
    if d < 0:
        p, d = -p, -d
    return 2 * (p % d) == d


def read_csv(path):
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def read_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def largest_remainder(total, keys, weights):
    """Split total across keys in proportion to weights, ties by ascending key.

    Returns (base_by_key, extra_by_key).
    """
    base = {k: 0 for k in keys}
    extra = {k: 0 for k in keys}
    wsum = sum(weights[k] for k in keys)
    if wsum <= 0 or total <= 0:
        return base, extra
    rema = {}
    for k in keys:
        q, r = divmod(total * weights[k], wsum)
        base[k] = q
        rema[k] = r
    leftover = total - sum(base.values())
    order = sorted(keys, key=lambda k: (-rema[k], k))
    for i in range(leftover):
        extra[order[i % len(order)]] += 1
    return base, extra


def dump_lines(path, rows):
    text = "".join(
        json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows
    )
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def dump_object(path, obj):
    text = json.dumps(obj, sort_keys=True, indent=2) + "\n"
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        h.update(fh.read())
    return h.hexdigest()


def compile_plan(data_dir, out_dir):
    unit_rules = read_json(os.path.join(data_dir, "unit_rules.json"))
    scoring_weights = read_json(os.path.join(data_dir, "scoring_weights.json"))
    language_policy = read_json(os.path.join(data_dir, "language_policy.json"))["languages"]
    safety_policy = read_json(os.path.join(data_dir, "safety_policy.json"))["strata"]
    quality_policy = read_json(os.path.join(data_dir, "quality_policy.json"))
    priority_order = read_json(os.path.join(data_dir, "priority_order.json"))
    curriculum = read_json(os.path.join(data_dir, "curriculum.json"))
    batch_rules = read_json(os.path.join(data_dir, "batch_rules.json"))

    sources = read_csv(os.path.join(data_dir, "sources.csv"))
    quality = {r["source_id"]: r for r in read_csv(os.path.join(data_dir, "quality.csv"))}
    freshness = {r["source_id"]: r for r in read_csv(os.path.join(data_dir, "freshness.csv"))}
    examples = read_csv(os.path.join(data_dir, "examples.csv"))
    capacity = read_csv(os.path.join(data_dir, "capacity.csv"))
    carry_state = {r["phase"]: r for r in read_csv(os.path.join(data_dir, "carry_state.csv"))}

    scale = int(unit_rules["scale"])
    score_scale = int(scoring_weights["scale"])
    half_life = int(scoring_weights["half_life_days"])
    priority_weights = scoring_weights["priority_weights"]
    blocked_domains = set(quality_policy["blocked_domains"])
    min_q_num = int(quality_policy["min_quality"]["num"])
    min_q_den = int(quality_policy["min_quality"]["den"])
    stratum_rank = priority_order["stratum_displacement_rank"]
    max_seq_tokens = int(batch_rules["max_seq_tokens"])
    adjacency_limit = int(batch_rules["adjacency_limit"])

    source_ids = [r["source_id"] for r in sources]
    src = {r["source_id"]: r for r in sources}

    phase_order = [r["phase"] for r in capacity]
    for ph in curriculum.get("phases", []):
        if ph not in phase_order:
            phase_order.append(ph)
    phase_index = {p: i for i, p in enumerate(phase_order)}
    budget = {r["phase"]: int(r["budget_tokens"]) for r in capacity}
    cap_tokens = {r["phase"]: int(r["cap_tokens"]) for r in capacity}

    exceptions = []

    def quiet(code, ref):
        exceptions.append({"code": code, "ref": ref})

    # ---- Stage 1, manifest canonicalization -----------------------------
    canonical_rows = []
    canonical_tokens = {}
    stage1_ties = []
    for sid in source_ids:
        row = src[sid]
        raw_frac = Fraction(row["raw_amount"])
        unit = row["raw_unit"]
        spec = unit_rules["units"][unit]
        p = raw_frac.numerator * int(spec["num"])
        d = int(spec["den"]) * raw_frac.denominator
        tokens = round_half_even(p, d)
        tie = is_tie(p, d)
        canonical_tokens[sid] = tokens
        canonical_rows.append({
            "source_id": sid,
            "raw_amount": raw_frac.numerator if raw_frac.denominator == 1 else row["raw_amount"],
            "raw_unit": unit,
            "canonical_tokens": tokens,
            "rounding_tie": tie,
        })
        if tie:
            stage1_ties.append(sid)
    for sid in stage1_ties:
        quiet("B_UNIT_ROUND_TIE", sid)

    # ---- Stage 2, eligibility projection --------------------------------
    eligibility_rows = []
    eligible = []
    for sid in source_ids:
        row = src[sid]
        qrow = quality.get(sid)
        q_num = int(qrow["q_num"]) if qrow else 0
        q_den = int(qrow["q_den"]) if qrow else 0
        if q_den == 0 or q_num * min_q_den < min_q_num * q_den:
            code = "E_LOW_QUALITY"
        elif row["language"] not in language_policy:
            code = "E_LANG_EXCLUDED"
        elif row["safety_stratum"] not in safety_policy:
            code = "E_STRATUM_UNKNOWN"
        elif row["domain"] in blocked_domains:
            code = "E_DOMAIN_BLOCKED"
        elif not canonical_tokens[sid] > 0:
            code = "E_EMPTY_TOKENS"
        else:
            code = "E_OK"
        ok = code == "E_OK"
        if ok:
            eligible.append(sid)
        eligibility_rows.append({"source_id": sid, "eligible": ok, "code": code})

    # ---- Stage 3, demand scoring ----------------------------------------
    demand_rows = []
    demand_score = {}
    for sid in eligible:
        row = src[sid]
        qrow = quality[sid]
        quality_bp = round_half_even(int(qrow["q_num"]) * score_scale, int(qrow["q_den"]))
        age_days = int(freshness[sid]["age_days"])
        fresh_bp = round_half_even(half_life * score_scale, half_life + age_days)
        priority_factor = int(priority_weights[str(int(row["priority"]))])
        lang_balance_bp = int(language_policy[row["language"]]["balance_bp"])
        s1 = round_half_even(canonical_tokens[sid] * quality_bp, score_scale)
        s2 = round_half_even(s1 * fresh_bp, score_scale)
        s3 = s2 * priority_factor
        score = round_half_even(s3 * lang_balance_bp, score_scale)
        demand_score[sid] = score
        demand_rows.append({
            "source_id": sid,
            "quality_bp": quality_bp,
            "fresh_bp": fresh_bp,
            "priority_factor": priority_factor,
            "lang_balance_bp": lang_balance_bp,
            "demand_score": score,
        })

    # ---- Stage 4, phase membership and proportional allocation ----------
    members = {p: [] for p in phase_order}
    membership_rows = []
    source_phases = {sid: [] for sid in source_ids}
    for sid in eligible:
        for ph in DOMAIN_PHASES.get(src[sid]["domain"], []):
            if ph in members:
                members[ph].append(sid)
                source_phases[sid].append(ph)
    for ph in phase_order:
        members[ph].sort()
        for sid in members[ph]:
            membership_rows.append({"phase": ph, "source_id": sid})
    for sid in source_ids:
        source_phases[sid].sort(key=lambda p: phase_index[p])

    proportional_rows = []
    target = {}
    for ph in phase_order:
        demand_sum = sum(demand_score[s] for s in members[ph] if demand_score[s] > 0)
        for sid in members[ph]:
            score = demand_score[sid]
            share_num = score * budget.get(ph, 0)
            if score > 0 and demand_sum > 0:
                tgt = round_half_even(share_num, demand_sum)
            else:
                tgt = 0
            target[(ph, sid)] = tgt
            proportional_rows.append({
                "phase": ph,
                "source_id": sid,
                "target_tokens": tgt,
                "share_num": share_num,
                "share_den": demand_sum,
            })

    # ---- Stage 5, protected floors --------------------------------------
    floor_rows = []
    floor = {}
    floored = {}
    for ph in phase_order:
        bind = False
        for sid in members[ph]:
            row = src[sid]
            safety_floor = int(safety_policy[row["safety_stratum"]]["floor_tokens"])
            language_floor = int(language_policy[row["language"]]["floor_tokens"])
            f = max(safety_floor, language_floor)
            floor[(ph, sid)] = f
            floored[(ph, sid)] = max(target[(ph, sid)], f)
            if f > target[(ph, sid)]:
                bind = True
            floor_rows.append({
                "phase": ph,
                "source_id": sid,
                "safety_floor": safety_floor,
                "language_floor": language_floor,
                "floor": f,
            })
        if bind:
            quiet("B_FLOOR_BIND", ph)

    # ---- Stage 7, curriculum precedence closure -------------------------
    edges = curriculum["edges"]
    nodes = list(phase_order)
    for e in edges:
        for endpoint in (e["from"], e["to"]):
            if endpoint not in nodes:
                nodes.append(endpoint)
    reach = {n: set() for n in nodes}
    for e in edges:
        reach[e["from"]].add(e["to"])
    changed = True
    while changed:
        changed = False
        for a in nodes:
            add = set()
            for b in reach[a]:
                add |= reach[b]
            if not add <= reach[a]:
                reach[a] |= add
                changed = True
    requires = {n: sorted(a for a in nodes if n in reach[a]) for n in nodes}
    closure_rows = []
    for ph in sorted(nodes):
        for req in requires[ph]:
            closure_rows.append({"phase": ph, "requires": req})

    # ---- Stage 8a, cycle report and cycle break -------------------------
    cycle_nodes = sorted(n for n in nodes if n in reach[n])
    cycle_rows = [{"cycle_nodes": cycle_nodes}] if cycle_nodes else []
    cycle_set = set(cycle_nodes)
    cycle_break_rows = []
    removed_edge_id = None
    candidates = [e for e in edges if e["from"] in cycle_set and e["to"] in cycle_set]
    if candidates:
        chosen = min(
            candidates,
            key=lambda e: (
                0 if e["hardness"] == "soft" else 1,
                e["from"],
                e["to"],
                e["edge_id"],
            ),
        )
        removed_edge_id = chosen["edge_id"]
        cycle_break_rows.append({
            "edge_id": chosen["edge_id"],
            "from": chosen["from"],
            "to": chosen["to"],
            "hardness": chosen["hardness"],
        })
    surviving = [e for e in edges if e["edge_id"] != removed_edge_id]

    indeg = {n: 0 for n in nodes}
    succ = {n: [] for n in nodes}
    for e in surviving:
        succ[e["from"]].append(e["to"])
        indeg[e["to"]] += 1
    remaining = set(nodes)
    topo = []
    while remaining:
        ready = sorted(n for n in remaining if indeg[n] == 0)
        node = ready[0] if ready else sorted(remaining)[0]
        remaining.discard(node)
        topo.append(node)
        for m in succ[node]:
            if m in remaining:
                indeg[m] -= 1
    topo_index = {p: i for i, p in enumerate(topo)}

    # ---- Stage 6 binding and stage 9 carry, walked in topological order --
    alloc = {}
    effective_cap = {}
    binding_rows = []
    cap_bind_quiet = []
    carry_rows = []
    carry_debt_quiet = []
    pending_credit = 0
    pending_debt = 0
    for ph in topo:
        if ph not in budget:
            continue
        crow = carry_state.get(ph, {"initial_credit": "0", "initial_debt": "0"})
        credit_in = int(crow["initial_credit"]) + pending_credit
        debt_in = int(crow["initial_debt"]) + pending_debt
        ec = cap_tokens[ph] + credit_in - debt_in
        effective_cap[ph] = ec
        current = {sid: floored[(ph, sid)] for sid in members[ph]}
        excess = sum(current.values()) - ec
        if excess > 0:
            cap_bind_quiet.append(("B_CAP_BIND", ph))
            order = sorted(
                members[ph],
                key=lambda s: (
                    stratum_rank[src[s]["safety_stratum"]],
                    -int(src[s]["priority"]),
                    s,
                ),
            )
            for sid in order:
                if excess <= 0:
                    break
                room = current[sid] - floor[(ph, sid)]
                if room <= 0:
                    continue
                cut = min(room, excess)
                before = current[sid]
                current[sid] = before - cut
                excess -= cut
                binding_rows.append({
                    "phase": ph,
                    "source_id": sid,
                    "before": before,
                    "after": current[sid],
                    "displaced": cut,
                    "kind": "above_floor",
                })
            if excess > 0:
                cap_bind_quiet.append(("B_FLOOR_OVER_CAP", ph))
                for sid in order:
                    if excess <= 0:
                        break
                    if current[sid] <= 0:
                        continue
                    cut = min(current[sid], excess)
                    before = current[sid]
                    current[sid] = before - cut
                    excess -= cut
                    binding_rows.append({
                        "phase": ph,
                        "source_id": sid,
                        "before": before,
                        "after": current[sid],
                        "displaced": cut,
                        "kind": "floor_cut",
                    })
        for sid in members[ph]:
            alloc[(ph, sid)] = current[sid]
        allocated = sum(current.values())
        credit_out = max(0, ec - allocated)
        debt_out = sum(max(0, floor[(ph, s)] - current[s]) for s in members[ph])
        carry_rows.append({
            "phase": ph,
            "credit_in": credit_in,
            "debt_in": debt_in,
            "credit_out": credit_out,
            "debt_out": debt_out,
        })
        if debt_out > 0:
            carry_debt_quiet.append(ph)
        pending_credit = credit_out
        pending_debt = debt_out

    for code, ph in cap_bind_quiet:
        quiet(code, ph)

    # ---- Stage 8b, precedence overturn ----------------------------------
    overturn_rows = []
    overturn_phases = []
    member_set = {p: set(members[p]) for p in phase_order}
    for ph in phase_order:
        hit = False
        for sid in members[ph]:
            if alloc.get((ph, sid), 0) <= 0:
                continue
            for req in requires.get(ph, []):
                if req == ph or req not in member_set:
                    continue
                if sid in member_set[req] and alloc.get((req, sid), 0) == 0:
                    overturn_rows.append({
                        "phase": ph,
                        "prereq_phase": req,
                        "source_id": sid,
                    })
                    hit = True
        if hit:
            overturn_phases.append(ph)
    overturn_rows.sort(
        key=lambda r: (phase_index[r["phase"]], phase_index[r["prereq_phase"]], r["source_id"])
    )

    if removed_edge_id is not None:
        quiet("B_CYCLE_BREAK", removed_edge_id)
    for ph in overturn_phases:
        quiet("B_PRECEDENCE_OVERTURN", ph)
    for ph in carry_debt_quiet:
        quiet("B_CARRY_DEBT", ph)

    # ---- Stage 10, global reconciliation --------------------------------
    pre_rows = []
    for ph in phase_order:
        for sid in members[ph]:
            pre_rows.append({"phase": ph, "source_id": sid, "tokens": alloc[(ph, sid)]})

    final = dict(alloc)
    reconciliation_rows = []
    clawed_sources = []
    for sid in source_ids:
        phases = source_phases[sid]
        if not phases:
            continue
        excess = sum(final[(p, sid)] for p in phases) - canonical_tokens[sid]
        if excess <= 0:
            continue
        for ph in sorted(phases, key=lambda p: -topo_index[p]):
            if excess <= 0:
                break
            amount = final[(ph, sid)]
            if amount <= 0:
                continue
            cut = min(amount, excess)
            final[(ph, sid)] = amount - cut
            excess -= cut
            reconciliation_rows.append({
                "source_id": sid,
                "phase": ph,
                "before": amount,
                "after": amount - cut,
                "clawed": cut,
            })
        clawed_sources.append(sid)
    for sid in clawed_sources:
        quiet("B_GLOBAL_CLAWBACK", sid)

    final_rows = []
    for ph in phase_order:
        for sid in members[ph]:
            final_rows.append({"phase": ph, "source_id": sid, "tokens": final[(ph, sid)]})

    source_total = {sid: sum(final[(p, sid)] for p in source_phases[sid]) for sid in source_ids}
    total_tokens = sum(source_total.values())

    # ---- Summaries -------------------------------------------------------
    language_rows = []
    for lang in sorted(language_policy):
        pol = language_policy[lang]
        language_rows.append({
            "language": lang,
            "tokens": sum(source_total[s] for s in source_ids if src[s]["language"] == lang),
            "min_required": round_half_even(int(pol["min_share_bp"]) * total_tokens, scale),
            "max_allowed": round_half_even(int(pol["max_share_bp"]) * total_tokens, scale),
        })

    safety_rows = []
    for stratum in sorted(safety_policy):
        safety_rows.append({
            "stratum": stratum,
            "tokens": sum(
                source_total[s] for s in source_ids if src[s]["safety_stratum"] == stratum
            ),
            "floor": int(safety_policy[stratum]["floor_tokens"]),
        })

    source_rows = [
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
            "allocated": sum(final[(ph, s)] for s in members[ph]),
            "cap": cap_tokens.get(ph, 0),
            "budget": budget.get(ph, 0),
        }
        for ph in phase_order
    ]

    # ---- Stage 11, rational example weights -----------------------------
    examples_by_source = {sid: [] for sid in source_ids}
    for row in examples:
        examples_by_source.setdefault(row["source_id"], []).append(row)
    for sid in examples_by_source:
        examples_by_source[sid].sort(key=lambda r: r["example_id"])

    ordered_source_ids = list(source_ids)
    for sid in sorted(examples_by_source):
        if sid not in src:
            ordered_source_ids.append(sid)

    rational_rows = []
    empty_groups = []
    for sid in ordered_source_ids:
        rows = examples_by_source.get(sid, [])
        if not rows:
            continue
        group_den = sum(int(r["token_length"]) for r in rows)
        if group_den == 0:
            empty_groups.append(sid)
            continue
        total = source_total.get(sid, 0)
        for r in rows:
            frac = Fraction(total * int(r["token_length"]), group_den)
            rational_rows.append({
                "example_id": r["example_id"],
                "source_id": sid,
                "w_num": frac.numerator,
                "w_den": frac.denominator,
            })
    for sid in empty_groups:
        quiet("B_EMPTY_GROUP", sid)

    # ---- Stage 12, integer weight materialization -----------------------
    materialized = {}
    fixed_rows = []
    residual_rows = []
    for sid in ordered_source_ids:
        rows = examples_by_source.get(sid, [])
        if not rows:
            continue
        keys = [r["example_id"] for r in rows]
        weights = {r["example_id"]: int(r["token_length"]) for r in rows}
        base, extra = largest_remainder(source_total.get(sid, 0), keys, weights)
        for eid in keys:
            materialized[eid] = base[eid] + extra[eid]
            fixed_rows.append({
                "example_id": eid,
                "source_id": sid,
                "materialized_tokens": materialized[eid],
            })
            if extra[eid] > 0:
                residual_rows.append({
                    "example_id": eid,
                    "source_id": sid,
                    "base_tokens": base[eid],
                    "residual_unit": extra[eid],
                })

    # ---- Stage 13, microbatch construction ------------------------------
    batch_rows = []
    batch_no = 0
    for ph in phase_order:
        items = []
        for sid in members[ph]:
            if final[(ph, sid)] <= 0:
                continue
            for r in examples_by_source.get(sid, []):
                eid = r["example_id"]
                if materialized.get(eid, 0) > 0:
                    items.append((eid, sid, materialized[eid]))
        cur = []
        cur_tokens = 0
        run_source = None
        run_len = 0
        for eid, sid, tok in items:
            if cur and (
                cur_tokens + tok > max_seq_tokens
                or (sid == run_source and run_len + 1 > adjacency_limit)
            ):
                batch_no += 1
                batch_rows.append({
                    "batch_id": "BATCH%04d" % batch_no,
                    "phase": ph,
                    "members": [m[0] for m in cur],
                    "seq_tokens": cur_tokens,
                })
                cur = []
                cur_tokens = 0
                run_source = None
                run_len = 0
            cur.append((eid, sid, tok))
            cur_tokens += tok
            if sid == run_source:
                run_len += 1
            else:
                run_source = sid
                run_len = 1
        if cur:
            batch_no += 1
            batch_rows.append({
                "batch_id": "BATCH%04d" % batch_no,
                "phase": ph,
                "members": [m[0] for m in cur],
                "seq_tokens": cur_tokens,
            })

    # ---- Invariants ------------------------------------------------------
    phase_within_cap = all(
        sum(final[(ph, s)] for s in members[ph]) <= effective_cap[ph]
        for ph in phase_order
        if ph in effective_cap
    )
    source_within_supply = all(source_total[s] <= canonical_tokens[s] for s in source_ids)
    materialized_sums_match = True
    for sid in ordered_source_ids:
        rows = examples_by_source.get(sid, [])
        if not rows or sum(int(r["token_length"]) for r in rows) == 0:
            continue
        if sum(materialized[r["example_id"]] for r in rows) != source_total.get(sid, 0):
            materialized_sums_match = False
    batches_respect_seq_cap = all(
        b["seq_tokens"] <= max_seq_tokens or len(b["members"]) == 1 for b in batch_rows
    )
    invariant_values = [
        phase_within_cap,
        source_within_supply,
        materialized_sums_match,
        batches_respect_seq_cap,
    ]
    invariant_rows = [
        {"invariant": name, "holds": value}
        for name, value in zip(INVARIANT_NAMES, invariant_values)
    ]

    objective = [
        total_tokens,
        len(reconciliation_rows),
        len(cycle_break_rows),
        len(binding_rows),
        len(batch_rows),
    ]

    # ---- Emission --------------------------------------------------------
    os.makedirs(out_dir, exist_ok=True)
    files = [
        ("canonical_sources.jsonl", canonical_rows),
        ("eligibility_ledger.jsonl", eligibility_rows),
        ("demand_score_ledger.jsonl", demand_rows),
        ("phase_membership.jsonl", membership_rows),
        ("proportional_allocation.jsonl", proportional_rows),
        ("protected_floor_ledger.jsonl", floor_rows),
        ("capacity_binding_trace.jsonl", binding_rows),
        ("precedence_closure.jsonl", closure_rows),
        ("cycle_report.jsonl", cycle_rows),
        ("cycle_break_decisions.jsonl", cycle_break_rows),
        ("precedence_overturn_trace.jsonl", overturn_rows),
        ("carry_state_ledger.jsonl", carry_rows),
        ("pre_reconciliation_ledger.jsonl", pre_rows),
        ("global_reconciliation_trace.jsonl", reconciliation_rows),
        ("final_allocation.jsonl", final_rows),
        ("language_allocation_summary.jsonl", language_rows),
        ("safety_allocation_summary.jsonl", safety_rows),
        ("source_allocation_summary.jsonl", source_rows),
        ("rational_example_weights.jsonl", rational_rows),
        ("fixed_point_weights.jsonl", fixed_rows),
        ("rounding_residual_ledger.jsonl", residual_rows),
        ("microbatch_manifest.jsonl", batch_rows),
        ("phase_token_totals.jsonl", phase_total_rows),
        ("invariant_report.jsonl", invariant_rows),
        ("exception_trace.jsonl", exceptions),
    ]
    for name, rows in files:
        dump_lines(os.path.join(out_dir, name), rows)

    dump_object(
        os.path.join(out_dir, "objective_tuple.json"),
        {"objective": objective, "order": list(OBJECTIVE_ORDER)},
    )

    source_path = os.path.abspath(__file__)
    with open(source_path, "rb") as fh:
        own_source = fh.read()
    program_path = os.path.join(out_dir, "plan.py")
    if os.path.abspath(program_path) != source_path:
        with open(program_path, "wb") as fh:
            fh.write(own_source)

    hashed = [name for name, _ in files] + ["objective_tuple.json", "plan.py"]
    manifest = {name: sha256_file(os.path.join(out_dir, name)) for name in hashed}
    dump_object(os.path.join(out_dir, "integrity_manifest.json"), manifest)

    chain = "0" * 64
    for name in sorted(manifest):
        chain = hashlib.sha256((chain + manifest[name]).encode("utf-8")).hexdigest()

    dump_object(os.path.join(out_dir, "final_state.json"), {
        "chain": chain,
        "total_tokens": total_tokens,
        "global_clawbacks": len(reconciliation_rows),
        "capacity_displacements": len(binding_rows),
        "cycle_edges_removed": len(cycle_break_rows),
        "precedence_overturns": len(overturn_rows),
        "microbatches": len(batch_rows),
        "examples_placed": sum(len(b["members"]) for b in batch_rows),
        "quiet_branches": len(exceptions),
        "objective": objective,
        "invariants_hold": all(invariant_values),
    })


def main(argv):
    if len(argv) != 3:
        sys.stderr.write("usage: plan.py <DATA_DIR> <OUT_DIR>\n")
        return 2
    compile_plan(argv[1], argv[2])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
