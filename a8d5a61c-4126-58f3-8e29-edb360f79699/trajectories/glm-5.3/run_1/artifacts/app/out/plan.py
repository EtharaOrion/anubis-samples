#!/usr/bin/env python3
"""TrainMix allocation compiler.

Derives the reproducible allocation certificate for a fine tune data mixture
from a directory of manifests, following the fourteen stage chain defined by
the specification documents shipped alongside those manifests. All arithmetic
is integer or exact rational with half to even quantization at every stage; no
floating point value is produced, compared or serialized anywhere.

Usage: python3 plan.py <DATA_DIR> <OUT_DIR>
"""

import csv
import hashlib
import json
import os
import shutil
import sys
from fractions import Fraction

PHASES = ["PH1", "PH2", "PH3"]
DOMAIN_PHASES = {
    "maint": ("PH1",),
    "manual": ("PH1", "PH2"),
    "incident": ("PH2",),
    "qa": ("PH2", "PH3"),
    "translated": ("PH1", "PH3"),
    "synthetic": ("PH3",),
    "reg": ("PH2",),
}
INITIAL_CHAIN = "0" * 64


# ---------------------------------------------------------------- utilities

def div_round_half_even(p, q):
    """round_half_even(p, q) for integer p over positive integer q."""
    if q <= 0:
        raise ZeroDivisionError("quantization needs a positive denominator")
    neg = p < 0
    p = -p if neg else p
    quot, rem = divmod(p, q)
    if 2 * rem > q or (2 * rem == q and quot % 2 == 1):
        quot += 1
    return -quot if neg else quot


def csv_dicts(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return [dict(row) for row in csv.DictReader(fh)]


def sif(obj):
    """Compact JSON object text with sorted keys, no trailing newline."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def jif(obj):
    """Indented JSON document text with sorted keys and one trailing newline."""
    return json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- stage 1

def stage1_canonicalize(sources_rows, unit_rules):
    canonical = []
    for row in sources_rows:
        raw_amount = int(row["raw_amount"])
        unit = row["raw_unit"]
        conv = unit_rules["units"][unit]
        numerator = raw_amount * int(conv["num"])
        denominator = int(conv["den"])
        tokens = div_round_half_even(numerator, denominator)
        tie = numerator >= 0 and denominator > 0 and 2 * (numerator % denominator) == denominator
        canonical.append({
            "source_id": row["source_id"],
            "raw_amount": raw_amount,
            "raw_unit": unit,
            "canonical_tokens": tokens,
            "rounding_tie": tie,
        })
    return canonical


# ---------------------------------------------------------------- stage 2

def stage2_eligibility(sources_rows, canonical, quality_rows, quality_policy,
                       language_policy, safety_policy):
    quality = {row["source_id"]: row for row in quality_rows}
    tokens = {row["source_id"]: row["canonical_tokens"] for row in canonical}
    min_q_num = int(quality_policy["min_quality"]["num"])
    min_q_den = int(quality_policy["min_quality"]["den"])
    blocked = quality_policy["blocked_domains"]
    languages = language_policy["languages"]
    strata = safety_policy["strata"]
    ledger = []
    for row in sources_rows:
        sid = row["source_id"]
        q = quality.get(sid)
        q_num = int(q["q_num"]) if q else 0
        q_den = int(q["q_den"]) if q else 0
        if q_den == 0 or q_num * min_q_den < min_q_num * q_den:
            code = "E_LOW_QUALITY"
        elif row["language"] not in languages:
            code = "E_LANG_EXCLUDED"
        elif row["safety_stratum"] not in strata:
            code = "E_STRATUM_UNKNOWN"
        elif row["domain"] in blocked:
            code = "E_DOMAIN_BLOCKED"
        elif tokens.get(sid, 0) <= 0:
            code = "E_EMPTY_TOKENS"
        else:
            code = "E_OK"
        ledger.append({"source_id": sid, "eligible": code == "E_OK", "code": code})
    return ledger


# ---------------------------------------------------------------- stage 3

def stage3_demand_scores(eligible_ids, sources_rows, canonical, quality_rows,
                         freshness_rows, scoring_weights, language_policy):
    info = {row["source_id"]: row for row in sources_rows}
    quality = {row["source_id"]: row for row in quality_rows}
    fresh = {row["source_id"]: row for row in freshness_rows}
    tokens = {row["source_id"]: row["canonical_tokens"] for row in canonical}
    scale = int(scoring_weights["scale"])
    half_life = int(scoring_weights["half_life_days"])
    weights = scoring_weights["priority_weights"]
    ledger = []
    for sid in eligible_ids:
        row = info[sid]
        q = quality[sid]
        f = fresh.get(sid)
        age_days = int(f["age_days"]) if f else 0
        quality_bp = div_round_half_even(int(q["q_num"]) * scale, int(q["q_den"]))
        fresh_bp = div_round_half_even(half_life * scale, half_life + age_days)
        priority_factor = int(weights[str(row["priority"])])
        lang_balance_bp = int(language_policy["languages"][row["language"]]["balance_bp"])
        s1 = div_round_half_even(tokens[sid] * quality_bp, scale)
        s2 = div_round_half_even(s1 * fresh_bp, scale)
        s3 = s2 * priority_factor
        demand_score = div_round_half_even(s3 * lang_balance_bp, scale)
        ledger.append({
            "source_id": sid,
            "quality_bp": quality_bp,
            "fresh_bp": fresh_bp,
            "priority_factor": priority_factor,
            "lang_balance_bp": lang_balance_bp,
            "demand_score": demand_score,
        })
    return ledger, {row["source_id"]: row["demand_score"] for row in ledger}


# ---------------------------------------------------------------- stages 7 and 8

def build_closure(phases, edges):
    """reach[p] is every phase reachable from p over one or more edges."""
    succ = {p: set() for p in phases}
    for e in edges:
        if e["from"] in succ and e["to"] in succ:
            succ[e["from"]].add(e["to"])
    reach = {p: set(succ[p]) for p in phases}
    for _ in range(len(phases) + 1):
        changed = False
        for p in phases:
            new = set(reach[p])
            for nxt in reach[p]:
                new |= succ[nxt]
            if new != reach[p]:
                reach[p] = new
                changed = True
        if not changed:
            break
    return reach


def topo_order(phases, edges, banned):
    nodes = list(phases)
    active = [e for e in edges if e["edge_id"] not in banned]
    indeg = {p: 0 for p in nodes}
    succ = {p: [] for p in nodes}
    for e in active:
        succ[e["from"]].append(e["to"])
        indeg[e["to"]] += 1
    ready = sorted(p for p in nodes if indeg[p] == 0)
    order = []
    while ready:
        node = ready.pop(0)
        order.append(node)
        for nxt in succ[node]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                ready.append(nxt)
        ready.sort()
    if len(order) < len(nodes):
        raise RuntimeError("curriculum graph is not acyclic after cycle breaking")
    return order


def edge_rank_key(e):
    """Hardness with soft below hard, then from, then to, then edge id."""
    return (0 if e["hardness"] == "soft" else 1, e["from"], e["to"], e["edge_id"])


def stage8_break_cycles(phases, edges):
    """Remove lowest ranked edges, one at a time, until the graph is acyclic."""
    removed = []
    banned = set()
    while True:
        active = [e for e in edges if e["edge_id"] not in banned]
        reach = build_closure(phases, active)
        cyc = sorted(p for p in phases if p in reach[p])
        if not cyc:
            break
        cyc_set = set(cyc)
        candidates = [e for e in active if e["from"] in cyc_set and e["to"] in cyc_set]
        if not candidates:
            break
        pick = min(candidates, key=edge_rank_key)
        banned.add(pick["edge_id"])
        removed.append(pick)
    return removed, banned


# ---------------------------------------------------------------- stages 4 to 6

def displacement_order(members, source_info, stratum_rank):
    def key(sid):
        row = source_info[sid]
        return (int(stratum_rank[row["safety_stratum"]]), -int(row["priority"]), sid)

    return sorted(members, key=key)


def run_binding(phase, members, floored, floors, eff_cap, source_info, stratum_rank,
                allocation, trace):
    excess = sum(floored[s] for s in members) - eff_cap
    if excess <= 0:
        for s in members:
            allocation[(phase, s)] = floored[s]
        return 0
    order = displacement_order(members, source_info, stratum_rank)
    for sid in order:
        if excess <= 0:
            break
        room = floored[sid] - floors[(phase, sid)]
        if room <= 0:
            continue
        take = min(room, excess)
        before = floored[sid]
        floored[sid] = before - take
        excess -= take
        trace.append({
            "phase": phase,
            "source_id": sid,
            "before": before,
            "after": floored[sid],
            "displaced": take,
            "kind": "above_floor" if floored[sid] >= floors[(phase, sid)] else "floor_cut",
        })
    if excess > 0:
        for sid in order:
            if excess <= 0:
                break
            if floored[sid] <= 0:
                continue
            before = floored[sid]
            take = min(before, excess)
            floored[sid] = before - take
            excess -= take
            trace.append({
                "phase": phase,
                "source_id": sid,
                "before": before,
                "after": floored[sid],
                "displaced": take,
                "kind": "floor_cut",
            })
    for sid in members:
        allocation[(phase, sid)] = floored[sid]


# ---------------------------------------------------------------- stage 11 and 12

def largest_remainder_split(total, keys, weights):
    """Split integer total across keys in proportion to positive weights."""
    weight_sum = sum(weights[k] for k in keys)
    if total <= 0 or weight_sum <= 0:
        return {k: 0 for k in keys}, {k: 0 for k in keys}
    base = {}
    rem = {}
    for k in keys:
        numerator = total * weights[k]
        base[k] = numerator // weight_sum
        rem[k] = numerator % weight_sum
    leftover = total - sum(base.values())
    for k in sorted(keys, key=lambda x: (-rem[x], x))[:leftover]:
        base[k] += 1
    return base, rem


# ---------------------------------------------------------------- packing

def build_microbatches(phase_order, members_by_phase, materialized, adjacency_limit,
                       max_seq_tokens):
    rows = []
    batch_id = 0
    for phase in phase_order:
        items = []
        for sid in members_by_phase[phase]:
            for ex in materialized.get(sid, []):
                if ex["materialized_tokens"] > 0:
                    items.append((sid, ex["example_id"], ex["materialized_tokens"]))
        current = []
        run_total = 0
        run_source = None
        run_len = 0

        def flush():
            nonlocal current, run_total, run_source, run_len, batch_id
            if not current:
                return
            batch_id += 1
            rows.append({
                "batch_id": "BATCH%04d" % batch_id,
                "phase": phase,
                "members": [ex for _, ex, _ in current],
                "seq_tokens": run_total,
            })
            current = []
            run_total = 0
            run_source = None
            run_len = 0

        for sid, ex, tok in items:
            if current and (run_total + tok > max_seq_tokens
                            or (sid == run_source and run_len >= adjacency_limit)):
                flush()
            if not current:
                run_source = sid
                run_len = 1
            else:
                if sid == run_source:
                    run_len += 1
                else:
                    run_source = sid
                    run_len = 1
            current.append((sid, ex, tok))
            run_total += tok
        flush()
    return rows


# ---------------------------------------------------------------- writing

class Writer:
    def __init__(self, out_dir):
        self.out_dir = out_dir
        self.buffers = {}

    def add_jsonl(self, name, rows):
        self.buffers[name] = "".join(sif(r) + "\n" for r in rows)

    def add_json(self, name, obj):
        self.buffers[name] = jif(obj)

    def write(self):
        for name, text in self.buffers.items():
            with open(os.path.join(self.out_dir, name), "w", encoding="utf-8",
                      newline="\n") as fh:
                fh.write(text)


# ---------------------------------------------------------------- main chain

def compile_plan(data_dir, out_dir):
    spec_dir = data_dir
    sources_rows = csv_dicts(os.path.join(spec_dir, "sources.csv"))
    quality_rows = csv_dicts(os.path.join(spec_dir, "quality.csv"))
    freshness_rows = csv_dicts(os.path.join(spec_dir, "freshness.csv"))
    examples_rows = csv_dicts(os.path.join(spec_dir, "examples.csv"))
    capacity_rows = csv_dicts(os.path.join(spec_dir, "capacity.csv"))
    carry_rows = csv_dicts(os.path.join(spec_dir, "carry_state.csv"))
    curriculum = json.load(open(os.path.join(spec_dir, "curriculum.json"),
                                encoding="utf-8"))
    unit_rules = json.load(open(os.path.join(spec_dir, "unit_rules.json"),
                               encoding="utf-8"))
    quality_policy = json.load(open(os.path.join(spec_dir, "quality_policy.json"),
                                    encoding="utf-8"))
    safety_policy = json.load(open(os.path.join(spec_dir, "safety_policy.json"),
                                   encoding="utf-8"))
    language_policy = json.load(open(os.path.join(spec_dir, "language_policy.json"),
                                     encoding="utf-8"))
    scoring_weights = json.load(open(os.path.join(spec_dir, "scoring_weights.json"),
                                     encoding="utf-8"))
    priority_order_cfg = json.load(open(os.path.join(spec_dir, "priority_order.json"),
                                        encoding="utf-8"))
    batch_rules = json.load(open(os.path.join(spec_dir, "batch_rules.json"),
                                 encoding="utf-8"))

    phases = list(curriculum.get("phases") or PHASES)
    # artifacts are emitted in canonical phase order regardless of manifest order
    phases = sorted(set(phases))
    edges = curriculum["edges"]
    scale = int(unit_rules["scale"])
    strata = safety_policy["strata"]
    languages = language_policy["languages"]
    stratum_rank = priority_order_cfg["stratum_displacement_rank"]
    adjacency_limit = int(batch_rules["adjacency_limit"])
    max_seq_tokens = int(batch_rules["max_seq_tokens"])

    # capacity and carry lookups by phase, independent of row order
    capacity_by_phase = {row["phase"]: row for row in capacity_rows}
    carry_by_phase = {row["phase"]: row for row in carry_rows}

    # stage 1
    canonical = stage1_canonicalize(sources_rows, unit_rules)
    unit_tie_exceptions = [{"code": "B_UNIT_ROUND_TIE", "ref": row["source_id"]}
                           for row in canonical if row["rounding_tie"]]
    canonical_tokens = {row["source_id"]: row["canonical_tokens"] for row in canonical}

    # stage 2
    eligibility = stage2_eligibility(sources_rows, canonical, quality_rows,
                                    quality_policy, language_policy, safety_policy)
    eligible_ids = [r["source_id"] for r in eligibility if r["eligible"]]

    # stage 3
    demand_rows, demand_scores = stage3_demand_scores(
        eligible_ids, sources_rows, canonical, quality_rows, freshness_rows,
        scoring_weights, language_policy)

    source_info = {row["source_id"]: row for row in sources_rows}

    # stage 4
    members_by_phase = {p: [] for p in phases}
    for row in canonical:
        sid = row["source_id"]
        if sid not in eligible_ids:
            continue
        domain = source_info[sid]["domain"]
        for phase in DOMAIN_PHASES.get(domain, ()):
            members_by_phase[phase].append(sid)
    for phase in phases:
        members_by_phase[phase].sort()

    proportional_rows = []
    targets = {}
    for phase in phases:
        members = members_by_phase[phase]
        scored = [s for s in members if demand_scores.get(s, 0) > 0]
        demand_sum = sum(demand_scores[s] for s in scored)
        budget = int(capacity_by_phase[phase]["budget_tokens"])
        for sid in members:
            score = demand_scores.get(sid, 0)
            share_num = score * budget
            share_den = demand_sum if demand_sum > 0 else 0
            target = div_round_half_even(share_num, share_den) if share_den > 0 else 0
            targets[(phase, sid)] = target
            proportional_rows.append({
                "phase": phase,
                "source_id": sid,
                "target_tokens": target,
                "share_num": share_num,
                "share_den": share_den,
            })

    # stage 5
    floor_rows = []
    floors = {}
    for phase in phases:
        for sid in members_by_phase[phase]:
            safety_floor = int(strata[source_info[sid]["safety_stratum"]]["floor_tokens"])
            language_floor = int(languages[source_info[sid]["language"]]["floor_tokens"])
            floor = max(safety_floor, language_floor)
            floors[(phase, sid)] = floor
            floor_rows.append({
                "phase": phase,
                "source_id": sid,
                "safety_floor": safety_floor,
                "language_floor": language_floor,
                "floor": floor,
            })

    # stages 7 and 8 first, so the carry chain has its topological order
    reach = build_closure(phases, edges)
    cycle_nodes = sorted(p for p in phases if p in reach[p])
    closure_rows = []
    for phase in phases:
        for req in sorted(reach[phase]):
            closure_rows.append({"phase": phase, "requires": req})
    removed_edges, banned = stage8_break_cycles(phases, edges)
    cycle_break_exceptions = [{"code": "B_CYCLE_BREAK", "ref": e["edge_id"]}
                              for e in removed_edges]
    order = topo_order(phases, edges, banned)

    # stage 6 in topological order, carrying credit and debt
    allocation = {}
    binding_trace = []
    carry_rows_out = []
    carry_credit = {p: int(carry_by_phase[p]["initial_credit"]) for p in phases}
    carry_debt = {p: int(carry_by_phase[p]["initial_debt"]) for p in phases}
    stage6_exceptions = []
    for idx, phase in enumerate(order):
        members = members_by_phase[phase]
        credit_in = carry_credit[phase]
        debt_in = carry_debt[phase]
        cap = int(capacity_by_phase[phase]["cap_tokens"])
        eff_cap = cap + credit_in - debt_in
        floored = {s: max(targets[(phase, s)], floors[(phase, s)]) for s in members}
        floor_bound = any(floored[s] > targets[(phase, s)] for s in members)
        total_floors = sum(floors[(phase, s)] for s in members)
        floored_total = sum(floored.values())
        # displacement only exists when there are tokens to remove
        excess = floored_total - eff_cap if floored_total > 0 else 0
        floor_over_cap = excess > 0 and total_floors > eff_cap
        if excess > 0:
            run_binding(phase, members, floored, floors, eff_cap, source_info,
                        stratum_rank, allocation, binding_trace)
        else:
            for s in members:
                allocation[(phase, s)] = floored[s]
        if floor_bound:
            stage6_exceptions.append({"code": "B_FLOOR_BIND", "ref": phase})
        if excess > 0:
            stage6_exceptions.append({"code": "B_CAP_BIND", "ref": phase})
            if floor_over_cap:
                stage6_exceptions.append(
                    {"code": "B_FLOOR_OVER_CAP", "ref": phase})
        allocated_sum = sum(allocation[(phase, s)] for s in members)
        credit_out = eff_cap - allocated_sum
        debt_out = sum(max(0, floors[(phase, s)] - allocation[(phase, s)])
                       for s in members)
        carry_rows_out.append({
            "phase": phase,
            "credit_in": credit_in,
            "debt_in": debt_in,
            "credit_out": credit_out,
            "debt_out": debt_out,
        })
        if idx + 1 < len(order):
            nxt = order[idx + 1]
            carry_credit[nxt] += credit_out
            carry_debt[nxt] += debt_out
    # stage 8 precedence overturns, judged on the stage 6 matrix
    overturns = []
    for phase in phases:
        for sid in members_by_phase[phase]:
            if allocation.get((phase, sid), 0) <= 0:
                continue
            for req in sorted(reach[phase]):
                if req == phase:
                    continue
                if sid in members_by_phase.get(req, ()):
                    if allocation.get((req, sid), 0) == 0:
                        overturns.append({
                            "phase": phase,
                            "prereq_phase": req,
                            "source_id": sid,
                        })
    overturn_exceptions = [{"code": "B_PRECEDENCE_OVERTURN", "ref": row["phase"]}
                           for row in overturns]
    carry_debt_exceptions = [{"code": "B_CARRY_DEBT", "ref": row["phase"]}
                             for row in carry_rows_out if row["debt_out"] > 0]

    pre_reconciliation = [{"phase": p, "source_id": s,
                           "tokens": allocation[(p, s)]}
                          for p in phases for s in members_by_phase[p]]

    # stage 10 global reconciliation, later phase in topological order first
    final = dict(allocation)
    claw_rows = []
    clawback_exceptions = []
    for sid in [r["source_id"] for r in canonical]:
        source_total = sum(final[(p, sid)] for p in phases if (p, sid) in final)
        supply = canonical_tokens[sid]
        if source_total > supply:
            excess = source_total - supply
            for phase in reversed(order):
                if excess <= 0:
                    break
                if (phase, sid) not in final:
                    continue
                cur = final[(phase, sid)]
                if cur <= 0:
                    claw = 0
                else:
                    claw = min(cur, excess)
                if claw > 0:
                    final[(phase, sid)] = cur - claw
                    excess -= claw
                    claw_rows.append({
                        "source_id": sid,
                        "phase": phase,
                        "before": cur,
                        "after": cur - claw,
                        "clawed": claw,
                    })
            clawback_exceptions.append({"code": "B_GLOBAL_CLAWBACK", "ref": sid})

    final_allocation_rows = [{"phase": p, "source_id": s, "tokens": final[(p, s)]}
                             for p in phases for s in members_by_phase[p]]

    # stage 11 rational example weights and stage 12 materialization
    examples_by_source = {row["source_id"]: [] for row in canonical}
    for row in examples_rows:
        examples_by_source.setdefault(row["source_id"], []).append(row)
    for sid in examples_by_source:
        examples_by_source[sid].sort(key=lambda r: r["example_id"])

    rational_rows = []
    fixed_rows = []
    residual_rows = []
    materialized_by_source = {}
    empty_group_exceptions = []
    source_total_final = {row["source_id"]: 0 for row in canonical}
    for row in final_allocation_rows:
        source_total_final[row["source_id"]] += row["tokens"]
    for row in canonical:
        sid = row["source_id"]
        total = source_total_final[sid]
        ex_rows = examples_by_source.get(sid, [])
        group_den = sum(int(r["token_length"]) for r in ex_rows)
        if ex_rows and group_den == 0:
            empty_group_exceptions.append({"code": "B_EMPTY_GROUP", "ref": sid})
        keys = [r["example_id"] for r in ex_rows]
        weights = {r["example_id"]: int(r["token_length"]) for r in ex_rows}
        base, rem = largest_remainder_split(total, keys, weights)
        if total > 0 and group_den > 0:
            for r in ex_rows:
                wnum = total * int(r["token_length"])
                wden = group_den
                g = Fraction(wnum, wden)
                rational_rows.append({
                    "example_id": r["example_id"],
                    "source_id": sid,
                    "w_num": g.numerator,
                    "w_den": g.denominator,
                })
        mats = []
        for r in ex_rows:
            fixed_rows.append({
                "example_id": r["example_id"],
                "source_id": sid,
                "materialized_tokens": base[r["example_id"]],
            })
            mats.append({"example_id": r["example_id"],
                         "materialized_tokens": base[r["example_id"]]})
            quotient = (total * int(r["token_length"])) // group_den if group_den else 0
            if base[r["example_id"]] == quotient + 1:
                residual_rows.append({
                    "example_id": r["example_id"],
                    "source_id": sid,
                    "base_tokens": quotient,
                    "residual_unit": 1,
                })
        materialized_by_source[sid] = mats

    # stage 13 microbatches, member sources with a positive final allocation
    members_positive = {p: [s for s in members_by_phase[p]
                            if final.get((p, s), 0) > 0]
                        for p in phases}
    batch_rows = build_microbatches(phases, members_positive, materialized_by_source,
                                    adjacency_limit, max_seq_tokens)

    # summaries
    total_tokens = sum(row["tokens"] for row in final_allocation_rows)
    lang_tokens = {lang: 0 for lang in sorted(languages)}
    for row in final_allocation_rows:
        lang = source_info[row["source_id"]]["language"]
        lang_tokens[lang] = lang_tokens.get(lang, 0) + row["tokens"]
    lang_rows = []
    for lang in sorted(languages):
        min_required = div_round_half_even(
            int(languages[lang]["min_share_bp"]) * total_tokens, scale)
        max_allowed = div_round_half_even(
            int(languages[lang]["max_share_bp"]) * total_tokens, scale)
        lang_rows.append({
            "language": lang,
            "tokens": lang_tokens.get(lang, 0),
            "min_required": min_required,
            "max_allowed": max_allowed,
        })

    stratum_tokens = {name: 0 for name in sorted(strata)}
    for row in final_allocation_rows:
        st = source_info[row["source_id"]]["safety_stratum"]
        stratum_tokens[st] = stratum_tokens.get(st, 0) + row["tokens"]
    safety_rows = []
    for name in sorted(strata):
        safety_rows.append({
            "stratum": name,
            "tokens": stratum_tokens.get(name, 0),
            "floor": int(strata[name]["floor_tokens"]),
        })

    source_summary_rows = [{
        "source_id": row["source_id"],
        "tokens": source_total_final[row["source_id"]],
        "canonical_tokens": row["canonical_tokens"],
    } for row in canonical]

    phase_total_rows = []
    for phase in phases:
        phase_total_rows.append({
            "phase": phase,
            "allocated": sum(final[(phase, s)] for s in members_by_phase[phase]),
            "cap": int(capacity_by_phase[phase]["cap_tokens"]),
            "budget": int(capacity_by_phase[phase]["budget_tokens"]),
        })

    # invariants
    inv_phase_within_cap = True
    inv_source_within_supply = True
    inv_materialized_sums_match = True
    inv_batches_respect_seq_cap = True
    for phase in phases:
        phase_alloc = sum(final[(phase, s)] for s in members_by_phase[phase])
        cap = int(capacity_by_phase[phase]["cap_tokens"])
        credit_in = row_credit_in(carry_rows_out, phase)
        debt_in = row_debt_in(carry_rows_out, phase)
        if phase_alloc > cap + credit_in - debt_in:
            inv_phase_within_cap = False
    for row in canonical:
        if source_total_final[row["source_id"]] > row["canonical_tokens"]:
            inv_source_within_supply = False
    for row in canonical:
        sid = row["source_id"]
        ex_rows = examples_by_source.get(sid, [])
        group_den = sum(int(r["token_length"]) for r in ex_rows)
        if ex_rows and group_den > 0:
            if sum(m["materialized_tokens"] for m in materialized_by_source[sid]) != \
                    source_total_final[sid]:
                inv_materialized_sums_match = False
    for row in batch_rows:
        if row["seq_tokens"] > max_seq_tokens and len(row["members"]) != 1:
            inv_batches_respect_seq_cap = False

    invariant_rows = [
        {"invariant": "phase_within_cap", "holds": inv_phase_within_cap},
        {"invariant": "source_within_supply", "holds": inv_source_within_supply},
        {"invariant": "materialized_sums_match", "holds": inv_materialized_sums_match},
        {"invariant": "batches_respect_seq_cap", "holds": inv_batches_respect_seq_cap},
    ]

    objective = [total_tokens, len(claw_rows), len(removed_edges),
                 len(binding_trace), len(batch_rows)]
    objective_order = ["total_tokens", "global_clawbacks", "cycle_edges_removed",
                       "capacity_displacements", "microbatches"]

    # exception trace, in the order the stages activate the branches
    exceptions = []
    exceptions.extend(unit_tie_exceptions)
    exceptions.extend(stage6_exceptions)
    exceptions.extend(cycle_break_exceptions)
    exceptions.extend(overturn_exceptions)
    exceptions.extend(carry_debt_exceptions)
    exceptions.extend(clawback_exceptions)
    exceptions.extend(empty_group_exceptions)

    # write everything
    writer = Writer(out_dir)
    writer.add_jsonl("canonical_sources.jsonl", canonical)
    writer.add_jsonl("eligibility_ledger.jsonl", eligibility)
    writer.add_jsonl("demand_score_ledger.jsonl", demand_rows)
    writer.add_jsonl("phase_membership.jsonl",
                     [{"phase": p, "source_id": s} for p in phases
                      for s in members_by_phase[p]])
    writer.add_jsonl("proportional_allocation.jsonl", proportional_rows)
    writer.add_jsonl("protected_floor_ledger.jsonl", floor_rows)
    writer.add_jsonl("capacity_binding_trace.jsonl", binding_trace)
    writer.add_jsonl("precedence_closure.jsonl", closure_rows)
    writer.add_jsonl("cycle_report.jsonl",
                     [{"cycle_nodes": cycle_nodes}] if cycle_nodes else [])
    writer.add_jsonl("cycle_break_decisions.jsonl",
                     [{"edge_id": e["edge_id"], "from": e["from"], "to": e["to"],
                       "hardness": e["hardness"]} for e in removed_edges])
    writer.add_jsonl("precedence_overturn_trace.jsonl", overturns)
    writer.add_jsonl("carry_state_ledger.jsonl", carry_rows_out)
    writer.add_jsonl("pre_reconciliation_ledger.jsonl", pre_reconciliation)
    writer.add_jsonl("global_reconciliation_trace.jsonl", claw_rows)
    writer.add_jsonl("final_allocation.jsonl", final_allocation_rows)
    writer.add_jsonl("language_allocation_summary.jsonl", lang_rows)
    writer.add_jsonl("safety_allocation_summary.jsonl",
                     sorted(safety_rows, key=lambda r: r["stratum"]))
    writer.add_jsonl("source_allocation_summary.jsonl", source_summary_rows)
    writer.add_jsonl("rational_example_weights.jsonl", rational_rows)
    writer.add_jsonl("fixed_point_weights.jsonl", fixed_rows)
    writer.add_jsonl("rounding_residual_ledger.jsonl", residual_rows)
    writer.add_jsonl("microbatch_manifest.jsonl", batch_rows)
    writer.add_jsonl("phase_token_totals.jsonl", phase_total_rows)
    writer.add_jsonl("invariant_report.jsonl", invariant_rows)
    writer.add_jsonl("exception_trace.jsonl", exceptions)
    writer.add_json("objective_tuple.json",
                    {"objective": objective, "order": objective_order})

    writer.write()

    # program copy, then digests over the final bytes on disk
    program_path = os.path.abspath(__file__)
    target = os.path.join(out_dir, "plan.py")
    if os.path.abspath(target) != program_path:
        shutil.copyfile(program_path, target)
    manifest = {}
    derived = sorted(list(writer.buffers.keys()))
    for name in derived + ["plan.py"]:
        with open(os.path.join(out_dir, name), "rb") as fh:
            manifest[name] = sha256_bytes(fh.read())
    with open(os.path.join(out_dir, "integrity_manifest.json"), "w",
              encoding="utf-8", newline="\n") as fh:
        fh.write(jif(manifest))

    chain = INITIAL_CHAIN
    for name in sorted(manifest):
        chain = sha256_bytes((chain + manifest[name]).encode("utf-8"))

    overturn_phases = sorted({r["phase"] for r in overturns})
    final_state = {
        "chain": chain,
        "total_tokens": total_tokens,
        "global_clawbacks": len(claw_rows),
        "capacity_displacements": len(binding_trace),
        "cycle_edges_removed": len(removed_edges),
        "precedence_overturns": len(overturns),
        "microbatches": len(batch_rows),
        "examples_placed": sum(len(r["members"]) for r in batch_rows),
        "quiet_branches": len(exceptions),
        "objective": objective,
        "invariants_hold": all(r["holds"] for r in invariant_rows),
    }
    with open(os.path.join(out_dir, "final_state.json"), "w", encoding="utf-8",
              newline="\n") as fh:
        fh.write(jif(final_state))

    return {
        "final_state": final_state,
        "total_tokens": total_tokens,
        "clawbacks": len(claw_rows),
        "removed_edges": [e["edge_id"] for e in removed_edges],
        "displacements": len(binding_trace),
        "batches": len(batch_rows),
        "exceptions": exceptions,
    }


def row_credit_in(rows, phase):
    for row in rows:
        if row["phase"] == phase:
            return row["credit_in"]
    return 0


def row_debt_in(rows, phase):
    for row in rows:
        if row["phase"] == phase:
            return row["debt_in"]
    return 0


def main(argv):
    if len(argv) != 3:
        sys.stderr.write("usage: plan.py <DATA_DIR> <OUT_DIR>\n")
        return 2
    data_dir = argv[1]
    out_dir = argv[2]
    if not os.path.isdir(data_dir):
        sys.stderr.write("data directory not found: %s\n" % data_dir)
        return 1
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)
    result = compile_plan(data_dir, out_dir)
    fs = result["final_state"]
    sys.stdout.write(
        "total_tokens=%d clawbacks=%d cycle_edges_removed=%d "
        "capacity_displacements=%d microbatches=%d\n"
        % (fs["total_tokens"], fs["global_clawbacks"], fs["cycle_edges_removed"],
           fs["capacity_displacements"], fs["microbatches"]))
    sys.stdout.write("objective=%s\n" % (result["final_state"]["objective"],))
    sys.stdout.write("chain=%s\n" % fs["chain"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
