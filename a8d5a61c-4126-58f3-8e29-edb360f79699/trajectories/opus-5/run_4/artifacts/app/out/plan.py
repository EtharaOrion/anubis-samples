#!/usr/bin/env python3
"""TrainMix allocation compiler.

Derives the deterministic training mixture certificate described by the
specification documents in the data directory.  All arithmetic is integer or
exact rational; no floating point value is produced, compared or serialized.

Usage: python3 plan.py <DATA_DIR> <OUT_DIR>
"""

import csv
import hashlib
import json
import os
import sys
from math import gcd

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

INVARIANT_NAMES = [
    "phase_within_cap",
    "source_within_supply",
    "materialized_sums_match",
    "batches_respect_seq_cap",
]


# --------------------------------------------------------------------------
# arithmetic
# --------------------------------------------------------------------------

def rhe(p, d):
    """round_half_even(p, d) over a positive denominator d."""
    if d <= 0:
        raise ValueError("quantization denominator must be positive")
    q, r = divmod(p, d)
    twice = 2 * r
    if twice > d:
        q += 1
    elif twice == d and q % 2 != 0:
        q += 1
    return q


def is_half(p, d):
    return d > 0 and 2 * (p % d) == d


def largest_remainder(total, weights, keys):
    """Split total across keys proportionally, leftover by remainder then key."""
    base = {k: 0 for k in keys}
    extra = {}
    if total == 0:
        return base, extra
    wsum = sum(weights[k] for k in keys)
    if wsum <= 0:
        return base, extra
    rems = {}
    for k in keys:
        q, r = divmod(total * weights[k], wsum)
        base[k] = q
        rems[k] = r
    leftover = total - sum(base.values())
    if leftover > 0:
        order = sorted(keys, key=lambda k: (-rems[k], k))
        for k in order[:leftover]:
            extra[k] = 1
    return base, extra


# --------------------------------------------------------------------------
# io helpers
# --------------------------------------------------------------------------

def read_csv(path):
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return [dict(row) for row in csv.DictReader(fh)]


def read_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True, separators=(",", ":")))
            fh.write("\n")


def write_json(path, obj):
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(obj, sort_keys=True, indent=2))
        fh.write("\n")


def digest_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------
# compiler
# --------------------------------------------------------------------------

def compile_plan(data_dir, out_dir):
    sources = read_csv(os.path.join(data_dir, "sources.csv"))
    quality_rows = read_csv(os.path.join(data_dir, "quality.csv"))
    freshness_rows = read_csv(os.path.join(data_dir, "freshness.csv"))
    example_rows = read_csv(os.path.join(data_dir, "examples.csv"))
    capacity_rows = read_csv(os.path.join(data_dir, "capacity.csv"))
    carry_rows = read_csv(os.path.join(data_dir, "carry_state.csv"))
    batch_rules = read_json(os.path.join(data_dir, "batch_rules.json"))
    curriculum = read_json(os.path.join(data_dir, "curriculum.json"))
    lang_policy = read_json(os.path.join(data_dir, "language_policy.json"))["languages"]
    priority_order = read_json(os.path.join(data_dir, "priority_order.json"))
    quality_policy = read_json(os.path.join(data_dir, "quality_policy.json"))
    safety_policy = read_json(os.path.join(data_dir, "safety_policy.json"))
    scoring_weights = read_json(os.path.join(data_dir, "scoring_weights.json"))
    unit_rules = read_json(os.path.join(data_dir, "unit_rules.json"))

    scale = unit_rules["scale"]
    score_scale = scoring_weights["scale"]
    half_life = scoring_weights["half_life_days"]
    priority_weights = scoring_weights["priority_weights"]
    strata = safety_policy["strata"]
    blocked = set(quality_policy["blocked_domains"])
    min_q_num = quality_policy["min_quality"]["num"]
    min_q_den = quality_policy["min_quality"]["den"]
    strat_rank = priority_order["stratum_displacement_rank"]
    max_seq = batch_rules["max_seq_tokens"]
    adjacency_limit = batch_rules["adjacency_limit"]

    quality = {r["source_id"]: (int(r["q_num"]), int(r["q_den"])) for r in quality_rows}
    freshness = {r["source_id"]: int(r["age_days"]) for r in freshness_rows}

    order = [r["source_id"] for r in sources]
    src = {}
    for row in sources:
        src[row["source_id"]] = {
            "language": row["language"],
            "domain": row["domain"],
            "source_class": row["source_class"],
            "safety_stratum": row["safety_stratum"],
            "priority": int(row["priority"]),
            "raw_amount": int(row["raw_amount"]),
            "raw_unit": row["raw_unit"],
        }

    phase_order = [r["phase"] for r in capacity_rows]
    for ph in curriculum["phases"]:
        if ph not in phase_order:
            phase_order.append(ph)
    budget = {r["phase"]: int(r["budget_tokens"]) for r in capacity_rows}
    cap = {r["phase"]: int(r["cap_tokens"]) for r in capacity_rows}
    init_credit = {r["phase"]: int(r["initial_credit"]) for r in carry_rows}
    init_debt = {r["phase"]: int(r["initial_debt"]) for r in carry_rows}

    examples = {}
    src_examples = {s: [] for s in order}
    for row in example_rows:
        eid = row["example_id"]
        examples[eid] = {
            "source_id": row["source_id"],
            "stratum": row["stratum"],
            "token_length": int(row["token_length"]),
        }
        src_examples.setdefault(row["source_id"], []).append(eid)
    for sid in src_examples:
        src_examples[sid].sort()

    ties = []
    floor_binds = []
    cap_binds = []
    floor_over_caps = []
    overturn_branches = []
    cycle_branches = []
    carry_debt_branches = []
    clawback_branches = []
    empty_group_branches = []

    # ---- stage 1, manifest canonicalization -----------------------------
    canonical = {}
    canonical_rows = []
    for sid in order:
        s = src[sid]
        unit = unit_rules["units"][s["raw_unit"]]
        p = s["raw_amount"] * unit["num"]
        d = unit["den"]
        tokens = rhe(p, d)
        tie = is_half(p, d)
        canonical[sid] = tokens
        canonical_rows.append({
            "source_id": sid,
            "raw_amount": s["raw_amount"],
            "raw_unit": s["raw_unit"],
            "canonical_tokens": tokens,
            "rounding_tie": tie,
        })
        if tie:
            ties.append({"code": "B_UNIT_ROUND_TIE", "ref": sid})

    # ---- stage 2, eligibility projection --------------------------------
    eligibility_rows = []
    eligible = []
    for sid in order:
        s = src[sid]
        q_num, q_den = quality.get(sid, (0, 0))
        if q_den == 0 or q_num * min_q_den < min_q_num * q_den:
            code = "E_LOW_QUALITY"
        elif s["language"] not in lang_policy:
            code = "E_LANG_EXCLUDED"
        elif s["safety_stratum"] not in strata:
            code = "E_STRATUM_UNKNOWN"
        elif s["domain"] in blocked:
            code = "E_DOMAIN_BLOCKED"
        elif canonical[sid] <= 0:
            code = "E_EMPTY_TOKENS"
        else:
            code = "E_OK"
        eligibility_rows.append({
            "source_id": sid,
            "eligible": code == "E_OK",
            "code": code,
        })
        if code == "E_OK":
            eligible.append(sid)

    # ---- stage 3, demand scoring ----------------------------------------
    demand = {}
    demand_rows = []
    for sid in eligible:
        s = src[sid]
        q_num, q_den = quality[sid]
        quality_bp = rhe(q_num * score_scale, q_den)
        fresh_den = half_life + freshness.get(sid, 0)
        fresh_bp = rhe(half_life * score_scale, fresh_den) if fresh_den > 0 else 0
        priority_factor = priority_weights[str(s["priority"])]
        balance_bp = lang_policy[s["language"]]["balance_bp"]
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

    # ---- stage 4, phase membership and proportional allocation ----------
    members = {ph: [] for ph in phase_order}
    for sid in eligible:
        for ph in DOMAIN_PHASES.get(src[sid]["domain"], ()):
            if ph in members:
                members[ph].append(sid)
    membership_rows = []
    for ph in phase_order:
        members[ph].sort()
        for sid in members[ph]:
            membership_rows.append({"phase": ph, "source_id": sid})

    target = {}
    proportional_rows = []
    for ph in phase_order:
        demand_sum = sum(demand[sid] for sid in members[ph] if demand[sid] > 0)
        b = budget.get(ph, 0)
        for sid in members[ph]:
            num = demand[sid] * b
            if demand[sid] > 0 and demand_sum > 0:
                tgt = rhe(num, demand_sum)
            else:
                tgt = 0
            target[(ph, sid)] = tgt
            proportional_rows.append({
                "phase": ph,
                "source_id": sid,
                "target_tokens": tgt,
                "share_num": num,
                "share_den": demand_sum,
            })

    # ---- stage 5, protected floors --------------------------------------
    floor = {}
    floor_rows = []
    for ph in phase_order:
        raised = False
        for sid in members[ph]:
            s = src[sid]
            safety_floor = strata[s["safety_stratum"]]["floor_tokens"]
            language_floor = lang_policy[s["language"]]["floor_tokens"]
            f = safety_floor if safety_floor >= language_floor else language_floor
            floor[(ph, sid)] = f
            floor_rows.append({
                "phase": ph,
                "source_id": sid,
                "safety_floor": safety_floor,
                "language_floor": language_floor,
                "floor": f,
            })
            if f > target[(ph, sid)]:
                raised = True
        if raised:
            floor_binds.append({"code": "B_FLOOR_BIND", "ref": ph})

    # ---- stage 7, curriculum precedence closure -------------------------
    edges = curriculum["edges"]
    # An edge from -> to orders "from" before "to", so "to" requires "from".
    closure = {ph: set() for ph in phase_order}
    for e in edges:
        closure.setdefault(e["from"], set())
        closure.setdefault(e["to"], set()).add(e["from"])
    changed = True
    while changed:
        changed = False
        for ph in list(closure):
            grown = set(closure[ph])
            for mid in closure[ph]:
                grown |= closure.get(mid, set())
            if grown != closure[ph]:
                closure[ph] = grown
                changed = True
    closure_rows = []
    for ph in sorted(closure):
        for req in sorted(closure[ph]):
            closure_rows.append({"phase": ph, "requires": req})

    # ---- stage 8, cycle break -------------------------------------------
    cycle_nodes = sorted(ph for ph in closure if ph in closure[ph])
    cycle_rows = []
    if cycle_nodes:
        cycle_rows.append({"cycle_nodes": cycle_nodes})
    cycle_set = set(cycle_nodes)
    cycle_break_rows = []
    removed_ids = set()
    if cycle_set:
        candidates = [e for e in edges
                      if e["from"] in cycle_set and e["to"] in cycle_set]
        if candidates:
            worst = min(candidates, key=lambda e: (
                0 if e["hardness"] == "soft" else 1,
                e["from"], e["to"], e["edge_id"]))
            removed_ids.add(worst["edge_id"])
            cycle_break_rows.append({
                "edge_id": worst["edge_id"],
                "from": worst["from"],
                "to": worst["to"],
                "hardness": worst["hardness"],
            })
            cycle_branches.append({"code": "B_CYCLE_BREAK", "ref": worst["edge_id"]})
    surviving = [e for e in edges if e["edge_id"] not in removed_ids]

    graph_phases = list(phase_order)
    for e in edges:
        for ph in (e["from"], e["to"]):
            if ph not in graph_phases:
                graph_phases.append(ph)
    indeg = {ph: 0 for ph in graph_phases}
    succ = {ph: [] for ph in graph_phases}
    for e in surviving:
        succ[e["from"]].append(e["to"])
        indeg[e["to"]] += 1
    topo = []
    remaining = set(graph_phases)
    while remaining:
        ready = sorted(ph for ph in remaining if indeg[ph] == 0)
        nxt = ready[0] if ready else min(remaining)
        remaining.discard(nxt)
        topo.append(nxt)
        for tgt in succ[nxt]:
            if tgt in remaining:
                indeg[tgt] -= 1

    # ---- stage 6 and 9, capacity binding with carry state ---------------
    alloc = {}
    binding_rows = []
    carry_ledger_rows = []
    eff_cap = {}
    prev_credit = 0
    prev_debt = 0
    first = True
    for ph in topo:
        if ph not in members:
            continue
        c_in = init_credit.get(ph, 0) + (0 if first else prev_credit)
        d_in = init_debt.get(ph, 0) + (0 if first else prev_debt)
        first = False
        capacity = cap.get(ph, 0) + c_in - d_in
        eff_cap[ph] = capacity
        cur = {}
        for sid in members[ph]:
            t = target[(ph, sid)]
            f = floor[(ph, sid)]
            cur[sid] = f if f > t else t
        total = sum(cur.values())
        deferred = 0
        if total > capacity:
            cap_binds.append({"code": "B_CAP_BIND", "ref": ph})
            excess = total - capacity
            disp_order = sorted(
                members[ph],
                key=lambda s: (strat_rank.get(src[s]["safety_stratum"], 0),
                               -src[s]["priority"], s))
            for sid in disp_order:
                if excess <= 0:
                    break
                room = cur[sid] - floor[(ph, sid)]
                if room <= 0:
                    continue
                cut = room if room < excess else excess
                binding_rows.append({
                    "phase": ph,
                    "source_id": sid,
                    "before": cur[sid],
                    "after": cur[sid] - cut,
                    "displaced": cut,
                    "kind": "above_floor",
                })
                cur[sid] -= cut
                excess -= cut
            if excess > 0:
                floor_over_caps.append({"code": "B_FLOOR_OVER_CAP", "ref": ph})
                for sid in disp_order:
                    if excess <= 0:
                        break
                    room = cur[sid]
                    if room <= 0:
                        continue
                    cut = room if room < excess else excess
                    binding_rows.append({
                        "phase": ph,
                        "source_id": sid,
                        "before": cur[sid],
                        "after": cur[sid] - cut,
                        "displaced": cut,
                        "kind": "floor_cut",
                    })
                    cur[sid] -= cut
                    excess -= cut
                    deferred += cut
        for sid in members[ph]:
            alloc[(ph, sid)] = cur[sid]
        allocated = sum(cur.values())
        unused = capacity - allocated
        c_out = unused if unused > 0 else 0
        carry_ledger_rows.append({
            "phase": ph,
            "credit_in": c_in,
            "debt_in": d_in,
            "credit_out": c_out,
            "debt_out": deferred,
        })
        if deferred > 0:
            carry_debt_branches.append({"code": "B_CARRY_DEBT", "ref": ph})
        prev_credit = c_out
        prev_debt = deferred

    # ---- stage 8, precedence overturn -----------------------------------
    overturn_rows = []
    for ph in phase_order:
        hit = False
        for req in sorted(closure.get(ph, set())):
            if req not in members:
                continue
            for sid in members[ph]:
                if sid not in members[req]:
                    continue
                if alloc[(ph, sid)] > 0 and alloc[(req, sid)] == 0:
                    overturn_rows.append({
                        "phase": ph,
                        "prereq_phase": req,
                        "source_id": sid,
                    })
                    hit = True
        if hit:
            overturn_branches.append({"code": "B_PRECEDENCE_OVERTURN", "ref": ph})

    # ---- stage 10, global reconciliation --------------------------------
    pre_rows = []
    for ph in phase_order:
        for sid in members[ph]:
            pre_rows.append({"phase": ph, "source_id": sid,
                             "tokens": alloc[(ph, sid)]})

    final = dict(alloc)
    recon_rows = []
    for sid in order:
        held = [ph for ph in phase_order if sid in members[ph]]
        total = sum(final[(ph, sid)] for ph in held)
        supply = canonical[sid]
        if total <= supply:
            continue
        excess = total - supply
        for ph in reversed(topo):
            if excess <= 0:
                break
            if ph not in members or sid not in members[ph]:
                continue
            have = final[(ph, sid)]
            if have <= 0:
                continue
            cut = have if have < excess else excess
            recon_rows.append({
                "source_id": sid,
                "phase": ph,
                "before": have,
                "after": have - cut,
                "clawed": cut,
            })
            final[(ph, sid)] = have - cut
            excess -= cut
        clawback_branches.append({"code": "B_GLOBAL_CLAWBACK", "ref": sid})

    final_rows = []
    for ph in phase_order:
        for sid in members[ph]:
            final_rows.append({"phase": ph, "source_id": sid,
                               "tokens": final[(ph, sid)]})

    source_total = {}
    for sid in order:
        source_total[sid] = sum(final[(ph, sid)] for ph in phase_order
                                if sid in members[ph])
    total_tokens = sum(source_total.values())

    # ---- stage 11, rational example weights -----------------------------
    rational_rows = []
    for sid in order:
        eids = src_examples.get(sid, [])
        if not eids:
            continue
        group_den = sum(examples[e]["token_length"] for e in eids)
        if group_den == 0:
            empty_group_branches.append({"code": "B_EMPTY_GROUP", "ref": sid})
            continue
        for eid in eids:
            num = source_total[sid] * examples[eid]["token_length"]
            den = group_den
            g = gcd(num, den) or 1
            rational_rows.append({
                "example_id": eid,
                "source_id": sid,
                "w_num": num // g,
                "w_den": den // g,
            })

    # ---- stage 12, integer weight materialization -----------------------
    materialized = {}
    fixed_rows = []
    residual_rows = []
    for sid in order:
        eids = src_examples.get(sid, [])
        if not eids:
            continue
        lengths = {e: examples[e]["token_length"] for e in eids}
        base, extra = largest_remainder(source_total[sid], lengths, eids)
        for eid in eids:
            got = base[eid] + extra.get(eid, 0)
            materialized[eid] = got
            fixed_rows.append({
                "example_id": eid,
                "source_id": sid,
                "materialized_tokens": got,
            })
        for eid in eids:
            if extra.get(eid, 0):
                residual_rows.append({
                    "example_id": eid,
                    "source_id": sid,
                    "base_tokens": base[eid],
                    "residual_unit": extra[eid],
                })

    # ---- stage 13, microbatch construction ------------------------------
    batch_rows = []
    batch_no = 0
    for ph in phase_order:
        seq = []
        for sid in members[ph]:
            if final[(ph, sid)] <= 0:
                continue
            for eid in src_examples.get(sid, []):
                if materialized.get(eid, 0) > 0:
                    seq.append(eid)
        cur_members = []
        cur_tokens = 0
        for eid in seq:
            length = materialized[eid]
            sid = examples[eid]["source_id"]
            if cur_members:
                trailing = 0
                for prev in reversed(cur_members):
                    if examples[prev]["source_id"] == sid:
                        trailing += 1
                    else:
                        break
                if cur_tokens + length > max_seq or trailing + 1 > adjacency_limit:
                    batch_no += 1
                    batch_rows.append({
                        "batch_id": "BATCH%04d" % batch_no,
                        "phase": ph,
                        "members": cur_members,
                        "seq_tokens": cur_tokens,
                    })
                    cur_members = []
                    cur_tokens = 0
            cur_members.append(eid)
            cur_tokens += length
        if cur_members:
            batch_no += 1
            batch_rows.append({
                "batch_id": "BATCH%04d" % batch_no,
                "phase": ph,
                "members": cur_members,
                "seq_tokens": cur_tokens,
            })

    # ---- summaries -------------------------------------------------------
    lang_rows = []
    for lang in sorted(lang_policy):
        tokens = sum(source_total[sid] for sid in order
                     if src[sid]["language"] == lang)
        lang_rows.append({
            "language": lang,
            "tokens": tokens,
            "min_required": rhe(lang_policy[lang]["min_share_bp"] * total_tokens,
                                scale),
            "max_allowed": rhe(lang_policy[lang]["max_share_bp"] * total_tokens,
                               scale),
        })

    safety_rows = []
    for stratum in sorted(strata):
        tokens = sum(source_total[sid] for sid in order
                     if src[sid]["safety_stratum"] == stratum)
        safety_rows.append({
            "stratum": stratum,
            "tokens": tokens,
            "floor": strata[stratum]["floor_tokens"],
        })

    source_rows = [{"source_id": sid, "tokens": source_total[sid],
                    "canonical_tokens": canonical[sid]} for sid in order]

    phase_total_rows = []
    for ph in phase_order:
        phase_total_rows.append({
            "phase": ph,
            "allocated": sum(final[(ph, sid)] for sid in members[ph]),
            "cap": cap.get(ph, 0),
            "budget": budget.get(ph, 0),
        })

    # ---- invariants ------------------------------------------------------
    phase_within_cap = all(
        sum(final[(ph, sid)] for sid in members[ph]) <= eff_cap.get(ph, cap.get(ph, 0))
        for ph in phase_order)
    source_within_supply = all(source_total[sid] <= canonical[sid] for sid in order)
    materialized_ok = True
    for sid in order:
        eids = src_examples.get(sid, [])
        if not eids:
            continue
        if sum(examples[e]["token_length"] for e in eids) == 0:
            continue
        if sum(materialized[e] for e in eids) != source_total[sid]:
            materialized_ok = False
    batches_ok = all(b["seq_tokens"] <= max_seq or len(b["members"]) == 1
                     for b in batch_rows)
    invariant_values = [phase_within_cap, source_within_supply,
                        materialized_ok, batches_ok]
    invariant_rows = [{"invariant": name, "holds": value}
                      for name, value in zip(INVARIANT_NAMES, invariant_values)]

    # ---- exception trace -------------------------------------------------
    exception_rows = (ties + floor_binds + cap_binds + floor_over_caps
                      + overturn_branches + cycle_branches + carry_debt_branches
                      + clawback_branches + empty_group_branches)

    objective = [total_tokens, len(recon_rows), len(cycle_break_rows),
                 len(binding_rows), len(batch_rows)]

    # ---- emission --------------------------------------------------------
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
    for name in JSONL_FILES:
        write_jsonl(os.path.join(out_dir, name), payload[name])

    write_json(os.path.join(out_dir, "objective_tuple.json"),
               {"objective": objective, "order": list(OBJECTIVE_ORDER)})

    manifest_names = JSONL_FILES + ["objective_tuple.json", "plan.py"]
    manifest = {name: digest_file(os.path.join(out_dir, name))
                for name in manifest_names}
    write_json(os.path.join(out_dir, "integrity_manifest.json"), manifest)

    chain = "0" * 64
    for name in sorted(manifest):
        chain = hashlib.sha256((chain + manifest[name]).encode("utf-8")).hexdigest()

    write_json(os.path.join(out_dir, "final_state.json"), {
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
        "invariants_hold": all(invariant_values),
    })


def main(argv):
    if len(argv) != 3:
        sys.stderr.write("usage: plan.py <DATA_DIR> <OUT_DIR>\n")
        return 2
    data_dir = argv[1]
    out_dir = argv[2]
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.abspath(__file__), "rb") as fh:
        own_source = fh.read()
    with open(os.path.join(out_dir, "plan.py"), "wb") as fh:
        fh.write(own_source)
    compile_plan(data_dir, out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
