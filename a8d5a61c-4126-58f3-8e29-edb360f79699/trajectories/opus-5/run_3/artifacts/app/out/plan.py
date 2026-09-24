#!/usr/bin/env python3
"""TrainMix allocation compiler.

Usage: python3 plan.py <DATA_DIR> <OUT_DIR>

Derives the training mixture certificate from the shipped manifests through the
fourteen stage chain described in the specification shipped with the data.
All arithmetic is exact integer / rational arithmetic; no float is produced.
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

def round_half_even(p, d):
    """Quantize p over the denominator d, rounding an exact half to even."""
    if d == 0:
        raise ZeroDivisionError("round_half_even with zero denominator")
    if d < 0:
        p, d = -p, -d
    q, r = divmod(p, d)
    twice = 2 * r
    if twice > d:
        return q + 1
    if twice == d:
        return q if q % 2 == 0 else q + 1
    return q


def is_half(p, d):
    if d == 0:
        return False
    if d < 0:
        p, d = -p, -d
    return 2 * (p % d) == d


def largest_remainder(total, keys, weights):
    """Split total over keys in proportion to weights, ties by ascending key.

    Returns (base, extra): the integer quotient per key and the leftover unit.
    """
    base = {k: 0 for k in keys}
    extra = {k: 0 for k in keys}
    wsum = sum(weights[k] for k in keys)
    if wsum <= 0 or total <= 0 or not keys:
        return base, extra
    rem = {}
    for k in keys:
        q, r = divmod(total * weights[k], wsum)
        base[k] = q
        rem[k] = r
    leftover = total - sum(base.values())
    order = sorted(keys, key=lambda k: (-rem[k], k))
    for k in order[:max(0, leftover)]:
        extra[k] = 1
    return base, extra


# ------------------------------------------------------------------- reading

def read_csv(path):
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return [dict(row) for row in csv.DictReader(fh)]


def read_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


# ------------------------------------------------------------------- writing

def dump_line(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def write_jsonl(out_dir, name, rows):
    text = "".join(dump_line(r) + "\n" for r in rows)
    with open(os.path.join(out_dir, name), "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def write_json(out_dir, name, obj):
    text = json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    with open(os.path.join(out_dir, name), "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def digest_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- compiler

def compile_plan(data_dir, out_dir):
    unit_rules = read_json(os.path.join(data_dir, "unit_rules.json"))
    scoring_weights = read_json(os.path.join(data_dir, "scoring_weights.json"))
    quality_policy = read_json(os.path.join(data_dir, "quality_policy.json"))
    language_policy = read_json(os.path.join(data_dir, "language_policy.json"))["languages"]
    safety_policy = read_json(os.path.join(data_dir, "safety_policy.json"))["strata"]
    priority_order = read_json(os.path.join(data_dir, "priority_order.json"))
    curriculum = read_json(os.path.join(data_dir, "curriculum.json"))
    batch_rules = read_json(os.path.join(data_dir, "batch_rules.json"))

    sources = read_csv(os.path.join(data_dir, "sources.csv"))
    quality = {r["source_id"]: r for r in read_csv(os.path.join(data_dir, "quality.csv"))}
    freshness = {r["source_id"]: r for r in read_csv(os.path.join(data_dir, "freshness.csv"))}
    examples_rows = read_csv(os.path.join(data_dir, "examples.csv"))
    capacity = read_csv(os.path.join(data_dir, "capacity.csv"))
    carry_in_rows = {r["phase"]: r for r in read_csv(os.path.join(data_dir, "carry_state.csv"))}

    scale = int(unit_rules["scale"])
    score_scale = int(scoring_weights["scale"])
    half_life = int(scoring_weights["half_life_days"])
    priority_weights = scoring_weights["priority_weights"]
    blocked_domains = set(quality_policy["blocked_domains"])
    min_q_num = int(quality_policy["min_quality"]["num"])
    min_q_den = int(quality_policy["min_quality"]["den"])

    order = [r["source_id"] for r in sources]
    src = {r["source_id"]: r for r in sources}

    phase_order = list(curriculum["phases"])
    for row in capacity:
        if row["phase"] not in phase_order:
            phase_order.append(row["phase"])
    cap_info = {r["phase"]: (int(r["budget_tokens"]), int(r["cap_tokens"])) for r in capacity}

    ex_stage1 = []
    ex_stage6 = []
    ex_stage8 = []
    ex_stage9 = []
    ex_stage10 = []
    ex_stage11 = []

    # --------------------------------------------- stage 1, canonicalization
    canonical_rows = []
    canonical = {}
    for sid in order:
        row = src[sid]
        raw_amount = int(row["raw_amount"])
        unit = row["raw_unit"]
        spec = unit_rules["units"][unit]
        num, den = int(spec["num"]), int(spec["den"])
        p = raw_amount * num
        tokens = round_half_even(p, den)
        tie = is_half(p, den)
        canonical[sid] = tokens
        canonical_rows.append({
            "source_id": sid,
            "raw_amount": raw_amount,
            "raw_unit": unit,
            "canonical_tokens": tokens,
            "rounding_tie": tie,
        })
        if tie:
            ex_stage1.append({"code": "B_UNIT_ROUND_TIE", "ref": sid})

    # ------------------------------------------------- stage 2, eligibility
    eligibility_rows = []
    eligible = []
    for sid in order:
        row = src[sid]
        q = quality.get(sid)
        q_num = int(q["q_num"]) if q else 0
        q_den = int(q["q_den"]) if q else 0
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
        else:
            code = "E_OK"
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
        quality_bp = round_half_even(int(q["q_num"]) * score_scale, int(q["q_den"]))
        age = int(freshness[sid]["age_days"])
        fresh_bp = round_half_even(half_life * score_scale, half_life + age)
        priority_factor = int(priority_weights[str(int(row["priority"]))])
        balance_bp = int(language_policy[row["language"]]["balance_bp"])
        s1 = round_half_even(canonical[sid] * quality_bp, score_scale)
        s2 = round_half_even(s1 * fresh_bp, score_scale)
        s3 = s2 * priority_factor
        score = round_half_even(s3 * balance_bp, score_scale)
        demand[sid] = score
        demand_rows.append({
            "source_id": sid,
            "quality_bp": quality_bp,
            "fresh_bp": fresh_bp,
            "priority_factor": priority_factor,
            "lang_balance_bp": balance_bp,
            "demand_score": score,
        })

    # ------------------------------------- stage 4, membership and targets
    members = {p: [] for p in phase_order}
    membership_rows = []
    for p in phase_order:
        for sid in eligible:
            if p in DOMAIN_PHASES.get(src[sid]["domain"], ()):
                members[p].append(sid)
                membership_rows.append({"phase": p, "source_id": sid})

    target = {}
    proportional_rows = []
    for p in phase_order:
        budget = cap_info[p][0]
        demand_sum = sum(demand[s] for s in members[p] if demand[s] > 0)
        for sid in members[p]:
            share_num = demand[sid] * budget
            share_den = demand_sum
            if demand[sid] > 0 and demand_sum > 0:
                tokens = round_half_even(share_num, share_den)
            else:
                tokens = 0
            target[(p, sid)] = tokens
            proportional_rows.append({
                "phase": p,
                "source_id": sid,
                "target_tokens": tokens,
                "share_num": share_num,
                "share_den": share_den,
            })

    # --------------------------------------------- stage 5, protected floors
    floor = {}
    floor_rows = []
    for p in phase_order:
        for sid in members[p]:
            row = src[sid]
            safety_floor = int(safety_policy[row["safety_stratum"]]["floor_tokens"])
            language_floor = int(language_policy[row["language"]]["floor_tokens"])
            value = safety_floor if safety_floor >= language_floor else language_floor
            floor[(p, sid)] = value
            floor_rows.append({
                "phase": p,
                "source_id": sid,
                "safety_floor": safety_floor,
                "language_floor": language_floor,
                "floor": value,
            })

    # ----------------------------------------- stage 7, precedence closure
    edges = curriculum["edges"]
    closure = {p: set() for p in phase_order}
    for e in edges:
        if e["to"] in closure:
            closure[e["to"]].add(e["from"])
    changed = True
    while changed:
        changed = False
        for p in phase_order:
            grown = set()
            for r in closure[p]:
                grown |= closure.get(r, set())
            if not grown <= closure[p]:
                closure[p] |= grown
                changed = True
    closure_rows = []
    for p in sorted(closure):
        for r in sorted(closure[p]):
            closure_rows.append({"phase": p, "requires": r})

    # ------------------------------------------------- stage 8, cycle break
    cycle_nodes = sorted(p for p in phase_order if p in closure[p])
    cycle_report_rows = []
    if cycle_nodes:
        cycle_report_rows.append({"cycle_nodes": cycle_nodes})
    cycle_break_rows = []
    removed_edge = None
    if cycle_nodes:
        cyc = set(cycle_nodes)
        candidates = [e for e in edges if e["from"] in cyc and e["to"] in cyc]
        if candidates:
            removed_edge = min(
                candidates,
                key=lambda e: (0 if e["hardness"] == "soft" else 1,
                               e["from"], e["to"], e["edge_id"]),
            )
            cycle_break_rows.append({
                "edge_id": removed_edge["edge_id"],
                "from": removed_edge["from"],
                "to": removed_edge["to"],
                "hardness": removed_edge["hardness"],
            })
            ex_stage8.append({"code": "B_CYCLE_BREAK", "ref": removed_edge["edge_id"]})

    surviving = [e for e in edges if e is not removed_edge]

    indeg = {p: 0 for p in phase_order}
    outs = {p: [] for p in phase_order}
    for e in surviving:
        f, t = e["from"], e["to"]
        if f in indeg and t in indeg:
            outs[f].append(t)
            indeg[t] += 1
    topo = []
    remaining = set(phase_order)
    while remaining:
        ready = sorted(p for p in remaining if indeg[p] == 0)
        p = ready[0] if ready else min(remaining)
        remaining.discard(p)
        topo.append(p)
        for t in outs[p]:
            if t in remaining:
                indeg[t] -= 1

    # --------------------------- stages 6 and 9, binding and carry state
    alloc = {}
    binding_rows = []
    carry_rows = []
    eff_cap = {}
    credit_carry = 0
    debt_carry = 0
    first_phase = True
    for p in topo:
        init = carry_in_rows.get(p)
        credit_in = int(init["initial_credit"]) if init else 0
        debt_in = int(init["initial_debt"]) if init else 0
        if not first_phase:
            credit_in += credit_carry
            debt_in += debt_carry
        first_phase = False
        effective = cap_info[p][1] + credit_in - debt_in
        eff_cap[p] = effective

        current = {}
        floor_bind = False
        for sid in members[p]:
            t = target[(p, sid)]
            f = floor[(p, sid)]
            current[sid] = t if t >= f else f
            if f > t:
                floor_bind = True
        if floor_bind:
            ex_stage6.append({"code": "B_FLOOR_BIND", "ref": p})

        excess = sum(current.values()) - effective
        floor_cut_total = 0
        if excess > 0:
            ex_stage6.append({"code": "B_CAP_BIND", "ref": p})
            seq = sorted(
                members[p],
                key=lambda s: (
                    int(priority_order["stratum_displacement_rank"][src[s]["safety_stratum"]]),
                    -int(src[s]["priority"]),
                    s,
                ),
            )
            for sid in seq:
                if excess <= 0:
                    break
                room = current[sid] - floor[(p, sid)]
                if room <= 0:
                    continue
                take = room if room < excess else excess
                before = current[sid]
                current[sid] = before - take
                excess -= take
                binding_rows.append({
                    "phase": p,
                    "source_id": sid,
                    "before": before,
                    "after": current[sid],
                    "displaced": take,
                    "kind": "above_floor",
                })
            if excess > 0:
                ex_stage6.append({"code": "B_FLOOR_OVER_CAP", "ref": p})
                for sid in seq:
                    if excess <= 0:
                        break
                    room = current[sid]
                    if room <= 0:
                        continue
                    take = room if room < excess else excess
                    before = current[sid]
                    current[sid] = before - take
                    excess -= take
                    floor_cut_total += take
                    binding_rows.append({
                        "phase": p,
                        "source_id": sid,
                        "before": before,
                        "after": current[sid],
                        "displaced": take,
                        "kind": "floor_cut",
                    })

        allocated = 0
        for sid in members[p]:
            alloc[(p, sid)] = current[sid]
            allocated += current[sid]
        credit_out = effective - allocated
        if credit_out < 0:
            credit_out = 0
        debt_out = floor_cut_total
        carry_rows.append({
            "phase": p,
            "credit_in": credit_in,
            "debt_in": debt_in,
            "credit_out": credit_out,
            "debt_out": debt_out,
        })
        if debt_out > 0:
            ex_stage9.append({"code": "B_CARRY_DEBT", "ref": p})
        credit_carry = credit_out
        debt_carry = debt_out

    pre_rows = []
    for p in phase_order:
        for sid in members[p]:
            pre_rows.append({"phase": p, "source_id": sid, "tokens": alloc[(p, sid)]})

    # ------------------------------------------ stage 8, precedence overturn
    overturn_rows = []
    for p in phase_order:
        hit = False
        for r in sorted(closure[p]):
            if r == p or r not in members:
                continue
            for sid in order:
                if sid in members[p] and sid in members[r]:
                    if alloc[(r, sid)] == 0 and alloc[(p, sid)] > 0:
                        overturn_rows.append({
                            "phase": p, "prereq_phase": r, "source_id": sid,
                        })
                        hit = True
        if hit:
            ex_stage8.append({"code": "B_PRECEDENCE_OVERTURN", "ref": p})

    # ------------------------------------------- stage 10, reconciliation
    final = dict(alloc)
    recon_rows = []
    for sid in order:
        held = [p for p in phase_order if sid in members[p]]
        total = sum(final[(p, sid)] for p in held)
        excess = total - canonical[sid]
        if excess <= 0:
            continue
        for p in reversed([q for q in topo if sid in members[q]]):
            if excess <= 0:
                break
            before = final[(p, sid)]
            if before <= 0:
                continue
            take = before if before < excess else excess
            final[(p, sid)] = before - take
            excess -= take
            recon_rows.append({
                "source_id": sid,
                "phase": p,
                "before": before,
                "after": final[(p, sid)],
                "clawed": take,
            })
        ex_stage10.append({"code": "B_GLOBAL_CLAWBACK", "ref": sid})

    final_rows = []
    for p in phase_order:
        for sid in members[p]:
            final_rows.append({"phase": p, "source_id": sid, "tokens": final[(p, sid)]})

    source_tokens = {
        sid: sum(final[(p, sid)] for p in phase_order if sid in members[p])
        for sid in order
    }
    total_tokens = sum(source_tokens.values())

    # ------------------------------------------------------------ summaries
    lang_rows = []
    for lang in sorted(language_policy):
        pol = language_policy[lang]
        lang_rows.append({
            "language": lang,
            "tokens": sum(source_tokens[s] for s in order if src[s]["language"] == lang),
            "min_required": round_half_even(int(pol["min_share_bp"]) * total_tokens, scale),
            "max_allowed": round_half_even(int(pol["max_share_bp"]) * total_tokens, scale),
        })

    safety_rows = []
    for stratum in sorted(safety_policy):
        safety_rows.append({
            "stratum": stratum,
            "tokens": sum(source_tokens[s] for s in order
                          if src[s]["safety_stratum"] == stratum),
            "floor": int(safety_policy[stratum]["floor_tokens"]),
        })

    source_rows = [
        {"source_id": s, "tokens": source_tokens[s], "canonical_tokens": canonical[s]}
        for s in order
    ]

    phase_total_rows = [
        {
            "phase": p,
            "allocated": sum(final[(p, s)] for s in members[p]),
            "cap": cap_info[p][1],
            "budget": cap_info[p][0],
        }
        for p in phase_order
    ]

    # ---------------------------------------- stages 11 and 12, weights
    ex_by_source = {}
    for row in examples_rows:
        ex_by_source.setdefault(row["source_id"], []).append(row)
    for sid in ex_by_source:
        ex_by_source[sid].sort(key=lambda r: r["example_id"])

    rational_rows = []
    fixed_rows = []
    residual_rows = []
    materialized = {}
    for sid in order:
        exs = ex_by_source.get(sid, [])
        if not exs:
            continue
        lengths = {r["example_id"]: int(r["token_length"]) for r in exs}
        keys = [r["example_id"] for r in exs]
        group_den = sum(lengths[k] for k in keys)
        total = source_tokens[sid]
        if group_den == 0:
            ex_stage11.append({"code": "B_EMPTY_GROUP", "ref": sid})
            for k in keys:
                materialized[k] = 0
                fixed_rows.append({
                    "example_id": k, "source_id": sid, "materialized_tokens": 0,
                })
            continue
        for k in keys:
            num = total * lengths[k]
            den = group_den
            g = gcd(abs(num), abs(den)) or 1
            num //= g
            den //= g
            if den < 0:
                num, den = -num, -den
            rational_rows.append({
                "example_id": k, "source_id": sid, "w_num": num, "w_den": den,
            })
        base, extra = largest_remainder(total, keys, lengths)
        for k in keys:
            materialized[k] = base[k] + extra[k]
            fixed_rows.append({
                "example_id": k, "source_id": sid,
                "materialized_tokens": materialized[k],
            })
            if extra[k]:
                residual_rows.append({
                    "example_id": k, "source_id": sid,
                    "base_tokens": base[k], "residual_unit": extra[k],
                })

    # ------------------------------------------- stage 13, microbatches
    max_seq = int(batch_rules["max_seq_tokens"])
    adjacency_limit = int(batch_rules["adjacency_limit"])
    batch_rows = []
    batch_no = 0
    for p in phase_order:
        cur_members = []
        cur_total = 0
        cur_src = None
        cur_run = 0
        for sid in sorted(s for s in members[p] if final[(p, s)] > 0):
            for row in ex_by_source.get(sid, []):
                eid = row["example_id"]
                tok = materialized.get(eid, 0)
                if tok <= 0:
                    continue
                if cur_members:
                    over_seq = cur_total + tok > max_seq
                    over_adj = sid == cur_src and cur_run + 1 > adjacency_limit
                    if over_seq or over_adj:
                        batch_no += 1
                        batch_rows.append({
                            "batch_id": "BATCH%04d" % batch_no,
                            "phase": p,
                            "members": cur_members,
                            "seq_tokens": cur_total,
                        })
                        cur_members = []
                        cur_total = 0
                        cur_src = None
                        cur_run = 0
                if sid == cur_src:
                    cur_run += 1
                else:
                    cur_src = sid
                    cur_run = 1
                cur_members.append(eid)
                cur_total += tok
        if cur_members:
            batch_no += 1
            batch_rows.append({
                "batch_id": "BATCH%04d" % batch_no,
                "phase": p,
                "members": cur_members,
                "seq_tokens": cur_total,
            })

    # ------------------------------------------------------------ invariants
    inv_phase = all(
        sum(final[(p, s)] for s in members[p]) <= eff_cap[p] for p in phase_order
    )
    inv_supply = all(source_tokens[s] <= canonical[s] for s in order)
    inv_mat = True
    for sid in order:
        exs = ex_by_source.get(sid, [])
        if not exs or sum(int(r["token_length"]) for r in exs) == 0:
            continue
        if sum(materialized[r["example_id"]] for r in exs) != source_tokens[sid]:
            inv_mat = False
    inv_batch = all(
        b["seq_tokens"] <= max_seq or len(b["members"]) == 1 for b in batch_rows
    )
    invariant_rows = [
        {"invariant": "phase_within_cap", "holds": inv_phase},
        {"invariant": "source_within_supply", "holds": inv_supply},
        {"invariant": "materialized_sums_match", "holds": inv_mat},
        {"invariant": "batches_respect_seq_cap", "holds": inv_batch},
    ]

    exception_rows = ex_stage1 + ex_stage6 + ex_stage8 + ex_stage9 + ex_stage10 + ex_stage11

    objective = [
        total_tokens,
        len(recon_rows),
        len(cycle_break_rows),
        len(binding_rows),
        len(batch_rows),
    ]

    # -------------------------------------------------------------- emission
    outputs = {
        "canonical_sources.jsonl": canonical_rows,
        "eligibility_ledger.jsonl": eligibility_rows,
        "demand_score_ledger.jsonl": demand_rows,
        "phase_membership.jsonl": membership_rows,
        "proportional_allocation.jsonl": proportional_rows,
        "protected_floor_ledger.jsonl": floor_rows,
        "capacity_binding_trace.jsonl": binding_rows,
        "precedence_closure.jsonl": closure_rows,
        "cycle_report.jsonl": cycle_report_rows,
        "cycle_break_decisions.jsonl": cycle_break_rows,
        "precedence_overturn_trace.jsonl": overturn_rows,
        "carry_state_ledger.jsonl": carry_rows,
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
    for name in JSONL_FILES:
        write_jsonl(out_dir, name, outputs[name])

    write_json(out_dir, "objective_tuple.json",
               {"objective": objective, "order": list(OBJECTIVE_ORDER)})

    self_path = os.path.abspath(__file__)
    target_path = os.path.abspath(os.path.join(out_dir, "plan.py"))
    with open(self_path, "rb") as fh:
        own_source = fh.read()
    if self_path != target_path:
        with open(target_path, "wb") as fh:
            fh.write(own_source)

    manifest = {}
    for name in JSONL_FILES + ["objective_tuple.json", "plan.py"]:
        manifest[name] = digest_of(os.path.join(out_dir, name))
    write_json(out_dir, "integrity_manifest.json", manifest)

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
        "examples_placed": sum(len(b["members"]) for b in batch_rows),
        "quiet_branches": len(exception_rows),
        "objective": objective,
        "invariants_hold": inv_phase and inv_supply and inv_mat and inv_batch,
    }
    write_json(out_dir, "final_state.json", final_state)
    return final_state


def main(argv):
    if len(argv) != 3:
        sys.stderr.write("usage: plan.py <DATA_DIR> <OUT_DIR>\n")
        return 2
    out_dir = argv[2]
    os.makedirs(out_dir, exist_ok=True)
    compile_plan(argv[1], out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
