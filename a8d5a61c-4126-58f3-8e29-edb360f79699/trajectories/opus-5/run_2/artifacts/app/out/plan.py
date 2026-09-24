#!/usr/bin/env python3
"""TrainMix allocation compiler.

Derives the training mixture certificate from the shipped manifests following the
fourteen stage chain in the specification.  All arithmetic is integer or exact
rational; no floating point value is produced, compared or serialized.

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


# --------------------------------------------------------------------------
# arithmetic
# --------------------------------------------------------------------------

def rhe(p, d):
    """round_half_even(p, d): quantize p over the positive denominator d."""
    if d == 0:
        raise ZeroDivisionError("round_half_even with a zero denominator")
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
    return 2 * (p % d) == d


def largest_remainder(total, keys, weight_of):
    """Split total across keys in proportion to weight, tie broken by key order.

    Returns (base, final) dicts.  Keys are handed out in the caller supplied
    sequence order, which also settles remainder ties (ascending key).
    """
    base = {k: 0 for k in keys}
    final = {k: 0 for k in keys}
    wsum = sum(weight_of(k) for k in keys)
    if wsum <= 0 or total <= 0 or not keys:
        return base, final
    rem = {}
    for k in keys:
        q, r = divmod(total * weight_of(k), wsum)
        base[k] = q
        rem[k] = r
    handed = total - sum(base.values())
    order = sorted(range(len(keys)), key=lambda i: (-rem[keys[i]], i))
    final.update(base)
    for i in order[:handed]:
        final[keys[i]] += 1
    return base, final


# --------------------------------------------------------------------------
# io
# --------------------------------------------------------------------------

def read_csv(path):
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return [dict(row) for row in csv.DictReader(fh)]


def read_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def write_jsonl(path, rows):
    if rows:
        text = "".join(
            json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n" for r in rows
        )
    else:
        text = ""
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


# --------------------------------------------------------------------------
# graph
# --------------------------------------------------------------------------

def transitive_closure(nodes, edges):
    """requires[p] = set of phases p depends on, through edges from -> to."""
    reach = {n: set() for n in nodes}
    for e in edges:
        if e["from"] in reach:
            reach[e["from"]].add(e["to"])
    changed = True
    while changed:
        changed = False
        for n in nodes:
            add = set()
            for m in reach[n]:
                add |= reach.get(m, set())
            if not add <= reach[n]:
                reach[n] |= add
                changed = True
    requires = {n: set() for n in nodes}
    for n in nodes:
        for m in reach[n]:
            if m in requires:
                requires[m].add(n)
    return requires, reach


def topological_order(nodes, edges):
    adj = {n: [] for n in nodes}
    indeg = {n: 0 for n in nodes}
    for e in edges:
        if e["from"] in adj and e["to"] in indeg:
            adj[e["from"]].append(e["to"])
            indeg[e["to"]] += 1
    order = []
    avail = sorted(n for n in nodes if indeg[n] == 0)
    while avail:
        n = avail.pop(0)
        order.append(n)
        for m in adj[n]:
            indeg[m] -= 1
            if indeg[m] == 0:
                avail.append(m)
                avail.sort()
    if len(order) < len(nodes):
        order.extend(sorted(n for n in nodes if n not in order))
    return order


# --------------------------------------------------------------------------
# compiler
# --------------------------------------------------------------------------

def compile_plan(data_dir, out_dir):
    unit_rules = read_json(os.path.join(data_dir, "unit_rules.json"))
    scoring_weights = read_json(os.path.join(data_dir, "scoring_weights.json"))
    quality_policy = read_json(os.path.join(data_dir, "quality_policy.json"))
    language_policy = read_json(os.path.join(data_dir, "language_policy.json"))["languages"]
    safety_policy = read_json(os.path.join(data_dir, "safety_policy.json"))["strata"]
    priority_order = read_json(os.path.join(data_dir, "priority_order.json"))
    curriculum = read_json(os.path.join(data_dir, "curriculum.json"))
    batch_rules = read_json(os.path.join(data_dir, "batch_rules.json"))

    scale = int(unit_rules["scale"])
    score_scale = int(scoring_weights["scale"])
    half_life = int(scoring_weights["half_life_days"])
    priority_weights = {str(k): int(v) for k, v in scoring_weights["priority_weights"].items()}
    blocked_domains = set(quality_policy.get("blocked_domains", []))
    min_q_num = int(quality_policy["min_quality"]["num"])
    min_q_den = int(quality_policy["min_quality"]["den"])
    stratum_rank = priority_order["stratum_displacement_rank"]

    sources = read_csv(os.path.join(data_dir, "sources.csv"))
    quality = {r["source_id"]: r for r in read_csv(os.path.join(data_dir, "quality.csv"))}
    freshness = {r["source_id"]: r for r in read_csv(os.path.join(data_dir, "freshness.csv"))}
    examples = read_csv(os.path.join(data_dir, "examples.csv"))
    capacity = read_csv(os.path.join(data_dir, "capacity.csv"))
    carry = {r["phase"]: r for r in read_csv(os.path.join(data_dir, "carry_state.csv"))}

    sids = [r["source_id"] for r in sources]
    src = {r["source_id"]: r for r in sources}

    phase_order = [r["phase"] for r in capacity]
    for p in curriculum.get("phases", []):
        if p not in phase_order:
            phase_order.append(p)
    budget = {r["phase"]: int(r["budget_tokens"]) for r in capacity}
    cap_tokens = {r["phase"]: int(r["cap_tokens"]) for r in capacity}
    init_credit = {p: int(carry[p]["initial_credit"]) if p in carry else 0 for p in phase_order}
    init_debt = {p: int(carry[p]["initial_debt"]) if p in carry else 0 for p in phase_order}

    ex_stage1 = []
    ex_stage6 = []
    ex_cycle = []
    ex_overturn = []
    ex_carry = []
    ex_claw = []
    ex_empty = []

    # ---- stage 1, manifest canonicalization -----------------------------
    canonical_rows = []
    canonical_tokens = {}
    for sid in sids:
        row = src[sid]
        raw_amount = int(row["raw_amount"])
        raw_unit = row["raw_unit"]
        unit = unit_rules["units"][raw_unit]
        num = int(unit["num"])
        den = int(unit["den"])
        p = raw_amount * num
        tok = rhe(p, den)
        tie = is_tie(p, den)
        canonical_tokens[sid] = tok
        canonical_rows.append(
            {
                "source_id": sid,
                "raw_amount": raw_amount,
                "raw_unit": raw_unit,
                "canonical_tokens": tok,
                "rounding_tie": tie,
            }
        )
        if tie:
            ex_stage1.append({"code": "B_UNIT_ROUND_TIE", "ref": sid})

    # ---- stage 2, eligibility projection --------------------------------
    eligibility_rows = []
    eligible = []
    for sid in sids:
        row = src[sid]
        q = quality[sid]
        q_num = int(q["q_num"])
        q_den = int(q["q_den"])
        if q_den == 0 or q_num * min_q_den < min_q_num * q_den:
            code = "E_LOW_QUALITY"
        elif row["language"] not in language_policy:
            code = "E_LANG_EXCLUDED"
        elif row["safety_stratum"] not in safety_policy:
            code = "E_STRATUM_UNKNOWN"
        elif row["domain"] in blocked_domains:
            code = "E_DOMAIN_BLOCKED"
        elif canonical_tokens[sid] <= 0:
            code = "E_EMPTY_TOKENS"
        else:
            code = "E_OK"
        eligibility_rows.append(
            {"source_id": sid, "eligible": code == "E_OK", "code": code}
        )
        if code == "E_OK":
            eligible.append(sid)

    # ---- stage 3, demand scoring ----------------------------------------
    demand_rows = []
    demand_score = {}
    for sid in eligible:
        row = src[sid]
        q = quality[sid]
        q_num = int(q["q_num"])
        q_den = int(q["q_den"])
        quality_bp = rhe(q_num * score_scale, q_den)
        age = int(freshness[sid]["age_days"])
        fresh_den = half_life + age
        fresh_bp = rhe(half_life * score_scale, fresh_den) if fresh_den != 0 else 0
        priority_factor = priority_weights[str(int(row["priority"]))]
        balance_bp = int(language_policy[row["language"]]["balance_bp"])
        s1 = rhe(canonical_tokens[sid] * quality_bp, score_scale)
        s2 = rhe(s1 * fresh_bp, score_scale)
        s3 = s2 * priority_factor
        score = rhe(s3 * balance_bp, score_scale)
        demand_score[sid] = score
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

    # ---- stage 4, phase membership --------------------------------------
    members = {p: [] for p in phase_order}
    for sid in eligible:
        for p in DOMAIN_PHASES.get(src[sid]["domain"], ()):
            if p in members:
                members[p].append(sid)
    membership_rows = [
        {"phase": p, "source_id": sid} for p in phase_order for sid in members[p]
    ]

    # ---- stage 7, precedence closure ------------------------------------
    edges = list(curriculum.get("edges", []))
    requires, reach = transitive_closure(phase_order, edges)
    closure_rows = [
        {"phase": p, "requires": r}
        for p in sorted(phase_order)
        for r in sorted(requires[p])
    ]

    # ---- stage 8a, cycle detection and break -----------------------------
    cycle_nodes = sorted(p for p in phase_order if p in reach.get(p, set()))
    cycle_rows = []
    cycle_break_rows = []
    removed_edge_ids = set()
    if cycle_nodes:
        cycle_rows.append({"cycle_nodes": list(cycle_nodes)})
        cset = set(cycle_nodes)
        candidates = [e for e in edges if e["from"] in cset and e["to"] in cset]
        if candidates:
            victim = min(
                candidates,
                key=lambda e: (
                    0 if e["hardness"] == "soft" else 1,
                    e["from"],
                    e["to"],
                    e["edge_id"],
                ),
            )
            removed_edge_ids.add(victim["edge_id"])
            cycle_break_rows.append(
                {
                    "edge_id": victim["edge_id"],
                    "from": victim["from"],
                    "to": victim["to"],
                    "hardness": victim["hardness"],
                }
            )
            ex_cycle.append({"code": "B_CYCLE_BREAK", "ref": victim["edge_id"]})
    surviving = [e for e in edges if e["edge_id"] not in removed_edge_ids]

    # ---- stage 9 ordering -------------------------------------------------
    topo = topological_order(phase_order, surviving)

    # ---- stages 4 to 6 and 9, per phase in topological order -------------
    proportional = {}
    floors = {}
    floor_detail = {}
    alloc = {p: {} for p in phase_order}
    binding_trace = {p: [] for p in phase_order}
    eff_cap = {}
    carry_rows_by_phase = {}
    credit_in = {p: init_credit[p] for p in phase_order}
    debt_in = {p: init_debt[p] for p in phase_order}

    for idx, phase in enumerate(topo):
        mem = members[phase]
        bud = budget.get(phase, 0)
        demand_sum = sum(demand_score[s] for s in mem if demand_score[s] > 0)
        targets = {}
        shares = {}
        for sid in mem:
            ds = demand_score[sid]
            share_num = ds * bud
            shares[sid] = (share_num, demand_sum)
            if ds > 0 and demand_sum > 0:
                targets[sid] = rhe(share_num, demand_sum)
            else:
                targets[sid] = 0
        proportional[phase] = (targets, shares)

        fl = {}
        detail = {}
        for sid in mem:
            row = src[sid]
            sfloor = int(safety_policy[row["safety_stratum"]]["floor_tokens"])
            lfloor = int(language_policy[row["language"]]["floor_tokens"])
            f = sfloor if sfloor >= lfloor else lfloor
            fl[sid] = f
            detail[sid] = (sfloor, lfloor, f)
        floors[phase] = fl
        floor_detail[phase] = detail

        floored = {sid: max(targets[sid], fl[sid]) for sid in mem}
        if any(fl[sid] > targets[sid] for sid in mem):
            ex_stage6.append({"code": "B_FLOOR_BIND", "ref": phase})

        cap = cap_tokens.get(phase, 0) + credit_in[phase] - debt_in[phase]
        eff_cap[phase] = cap

        order = sorted(
            mem,
            key=lambda s: (
                stratum_rank[src[s]["safety_stratum"]],
                -int(src[s]["priority"]),
                s,
            ),
        )
        cur = dict(floored)
        excess = sum(cur.values()) - cap
        if excess > 0:
            ex_stage6.append({"code": "B_CAP_BIND", "ref": phase})
            for sid in order:
                if excess <= 0:
                    break
                room = cur[sid] - fl[sid]
                if room > 0:
                    cut = room if room < excess else excess
                    before = cur[sid]
                    cur[sid] = before - cut
                    excess -= cut
                    binding_trace[phase].append(
                        {
                            "phase": phase,
                            "source_id": sid,
                            "before": before,
                            "after": cur[sid],
                            "displaced": cut,
                            "kind": "above_floor",
                        }
                    )
            if excess > 0:
                ex_stage6.append({"code": "B_FLOOR_OVER_CAP", "ref": phase})
                for sid in order:
                    if excess <= 0:
                        break
                    room = cur[sid]
                    if room > 0:
                        cut = room if room < excess else excess
                        before = cur[sid]
                        cur[sid] = before - cut
                        excess -= cut
                        binding_trace[phase].append(
                            {
                                "phase": phase,
                                "source_id": sid,
                                "before": before,
                                "after": cur[sid],
                                "displaced": cut,
                                "kind": "floor_cut",
                            }
                        )
        alloc[phase] = cur

        allocated = sum(cur.values())
        unused = cap - allocated
        credit_out = unused if unused > 0 else 0
        debt_out = sum(fl[sid] - cur[sid] for sid in mem if fl[sid] > cur[sid])
        carry_rows_by_phase[phase] = {
            "phase": phase,
            "credit_in": credit_in[phase],
            "debt_in": debt_in[phase],
            "credit_out": credit_out,
            "debt_out": debt_out,
        }
        if debt_out > 0:
            ex_carry.append({"code": "B_CARRY_DEBT", "ref": phase})
        if idx + 1 < len(topo):
            nxt = topo[idx + 1]
            credit_in[nxt] += credit_out
            debt_in[nxt] += debt_out

    proportional_rows = []
    floor_rows = []
    for p in phase_order:
        targets, shares = proportional[p]
        for sid in members[p]:
            proportional_rows.append(
                {
                    "phase": p,
                    "source_id": sid,
                    "target_tokens": targets[sid],
                    "share_num": shares[sid][0],
                    "share_den": shares[sid][1],
                }
            )
            sfloor, lfloor, f = floor_detail[p][sid]
            floor_rows.append(
                {
                    "phase": p,
                    "source_id": sid,
                    "safety_floor": sfloor,
                    "language_floor": lfloor,
                    "floor": f,
                }
            )
    binding_rows = [r for p in topo for r in binding_trace[p]]
    carry_rows = [carry_rows_by_phase[p] for p in topo]
    pre_rows = [
        {"phase": p, "source_id": sid, "tokens": alloc[p][sid]}
        for p in phase_order
        for sid in members[p]
    ]

    # ---- stage 8b, precedence overturn -----------------------------------
    overturn_rows = []
    for p in sorted(phase_order):
        for prereq in sorted(requires[p]):
            if prereq == p:
                continue
            for sid in sorted(set(members[p]) & set(members[prereq])):
                if alloc[p][sid] > 0 and alloc[prereq][sid] == 0:
                    overturn_rows.append(
                        {"phase": p, "prereq_phase": prereq, "source_id": sid}
                    )
                    ex_overturn.append({"code": "B_PRECEDENCE_OVERTURN", "ref": p})

    # ---- stage 10, global reconciliation ---------------------------------
    final = {p: dict(alloc[p]) for p in phase_order}
    recon_rows = []
    for sid in sids:
        total = sum(final[p][sid] for p in phase_order if sid in final[p])
        supply = canonical_tokens[sid]
        if total > supply:
            excess = total - supply
            ex_claw.append({"code": "B_GLOBAL_CLAWBACK", "ref": sid})
            for p in reversed(topo):
                if excess <= 0:
                    break
                if sid not in final[p]:
                    continue
                before = final[p][sid]
                if before <= 0:
                    continue
                clawed = before if before < excess else excess
                final[p][sid] = before - clawed
                excess -= clawed
                recon_rows.append(
                    {
                        "source_id": sid,
                        "phase": p,
                        "before": before,
                        "after": final[p][sid],
                        "clawed": clawed,
                    }
                )

    final_rows = [
        {"phase": p, "source_id": sid, "tokens": final[p][sid]}
        for p in phase_order
        for sid in members[p]
    ]

    source_total = {
        sid: sum(final[p][sid] for p in phase_order if sid in final[p]) for sid in sids
    }
    total_tokens = sum(source_total.values())

    # ---- summaries --------------------------------------------------------
    lang_rows = []
    for lang in sorted(language_policy):
        pol = language_policy[lang]
        tokens = sum(source_total[s] for s in sids if src[s]["language"] == lang)
        lang_rows.append(
            {
                "language": lang,
                "tokens": tokens,
                "min_required": rhe(int(pol["min_share_bp"]) * total_tokens, scale),
                "max_allowed": rhe(int(pol["max_share_bp"]) * total_tokens, scale),
            }
        )

    safety_rows = []
    for stratum in sorted(safety_policy):
        tokens = sum(source_total[s] for s in sids if src[s]["safety_stratum"] == stratum)
        safety_rows.append(
            {
                "stratum": stratum,
                "tokens": tokens,
                "floor": int(safety_policy[stratum]["floor_tokens"]),
            }
        )

    source_rows = [
        {
            "source_id": sid,
            "tokens": source_total[sid],
            "canonical_tokens": canonical_tokens[sid],
        }
        for sid in sids
    ]

    phase_total_rows = [
        {
            "phase": p,
            "allocated": sum(final[p].values()),
            "cap": cap_tokens.get(p, 0),
            "budget": budget.get(p, 0),
        }
        for p in phase_order
    ]

    # ---- stages 11 and 12, example weights --------------------------------
    ex_by_source = {}
    for row in examples:
        ex_by_source.setdefault(row["source_id"], []).append(row)
    for sid in ex_by_source:
        ex_by_source[sid].sort(key=lambda r: r["example_id"])

    rational_rows = []
    fixed_rows = []
    residual_rows = []
    materialized = {}
    for sid in sids:
        rows = ex_by_source.get(sid, [])
        if not rows:
            continue
        keys = [r["example_id"] for r in rows]
        length = {r["example_id"]: int(r["token_length"]) for r in rows}
        group_den = sum(length[k] for k in keys)
        total = source_total[sid]
        if group_den == 0:
            ex_empty.append({"code": "B_EMPTY_GROUP", "ref": sid})
            for k in keys:
                materialized[k] = 0
                fixed_rows.append(
                    {"example_id": k, "source_id": sid, "materialized_tokens": 0}
                )
            continue
        for k in keys:
            num = total * length[k]
            g = gcd(num, group_den)
            rational_rows.append(
                {
                    "example_id": k,
                    "source_id": sid,
                    "w_num": num // g,
                    "w_den": group_den // g,
                }
            )
        base, split = largest_remainder(total, keys, lambda k: length[k])
        for k in keys:
            materialized[k] = split[k]
            fixed_rows.append(
                {"example_id": k, "source_id": sid, "materialized_tokens": split[k]}
            )
            if split[k] > base[k]:
                residual_rows.append(
                    {
                        "example_id": k,
                        "source_id": sid,
                        "base_tokens": base[k],
                        "residual_unit": 1,
                    }
                )

    # examples of sources absent from sources.csv still need a materialized entry
    known = set(sids)
    for sid in sorted(k for k in ex_by_source if k not in known):
        for r in ex_by_source[sid]:
            materialized[r["example_id"]] = 0
            fixed_rows.append(
                {
                    "example_id": r["example_id"],
                    "source_id": sid,
                    "materialized_tokens": 0,
                }
            )

    # ---- stage 13, microbatch construction --------------------------------
    max_seq = int(batch_rules["max_seq_tokens"])
    adjacency_limit = int(batch_rules["adjacency_limit"])
    batch_rows = []
    batch_no = 0
    for phase in phase_order:
        cur_members = []
        cur_sources = []
        cur_tokens = 0

        def flush():
            nonlocal batch_no, cur_members, cur_sources, cur_tokens
            if cur_members:
                batch_no += 1
                batch_rows.append(
                    {
                        "batch_id": "BATCH%04d" % batch_no,
                        "phase": phase,
                        "members": list(cur_members),
                        "seq_tokens": cur_tokens,
                    }
                )
                cur_members = []
                cur_sources = []
                cur_tokens = 0

        for sid in sorted(s for s in members[phase] if final[phase][s] > 0):
            for r in ex_by_source.get(sid, []):
                eid = r["example_id"]
                tok = materialized.get(eid, 0)
                if tok <= 0:
                    continue
                if cur_members:
                    run = 0
                    for s in reversed(cur_sources):
                        if s == sid:
                            run += 1
                        else:
                            break
                    if cur_tokens + tok > max_seq or run >= adjacency_limit:
                        flush()
                cur_members.append(eid)
                cur_sources.append(sid)
                cur_tokens += tok
        flush()

    # ---- invariants -------------------------------------------------------
    inv_phase = all(sum(final[p].values()) <= eff_cap[p] for p in phase_order)
    inv_supply = all(source_total[s] <= canonical_tokens[s] for s in sids)
    inv_mat = True
    for sid in sids:
        rows = ex_by_source.get(sid, [])
        if not rows:
            continue
        if sum(int(r["token_length"]) for r in rows) == 0:
            continue
        if sum(materialized[r["example_id"]] for r in rows) != source_total[sid]:
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

    exception_rows = (
        ex_stage1 + ex_stage6 + ex_cycle + ex_overturn + ex_carry + ex_claw + ex_empty
    )

    objective = [
        total_tokens,
        len(recon_rows),
        len(cycle_break_rows),
        len(binding_rows),
        len(batch_rows),
    ]

    # ---- emission ---------------------------------------------------------
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
        write_jsonl(os.path.join(out_dir, name), payload[name])
    write_json(
        os.path.join(out_dir, "objective_tuple.json"),
        {"objective": objective, "order": list(OBJECTIVE_ORDER)},
    )

    self_path = os.path.abspath(__file__)
    target = os.path.join(out_dir, "plan.py")
    with open(self_path, "rb") as fh:
        source_bytes = fh.read()
    if os.path.abspath(target) != self_path:
        with open(target, "wb") as fh:
            fh.write(source_bytes)

    hashed = sorted(JSONL_FILES + ["objective_tuple.json", "plan.py"])
    manifest = {name: sha256_file(os.path.join(out_dir, name)) for name in hashed}
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
        "examples_placed": sum(len(b["members"]) for b in batch_rows),
        "quiet_branches": len(exception_rows),
        "objective": objective,
        "invariants_hold": inv_phase and inv_supply and inv_mat and inv_batch,
    }
    write_json(os.path.join(out_dir, "final_state.json"), final_state)
    return final_state


def main(argv):
    if len(argv) != 3:
        sys.stderr.write("usage: plan.py <DATA_DIR> <OUT_DIR>\n")
        return 2
    data_dir = argv[1]
    out_dir = argv[2]
    os.makedirs(out_dir, exist_ok=True)
    compile_plan(data_dir, out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
