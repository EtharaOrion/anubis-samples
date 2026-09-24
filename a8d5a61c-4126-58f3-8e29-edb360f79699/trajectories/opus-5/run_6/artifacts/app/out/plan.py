#!/usr/bin/env python3
"""TrainMix allocation compiler.

Derives the reproducible training mixture certificate from the manifests in a
data directory and writes the artifact set into an output directory.

    python3 plan.py <DATA_DIR> <OUT_DIR>

All arithmetic is integer or exact rational; no float is produced or compared.
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


def round_half_even(p, d):
    """Quantize p over positive denominator d, half to even."""
    if d <= 0:
        raise ValueError("round_half_even requires a positive denominator")
    q, r = divmod(p, d)
    twice = 2 * r
    if twice > d:
        return q + 1
    if twice == d and q % 2 != 0:
        return q + 1
    return q


def is_tie(p, d):
    return 2 * (p % d) == d


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return [row for row in csv.DictReader(fh)
                if any((v or "").strip() for v in row.values())]


def read_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def dump_lines(path, rows):
    text = "".join(
        json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
        for row in rows)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def dump_object(path, obj):
    text = json.dumps(obj, sort_keys=True, indent=2) + "\n"
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def digest_of(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def transitive_closure(phases, edges):
    """requires[p]: an edge from->to means to requires from."""
    direct = {p: set() for p in phases}
    for e in edges:
        if e["to"] in direct and e["from"] in direct:
            direct[e["to"]].add(e["from"])
    closure = {p: set(direct[p]) for p in phases}
    changed = True
    while changed:
        changed = False
        for p in phases:
            add = set()
            for q in closure[p]:
                add |= closure[q]
            if not add <= closure[p]:
                closure[p] |= add
                changed = True
    return closure


def cycle_nodes_of(phases, edges):
    closure = transitive_closure(phases, edges)
    return sorted(p for p in phases if p in closure[p])


def topological_order(phases, edges):
    """Kahn over surviving edges (from precedes to), ties by phase name."""
    preds = {p: set() for p in phases}
    for e in edges:
        f, t = e["from"], e["to"]
        if f in preds and t in preds and f != t:
            preds[t].add(f)
    remaining = {p: set(preds[p]) for p in phases}
    order = []
    while remaining:
        ready = sorted(p for p, ps in remaining.items() if not ps)
        pick = ready[0] if ready else sorted(remaining)[0]
        order.append(pick)
        del remaining[pick]
        for p in remaining:
            remaining[p].discard(pick)
    return order


def main(argv):
    if len(argv) != 3:
        sys.stderr.write("usage: plan.py <DATA_DIR> <OUT_DIR>\n")
        return 2
    data_dir, out_dir = argv[1], argv[2]
    os.makedirs(out_dir, exist_ok=True)

    def dpath(name):
        return os.path.join(data_dir, name)

    def opath(name):
        return os.path.join(out_dir, name)

    sources_rows = read_csv(dpath("sources.csv"))
    quality_rows = read_csv(dpath("quality.csv"))
    freshness_rows = read_csv(dpath("freshness.csv"))
    example_rows = read_csv(dpath("examples.csv"))
    capacity_rows = read_csv(dpath("capacity.csv"))
    carry_rows = read_csv(dpath("carry_state.csv"))

    unit_rules = read_json(dpath("unit_rules.json"))
    quality_policy = read_json(dpath("quality_policy.json"))
    safety_policy = read_json(dpath("safety_policy.json"))
    language_policy = read_json(dpath("language_policy.json"))
    scoring_weights = read_json(dpath("scoring_weights.json"))
    priority_order = read_json(dpath("priority_order.json"))
    curriculum = read_json(dpath("curriculum.json"))
    batch_rules = read_json(dpath("batch_rules.json"))

    scale = int(unit_rules["scale"])
    score_scale = int(scoring_weights["scale"])
    languages = language_policy["languages"]
    strata = safety_policy["strata"]
    units = unit_rules["units"]
    blocked_domains = set(quality_policy["blocked_domains"])
    min_q_num = int(quality_policy["min_quality"]["num"])
    min_q_den = int(quality_policy["min_quality"]["den"])
    half_life = int(scoring_weights["half_life_days"])
    priority_weights = scoring_weights["priority_weights"]
    displacement_rank = priority_order["stratum_displacement_rank"]

    source_ids = [r["source_id"] for r in sources_rows]
    sources = {r["source_id"]: r for r in sources_rows}
    quality = {r["source_id"]: (int(r["q_num"]), int(r["q_den"]))
               for r in quality_rows}
    freshness = {r["source_id"]: int(r["age_days"]) for r in freshness_rows}

    phase_ids = list(curriculum["phases"])
    for r in capacity_rows:
        if r["phase"] not in phase_ids:
            phase_ids.append(r["phase"])
    budget = {r["phase"]: int(r["budget_tokens"]) for r in capacity_rows}
    cap = {r["phase"]: int(r["cap_tokens"]) for r in capacity_rows}
    init_credit = {r["phase"]: int(r["initial_credit"]) for r in carry_rows}
    init_debt = {r["phase"]: int(r["initial_debt"]) for r in carry_rows}

    exceptions = []

    def quiet(code, ref):
        exceptions.append({"code": code, "ref": ref})

    # ---------------- Stage 1: manifest canonicalization ----------------
    canonical_rows = []
    canonical_tokens = {}
    ties = []
    for sid in source_ids:
        src = sources[sid]
        raw_amount = int(src["raw_amount"])
        raw_unit = src["raw_unit"]
        unit = units[raw_unit]
        num, den = int(unit["num"]), int(unit["den"])
        p = raw_amount * num
        tok = round_half_even(p, den)
        tie = is_tie(p, den)
        canonical_tokens[sid] = tok
        if tie:
            ties.append(sid)
        canonical_rows.append({
            "source_id": sid,
            "raw_amount": raw_amount,
            "raw_unit": raw_unit,
            "canonical_tokens": tok,
            "rounding_tie": tie,
        })
    for sid in ties:
        quiet("B_UNIT_ROUND_TIE", sid)

    # ---------------- Stage 2: eligibility projection ----------------
    eligibility_rows = []
    eligible = []
    for sid in source_ids:
        src = sources[sid]
        q_num, q_den = quality.get(sid, (0, 0))
        if q_den <= 0 or q_num * min_q_den < min_q_num * q_den:
            code = "E_LOW_QUALITY"
        elif src["language"] not in languages:
            code = "E_LANG_EXCLUDED"
        elif src["safety_stratum"] not in strata:
            code = "E_STRATUM_UNKNOWN"
        elif src["domain"] in blocked_domains:
            code = "E_DOMAIN_BLOCKED"
        elif canonical_tokens[sid] <= 0:
            code = "E_EMPTY_TOKENS"
        else:
            code = "E_OK"
        if code == "E_OK":
            eligible.append(sid)
        eligibility_rows.append({
            "source_id": sid,
            "eligible": code == "E_OK",
            "code": code,
        })

    # ---------------- Stage 3: demand scoring ----------------
    demand_rows = []
    demand_score = {}
    for sid in eligible:
        src = sources[sid]
        q_num, q_den = quality[sid]
        quality_bp = round_half_even(q_num * score_scale, q_den)
        fresh_bp = round_half_even(half_life * score_scale,
                                   half_life + freshness[sid])
        priority_factor = int(priority_weights[str(int(src["priority"]))])
        lang_balance_bp = int(languages[src["language"]]["balance_bp"])
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

    # ------------- Stage 4: membership and proportional target -------------
    members = {ph: [] for ph in phase_ids}
    membership_rows = []
    for sid in eligible:
        for ph in DOMAIN_PHASES.get(sources[sid]["domain"], ()):
            if ph in members:
                members[ph].append(sid)
    for ph in phase_ids:
        members[ph].sort()
        for sid in members[ph]:
            membership_rows.append({"phase": ph, "source_id": sid})

    proportional_rows = []
    target = {}
    for ph in phase_ids:
        demand_sum = sum(demand_score[s] for s in members[ph]
                         if demand_score[s] > 0)
        for sid in members[ph]:
            share_num = demand_score[sid] * budget.get(ph, 0)
            share_den = demand_sum
            if demand_score[sid] > 0 and demand_sum > 0:
                tgt = round_half_even(share_num, share_den)
            else:
                tgt = 0
            target[(ph, sid)] = tgt
            proportional_rows.append({
                "phase": ph,
                "source_id": sid,
                "target_tokens": tgt,
                "share_num": share_num,
                "share_den": share_den,
            })

    # ---------------- Stage 5: protected floors ----------------
    floor_rows = []
    floor = {}
    for ph in phase_ids:
        for sid in members[ph]:
            src = sources[sid]
            safety_floor = int(strata[src["safety_stratum"]]["floor_tokens"])
            language_floor = int(languages[src["language"]]["floor_tokens"])
            fl = max(safety_floor, language_floor)
            floor[(ph, sid)] = fl
            floor_rows.append({
                "phase": ph,
                "source_id": sid,
                "safety_floor": safety_floor,
                "language_floor": language_floor,
                "floor": fl,
            })

    # ---------------- Stage 7: precedence closure ----------------
    edges = list(curriculum["edges"])
    closure = transitive_closure(phase_ids, edges)
    closure_rows = []
    for ph in sorted(phase_ids):
        for req in sorted(closure[ph]):
            closure_rows.append({"phase": ph, "requires": req})

    # ---------------- Stage 8a: cycle break ----------------
    surviving = list(edges)
    cycle_rows = []
    cycle_break_rows = []
    while True:
        nodes = cycle_nodes_of(phase_ids, surviving)
        if not nodes:
            break
        node_set = set(nodes)
        candidates = [e for e in surviving
                      if e["from"] in node_set and e["to"] in node_set]
        if not candidates:
            break
        pick = min(candidates, key=lambda e: (
            0 if e["hardness"] == "soft" else 1,
            e["from"], e["to"], e["edge_id"]))
        cycle_rows.append({"cycle_nodes": nodes})
        cycle_break_rows.append({
            "edge_id": pick["edge_id"],
            "from": pick["from"],
            "to": pick["to"],
            "hardness": pick["hardness"],
        })
        surviving = [e for e in surviving if e is not pick]

    topo = topological_order(phase_ids, surviving)

    # -------- Stages 5/6/9: floor bind, capacity binding, carry state ------
    displacement_rows = []
    floor_bind_phases = []
    cap_bind_phases = []
    floor_over_cap_phases = set()
    carry_debt_phases = []
    carry_ledger_rows = []
    alloc = {}
    credit_in = {ph: init_credit.get(ph, 0) for ph in phase_ids}
    debt_in = {ph: init_debt.get(ph, 0) for ph in phase_ids}
    effective_cap = {}

    for idx, ph in enumerate(topo):
        c_in = credit_in[ph]
        d_in = debt_in[ph]
        eff = cap.get(ph, 0) + c_in - d_in
        effective_cap[ph] = eff
        current = {}
        raised = False
        for sid in members[ph]:
            tgt = target[(ph, sid)]
            fl = floor[(ph, sid)]
            current[sid] = max(tgt, fl)
            if fl > tgt:
                raised = True
        if raised:
            floor_bind_phases.append(ph)
        total = sum(current.values())
        floor_cut_total = 0
        if total > eff:
            cap_bind_phases.append(ph)
            excess = total - eff
            order = sorted(members[ph], key=lambda s: (
                displacement_rank[sources[s]["safety_stratum"]],
                -int(sources[s]["priority"]),
                s))
            for sid in order:
                if excess <= 0:
                    break
                take = min(current[sid] - floor[(ph, sid)], excess)
                if take > 0:
                    before = current[sid]
                    current[sid] = before - take
                    excess -= take
                    displacement_rows.append({
                        "phase": ph,
                        "source_id": sid,
                        "before": before,
                        "after": current[sid],
                        "displaced": take,
                        "kind": "above_floor",
                    })
            if excess > 0:
                floor_over_cap_phases.add(ph)
                for sid in order:
                    if excess <= 0:
                        break
                    take = min(current[sid], excess)
                    if take > 0:
                        before = current[sid]
                        current[sid] = before - take
                        excess -= take
                        floor_cut_total += take
                        displacement_rows.append({
                            "phase": ph,
                            "source_id": sid,
                            "before": before,
                            "after": current[sid],
                            "displaced": take,
                            "kind": "floor_cut",
                        })
        allocated = sum(current.values())
        for sid in members[ph]:
            alloc[(ph, sid)] = current[sid]
        credit_out = max(0, eff - allocated)
        debt_out = floor_cut_total
        if debt_out > 0:
            carry_debt_phases.append(ph)
        carry_ledger_rows.append({
            "phase": ph,
            "credit_in": c_in,
            "debt_in": d_in,
            "credit_out": credit_out,
            "debt_out": debt_out,
        })
        if idx + 1 < len(topo):
            nxt = topo[idx + 1]
            credit_in[nxt] += credit_out
            debt_in[nxt] += debt_out

    for ph in floor_bind_phases:
        quiet("B_FLOOR_BIND", ph)
    for ph in cap_bind_phases:
        quiet("B_CAP_BIND", ph)
        if ph in floor_over_cap_phases:
            quiet("B_FLOOR_OVER_CAP", ph)
    for row in cycle_break_rows:
        quiet("B_CYCLE_BREAK", row["edge_id"])

    # ---------------- Stage 8b: precedence overturn ----------------
    overturn_rows = []
    for ph in phase_ids:
        for req in sorted(closure[ph]):
            if req == ph:
                continue
            for sid in members[ph]:
                if sid not in members[req]:
                    continue
                if alloc[(req, sid)] == 0 and alloc[(ph, sid)] > 0:
                    overturn_rows.append({
                        "phase": ph,
                        "prereq_phase": req,
                        "source_id": sid,
                    })
    for row in overturn_rows:
        quiet("B_PRECEDENCE_OVERTURN", row["phase"])

    for ph in carry_debt_phases:
        quiet("B_CARRY_DEBT", ph)

    pre_rows = [{"phase": ph, "source_id": sid, "tokens": alloc[(ph, sid)]}
                for ph in phase_ids for sid in members[ph]]

    # ---------------- Stage 10: global reconciliation ----------------
    reconciliation_rows = []
    clawed_sources = []
    for sid in source_ids:
        total = sum(alloc[(ph, sid)] for ph in phase_ids
                    if sid in members[ph])
        supply = canonical_tokens[sid]
        if total <= supply:
            continue
        excess = total - supply
        for ph in reversed(topo):
            if excess <= 0:
                break
            if sid not in members[ph]:
                continue
            before = alloc[(ph, sid)]
            take = min(before, excess)
            if take > 0:
                alloc[(ph, sid)] = before - take
                excess -= take
                reconciliation_rows.append({
                    "source_id": sid,
                    "phase": ph,
                    "before": before,
                    "after": alloc[(ph, sid)],
                    "clawed": take,
                })
        clawed_sources.append(sid)
    for sid in clawed_sources:
        quiet("B_GLOBAL_CLAWBACK", sid)

    final_rows = [{"phase": ph, "source_id": sid, "tokens": alloc[(ph, sid)]}
                  for ph in phase_ids for sid in members[ph]]

    source_tokens = {
        sid: sum(alloc[(ph, sid)] for ph in phase_ids if sid in members[ph])
        for sid in source_ids}
    total_tokens = sum(source_tokens.values())

    # ---------------- Summaries ----------------
    language_rows = []
    for lang in sorted(languages):
        tokens = sum(source_tokens[s] for s in source_ids
                     if sources[s]["language"] == lang)
        language_rows.append({
            "language": lang,
            "tokens": tokens,
            "min_required": round_half_even(
                int(languages[lang]["min_share_bp"]) * total_tokens, scale),
            "max_allowed": round_half_even(
                int(languages[lang]["max_share_bp"]) * total_tokens, scale),
        })

    safety_rows = []
    for stratum in sorted(strata):
        tokens = sum(source_tokens[s] for s in source_ids
                     if sources[s]["safety_stratum"] == stratum)
        safety_rows.append({
            "stratum": stratum,
            "tokens": tokens,
            "floor": int(strata[stratum]["floor_tokens"]),
        })

    source_summary_rows = [{
        "source_id": sid,
        "tokens": source_tokens[sid],
        "canonical_tokens": canonical_tokens[sid],
    } for sid in source_ids]

    phase_total_rows = [{
        "phase": ph,
        "allocated": sum(alloc[(ph, sid)] for sid in members[ph]),
        "cap": cap.get(ph, 0),
        "budget": budget.get(ph, 0),
    } for ph in phase_ids]

    # ------- Stages 11 and 12: rational weights and materialization -------
    source_order = {sid: i for i, sid in enumerate(source_ids)}
    examples_by_source = {sid: [] for sid in source_ids}
    for row in example_rows:
        sid = row["source_id"]
        if sid not in examples_by_source:
            examples_by_source[sid] = []
            source_order[sid] = len(source_order)
        examples_by_source[sid].append(
            (row["example_id"], int(row["token_length"])))
    ordered_sources = sorted(examples_by_source, key=lambda s: source_order[s])
    for sid in ordered_sources:
        examples_by_source[sid].sort()

    rational_rows = []
    fixed_rows = []
    residual_rows = []
    empty_groups = []
    materialized = {}
    materialized_sums_match = True
    for sid in ordered_sources:
        items = examples_by_source[sid]
        if not items:
            continue
        group_den = sum(length for _, length in items)
        total = source_tokens.get(sid, 0)
        if group_den == 0:
            empty_groups.append(sid)
            for eid, _ in items:
                materialized[eid] = 0
                fixed_rows.append({
                    "example_id": eid,
                    "source_id": sid,
                    "materialized_tokens": 0,
                })
            continue
        for eid, length in items:
            num = total * length
            g = gcd(num, group_den)
            rational_rows.append({
                "example_id": eid,
                "source_id": sid,
                "w_num": num // g,
                "w_den": group_den // g,
            })
        base = {}
        rem = {}
        for eid, length in items:
            base[eid], rem[eid] = divmod(total * length, group_den)
        leftover = total - sum(base.values())
        ranked = sorted((eid for eid, _ in items),
                        key=lambda e: (-rem[e], e))
        got = set(ranked[:leftover])
        for eid, _ in items:
            value = base[eid] + (1 if eid in got else 0)
            materialized[eid] = value
            fixed_rows.append({
                "example_id": eid,
                "source_id": sid,
                "materialized_tokens": value,
            })
            if eid in got:
                residual_rows.append({
                    "example_id": eid,
                    "source_id": sid,
                    "base_tokens": base[eid],
                    "residual_unit": 1,
                })
        if sum(materialized[eid] for eid, _ in items) != total:
            materialized_sums_match = False
    for sid in empty_groups:
        quiet("B_EMPTY_GROUP", sid)

    # ---------------- Stage 13: microbatch construction ----------------
    max_seq = int(batch_rules["max_seq_tokens"])
    adjacency_limit = int(batch_rules["adjacency_limit"])
    batch_rows = []
    batch_no = 0
    for ph in phase_ids:
        queue = []
        for sid in members[ph]:
            if alloc[(ph, sid)] <= 0:
                continue
            for eid, _ in examples_by_source.get(sid, []):
                if materialized.get(eid, 0) > 0:
                    queue.append((eid, sid, materialized[eid]))
        cur = []
        cur_sources = []
        cur_tokens = 0
        for eid, sid, tokens in queue:
            if cur:
                run = 0
                for prev in reversed(cur_sources):
                    if prev != sid:
                        break
                    run += 1
                if cur_tokens + tokens > max_seq or run + 1 > adjacency_limit:
                    batch_no += 1
                    batch_rows.append({
                        "batch_id": "BATCH%04d" % batch_no,
                        "phase": ph,
                        "members": cur,
                        "seq_tokens": cur_tokens,
                    })
                    cur = []
                    cur_sources = []
                    cur_tokens = 0
            cur.append(eid)
            cur_sources.append(sid)
            cur_tokens += tokens
        if cur:
            batch_no += 1
            batch_rows.append({
                "batch_id": "BATCH%04d" % batch_no,
                "phase": ph,
                "members": cur,
                "seq_tokens": cur_tokens,
            })

    # ---------------- Invariants ----------------
    invariant_rows = [
        {"invariant": "phase_within_cap", "holds": all(
            sum(alloc[(ph, sid)] for sid in members[ph]) <= effective_cap[ph]
            for ph in phase_ids)},
        {"invariant": "source_within_supply", "holds": all(
            source_tokens[sid] <= canonical_tokens[sid]
            for sid in source_ids)},
        {"invariant": "materialized_sums_match",
         "holds": materialized_sums_match},
        {"invariant": "batches_respect_seq_cap", "holds": all(
            row["seq_tokens"] <= max_seq or len(row["members"]) == 1
            for row in batch_rows)},
    ]

    objective = [
        total_tokens,
        len(reconciliation_rows),
        len(cycle_break_rows),
        len(displacement_rows),
        len(batch_rows),
    ]
    objective_order = [
        "total_tokens",
        "global_clawbacks",
        "cycle_edges_removed",
        "capacity_displacements",
        "microbatches",
    ]

    # ---------------- Stage 14: emission ----------------
    dump_lines(opath("canonical_sources.jsonl"), canonical_rows)
    dump_lines(opath("eligibility_ledger.jsonl"), eligibility_rows)
    dump_lines(opath("demand_score_ledger.jsonl"), demand_rows)
    dump_lines(opath("phase_membership.jsonl"), membership_rows)
    dump_lines(opath("proportional_allocation.jsonl"), proportional_rows)
    dump_lines(opath("protected_floor_ledger.jsonl"), floor_rows)
    dump_lines(opath("capacity_binding_trace.jsonl"), displacement_rows)
    dump_lines(opath("precedence_closure.jsonl"), closure_rows)
    dump_lines(opath("cycle_report.jsonl"), cycle_rows)
    dump_lines(opath("cycle_break_decisions.jsonl"), cycle_break_rows)
    dump_lines(opath("precedence_overturn_trace.jsonl"), overturn_rows)
    dump_lines(opath("carry_state_ledger.jsonl"), carry_ledger_rows)
    dump_lines(opath("pre_reconciliation_ledger.jsonl"), pre_rows)
    dump_lines(opath("global_reconciliation_trace.jsonl"), reconciliation_rows)
    dump_lines(opath("final_allocation.jsonl"), final_rows)
    dump_lines(opath("language_allocation_summary.jsonl"), language_rows)
    dump_lines(opath("safety_allocation_summary.jsonl"), safety_rows)
    dump_lines(opath("source_allocation_summary.jsonl"), source_summary_rows)
    dump_lines(opath("rational_example_weights.jsonl"), rational_rows)
    dump_lines(opath("fixed_point_weights.jsonl"), fixed_rows)
    dump_lines(opath("rounding_residual_ledger.jsonl"), residual_rows)
    dump_lines(opath("microbatch_manifest.jsonl"), batch_rows)
    dump_lines(opath("phase_token_totals.jsonl"), phase_total_rows)
    dump_lines(opath("invariant_report.jsonl"), invariant_rows)
    dump_lines(opath("exception_trace.jsonl"), exceptions)
    dump_object(opath("objective_tuple.json"),
                {"objective": objective, "order": objective_order})

    program_source = os.path.abspath(__file__)
    program_target = opath("plan.py")
    with open(program_source, "rb") as fh:
        program_bytes = fh.read()
    if os.path.abspath(program_target) != program_source:
        with open(program_target, "wb") as fh:
            fh.write(program_bytes)

    hashed = [
        "canonical_sources.jsonl", "eligibility_ledger.jsonl",
        "demand_score_ledger.jsonl", "phase_membership.jsonl",
        "proportional_allocation.jsonl", "protected_floor_ledger.jsonl",
        "capacity_binding_trace.jsonl", "precedence_closure.jsonl",
        "cycle_report.jsonl", "cycle_break_decisions.jsonl",
        "precedence_overturn_trace.jsonl", "carry_state_ledger.jsonl",
        "pre_reconciliation_ledger.jsonl",
        "global_reconciliation_trace.jsonl", "final_allocation.jsonl",
        "language_allocation_summary.jsonl", "safety_allocation_summary.jsonl",
        "source_allocation_summary.jsonl", "rational_example_weights.jsonl",
        "fixed_point_weights.jsonl", "rounding_residual_ledger.jsonl",
        "microbatch_manifest.jsonl", "phase_token_totals.jsonl",
        "invariant_report.jsonl", "exception_trace.jsonl",
        "objective_tuple.json", "plan.py",
    ]
    manifest = {name: digest_of(opath(name)) for name in hashed}
    dump_object(opath("integrity_manifest.json"), manifest)

    chain = "0" * 64
    for name in sorted(manifest):
        chain = hashlib.sha256(
            (chain + manifest[name]).encode("utf-8")).hexdigest()

    dump_object(opath("final_state.json"), {
        "chain": chain,
        "total_tokens": total_tokens,
        "global_clawbacks": len(reconciliation_rows),
        "capacity_displacements": len(displacement_rows),
        "cycle_edges_removed": len(cycle_break_rows),
        "precedence_overturns": len(overturn_rows),
        "microbatches": len(batch_rows),
        "examples_placed": sum(len(row["members"]) for row in batch_rows),
        "quiet_branches": len(exceptions),
        "objective": objective,
        "invariants_hold": all(row["holds"] for row in invariant_rows),
    })
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
