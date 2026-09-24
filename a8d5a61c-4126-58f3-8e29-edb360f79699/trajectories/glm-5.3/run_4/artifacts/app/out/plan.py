#!/usr/bin/env python3
"""TrainMix allocation compiler.

Derives the weighted training plan, phase allocations and batch manifests from
the manifest records in a data directory, following the shipped specification.
Every number is an integer or an exact rational (fractions.Fraction); no
floating point value is produced, compared or serialized anywhere.  The data
directory is treated as read only and every derived file is written inside the
output directory handed over on the command line.

Usage: plan.py <DATA_DIR> <OUT_DIR>
"""

import csv
import hashlib
import json
import os
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

ABOVE_FLOOR = "above_floor"
FLOOR_CUT = "floor_cut"

OBJECTIVE_ORDER = [
    "total_tokens",
    "global_clawbacks",
    "cycle_edges_removed",
    "capacity_displacements",
    "microbatches",
]


# ---------------------------------------------------------------------------
# exact arithmetic helpers


def rhe(num, den):
    """round_half_even(p, d) for integer p over positive denominator d."""
    if den <= 0:
        raise ValueError("round_half_even requires a positive denominator")
    neg = num < 0
    p = -num if neg else num
    q, r = divmod(p, den)
    if 2 * r > den or (2 * r == den and q % 2 == 1):
        q += 1
    return -q if neg else q


def is_exact_half(num, den):
    return 2 * (abs(num) % den) == den


def largest_remainder(total, keyed_weights):
    """Largest remainder split, ties by ascending key.

    keyed_weights is a sequence of (key, non negative integer weight) pairs.
    Returns a mapping of key to (base quotient, materialized count).
    """
    keys = [k for k, _ in keyed_weights]
    wsum = sum(w for _, w in keyed_weights)
    if wsum <= 0 or total == 0:
        return {k: (0, 0) for k in keys}
    quotients = {}
    base = {}
    rems = []
    for k, w in keyed_weights:
        q, r = divmod(total * w, wsum)
        quotients[k] = q
        base[k] = q
        rems.append((k, r))
    leftover = total - sum(base.values())
    ranked = sorted(rems, key=lambda kr: (-kr[1], kr[0]))
    for i in range(leftover):
        base[ranked[i][0]] += 1
    return {k: (quotients[k], base[k]) for k in keys}


def transitive_closure(edges, phases):
    """Map each phase to the set of phases reachable from it, itself excluded."""
    reach = {p: set() for p in phases}
    for e in edges:
        f, t = e["from"], e["to"]
        if f in reach and t in reach:
            reach[f].add(t)
    changed = True
    while changed:
        changed = False
        for p in phases:
            grown = set()
            for q in reach[p]:
                grown |= reach[q]
            if not grown <= reach[p]:
                reach[p] |= grown
                changed = True
    return reach


def topological_order(edges, phases):
    """Kahn order over the given edges, ready nodes taken in phase name order."""
    succ = {p: set() for p in phases}
    indeg = {p: 0 for p in phases}
    for e in edges:
        f, t = e["from"], e["to"]
        if f in succ and t in succ and t not in succ[f]:
            succ[f].add(t)
            indeg[t] += 1
    ready = sorted(p for p in phases if indeg[p] == 0)
    order = []
    while ready:
        p = ready.pop(0)
        order.append(p)
        for q in sorted(succ[p]):
            indeg[q] -= 1
            if indeg[q] == 0:
                ready.append(q)
        ready.sort()
    for p in sorted(phases):
        if p not in order:
            order.append(p)
    return order


# ---------------------------------------------------------------------------
# serialization helpers


def jsonl_bytes(rows):
    body = "".join(
        json.dumps(r, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n"
        for r in rows
    )
    return body.encode("utf-8")


def json_bytes(obj):
    return (json.dumps(obj, sort_keys=True, ensure_ascii=False, indent=2) + "\n").encode(
        "utf-8"
    )


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


class Quiet:
    """Exception trace, recorded in the order the stages activate branches."""

    def __init__(self):
        self.events = []

    def __call__(self, code, ref):
        self.events.append({"code": code, "ref": ref})


# ---------------------------------------------------------------------------
# manifests


class Manifests:
    """A read only snapshot of every record the compiler depends on."""

    def __init__(self, root):
        self.root = root
        self.sources = self._rows("sources.csv")
        self.examples = self._rows("examples.csv")
        self.quality = self._keyed("quality.csv", "source_id")
        self.freshness = self._keyed("freshness.csv", "source_id")
        self.capacity = self._keyed("capacity.csv", "phase")
        self.carry = self._keyed("carry_state.csv", "phase")
        self.unit_rules = self._doc("unit_rules.json")
        self.quality_policy = self._doc("quality_policy.json")
        self.language_policy = self._doc("language_policy.json")
        self.safety_policy = self._doc("safety_policy.json")
        self.priority_order = self._doc("priority_order.json")
        self.scoring_weights = self._doc("scoring_weights.json")
        self.batch_rules = self._doc("batch_rules.json")
        self.curriculum = self._doc("curriculum.json")

    def _open(self, name):
        return open(os.path.join(self.root, name), encoding="utf-8", newline="")

    def _rows(self, name):
        with self._open(name) as f:
            return [dict(r) for r in csv.DictReader(f)]

    def _keyed(self, name, key):
        with self._open(name) as f:
            return {r[key]: dict(r) for r in csv.DictReader(f)}

    def _doc(self, name):
        with self._open(name) as f:
            return json.load(f)


# ---------------------------------------------------------------------------
# compiler stages


class Compiler:
    def __init__(self, d):
        self.d = d
        self.q = Quiet()
        self.src = {s["source_id"]: s for s in d.sources}
        self.order = [s["source_id"] for s in d.sources]
        self.canonical = []
        self.canon = {}
        self.eligibility = []
        self.eligible = []
        self.demand = {}
        self.membership = {ph: [] for ph in PHASES}
        self.phases_of = {}
        self.prop = []
        self.target = {}
        self.floor_rows = []
        self.floor = {}
        self.bind_trace = []
        self.alloc = {ph: {} for ph in PHASES}
        self.eff_cap = {}
        self.stage6 = {ph: {} for ph in PHASES}
        self.carry_rows = []
        self.claw_rows = []
        self.overturns = []
        self.batches = []

    # stage 1 -------------------------------------------------------------
    def canonicalize(self):
        units = self.d.unit_rules["units"]
        for sid in self.order:
            s = self.src[sid]
            u = units[s["raw_unit"]]
            num = int(s["raw_amount"]) * int(u["num"])
            den = int(u["den"])
            tie = is_exact_half(num, den)
            if tie:
                self.q("B_UNIT_ROUND_TIE", sid)
            tokens = rhe(num, den)
            self.canonical.append(
                {
                    "source_id": sid,
                    "raw_amount": int(s["raw_amount"]),
                    "raw_unit": s["raw_unit"],
                    "canonical_tokens": tokens,
                    "rounding_tie": tie,
                }
            )
            self.canon[sid] = tokens

    # stage 2 -------------------------------------------------------------
    def project_eligibility(self):
        d = self.d
        blocked = set(d.quality_policy.get("blocked_domains") or [])
        mq = d.quality_policy["min_quality"]
        mq_num, mq_den = int(mq["num"]), int(mq["den"])
        for sid in self.order:
            s = self.src[sid]
            row = d.quality.get(sid)
            q_num = int(row["q_num"]) if row else 0
            q_den = int(row["q_den"]) if row else 0
            if q_den <= 0 or q_num * mq_den < mq_num * q_den:
                code = "E_LOW_QUALITY"
            elif s["language"] not in d.language_policy["languages"]:
                code = "E_LANG_EXCLUDED"
            elif s["safety_stratum"] not in d.safety_policy["strata"]:
                code = "E_STRATUM_UNKNOWN"
            elif s["domain"] in blocked:
                code = "E_DOMAIN_BLOCKED"
            elif self.canon[sid] <= 0:
                code = "E_EMPTY_TOKENS"
            else:
                code = "E_OK"
            self.eligibility.append(
                {"source_id": sid, "eligible": code == "E_OK", "code": code}
            )
            if code == "E_OK":
                self.eligible.append(sid)

    # stage 3 -------------------------------------------------------------
    def score_demand(self):
        d = self.d
        sw = d.scoring_weights
        scale = int(sw["scale"])
        half_life = int(sw["half_life_days"])
        weights = sw["priority_weights"]
        for sid in self.eligible:
            qrow = d.quality[sid]
            q_num, q_den = int(qrow["q_num"]), int(qrow["q_den"])
            quality_bp = rhe(q_num * scale, q_den) if q_den > 0 else 0
            age = int(d.freshness[sid]["age_days"])
            f_den = half_life + age
            fresh_bp = rhe(half_life * scale, f_den) if f_den > 0 else 0
            priority_factor = int(weights[str(self.src[sid]["priority"])])
            lang_balance_bp = int(
                d.language_policy["languages"][self.src[sid]["language"]]["balance_bp"]
            )
            s1 = rhe(self.canon[sid] * quality_bp, scale)
            s2 = rhe(s1 * fresh_bp, scale)
            s3 = s2 * priority_factor
            score = rhe(s3 * lang_balance_bp, scale)
            self.demand[sid] = {
                "source_id": sid,
                "quality_bp": quality_bp,
                "fresh_bp": fresh_bp,
                "priority_factor": priority_factor,
                "lang_balance_bp": lang_balance_bp,
                "demand_score": score,
            }

    # stage 4 -------------------------------------------------------------
    def assign_membership_and_targets(self):
        d = self.d
        for sid in self.order:
            if sid not in self.eligible:
                continue
            phases = list(DOMAIN_PHASES[self.src[sid]["domain"]])
            self.phases_of[sid] = phases
            for ph in phases:
                self.membership[ph].append(sid)
        for ph in PHASES:
            members = self.membership[ph]
            budget = int(d.capacity[ph]["budget_tokens"])
            dsum = sum(
                self.demand[s]["demand_score"]
                for s in members
                if self.demand[s]["demand_score"] > 0
            )
            for sid in members:
                score = self.demand[sid]["demand_score"]
                snum = score * budget
                if score > 0 and dsum > 0:
                    target = rhe(snum, dsum)
                else:
                    target = 0
                self.target[(ph, sid)] = target
                self.prop.append(
                    {
                        "phase": ph,
                        "source_id": sid,
                        "target_tokens": target,
                        "share_num": snum,
                        "share_den": dsum,
                    }
                )

    # stage 5 -------------------------------------------------------------
    def assign_floors(self):
        d = self.d
        for ph in PHASES:
            for sid in self.membership[ph]:
                sf = int(
                    d.safety_policy["strata"][self.src[sid]["safety_stratum"]]["floor_tokens"]
                )
                lf = int(
                    d.language_policy["languages"][self.src[sid]["language"]]["floor_tokens"]
                )
                self.floor[(ph, sid)] = max(sf, lf)
                self.floor_rows.append(
                    {
                        "phase": ph,
                        "source_id": sid,
                        "safety_floor": sf,
                        "language_floor": lf,
                        "floor": max(sf, lf),
                    }
                )

    # stage 7 -------------------------------------------------------------
    def close_precedence(self):
        curriculum = self.d.curriculum
        self.phases = list(curriculum.get("phases") or PHASES)
        self.edges = list(curriculum.get("edges") or [])
        self.reach = transitive_closure(self.edges, self.phases)
        self.closure_rows = [
            {"phase": p, "requires": r}
            for p in sorted(self.phases)
            for r in sorted(self.reach[p])
        ]

    # stage 8, cycle break -------------------------------------------------
    def break_cycles(self):
        """Remove the single lowest ranked edge among the cycle edges.

        The rule removes exactly one edge: rank by hardness with soft below
        hard, then from phase, then to phase, then edge id, and drop the lowest
        ranked edge whose endpoints both sit on a cycle.
        """
        surviving = list(self.edges)
        reach = transitive_closure(surviving, self.phases)
        nodes = sorted(p for p in self.phases if p in reach[p])
        self.cycle_nodes = nodes
        self.breaks = []
        if nodes:
            on = set(nodes)
            candidates = [e for e in surviving if e["from"] in on and e["to"] in on]
            if candidates:
                edge = min(
                    candidates,
                    key=lambda e: (
                        0 if e["hardness"] == "soft" else 1,
                        e["from"],
                        e["to"],
                        e["edge_id"],
                    ),
                )
                surviving = [e for e in surviving if e is not edge]
                self.breaks.append(edge)
                self.q("B_CYCLE_BREAK", edge["edge_id"])
                reach = transitive_closure(surviving, self.phases)
        self.surviving = surviving
        self.reach_after = reach
        self.topo = topological_order(surviving, self.phases)

    # stages 6 and 9 ------------------------------------------------------
    def bind_and_carry(self):
        d = self.d
        succ = {p: set() for p in self.phases}
        for e in self.surviving:
            if e["from"] in succ and e["to"] in succ:
                succ[e["from"]].add(e["to"])
        credit = {p: 0 for p in self.phases}
        debt = {p: 0 for p in self.phases}
        for p in self.phases:
            row = d.carry.get(p)
            if row:
                credit[p] += int(row["initial_credit"])
                debt[p] += int(row["initial_debt"])
        rank = d.priority_order["stratum_displacement_rank"]
        for ph in self.topo:
            members = self.membership[ph]
            cap = int(d.capacity[ph]["cap_tokens"])
            eff = cap + credit[ph] - debt[ph]
            self.eff_cap[ph] = eff
            cur = {}
            raised = False
            for sid in members:
                fl = self.floor[(ph, sid)]
                tgt = self.target[(ph, sid)]
                cur[sid] = tgt if tgt >= fl else fl
                if fl > tgt:
                    raised = True
            if raised:
                self.q("B_FLOOR_BIND", ph)
            total = sum(cur.values())
            if total > eff:
                self.q("B_CAP_BIND", ph)
                order = sorted(
                    members,
                    key=lambda s: (
                        int(rank[self.src[s]["safety_stratum"]]),
                        -int(self.src[s]["priority"]),
                        s,
                    ),
                )
                excess = total - eff
                for sid in order:
                    if excess <= 0:
                        break
                    room = cur[sid] - self.floor[(ph, sid)]
                    if room > 0:
                        take = room if room < excess else excess
                        before = cur[sid]
                        cur[sid] = before - take
                        excess -= take
                        self.bind_trace.append(
                            {
                                "phase": ph,
                                "source_id": sid,
                                "before": before,
                                "after": cur[sid],
                                "displaced": take,
                                "kind": ABOVE_FLOOR,
                            }
                        )
                if excess > 0:
                    self.q("B_FLOOR_OVER_CAP", ph)
                    for sid in order:
                        if excess <= 0:
                            break
                        if cur[sid] > 0:
                            take = cur[sid] if cur[sid] < excess else excess
                            before = cur[sid]
                            cur[sid] = before - take
                            excess -= take
                            self.bind_trace.append(
                                {
                                    "phase": ph,
                                    "source_id": sid,
                                    "before": before,
                                    "after": cur[sid],
                                    "displaced": take,
                                    "kind": FLOOR_CUT,
                                }
                            )
            self.stage6[ph] = cur
            used = sum(cur.values())
            credit_out = eff - used
            debt_out = sum(max(0, self.floor[(ph, s)] - cur[s]) for s in members)
            self.carry_rows.append(
                {
                    "phase": ph,
                    "credit_in": credit[ph],
                    "debt_in": debt[ph],
                    "credit_out": credit_out,
                    "debt_out": debt_out,
                }
            )
            if debt_out > 0:
                self.q("B_CARRY_DEBT", ph)
            for nxt in sorted(succ[ph]):
                credit[nxt] += credit_out
                debt[nxt] += debt_out

    # stage 10 ------------------------------------------------------------
    def reconcile(self):
        pos = {p: i for i, p in enumerate(self.topo)}
        self.alloc = {ph: dict(self.stage6[ph]) for ph in PHASES}
        for sid in self.order:
            phases = [ph for ph in PHASES if sid in self.alloc[ph]]
            total = sum(self.alloc[ph][sid] for ph in phases)
            supply = self.canon[sid]
            if total <= supply:
                continue
            self.q("B_GLOBAL_CLAWBACK", sid)
            excess = total - supply
            for ph in sorted(phases, key=lambda p: -pos.get(p, len(PHASES))):
                if excess <= 0:
                    break
                have = self.alloc[ph][sid]
                if have <= 0:
                    continue
                take = have if have < excess else excess
                self.alloc[ph][sid] = have - take
                excess -= take
                self.claw_rows.append(
                    {
                        "source_id": sid,
                        "phase": ph,
                        "before": have,
                        "after": have - take,
                        "clawed": take,
                    }
                )
        rows = []
        for sid in self.order:
            for ph in PHASES:
                if ph not in self.alloc or sid not in self.alloc[ph]:
                    continue
                if self.alloc[ph][sid] <= 0:
                    continue
                for pre in sorted(self.reach_after[ph]):
                    if pre == ph:
                        continue
                    if sid in self.alloc.get(pre, {}):
                        if self.alloc[pre][sid] == 0:
                            rows.append(
                                {
                                    "phase": ph,
                                    "prereq_phase": pre,
                                    "source_id": sid,
                                }
                            )
        self.overturns = sorted(
            rows, key=lambda r: (r["phase"], r["prereq_phase"], r["source_id"])
        )
        for r in self.overturns:
            self.q("B_PRECEDENCE_OVERTURN", r["phase"])

    # stages 11 and 12 ----------------------------------------------------
    def derive_examples(self):
        d = self.d
        self.final_rows = [
            {"phase": ph, "source_id": sid, "tokens": self.alloc[ph][sid]}
            for ph in PHASES
            for sid in self.membership[ph]
        ]
        self.total_tokens = sum(r["tokens"] for r in self.final_rows)
        self.source_total = {
            sid: sum(self.alloc[ph][sid] for ph in self.phases_of.get(sid, []))
            for sid in self.order
        }
        grouped = {sid: [] for sid in self.order}
        for x in d.examples:
            grouped[x["source_id"]].append(x)
        for sid in self.order:
            grouped[sid].sort(key=lambda x: x["example_id"])
        self.grouped = grouped
        self.rational_rows = []
        self.fixed_rows = []
        self.residual_rows = []
        self.materialized = {}
        self.group_den = {}
        for sid in self.order:
            exs = grouped[sid]
            gden = sum(int(x["token_length"]) for x in exs)
            self.group_den[sid] = gden
            if exs and gden == 0:
                self.q("B_EMPTY_GROUP", sid)
            total = self.source_total[sid]
            if not exs:
                continue
            if gden > 0:
                for x in exs:
                    w = Fraction(total * int(x["token_length"]), gden)
                    self.rational_rows.append(
                        {
                            "example_id": x["example_id"],
                            "source_id": sid,
                            "w_num": w.numerator,
                            "w_den": w.denominator,
                        }
                    )
            if gden <= 0 or total == 0:
                for x in exs:
                    self.materialized[x["example_id"]] = 0
                    self.fixed_rows.append(
                        {
                            "example_id": x["example_id"],
                            "source_id": sid,
                            "materialized_tokens": 0,
                        }
                    )
                continue
            split = largest_remainder(
                total, [(x["example_id"], int(x["token_length"])) for x in exs]
            )
            for x in exs:
                eid = x["example_id"]
                base, got = split[eid]
                self.materialized[eid] = got
                self.fixed_rows.append(
                    {
                        "example_id": eid,
                        "source_id": sid,
                        "materialized_tokens": got,
                    }
                )
                if got == base + 1:
                    self.residual_rows.append(
                        {
                            "example_id": eid,
                            "source_id": sid,
                            "base_tokens": base,
                            "residual_unit": 1,
                        }
                    )
        self.example_source = {x["example_id"]: x["source_id"] for x in d.examples}

    # stage 13 ------------------------------------------------------------
    def build_microbatches(self):
        d = self.d
        max_seq = int(d.batch_rules["max_seq_tokens"])
        adj = int(d.batch_rules["adjacency_limit"])
        batches = []
        for ph in PHASES:
            for sid in self.membership[ph]:
                if self.alloc[ph][sid] <= 0:
                    continue
                for x in self.grouped[sid]:
                    eid = x["example_id"]
                    tok = self.materialized.get(eid, 0)
                    if tok <= 0:
                        continue
                    if batches:
                        b = batches[-1]
                        tail = 0
                        for m in reversed(b["members"]):
                            if self.example_source[m] == sid:
                                tail += 1
                            else:
                                break
                        if (
                            b["phase"] == ph
                            and b["seq_tokens"] + tok <= max_seq
                            and tail < adj
                        ):
                            b["members"].append(eid)
                            b["seq_tokens"] += tok
                            continue
                    batches.append(
                        {
                            "batch_id": "BATCH%04d" % (len(batches) + 1),
                            "phase": ph,
                            "members": [eid],
                            "seq_tokens": tok,
                        }
                    )
        self.batches = batches

    # invariants ----------------------------------------------------------
    def check_invariants(self):
        d = self.d
        within_cap = all(
            sum(self.alloc[ph].values()) <= self.eff_cap[ph] for ph in PHASES
        )
        within_supply = all(
            self.source_total[sid] <= self.canon[sid] for sid in self.order
        )
        sums_match = True
        for sid in self.order:
            exs = self.grouped[sid]
            if not exs or self.group_den[sid] <= 0:
                continue
            if sum(self.materialized[x["example_id"]] for x in exs) != self.source_total[sid]:
                sums_match = False
        max_seq = int(d.batch_rules["max_seq_tokens"])
        batches_ok = all(
            b["seq_tokens"] <= max_seq or len(b["members"]) == 1 for b in self.batches
        )
        return {
            "phase_within_cap": within_cap,
            "source_within_supply": within_supply,
            "materialized_sums_match": sums_match,
            "batches_respect_seq_cap": batches_ok,
        }

    def run(self):
        self.canonicalize()
        self.project_eligibility()
        self.score_demand()
        self.assign_membership_and_targets()
        self.assign_floors()
        self.close_precedence()
        self.break_cycles()
        self.bind_and_carry()
        self.reconcile()
        self.derive_examples()
        self.build_microbatches()
        return self


# ---------------------------------------------------------------------------
# artifact emission


def build_artifacts(c, program_path):
    d = c.d
    scale = int(d.unit_rules["scale"])
    files = {}

    files["canonical_sources.jsonl"] = jsonl_bytes(c.canonical)
    files["eligibility_ledger.jsonl"] = jsonl_bytes(c.eligibility)
    files["demand_score_ledger.jsonl"] = jsonl_bytes(
        [c.demand[s] for s in c.order if s in c.demand]
    )
    files["phase_membership.jsonl"] = jsonl_bytes(
        [{"phase": ph, "source_id": sid} for ph in PHASES for sid in c.membership[ph]]
    )
    files["proportional_allocation.jsonl"] = jsonl_bytes(c.prop)
    files["protected_floor_ledger.jsonl"] = jsonl_bytes(c.floor_rows)
    files["capacity_binding_trace.jsonl"] = jsonl_bytes(c.bind_trace)
    files["precedence_closure.jsonl"] = jsonl_bytes(c.closure_rows)
    files["cycle_report.jsonl"] = jsonl_bytes(
        [{"cycle_nodes": c.cycle_nodes}] if c.cycle_nodes else []
    )
    files["cycle_break_decisions.jsonl"] = jsonl_bytes(
        [
            {
                "edge_id": e["edge_id"],
                "from": e["from"],
                "to": e["to"],
                "hardness": e["hardness"],
            }
            for e in c.breaks
        ]
    )
    files["precedence_overturn_trace.jsonl"] = jsonl_bytes(c.overturns)
    files["carry_state_ledger.jsonl"] = jsonl_bytes(c.carry_rows)
    files["pre_reconciliation_ledger.jsonl"] = jsonl_bytes(
        [
            {"phase": ph, "source_id": sid, "tokens": c.stage6[ph][sid]}
            for ph in PHASES
            for sid in c.membership[ph]
        ]
    )
    files["global_reconciliation_trace.jsonl"] = jsonl_bytes(c.claw_rows)
    files["final_allocation.jsonl"] = jsonl_bytes(c.final_rows)

    lang_rows = []
    for lang in sorted(d.language_policy["languages"]):
        pol = d.language_policy["languages"][lang]
        tokens = sum(
            c.alloc[ph][sid]
            for ph in PHASES
            for sid in c.membership[ph]
            if c.src[sid]["language"] == lang
        )
        lang_rows.append(
            {
                "language": lang,
                "tokens": tokens,
                "min_required": rhe(int(pol["min_share_bp"]) * c.total_tokens, scale),
                "max_allowed": rhe(int(pol["max_share_bp"]) * c.total_tokens, scale),
            }
        )
    files["language_allocation_summary.jsonl"] = jsonl_bytes(lang_rows)

    safety_rows = []
    for st in sorted(d.safety_policy["strata"]):
        tokens = sum(
            c.alloc[ph][sid]
            for ph in PHASES
            for sid in c.membership[ph]
            if c.src[sid]["safety_stratum"] == st
        )
        safety_rows.append(
            {
                "stratum": st,
                "tokens": tokens,
                "floor": int(d.safety_policy["strata"][st]["floor_tokens"]),
            }
        )
    files["safety_allocation_summary.jsonl"] = jsonl_bytes(safety_rows)

    files["source_allocation_summary.jsonl"] = jsonl_bytes(
        [
            {
                "source_id": sid,
                "tokens": c.source_total[sid],
                "canonical_tokens": c.canon[sid],
            }
            for sid in c.order
        ]
    )
    files["rational_example_weights.jsonl"] = jsonl_bytes(c.rational_rows)
    files["fixed_point_weights.jsonl"] = jsonl_bytes(c.fixed_rows)
    files["rounding_residual_ledger.jsonl"] = jsonl_bytes(c.residual_rows)
    files["microbatch_manifest.jsonl"] = jsonl_bytes(c.batches)
    files["phase_token_totals.jsonl"] = jsonl_bytes(
        [
            {
                "phase": ph,
                "allocated": sum(c.alloc[ph].values()),
                "cap": int(d.capacity[ph]["cap_tokens"]),
                "budget": int(d.capacity[ph]["budget_tokens"]),
            }
            for ph in PHASES
        ]
    )

    invariants = c.check_invariants()
    files["invariant_report.jsonl"] = jsonl_bytes(
        [
            {"invariant": name, "holds": invariants[name]}
            for name in (
                "phase_within_cap",
                "source_within_supply",
                "materialized_sums_match",
                "batches_respect_seq_cap",
            )
        ]
    )
    files["exception_trace.jsonl"] = jsonl_bytes(c.q.events)

    objective = [
        c.total_tokens,
        len(c.claw_rows),
        len(c.breaks),
        len(c.bind_trace),
        len(c.batches),
    ]
    files["objective_tuple.json"] = json_bytes(
        {"objective": objective, "order": OBJECTIVE_ORDER}
    )

    with open(program_path, "rb") as f:
        program_bytes = f.read()

    hashed = {name: data for name, data in files.items()}
    hashed["plan.py"] = program_bytes
    digests = {name: sha256_bytes(data) for name, data in hashed.items()}
    files["integrity_manifest.json"] = json_bytes(
        {name: digests[name] for name in sorted(digests)}
    )

    chain = "0" * 64
    for name in sorted(digests):
        chain = sha256_bytes((chain + digests[name]).encode("utf-8"))
    files["final_state.json"] = json_bytes(
        {
            "chain": chain,
            "total_tokens": c.total_tokens,
            "global_clawbacks": len(c.claw_rows),
            "capacity_displacements": len(c.bind_trace),
            "cycle_edges_removed": len(c.breaks),
            "precedence_overturns": len(c.overturns),
            "microbatches": len(c.batches),
            "examples_placed": sum(len(b["members"]) for b in c.batches),
            "quiet_branches": len(c.q.events),
            "objective": objective,
            "invariants_hold": all(invariants.values()),
        }
    )
    return files


def main(argv):
    if len(argv) != 3:
        sys.stderr.write("usage: plan.py <DATA_DIR> <OUT_DIR>\n")
        return 2
    data_dir, out_dir = argv[1], argv[2]
    manifests = Manifests(data_dir)
    compiler = Compiler(manifests).run()
    files = build_artifacts(compiler, os.path.abspath(__file__))
    os.makedirs(out_dir, exist_ok=True)
    for name in sorted(files):
        with open(os.path.join(out_dir, name), "wb") as f:
            f.write(files[name])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
