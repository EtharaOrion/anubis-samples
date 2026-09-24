#!/usr/bin/env python3
"""TrainMix data mixture compiler.

Derives the weighted training plan, phase allocations and batch manifests from the
source manifests, policy constraints, curriculum dependencies and token budgets in a
data directory.  Every number is an integer or an exact rational; no floating point
value is produced, compared or serialized anywhere.

Usage:  python3 plan.py <DATA_DIR> <OUT_DIR>
"""

import csv
import hashlib
import json
import os
import sys
from fractions import Fraction

# Fixed domain to phase map from the compiler specification, stage 4.
DOMAIN_PHASES = {
    "maint": ("PH1",),
    "manual": ("PH1", "PH2"),
    "incident": ("PH2",),
    "qa": ("PH2", "PH3"),
    "translated": ("PH1", "PH3"),
    "synthetic": ("PH3",),
    "reg": ("PH2",),
}

OBJECTIVE_ORDER = [
    "total_tokens",
    "global_clawbacks",
    "cycle_edges_removed",
    "capacity_displacements",
    "microbatches",
]


# --------------------------------------------------------------------------- #
# exact arithmetic
# --------------------------------------------------------------------------- #

def round_half_even(p, d):
    """Round the rational p/d to an integer, halves to the even quotient."""
    q, r = divmod(p, d)
    if 2 * r > d:
        return q + 1
    if 2 * r == d and q % 2 == 1:
        return q + 1
    return q


# --------------------------------------------------------------------------- #
# input helpers
# --------------------------------------------------------------------------- #

def load_json(data_dir, name, default):
    path = os.path.join(data_dir, name)
    if not os.path.isfile(path):
        return default
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_csv(data_dir, name):
    path = os.path.join(data_dir, name)
    if not os.path.isfile(path):
        return []
    with open(path, "r", newline="", encoding="utf-8") as fh:
        return [dict(row) for row in csv.DictReader(fh)]


def to_int(value, default=0):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# serialization
# --------------------------------------------------------------------------- #

def jsonl_bytes(keys, rows):
    lines = [
        json.dumps(
            {k: row[k] for k in keys},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        for row in rows
    ]
    if not lines:
        return b""
    return ("\n".join(lines) + "\n").encode("utf-8")


def json_bytes(obj):
    return (
        json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    ).encode("utf-8")


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- #
# graph helpers
# --------------------------------------------------------------------------- #

def reachability(nodes, edges):
    """Transitive closure as a map node -> set of nodes reachable by a non empty path."""
    adj = {n: set() for n in nodes}
    for e in edges:
        a, b = e["from"], e["to"]
        if a in adj and b in adj:
            adj[a].add(b)
    closure = {}
    for n in nodes:
        seen = set()
        stack = list(adj[n])
        while stack:
            m = stack.pop()
            if m in seen:
                continue
            seen.add(m)
            stack.extend(adj[m])
        closure[n] = seen
    return adj, closure


def topological_order(nodes, edges):
    """Topological order by Kahn's algorithm, ready ties by ascending phase name."""
    succ = {n: set() for n in nodes}
    indeg = {n: 0 for n in nodes}
    for e in edges:
        a, b = e["from"], e["to"]
        if a in succ and b in succ and b not in succ[a]:
            succ[a].add(b)
            indeg[b] += 1
    ready = sorted(n for n in nodes if indeg[n] == 0)
    order = []
    while ready:
        node = ready.pop(0)
        order.append(node)
        changed = False
        for nxt in sorted(succ[node]):
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                ready.append(nxt)
                changed = True
        if changed:
            ready.sort()
    return order


def largest_remainder_split(total, weights):
    """Split an integer total across keys in proportion to positive weights."""
    keys = [k for k, w in weights.items() if w > 0]
    if total <= 0 or not keys:
        return {k: 0 for k in weights}, {}
    wsum = sum(weights[k] for k in keys)
    if wsum <= 0:
        return {k: 0 for k in weights}, {}
    base = {}
    remainders = []
    assigned = 0
    for k in keys:
        q, r = divmod(total * weights[k], wsum)
        base[k] = q
        assigned += q
        remainders.append((r, k))
    leftover = total - assigned
    order = sorted(remainders, key=lambda item: (-item[0], item[1]))
    receiver = set()
    for i in range(leftover):
        receiver.add(order[i % len(order)][1])
    result = dict(base)
    for k in weights:
        result.setdefault(k, 0)
    for k in receiver:
        result[k] += 1
    residuals = {k: base[k] for k in receiver}
    return result, residuals


# --------------------------------------------------------------------------- #
# compiler
# --------------------------------------------------------------------------- #

def compile_plan(data_dir, out_dir):
    unit_rules = load_json(data_dir, "unit_rules.json", {})
    quality_policy = load_json(data_dir, "quality_policy.json", {})
    language_policy = load_json(data_dir, "language_policy.json", {})
    safety_policy = load_json(data_dir, "safety_policy.json", {})
    priority_order = load_json(data_dir, "priority_order.json", {})
    scoring_weights = load_json(data_dir, "scoring_weights.json", {})
    batch_rules = load_json(data_dir, "batch_rules.json", {})
    curriculum = load_json(data_dir, "curriculum.json", {})

    scale = to_int(unit_rules.get("scale", 10000), 10000)
    units = unit_rules.get("units", {}) or {}
    scoring_scale = to_int(scoring_weights.get("scale", scale), scale)
    half_life = to_int(scoring_weights.get("half_life_days", 0), 0)
    priority_weights = {
        str(k): to_int(v)
        for k, v in (scoring_weights.get("priority_weights", {}) or {}).items()
    }
    min_quality = quality_policy.get("min_quality", {}) or {}
    mq_num = to_int(min_quality.get("num", 0), 0)
    mq_den = to_int(min_quality.get("den", 1), 1)
    blocked_domains = set(quality_policy.get("blocked_domains", []) or [])
    languages = language_policy.get("languages", {}) or {}
    strata = safety_policy.get("strata", {}) or {}
    displacement_rank = {
        str(k): to_int(v)
        for k, v in (priority_order.get("stratum_displacement_rank", {}) or {}).items()
    }
    max_seq_tokens = to_int(batch_rules.get("max_seq_tokens", 0), 0)
    adjacency_limit = to_int(batch_rules.get("adjacency_limit", 0), 0)

    source_rows = load_csv(data_dir, "sources.csv")
    quality_rows = load_csv(data_dir, "quality.csv")
    freshness_rows = load_csv(data_dir, "freshness.csv")
    example_rows = load_csv(data_dir, "examples.csv")
    capacity_rows = load_csv(data_dir, "capacity.csv")
    carry_rows = load_csv(data_dir, "carry_state.csv")

    quality = {str(r.get("source_id", "")).strip(): r for r in quality_rows}
    freshness = {str(r.get("source_id", "")).strip(): r for r in freshness_rows}

    budget = {}
    cap = {}
    for r in capacity_rows:
        ph = str(r.get("phase", "")).strip()
        budget[ph] = to_int(r.get("budget_tokens"), 0)
        cap[ph] = to_int(r.get("cap_tokens"), 0)
    initial_credit = {}
    initial_debt = {}
    for r in carry_rows:
        ph = str(r.get("phase", "")).strip()
        initial_credit[ph] = to_int(r.get("initial_credit"), 0)
        initial_debt[ph] = to_int(r.get("initial_debt"), 0)

    phase_set = set()
    for ph in curriculum.get("phases", []) or []:
        phase_set.add(str(ph))
    for ph in budget:
        phase_set.add(ph)
    if not phase_set:
        phase_set = {"PH1", "PH2", "PH3"}
    phase_order = sorted(phase_set)

    sources = []
    for r in source_rows:
        sources.append(
            {
                "source_id": str(r.get("source_id", "")).strip(),
                "language": str(r.get("language", "")).strip(),
                "domain": str(r.get("domain", "")).strip(),
                "source_class": str(r.get("source_class", "")).strip(),
                "safety_stratum": str(r.get("safety_stratum", "")).strip(),
                "priority": str(r.get("priority", "")).strip(),
                "raw_amount": to_int(r.get("raw_amount"), 0),
                "raw_unit": str(r.get("raw_unit", "")).strip(),
            }
        )
    seen = set()
    canonical = []
    for s in sources:
        if s["source_id"] in seen:
            continue
        seen.add(s["source_id"])
        canonical.append(s)

    examples_by_source = {}
    for r in example_rows:
        ex_id = str(r.get("example_id", "")).strip()
        src = str(r.get("source_id", "")).strip()
        examples_by_source.setdefault(src, []).append(
            {"example_id": ex_id, "token_length": to_int(r.get("token_length"), 0)}
        )
    for src in examples_by_source:
        examples_by_source[src].sort(key=lambda e: e["example_id"])

    exceptions = []  # (stage, seq, code, ref)

    def branch(stage, code, ref):
        exceptions.append((stage, len(exceptions), code, ref))

    # ---------------------------------------------------------------- stage 1
    for s in canonical:
        unit = units.get(s["raw_unit"], {}) or {}
        num = to_int(unit.get("num", 1), 1)
        den = to_int(unit.get("den", 1), 1)
        if den <= 0:
            den = 1
        q, r = divmod(s["raw_amount"] * num, den)
        tie = 2 * r == den
        if 2 * r > den or (tie and q % 2 == 1):
            q += 1
        s["canonical_tokens"] = q
        s["rounding_tie"] = tie
        if tie:
            branch(1, "B_UNIT_ROUND_TIE", s["source_id"])

    # ---------------------------------------------------------------- stage 2
    for s in canonical:
        qrow = quality.get(s["source_id"], {}) or {}
        q_num = to_int(qrow.get("q_num"), 0)
        q_den = to_int(qrow.get("q_den"), 0)
        s["q_num"] = q_num
        s["q_den"] = q_den
        code = "E_OK"
        if q_den == 0 or q_num * mq_den < mq_num * q_den:
            code = "E_LOW_QUALITY"
        elif s["language"] not in languages:
            code = "E_LANG_EXCLUDED"
        elif s["safety_stratum"] not in strata:
            code = "E_STRATUM_UNKNOWN"
        elif s["domain"] in blocked_domains:
            code = "E_DOMAIN_BLOCKED"
        elif s["canonical_tokens"] <= 0:
            code = "E_EMPTY_TOKENS"
        s["eligible"] = code == "E_OK"
        s["code"] = code

    eligible = [s for s in canonical if s["eligible"]]

    # ---------------------------------------------------------------- stage 3
    for s in eligible:
        q_den = s["q_den"]
        if q_den <= 0:
            q_den = 1
        quality_bp = round_half_even(s["q_num"] * scoring_scale, q_den)
        age = to_int(freshness.get(s["source_id"], {}).get("age_days"), 0)
        fden = half_life + age
        if fden <= 0:
            fresh_bp = 0
        else:
            fresh_bp = round_half_even(half_life * scoring_scale, fden)
        priority_factor = priority_weights.get(s["priority"], 0)
        lang_balance_bp = to_int(
            (languages.get(s["language"], {}) or {}).get("balance_bp", 0), 0
        )
        s1 = round_half_even(s["canonical_tokens"] * quality_bp, scoring_scale)
        s2 = round_half_even(s1 * fresh_bp, scoring_scale)
        s3 = s2 * priority_factor
        s["demand_score"] = round_half_even(s3 * lang_balance_bp, scoring_scale)
        s["quality_bp"] = quality_bp
        s["fresh_bp"] = fresh_bp
        s["priority_factor"] = priority_factor
        s["lang_balance_bp"] = lang_balance_bp

    # ---------------------------------------------------------------- stage 4
    membership = {ph: [] for ph in phase_order}
    for s in eligible:
        for ph in DOMAIN_PHASES.get(s["domain"], ()):
            if ph in membership:
                membership[ph].append(s)
    for ph in phase_order:
        membership[ph].sort(key=lambda s: s["source_id"])

    targets = {}
    for ph in phase_order:
        members = membership[ph]
        demand_sum = sum(s["demand_score"] for s in members if s["demand_score"] > 0)
        ph_budget = budget.get(ph, 0)
        for s in members:
            share_num = s["demand_score"] * ph_budget
            share_den = demand_sum
            if s["demand_score"] <= 0 or demand_sum <= 0:
                target = 0
                share_num = 0
                share_den = demand_sum
            else:
                target = round_half_even(share_num, share_den)
            targets[(ph, s["source_id"])] = {
                "target_tokens": target,
                "share_num": share_num,
                "share_den": share_den,
            }

    # ---------------------------------------------------------------- stage 5
    floors = {}
    for ph in phase_order:
        for s in membership[ph]:
            safety_floor = to_int(
                (strata.get(s["safety_stratum"], {}) or {}).get("floor_tokens", 0), 0
            )
            language_floor = to_int(
                (languages.get(s["language"], {}) or {}).get("floor_tokens", 0), 0
            )
            floors[(ph, s["source_id"])] = {
                "safety_floor": safety_floor,
                "language_floor": language_floor,
                "floor": max(safety_floor, language_floor),
            }
        if any(
            floors[(ph, s["source_id"])]["floor"]
            > targets[(ph, s["source_id"])]["target_tokens"]
            for s in membership[ph]
        ):
            branch(5, "B_FLOOR_BIND", ph)

    # ------------------------------------------------------- stages 7 and 8
    edges = []
    for e in curriculum.get("edges", []) or []:
        eid = str(e.get("edge_id", "")).strip()
        frm = str(e.get("from", "")).strip()
        to = str(e.get("to", "")).strip()
        hardness = str(e.get("hardness", "")).strip()
        edges.append({"edge_id": eid, "from": frm, "to": to, "hardness": hardness})
    graph_nodes = set(phase_set)
    for e in edges:
        graph_nodes.add(e["from"])
        graph_nodes.add(e["to"])

    _, closure = reachability(graph_nodes, edges)
    cycle_nodes = sorted(n for n in graph_nodes if n in closure.get(n, set()))
    removed = None
    if cycle_nodes:
        hardness_rank = {"soft": 0, "hard": 1}
        candidates = [
            e for e in edges if e["from"] in cycle_nodes and e["to"] in cycle_nodes
        ]
        candidates.sort(
            key=lambda e: (
                hardness_rank.get(e["hardness"], 2),
                e["from"],
                e["to"],
                e["edge_id"],
            )
        )
        if candidates:
            removed = candidates[0]
    surviving = [e for e in edges if e is not removed]
    topo = topological_order(graph_nodes, surviving)
    if removed is not None:
        branch(8, "B_CYCLE_BREAK", removed["edge_id"])
    cycle_edges_removed = 1 if removed is not None else 0

    # ------------------------------------------------- stages 6 and 9 (carry)
    allocation = {}
    binding_trace = []
    carry_ledger = []
    effective_cap = {}
    pre_reconcile = {}
    for idx, ph in enumerate(topo):
        members = membership.get(ph, [])
        credit_in = initial_credit.get(ph, 0) + (
            carry_ledger[idx - 1]["credit_out"] if idx > 0 else 0
        )
        debt_in = initial_debt.get(ph, 0) + (
            carry_ledger[idx - 1]["debt_out"] if idx > 0 else 0
        )
        eff_cap = cap.get(ph, 0) + credit_in - debt_in
        effective_cap[ph] = eff_cap

        current = {}
        for s in members:
            key = (ph, s["source_id"])
            current[s["source_id"]] = max(
                targets[key]["target_tokens"], floors[key]["floor"]
            )

        total = sum(current.values())
        if total > eff_cap:
            branch(6, "B_CAP_BIND", ph)
            order = sorted(
                members,
                key=lambda s: (
                    displacement_rank.get(s["safety_stratum"], 0),
                    -to_int(s["priority"], 0),
                    s["source_id"],
                ),
            )
            excess = total - eff_cap
            for s in order:
                if excess <= 0:
                    break
                sid = s["source_id"]
                floor = floors[(ph, sid)]["floor"]
                room = current[sid] - floor
                if room > 0:
                    take = room if room <= excess else excess
                    before = current[sid]
                    current[sid] -= take
                    excess -= take
                    binding_trace.append(
                        {
                            "phase": ph,
                            "source_id": sid,
                            "before": before,
                            "after": current[sid],
                            "displaced": take,
                            "kind": "above_floor",
                        }
                    )
            if excess > 0:
                branch(6, "B_FLOOR_OVER_CAP", ph)
                for s in order:
                    if excess <= 0:
                        break
                    sid = s["source_id"]
                    take = current[sid] if current[sid] <= excess else excess
                    if take > 0:
                        before = current[sid]
                        current[sid] -= take
                        excess -= take
                        binding_trace.append(
                            {
                                "phase": ph,
                                "source_id": sid,
                                "before": before,
                                "after": current[sid],
                                "displaced": take,
                                "kind": "floor_cut",
                            }
                        )

        for s in members:
            allocation[(ph, s["source_id"])] = current[s["source_id"]]
            pre_reconcile[(ph, s["source_id"])] = current[s["source_id"]]

        allocated_total = sum(current.values())
        unused = eff_cap - allocated_total
        credit_out = unused if unused > 0 else 0
        debt_out = sum(
            max(0, floors[(ph, s["source_id"])]["floor"] - current[s["source_id"]])
            for s in members
        )
        if debt_out > 0:
            branch(9, "B_CARRY_DEBT", ph)
        carry_ledger.append(
            {
                "phase": ph,
                "credit_in": credit_in,
                "debt_in": debt_in,
                "credit_out": credit_out,
                "debt_out": debt_out,
            }
        )

    # ------------------------------------------------------- stage 8 (overturn)
    overturns = []
    for ph in phase_order:
        for prereq in sorted(closure.get(ph, set())):
            if prereq == ph:
                continue
            for s in membership.get(ph, []):
                sid = s["source_id"]
                if (ph, sid) not in allocation or (prereq, sid) not in allocation:
                    continue
                if allocation[(prereq, sid)] == 0 and allocation[(ph, sid)] > 0:
                    overturns.append(
                        {"phase": ph, "prereq_phase": prereq, "source_id": sid}
                    )
                    branch(8, "B_PRECEDENCE_OVERTURN", ph)

    # -------------------------------------------------------------- stage 10
    clawback_trace = []
    for s in canonical:
        sid = s["source_id"]
        phases_of = [ph for ph in topo if (ph, sid) in allocation]
        total_alloc = sum(allocation[(ph, sid)] for ph in phases_of)
        supply = s["canonical_tokens"]
        if total_alloc > supply:
            excess = total_alloc - supply
            for ph in reversed(topo):
                if excess <= 0:
                    break
                if (ph, sid) not in allocation or allocation[(ph, sid)] <= 0:
                    continue
                before = allocation[(ph, sid)]
                take = before if before <= excess else excess
                allocation[(ph, sid)] = before - take
                excess -= take
                clawback_trace.append(
                    {
                        "source_id": sid,
                        "phase": ph,
                        "before": before,
                        "after": allocation[(ph, sid)],
                        "clawed": take,
                    }
                )
            branch(10, "B_GLOBAL_CLAWBACK", sid)

    # ------------------------------------------------------- stages 11 and 12
    rational_rows = []
    fixed_rows = []
    residual_rows = []
    materialized = {}
    for s in canonical:
        sid = s["source_id"]
        exs = examples_by_source.get(sid, [])
        token_total = sum(allocation.get((ph, sid), 0) for ph in phase_order)
        s["token_total"] = token_total
        group_den = sum(e["token_length"] for e in exs)
        s["group_den"] = group_den
        if exs and group_den == 0:
            branch(11, "B_EMPTY_GROUP", sid)
        weights = {e["example_id"]: e["token_length"] for e in exs}
        split, residuals = largest_remainder_split(token_total, weights)
        for e in exs:
            eid = e["example_id"]
            mat = split.get(eid, 0)
            materialized[(sid, eid)] = mat
            fixed_rows.append(
                {
                    "example_id": eid,
                    "source_id": sid,
                    "materialized_tokens": mat,
                }
            )
            if eid in residuals:
                residual_rows.append(
                    {
                        "example_id": eid,
                        "source_id": sid,
                        "base_tokens": residuals[eid],
                        "residual_unit": 1,
                    }
                )
            if group_den > 0:
                frac = Fraction(token_total * e["token_length"], group_den)
                rational_rows.append(
                    {
                        "example_id": eid,
                        "source_id": sid,
                        "w_num": frac.numerator,
                        "w_den": frac.denominator,
                    }
                )

    # ------------------------------------------------------------- stage 13
    micro_rows = []
    batch_counter = 0
    for ph in phase_order:
        items = []
        for s in membership.get(ph, []):
            if allocation.get((ph, s["source_id"]), 0) <= 0:
                continue
            for e in examples_by_source.get(s["source_id"], []):
                if materialized.get((s["source_id"], e["example_id"]), 0) > 0:
                    items.append(
                        (
                            s["source_id"],
                            e["example_id"],
                            materialized[(s["source_id"], e["example_id"])],
                        )
                    )
        current = []
        cur_tokens = 0
        cur_run = 0
        last_src = None
        to_flush = None

        for sid, eid, tok in items:
            new_batch = False
            if current:
                if cur_tokens + tok > max_seq_tokens:
                    new_batch = True
                elif sid == last_src and cur_run + 1 > adjacency_limit:
                    new_batch = True
            if new_batch:
                to_flush = (current, cur_tokens)
            else:
                to_flush = None
            if to_flush is not None:
                batch_counter += 1
                micro_rows.append(
                    {
                        "batch_id": "BATCH%04d" % batch_counter,
                        "phase": ph,
                        "members": list(to_flush[0]),
                        "seq_tokens": to_flush[1],
                    }
                )
                current = []
                cur_tokens = 0
                cur_run = 0
                last_src = None
            if sid == last_src:
                cur_run += 1
            else:
                cur_run = 1
                last_src = sid
            current.append(eid)
            cur_tokens += tok
        if current:
            batch_counter += 1
            micro_rows.append(
                {
                    "batch_id": "BATCH%04d" % batch_counter,
                    "phase": ph,
                    "members": list(current),
                    "seq_tokens": cur_tokens,
                }
            )

    # ----------------------------------------------------------- stage 14
    total_tokens = sum(allocation.values())

    language_rows = []
    for lang in sorted(languages):
        pol = languages[lang] or {}
        tokens = sum(s["token_total"] for s in canonical if s["language"] == lang)
        min_required = round_half_even(
            to_int(pol.get("min_share_bp"), 0) * total_tokens, scale
        )
        max_allowed = round_half_even(
            to_int(pol.get("max_share_bp"), 0) * total_tokens, scale
        )
        language_rows.append(
            {
                "language": lang,
                "tokens": tokens,
                "min_required": min_required,
                "max_allowed": max_allowed,
            }
        )

    safety_rows = []
    for st in sorted(strata):
        tokens = sum(
            s["token_total"] for s in canonical if s["safety_stratum"] == st
        )
        safety_rows.append(
            {
                "stratum": st,
                "tokens": tokens,
                "floor": to_int((strata[st] or {}).get("floor_tokens"), 0),
            }
        )

    source_summary_rows = [
        {
            "source_id": s["source_id"],
            "tokens": s["token_total"],
            "canonical_tokens": s["canonical_tokens"],
        }
        for s in canonical
    ]

    phase_total_rows = [
        {
            "phase": ph,
            "allocated": sum(
                allocation.get((ph, s["source_id"]), 0) for s in membership.get(ph, [])
            ),
            "cap": cap.get(ph, 0),
            "budget": budget.get(ph, 0),
        }
        for ph in phase_order
    ]

    examples_placed = sum(len(r["members"]) for r in micro_rows)

    # ---------------------------------------------------------- invariants
    phase_within_cap = all(
        sum(allocation.get((ph, s["source_id"]), 0) for s in membership.get(ph, []))
        <= effective_cap.get(ph, cap.get(ph, 0))
        for ph in phase_order
    )
    source_within_supply = all(
        s["token_total"] <= s["canonical_tokens"] for s in canonical
    )
    materialized_sums_match = True
    for s in canonical:
        exs = examples_by_source.get(s["source_id"], [])
        if not exs or sum(e["token_length"] for e in exs) <= 0:
            continue
        if sum(
            materialized.get((s["source_id"], e["example_id"]), 0) for e in exs
        ) != s["token_total"]:
            materialized_sums_match = False
    batches_respect_seq_cap = all(
        r["seq_tokens"] <= max_seq_tokens or len(r["members"]) == 1
        for r in micro_rows
    )

    invariant_rows = [
        {"invariant": "phase_within_cap", "holds": phase_within_cap},
        {"invariant": "source_within_supply", "holds": source_within_supply},
        {"invariant": "materialized_sums_match", "holds": materialized_sums_match},
        {"invariant": "batches_respect_seq_cap", "holds": batches_respect_seq_cap},
    ]
    invariants_hold = all(r["holds"] for r in invariant_rows)

    # ------------------------------------------------------------- outputs
    outputs = {}

    outputs["canonical_sources.jsonl"] = jsonl_bytes(
        ["source_id", "raw_amount", "raw_unit", "canonical_tokens", "rounding_tie"],
        [
            {
                "source_id": s["source_id"],
                "raw_amount": s["raw_amount"],
                "raw_unit": s["raw_unit"],
                "canonical_tokens": s["canonical_tokens"],
                "rounding_tie": s["rounding_tie"],
            }
            for s in canonical
        ],
    )
    outputs["eligibility_ledger.jsonl"] = jsonl_bytes(
        ["source_id", "eligible", "code"],
        [
            {"source_id": s["source_id"], "eligible": s["eligible"], "code": s["code"]}
            for s in canonical
        ],
    )
    outputs["demand_score_ledger.jsonl"] = jsonl_bytes(
        [
            "source_id",
            "quality_bp",
            "fresh_bp",
            "priority_factor",
            "lang_balance_bp",
            "demand_score",
        ],
        [
            {
                "source_id": s["source_id"],
                "quality_bp": s["quality_bp"],
                "fresh_bp": s["fresh_bp"],
                "priority_factor": s["priority_factor"],
                "lang_balance_bp": s["lang_balance_bp"],
                "demand_score": s["demand_score"],
            }
            for s in eligible
        ],
    )
    outputs["phase_membership.jsonl"] = jsonl_bytes(
        ["phase", "source_id"],
        [
            {"phase": ph, "source_id": s["source_id"]}
            for ph in phase_order
            for s in membership[ph]
        ],
    )
    outputs["proportional_allocation.jsonl"] = jsonl_bytes(
        ["phase", "source_id", "target_tokens", "share_num", "share_den"],
        [
            {
                "phase": ph,
                "source_id": s["source_id"],
                "target_tokens": targets[(ph, s["source_id"])]["target_tokens"],
                "share_num": targets[(ph, s["source_id"])]["share_num"],
                "share_den": targets[(ph, s["source_id"])]["share_den"],
            }
            for ph in phase_order
            for s in membership[ph]
        ],
    )
    outputs["protected_floor_ledger.jsonl"] = jsonl_bytes(
        ["phase", "source_id", "safety_floor", "language_floor", "floor"],
        [
            {
                "phase": ph,
                "source_id": s["source_id"],
                "safety_floor": floors[(ph, s["source_id"])]["safety_floor"],
                "language_floor": floors[(ph, s["source_id"])]["language_floor"],
                "floor": floors[(ph, s["source_id"])]["floor"],
            }
            for ph in phase_order
            for s in membership[ph]
        ],
    )
    outputs["capacity_binding_trace.jsonl"] = jsonl_bytes(
        ["phase", "source_id", "before", "after", "displaced", "kind"], binding_trace
    )
    closure_rows = [
        {"phase": ph, "requires": req}
        for ph in sorted(graph_nodes)
        for req in sorted(closure.get(ph, set()))
        if req != ph
    ]
    outputs["precedence_closure.jsonl"] = jsonl_bytes(
        ["phase", "requires"], closure_rows
    )
    outputs["cycle_report.jsonl"] = (
        jsonl_bytes(["cycle_nodes"], [{"cycle_nodes": cycle_nodes}])
        if cycle_nodes
        else b""
    )
    outputs["cycle_break_decisions.jsonl"] = (
        jsonl_bytes(
            ["edge_id", "from", "to", "hardness"],
            [
                {
                    "edge_id": removed["edge_id"],
                    "from": removed["from"],
                    "to": removed["to"],
                    "hardness": removed["hardness"],
                }
            ],
        )
        if removed is not None
        else b""
    )
    outputs["precedence_overturn_trace.jsonl"] = jsonl_bytes(
        ["phase", "prereq_phase", "source_id"], overturns
    )
    outputs["carry_state_ledger.jsonl"] = jsonl_bytes(
        ["phase", "credit_in", "debt_in", "credit_out", "debt_out"], carry_ledger
    )
    outputs["pre_reconciliation_ledger.jsonl"] = jsonl_bytes(
        ["phase", "source_id", "tokens"],
        [
            {"phase": ph, "source_id": sid, "tokens": pre_reconcile[(ph, sid)]}
            for ph in phase_order
            for sid in sorted(s["source_id"] for s in membership.get(ph, []))
            if (ph, sid) in pre_reconcile
        ],
    )
    outputs["global_reconciliation_trace.jsonl"] = jsonl_bytes(
        ["source_id", "phase", "before", "after", "clawed"], clawback_trace
    )
    outputs["final_allocation.jsonl"] = jsonl_bytes(
        ["phase", "source_id", "tokens"],
        [
            {"phase": ph, "source_id": sid, "tokens": allocation[(ph, sid)]}
            for ph in phase_order
            for sid in sorted(s["source_id"] for s in membership.get(ph, []))
            if (ph, sid) in allocation
        ],
    )
    outputs["language_allocation_summary.jsonl"] = jsonl_bytes(
        ["language", "tokens", "min_required", "max_allowed"], language_rows
    )
    outputs["safety_allocation_summary.jsonl"] = jsonl_bytes(
        ["stratum", "tokens", "floor"], safety_rows
    )
    outputs["source_allocation_summary.jsonl"] = jsonl_bytes(
        ["source_id", "tokens", "canonical_tokens"], source_summary_rows
    )
    outputs["rational_example_weights.jsonl"] = jsonl_bytes(
        ["example_id", "source_id", "w_num", "w_den"], rational_rows
    )
    outputs["fixed_point_weights.jsonl"] = jsonl_bytes(
        ["example_id", "source_id", "materialized_tokens"], fixed_rows
    )
    outputs["rounding_residual_ledger.jsonl"] = jsonl_bytes(
        ["example_id", "source_id", "base_tokens", "residual_unit"], residual_rows
    )
    outputs["microbatch_manifest.jsonl"] = jsonl_bytes(
        ["batch_id", "phase", "members", "seq_tokens"], micro_rows
    )
    outputs["phase_token_totals.jsonl"] = jsonl_bytes(
        ["phase", "allocated", "cap", "budget"], phase_total_rows
    )
    outputs["invariant_report.jsonl"] = jsonl_bytes(
        ["invariant", "holds"], invariant_rows
    )
    outputs["exception_trace.jsonl"] = jsonl_bytes(
        ["code", "ref"],
        [
            {"code": code, "ref": ref}
            for _, _, code, ref in sorted(exceptions, key=lambda x: (x[0], x[1]))
        ],
    )

    global_clawbacks = len(clawback_trace)
    capacity_displacements = len(binding_trace)
    microbatches = len(micro_rows)
    objective = [
        total_tokens,
        global_clawbacks,
        cycle_edges_removed,
        capacity_displacements,
        microbatches,
    ]
    outputs["objective_tuple.json"] = json_bytes(
        {"objective": objective, "order": OBJECTIVE_ORDER}
    )

    # ------------------------------------------------------------- writing
    os.makedirs(out_dir, exist_ok=True)
    program_path = os.path.abspath(__file__)
    with open(program_path, "rb") as fh:
        program_bytes = fh.read()

    manifest_names = sorted(list(outputs.keys()) + ["plan.py"])
    digests = {name: sha256_hex(data) for name, data in outputs.items()}
    digests["plan.py"] = sha256_hex(program_bytes)

    for name, data in outputs.items():
        with open(os.path.join(out_dir, name), "wb") as fh:
            fh.write(data)
    with open(os.path.join(out_dir, "plan.py"), "wb") as fh:
        fh.write(program_bytes)

    outputs["integrity_manifest.json"] = json_bytes(
        {name: digests[name] for name in manifest_names}
    )
    with open(os.path.join(out_dir, "integrity_manifest.json"), "wb") as fh:
        fh.write(outputs["integrity_manifest.json"])

    chain = "0" * 64
    for name in manifest_names:
        chain = hashlib.sha256((chain + digests[name]).encode("utf-8")).hexdigest()

    final_state = {
        "chain": chain,
        "total_tokens": total_tokens,
        "global_clawbacks": global_clawbacks,
        "capacity_displacements": capacity_displacements,
        "cycle_edges_removed": cycle_edges_removed,
        "precedence_overturns": len(overturns),
        "microbatches": microbatches,
        "examples_placed": examples_placed,
        "quiet_branches": len(exceptions),
        "objective": objective,
        "invariants_hold": invariants_hold,
    }
    with open(os.path.join(out_dir, "final_state.json"), "wb") as fh:
        fh.write(json_bytes(final_state))


def main(argv):
    if len(argv) != 3:
        sys.stderr.write("usage: plan.py <DATA_DIR> <OUT_DIR>\n")
        return 2
    compile_plan(os.path.abspath(argv[1]), os.path.abspath(argv[2]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
