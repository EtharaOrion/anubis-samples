#!/usr/bin/env python3
"""TrainMix allocation compiler.

Derives the reproducible allocation certificate from the manifests in a data
directory:  plan.py <DATA_DIR> <OUT_DIR>

Every number is an integer or an exact rational.  No floating point value is
produced, compared or serialized anywhere.  The pipeline follows the fourteen
stages of compiler_spec.md together with scoring_rules.md,
allocation_rules.md, weight_rules.md, serialization_rules.md,
artifact_contract.md and reason_codes.md.
"""

import csv
import hashlib
import json
import os
import shutil
import sys
from fractions import Fraction

# Domain to phase mapping is fixed by the specification (stage 4).
DOMAIN_PHASES = {
    "maint": ("PH1",),
    "manual": ("PH1", "PH2"),
    "incident": ("PH2",),
    "qa": ("PH2", "PH3"),
    "translated": ("PH1", "PH3"),
    "synthetic": ("PH3",),
    "reg": ("PH2",),
}

# Quiet branch codes keyed by their reason_codes.md listing order, so that the
# exception trace lists each activation under its code in stage order.
BRANCH_ORDER = [
    "B_UNIT_ROUND_TIE",
    "B_FLOOR_BIND",
    "B_CAP_BIND",
    "B_FLOOR_OVER_CAP",
    "B_CYCLE_BREAK",
    "B_PRECEDENCE_OVERTURN",
    "B_CARRY_DEBT",
    "B_GLOBAL_CLAWBACK",
    "B_EMPTY_GROUP",
]

JSON_FILES = ("objective_tuple.json", "integrity_manifest.json", "final_state.json")

# Every derived lines file except integrity_manifest.json and final_state.json,
# which are sealed after the others, in the order they are produced.
LINES_FILES = [
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
# exact arithmetic
# --------------------------------------------------------------------------

def round_half_even(num, den):
    """Quantize num/den with a positive denominator, half to even."""
    if den < 0:
        num, den = -num, -den
    quot, rem = divmod(num, den)
    twice = 2 * rem
    if twice > den:
        return quot + 1
    if twice < den:
        return quot
    return quot if quot % 2 == 0 else quot + 1


def qfrac(value):
    """Quantize an exact rational, half to even."""
    return round_half_even(value.numerator, value.denominator)


def to_rational(text):
    if isinstance(text, bool):
        return Fraction(int(text))
    if isinstance(text, int):
        return Fraction(text)
    text = str(text or "").strip()
    if not text:
        return Fraction(0)
    return Fraction(text)


def to_int(text):
    value = to_rational(text)
    if value.denominator != 1:
        raise ValueError("expected an integer, got %s" % text)
    return value.numerator


def reduce_fraction(num, den):
    """Fully reduced fraction with a positive denominator, as a tuple."""
    if den < 0:
        num, den = -num, -den
    if num == 0:
        return 0, 1
    g = _gcd(abs(num), abs(den))
    return num // g, den // g


def _gcd(a, b):
    while b:
        a, b = b, a % b
    return a or 1


# --------------------------------------------------------------------------
# manifest loading
# --------------------------------------------------------------------------

def read_json(data_dir, name):
    path = os.path.join(data_dir, name)
    if not os.path.isfile(path):
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def read_csv(data_dir, name):
    path = os.path.join(data_dir, name)
    if not os.path.isfile(path):
        return []
    with open(path, "r", encoding="utf-8", newline="") as handle:
        rows = []
        for row in csv.DictReader(handle):
            clean = {
                (k.strip() if isinstance(k, str) else k):
                    (v.strip() if isinstance(v, str) else v)
                for k, v in row.items()
            }
            first = clean.get("source_id") or clean.get("phase") or clean.get("example_id")
            if not first:
                continue
            rows.append(clean)
        return rows


def unique_in_order(rows, key="source_id"):
    """Keep each row by first appearance of its key."""
    seen = set()
    ordered = []
    for row in rows:
        value = row.get(key)
        if value in seen:
            continue
        seen.add(value)
        ordered.append(row)
    return ordered


class Manifest(object):
    def __init__(self, data_dir):
        self.data_dir = data_dir
        unit_rules = read_json(data_dir, "unit_rules.json")
        self.scale = to_int(unit_rules.get("scale", 10000))
        self.units = {
            name: (to_int(spec["num"]), to_int(spec["den"]))
            for name, spec in (unit_rules.get("units") or {}).items()
        }
        self.languages = read_json(data_dir, "language_policy.json").get("languages") or {}
        self.strata = read_json(data_dir, "safety_policy.json").get("strata") or {}
        quality_policy = read_json(data_dir, "quality_policy.json")
        min_quality = quality_policy.get("min_quality") or {"num": 0, "den": 1}
        self.min_quality = (to_int(min_quality["num"]), to_int(min_quality["den"]))
        self.blocked_domains = list(quality_policy.get("blocked_domains") or [])
        weights = read_json(data_dir, "scoring_weights.json")
        self.half_life = to_int(weights.get("half_life_days", 0))
        self.priority_weights = {
            key: to_int(value) for key, value in (weights.get("priority_weights") or {}).items()
        }
        order = read_json(data_dir, "priority_order.json")
        self.stratum_rank = {
            key: to_int(value)
            for key, value in (order.get("stratum_displacement_rank") or {}).items()
        }
        batch = read_json(data_dir, "batch_rules.json")
        self.max_seq_tokens = to_int(batch.get("max_seq_tokens", 0))
        self.adjacency_limit = to_int(batch.get("adjacency_limit", 0))

        curriculum = read_json(data_dir, "curriculum.json")
        self.curriculum_phases = [str(p) for p in curriculum.get("phases") or []]
        self.edges = [
            {
                "edge_id": str(edge["edge_id"]),
                "from": str(edge["from"]),
                "to": str(edge["to"]),
                "hardness": str(edge.get("hardness", "soft")),
            }
            for edge in curriculum.get("edges") or []
        ]

        self.sources = unique_in_order(read_csv(data_dir, "sources.csv"))
        self.quality = {row["source_id"]: row
                        for row in unique_in_order(read_csv(data_dir, "quality.csv"))}
        self.freshness = {row["source_id"]: row
                          for row in unique_in_order(read_csv(data_dir, "freshness.csv"))}
        self.examples = unique_in_order(read_csv(data_dir, "examples.csv"), "example_id")
        self.capacity = {row["phase"]: row for row in read_csv(data_dir, "capacity.csv")}
        self.carry = {row["phase"]: row for row in read_csv(data_dir, "carry_state.csv")}


# --------------------------------------------------------------------------
# compiler
# --------------------------------------------------------------------------

class Compiler(object):
    def __init__(self, manifest):
        self.m = manifest
        self.example_source = {
            row["example_id"]: row.get("source_id") for row in manifest.examples
        }
        self.exceptions = []           # (code, ref)
        self.canonical = {}            # sid -> canonical tokens
        self.ties = {}                 # sid -> rounding tie
        self.eligible = {}             # sid -> bool
        self.eligibility_code = {}     # sid -> code
        self.demand = {}               # sid -> score
        self.factors = {}              # sid -> dict of factors
        self.membership = {}           # phase -> [sid]
        self.phases = []
        self.phase_set = set()
        self.phase_order = []
        self.topo_order = []
        self.target = {}               # (phase, sid) -> tokens
        self.share = {}                # (phase, sid) -> (num, den)
        self.floors = {}               # (phase, sid) -> floor
        self.floor_parts = {}          # (phase, sid) -> (safety, language)
        self.bound = {}                # (phase, sid) -> stage 6 allocation
        self.effective_cap = {}        # phase -> effective capacity
        self.carry_rows = []
        self.carry_out = {}
        self.final = {}                # (phase, sid) -> reconciled tokens
        self.materialized = {}         # example id -> tokens
        self.batches = []
        self.counters = {
            "global_clawbacks": 0,
            "cycle_edges_removed": 0,
            "capacity_displacements": 0,
            "precedence_overturns": 0,
        }

    # -- stage 0 helpers ---------------------------------------------------

    def phases_of(self, domain):
        return list(DOMAIN_PHASES.get(domain, ()))

    def note(self, code, ref):
        """Record a quiet branch activation once per code and reference."""
        ref = str(ref)
        for known in self.exceptions:
            if known == (code, ref):
                return
        self.exceptions.append((code, ref))

    # -- stage 1 -----------------------------------------------------------

    def stage1_canonicalize(self):
        for row in self.m.sources:
            sid = row["source_id"]
            unit = row.get("raw_unit", "tok")
            num, den = self.m.units.get(unit, (1, 1))
            amount = to_rational(row.get("raw_amount", "0"))
            value = amount * Fraction(num, den)
            self.canonical[sid] = qfrac(value)
            _, rem = divmod(value.numerator, value.denominator)
            tie = 2 * rem == value.denominator
            self.ties[sid] = tie
            if tie:
                self.note("B_UNIT_ROUND_TIE", sid)

    # -- stage 2 -----------------------------------------------------------

    def stage2_eligibility(self):
        mq_num, mq_den = self.m.min_quality
        if mq_den == 0:
            # The explicit zero denominator rule covers quality.csv only; a
            # policy with no usable minimum imposes no quality constraint.
            mq_num, mq_den = 0, 1
        for row in self.m.sources:
            sid = row["source_id"]
            qrow = self.m.quality.get(sid) or {}
            q_num = to_int(qrow.get("q_num", "0"))
            q_den = to_int(qrow.get("q_den", "1"))
            tokens = self.canonical[sid]
            if q_den == 0 or q_num * mq_den < mq_num * q_den:
                code = "E_LOW_QUALITY"
            elif row.get("language") not in self.m.languages:
                code = "E_LANG_EXCLUDED"
            elif row.get("safety_stratum") not in self.m.strata:
                code = "E_STRATUM_UNKNOWN"
            elif row.get("domain") in self.m.blocked_domains:
                code = "E_DOMAIN_BLOCKED"
            elif tokens <= 0:
                code = "E_EMPTY_TOKENS"
            else:
                code = "E_OK"
            self.eligibility_code[sid] = code
            self.eligible[sid] = code == "E_OK"

    # -- stage 3 -----------------------------------------------------------

    def stage3_scoring(self):
        scale = self.m.scale
        for row in self.m.sources:
            sid = row["source_id"]
            if not self.eligible[sid]:
                continue
            qrow = self.m.quality.get(sid) or {}
            q_num = to_int(qrow.get("q_num", "0"))
            q_den = to_int(qrow.get("q_den", "1"))
            frow = self.m.freshness.get(sid) or {}
            age = to_int(frow.get("age_days", "0"))
            half_life = self.m.half_life
            quality_bp = round_half_even(q_num * scale, q_den)
            fresh_den = half_life + age
            fresh_bp = round_half_even(half_life * scale, fresh_den) if fresh_den > 0 else 0
            priority_key = row.get("priority", "")
            priority_factor = self.m.priority_weights.get(str(priority_key), 1)
            lang = self.m.languages[row["language"]]
            balance_bp = to_int(lang.get("balance_bp", scale))
            s1 = round_half_even(self.canonical[sid] * quality_bp, scale)
            s2 = round_half_even(s1 * fresh_bp, scale)
            s3 = s2 * priority_factor
            score = round_half_even(s3 * balance_bp, scale)
            self.factors[sid] = {
                "quality_bp": quality_bp,
                "fresh_bp": fresh_bp,
                "priority_factor": priority_factor,
                "lang_balance_bp": balance_bp,
            }
            self.demand[sid] = score

    # -- stages 4 and 5 ----------------------------------------------------

    def phase_sort_key(self, phase):
        """Natural phase order, so PH2 sorts before PH10."""
        head = phase.rstrip("0123456789")
        tail = phase[len(head):]
        return (head, int(tail) if tail else 0, phase)

    def ordered_phases(self):
        """Declared curriculum phase order first, then any extra phase."""
        declared = [p for p in self.m.curriculum_phases if p in self.phase_set]
        extras = sorted(self.phase_set - set(declared), key=self.phase_sort_key)
        return declared + extras

    def stage45_membership_targets_floors(self):
        self.phase_set = set(self.m.curriculum_phases) | set(self.m.capacity)
        for row in self.m.sources:
            if self.eligible[row["source_id"]]:
                self.phase_set.update(self.phases_of(row.get("domain")))
        self.phases = self.ordered_phases()
        for phase in self.phases:
            members = [
                row["source_id"]
                for row in self.m.sources
                if self.eligible[row["source_id"]]
                and phase in self.phases_of(row.get("domain"))
            ]
            self.membership[phase] = members
            budget = self.phase_budget(phase)
            scored = [sid for sid in members if self.demand.get(sid, 0) > 0]
            demand_sum = sum(self.demand[sid] for sid in scored)
            for sid in members:
                if self.demand.get(sid, 0) > 0 and demand_sum > 0:
                    num = self.demand[sid] * budget
                    self.share[(phase, sid)] = (num, demand_sum)
                    self.target[(phase, sid)] = round_half_even(num, demand_sum)
                else:
                    # Zero demand score or zero demand sum: target zero, and the
                    # recorded denominator stays the phase demand sum as it is.
                    self.share[(phase, sid)] = (0, demand_sum)
                    self.target[(phase, sid)] = 0
            for sid in members:
                row = self.source_row(sid)
                safety_floor = to_int(self.m.strata[row["safety_stratum"]].get("floor_tokens", 0))
                language_floor = to_int(self.m.languages[row["language"]].get("floor_tokens", 0))
                floor = max(safety_floor, language_floor)
                self.floor_parts[(phase, sid)] = (safety_floor, language_floor)
                self.floors[(phase, sid)] = floor
                if floor > self.target[(phase, sid)]:
                    self.note("B_FLOOR_BIND", phase)

    def source_row(self, sid):
        for row in self.m.sources:
            if row["source_id"] == sid:
                return row
        return {}

    def phase_budget(self, phase):
        return to_int(self.m.capacity.get(phase, {}).get("budget_tokens", "0"))

    def phase_cap(self, phase):
        return to_int(self.m.capacity.get(phase, {}).get("cap_tokens", "0"))

    # -- stage 7 -----------------------------------------------------------

    def build_closure(self):
        adjacency = {}
        for phase in self.phase_set:
            adjacency[phase] = []
        for edge in self.m.edges:
            adjacency.setdefault(edge["from"], []).append(edge["to"])
        nodes = sorted(adjacency)
        reach = {}
        for node in nodes:
            seen = set()
            stack = list(adjacency[node])
            while stack:
                nxt = stack.pop()
                if nxt in seen:
                    continue
                seen.add(nxt)
                stack.extend(adjacency.get(nxt, ()))
            reach[node] = seen
        return reach

    # -- stage 8 -----------------------------------------------------------

    def stage8_break_cycle(self, reach):
        cycle_nodes = sorted(node for node in reach if node in reach[node])
        removed = None
        if cycle_nodes:
            node_set = set(cycle_nodes)
            ranked = sorted(
                (edge for edge in self.m.edges
                 if edge["from"] in node_set and edge["to"] in node_set),
                key=lambda e: (
                    0 if e["hardness"] == "soft" else 1,
                    e["from"],
                    e["to"],
                    e["edge_id"],
                ),
            )
            if ranked:
                removed = ranked[0]
                self.counters["cycle_edges_removed"] = 1
                self.note("B_CYCLE_BREAK", removed["edge_id"])
        surviving = [edge for edge in self.m.edges if removed is None or edge is not removed]
        return cycle_nodes, removed, surviving

    def topological_order(self, edges):
        nodes = set(self.phases)
        nodes.update(edge["from"] for edge in edges)
        nodes.update(edge["to"] for edge in edges)
        indegree = {node: 0 for node in nodes}
        adjacency = {node: [] for node in nodes}
        for edge in edges:
            adjacency[edge["from"]].append(edge["to"])
            indegree[edge["to"]] += 1
        order = []
        ready = sorted(node for node in nodes if indegree[node] == 0)
        while ready:
            node = ready.pop(0)
            order.append(node)
            for nxt in sorted(adjacency[node]):
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    ready.append(nxt)
            ready.sort()
        for node in sorted(nodes):
            if node not in order:
                order.append(node)
        return order

    # -- stages 6 and 9 ----------------------------------------------------

    def stage69_bind_and_carry(self, topo_order):
        trace = []
        prev = None
        for phase in topo_order:
            carry_row = self.m.carry.get(phase) or {}
            credit_out_prev = self.carry_out.get(prev, {}).get("credit", 0) if prev else 0
            debt_out_prev = self.carry_out.get(prev, {}).get("debt", 0) if prev else 0
            credit_in = to_int(carry_row.get("initial_credit", "0")) + credit_out_prev
            debt_in = to_int(carry_row.get("initial_debt", "0")) + debt_out_prev
            effective = self.phase_cap(phase) + credit_in - debt_in
            self.effective_cap[phase] = effective
            members = self.membership.get(phase, [])
            floored = {
                sid: max(self.target[(phase, sid)], self.floors[(phase, sid)])
                for sid in members
            }
            total = sum(floored.values())
            alloc = dict(floored)
            if total > effective:
                self.note("B_CAP_BIND", phase)
                excess = total - effective
                order = sorted(
                    members,
                    key=lambda sid: (
                        self.m.stratum_rank.get(self.source_row(sid).get("safety_stratum"), 0),
                        -to_int(self.source_row(sid).get("priority", "0")),
                        sid,
                    ),
                )
                for sid in order:
                    if excess <= 0:
                        break
                    room = alloc[sid] - self.floors[(phase, sid)]
                    if room > 0:
                        take = min(room, excess)
                        before = alloc[sid]
                        alloc[sid] -= take
                        excess -= take
                        after = alloc[sid]
                        # A displacement that lands on the floor stays in the
                        # region above it; only a cut strictly below the floor
                        # is a floor cut.
                        kind = "floor_cut" if after < self.floors[(phase, sid)] else "above_floor"
                        trace.append((phase, sid, before, after, take, kind))
                        self.counters["capacity_displacements"] += 1
                if excess > 0:
                    self.note("B_FLOOR_OVER_CAP", phase)
                    for sid in order:
                        if excess <= 0:
                            break
                        room = alloc[sid]
                        if room > 0:
                            take = min(room, excess)
                            before = alloc[sid]
                            alloc[sid] -= take
                            excess -= take
                            trace.append((phase, sid, before, alloc[sid], take, "floor_cut"))
                            self.counters["capacity_displacements"] += 1
            for sid in members:
                self.bound[(phase, sid)] = alloc[sid]
            allocated = sum(alloc.values())
            credit_out = effective - allocated if effective > allocated else 0
            debt_out = sum(
                max(0, self.floors[(phase, sid)] - alloc[sid]) for sid in members
            )
            if debt_out > 0:
                self.note("B_CARRY_DEBT", phase)
            self.carry_out[phase] = {"credit": credit_out, "debt": debt_out}
            self.carry_rows.append((phase, credit_in, debt_in, credit_out, debt_out))
            prev = phase
        return trace

    # -- stage 8b ----------------------------------------------------------

    def stage8b_overturns(self, reach):
        rows = []
        for phase in self.phases:
            for prereq in sorted(reach.get(phase, ())):
                for sid in self.membership.get(phase, []):
                    if sid not in self.membership.get(prereq, []):
                        continue
                    there = self.bound.get((prereq, sid), 0)
                    here = self.bound.get((phase, sid), 0)
                    if there == 0 and here > 0:
                        rows.append((phase, prereq, sid))
        for phase, prereq, sid in rows:
            self.counters["precedence_overturns"] += 1
            self.note("B_PRECEDENCE_OVERTURN", phase)
        return rows

    # -- stage 10 ----------------------------------------------------------

    def stage10_reconcile(self, topo_order):
        rows = []
        for sid in [row["source_id"] for row in self.m.sources]:
            supply = self.canonical[sid]
            for phase in topo_order:
                self.final[(phase, sid)] = self.bound.get((phase, sid), 0)
            total = sum(self.bound.get((phase, sid), 0) for phase in topo_order)
            if total <= supply:
                continue
            self.note("B_GLOBAL_CLAWBACK", sid)
            excess = total - supply
            for phase in reversed(topo_order):
                if excess <= 0:
                    break
                before = self.final[(phase, sid)]
                if before <= 0:
                    continue
                take = min(before, excess)
                after = before - take
                self.final[(phase, sid)] = after
                excess -= take
                rows.append((sid, phase, before, after, take))
                self.counters["global_clawbacks"] += 1
        return rows

    # -- stage 11 ----------------------------------------------------------

    def source_examples(self):
        groups = {}
        for row in self.m.examples:
            sid = row.get("source_id")
            if sid is None:
                continue
            groups.setdefault(sid, []).append(row)
        return groups

    def stage11_rational_weights(self, token_totals):
        groups = self.source_examples()
        rows = []
        for row in self.m.sources:
            sid = row["source_id"]
            examples = sorted(groups.get(sid, []), key=lambda r: r["example_id"])
            if not examples:
                continue
            denominator = sum(to_int(ex.get("token_length", "0")) for ex in examples)
            if denominator == 0:
                self.note("B_EMPTY_GROUP", sid)
                continue
            total = token_totals.get(sid, 0)
            for ex in examples:
                length = to_int(ex.get("token_length", "0"))
                num, den = reduce_fraction(total * length, denominator)
                rows.append((ex["example_id"], sid, num, den))
        return rows

    # -- stage 12 ----------------------------------------------------------

    def stage12_materialize(self, token_totals):
        groups = self.source_examples()
        weight_rows = []
        residual_rows = []
        for row in self.m.sources:
            sid = row["source_id"]
            examples = sorted(groups.get(sid, []), key=lambda r: r["example_id"])
            total = token_totals.get(sid, 0)
            lengths = {ex["example_id"]: to_int(ex.get("token_length", "0")) for ex in examples}
            denominator = sum(lengths.values())
            materialized = {ex["example_id"]: 0 for ex in examples}
            if examples and denominator > 0 and total > 0:
                bases = {}
                remainders = {}
                for ex in examples:
                    eid = ex["example_id"]
                    product = total * lengths[eid]
                    bases[eid] = product // denominator
                    remainders[eid] = product - bases[eid] * denominator
                leftover = total - sum(bases.values())
                order = sorted(examples, key=lambda ex: (-remainders[ex["example_id"]],
                                                         ex["example_id"]))
                winners = {ex["example_id"] for ex in order[:leftover]}
                for ex in examples:
                    eid = ex["example_id"]
                    materialized[eid] = bases[eid]
                    if eid in winners:
                        materialized[eid] += 1
                        residual_rows.append((eid, sid, bases[eid], 1))
            for ex in examples:
                self.materialized[ex["example_id"]] = materialized[ex["example_id"]]
                weight_rows.append((ex["example_id"], sid, materialized[ex["example_id"]]))
        return weight_rows, residual_rows

    # -- stage 13 ----------------------------------------------------------

    def stage13_microbatches(self):
        """Pack examples into batches; each batch carries its phase."""
        limit = self.m.adjacency_limit
        cap = self.m.max_seq_tokens
        batches = []
        state = {"members": [], "tokens": 0, "source": None, "run": 0}

        def close():
            if state["members"]:
                batches.append((state["phase"], list(state["members"]), state["tokens"]))
            state["members"] = []
            state["tokens"] = 0
            state["source"] = None
            state["run"] = 0

        for phase in self.phases:
            state["phase"] = phase
            members = [
                sid for sid in self.membership.get(phase, [])
                if self.final.get((phase, sid), 0) > 0
            ]
            for sid in sorted(members):
                eids = sorted(
                    eid for eid, src in self.example_source.items()
                    if src == sid and self.materialized.get(eid, 0) > 0
                )
                for eid in eids:
                    tokens = self.materialized[eid]
                    if not state["members"]:
                        state.update(members=[eid], tokens=tokens, source=sid, run=1)
                        continue
                    same = sid == state["source"]
                    next_run = state["run"] + 1 if same else 1
                    if state["tokens"] + tokens <= cap and next_run <= limit:
                        state["members"].append(eid)
                        state["tokens"] += tokens
                        state["source"] = sid
                        state["run"] = next_run
                    else:
                        close()
                        state["phase"] = phase
                        state.update(members=[eid], tokens=tokens, source=sid, run=1)
            close()
        return batches

    # -- stage 14 ----------------------------------------------------------

    def run(self):
        self.stage1_canonicalize()
        self.stage2_eligibility()
        self.stage3_scoring()
        self.stage45_membership_targets_floors()
        reach = self.build_closure()
        cycle_nodes, removed, surviving = self.stage8_break_cycle(reach)
        topo_order = self.topological_order(surviving)
        self.topo_order = topo_order
        trace = self.stage69_bind_and_carry(topo_order)
        overturns = self.stage8b_overturns(reach)
        clawbacks = self.stage10_reconcile(topo_order)

        token_totals = {}
        for row in self.m.sources:
            sid = row["source_id"]
            token_totals[sid] = sum(
                self.final.get((phase, sid), 0) for phase in topo_order
            )
        rational_rows = self.stage11_rational_weights(token_totals)
        weight_rows, residual_rows = self.stage12_materialize(token_totals)
        batches = self.stage13_microbatches()

        total_tokens = sum(token_totals.values())
        examples_placed = sum(len(members) for _, members, _ in batches)

        invariants = self.check_invariants(batches)

        objective = [
            total_tokens,
            self.counters["global_clawbacks"],
            self.counters["cycle_edges_removed"],
            len(trace),
            len(batches),
        ]

        artifacts = {}
        artifacts["canonical_sources.jsonl"] = [
            {"source_id": row["source_id"], "raw_amount": to_int(row.get("raw_amount", "0")),
             "raw_unit": row.get("raw_unit", ""), "canonical_tokens": self.canonical[row["source_id"]],
             "rounding_tie": self.ties[row["source_id"]]}
            for row in self.m.sources
        ]
        artifacts["eligibility_ledger.jsonl"] = [
            {"source_id": row["source_id"], "eligible": self.eligible[row["source_id"]],
             "code": self.eligibility_code[row["source_id"]]}
            for row in self.m.sources
        ]
        artifacts["demand_score_ledger.jsonl"] = [
            {"source_id": sid, "quality_bp": self.factors[sid]["quality_bp"],
             "fresh_bp": self.factors[sid]["fresh_bp"],
             "priority_factor": self.factors[sid]["priority_factor"],
             "lang_balance_bp": self.factors[sid]["lang_balance_bp"],
             "demand_score": self.demand[sid]}
            for sid in [r["source_id"] for r in self.m.sources if self.eligible[r["source_id"]]]
        ]
        membership_rows = []
        for phase in self.phases:
            for sid in self.membership.get(phase, []):
                membership_rows.append({"phase": phase, "source_id": sid})
        artifacts["phase_membership.jsonl"] = membership_rows
        proportional_rows = []
        floor_rows = []
        for phase in self.phases:
            for sid in self.membership.get(phase, []):
                num, den = self.share[(phase, sid)]
                proportional_rows.append({
                    "phase": phase, "source_id": sid,
                    "target_tokens": self.target[(phase, sid)],
                    "share_num": num, "share_den": den,
                })
                safety_floor, language_floor = self.floor_parts[(phase, sid)]
                floor_rows.append({
                    "phase": phase, "source_id": sid,
                    "safety_floor": safety_floor, "language_floor": language_floor,
                    "floor": self.floors[(phase, sid)],
                })
        artifacts["proportional_allocation.jsonl"] = proportional_rows
        artifacts["protected_floor_ledger.jsonl"] = floor_rows
        artifacts["capacity_binding_trace.jsonl"] = [
            {"phase": phase, "source_id": sid, "before": before, "after": after,
             "displaced": displaced, "kind": kind}
            for phase, sid, before, after, displaced, kind in trace
        ]
        closure_rows = []
        for phase in sorted(reach):
            for requires in sorted(reach[phase]):
                closure_rows.append({"phase": phase, "requires": requires})
        artifacts["precedence_closure.jsonl"] = closure_rows
        artifacts["cycle_report.jsonl"] = (
            [{"cycle_nodes": cycle_nodes}] if cycle_nodes else []
        )
        artifacts["cycle_break_decisions.jsonl"] = (
            [{"edge_id": removed["edge_id"], "from": removed["from"], "to": removed["to"],
              "hardness": removed["hardness"]}] if removed else []
        )
        artifacts["precedence_overturn_trace.jsonl"] = [
            {"phase": phase, "prereq_phase": prereq, "source_id": sid}
            for phase, prereq, sid in overturns
        ]
        artifacts["carry_state_ledger.jsonl"] = [
            {"phase": phase, "credit_in": credit_in, "debt_in": debt_in,
             "credit_out": credit_out, "debt_out": debt_out}
            for phase, credit_in, debt_in, credit_out, debt_out in self.carry_rows
        ]
        artifacts["pre_reconciliation_ledger.jsonl"] = [
            {"phase": phase, "source_id": sid, "tokens": self.bound[(phase, sid)]}
            for phase in self.phases for sid in self.membership.get(phase, [])
        ]
        artifacts["global_reconciliation_trace.jsonl"] = [
            {"source_id": sid, "phase": phase, "before": before, "after": after,
             "clawed": clawed}
            for sid, phase, before, after, clawed in clawbacks
        ]
        artifacts["final_allocation.jsonl"] = [
            {"phase": phase, "source_id": sid, "tokens": self.final[(phase, sid)]}
            for phase in self.phases for sid in self.membership.get(phase, [])
        ]
        scale = self.m.scale
        language_rows = []
        for language in sorted(self.m.languages):
            spec = self.m.languages[language]
            tokens = sum(
                token_totals[row["source_id"]]
                for row in self.m.sources if row.get("language") == language
            )
            language_rows.append({
                "language": language, "tokens": tokens,
                "min_required": round_half_even(to_int(spec.get("min_share_bp", 0)) * total_tokens, scale),
                "max_allowed": round_half_even(to_int(spec.get("max_share_bp", 0)) * total_tokens, scale),
            })
        artifacts["language_allocation_summary.jsonl"] = language_rows
        safety_rows = []
        for stratum in sorted(self.m.strata):
            tokens = sum(
                token_totals[row["source_id"]]
                for row in self.m.sources if row.get("safety_stratum") == stratum
            )
            safety_rows.append({
                "stratum": stratum, "tokens": tokens,
                "floor": to_int(self.m.strata[stratum].get("floor_tokens", 0)),
            })
        artifacts["safety_allocation_summary.jsonl"] = safety_rows
        artifacts["source_allocation_summary.jsonl"] = [
            {"source_id": row["source_id"], "tokens": token_totals[row["source_id"]],
             "canonical_tokens": self.canonical[row["source_id"]]}
            for row in self.m.sources
        ]
        artifacts["rational_example_weights.jsonl"] = [
            {"example_id": eid, "source_id": sid, "w_num": num, "w_den": den}
            for eid, sid, num, den in rational_rows
        ]
        artifacts["fixed_point_weights.jsonl"] = [
            {"example_id": eid, "source_id": sid, "materialized_tokens": tokens}
            for eid, sid, tokens in weight_rows
        ]
        artifacts["rounding_residual_ledger.jsonl"] = [
            {"example_id": eid, "source_id": sid, "base_tokens": base, "residual_unit": unit}
            for eid, sid, base, unit in residual_rows
        ]
        artifacts["microbatch_manifest.jsonl"] = [
            {"batch_id": "BATCH%04d" % index, "phase": phase, "members": members,
             "seq_tokens": seq}
            for index, (phase, members, seq) in enumerate(batches, 1)
        ]
        artifacts["phase_token_totals.jsonl"] = [
            {"phase": phase,
             "allocated": sum(self.final.get((phase, sid), 0)
                              for sid in self.membership.get(phase, [])),
             "cap": self.phase_cap(phase),
             "budget": self.phase_budget(phase)}
            for phase in self.phases
        ]
        artifacts["invariant_report.jsonl"] = [
            {"invariant": name, "holds": bool(value)} for name, value in invariants
        ]
        artifacts["exception_trace.jsonl"] = [
            {"code": code, "ref": ref} for code, ref in self.ordered_exceptions()
        ]

        return artifacts, objective, {
            "total_tokens": total_tokens,
            "examples_placed": examples_placed,
            "invariants_hold": all(bool(value) for _, value in invariants),
        }

    def ordered_exceptions(self):
        rank = {code: index for index, code in enumerate(BRANCH_ORDER)}
        groups = {}
        for code, ref in self.exceptions:
            groups.setdefault(code, []).append((code, ref))
        ordered = []
        for code in sorted(groups, key=lambda c: rank.get(c, len(BRANCH_ORDER))):
            ordered.extend(groups[code])
        return ordered

    def check_invariants(self, batches):
        within_cap = True
        for phase in self.phases:
            allocated = sum(self.final.get((phase, sid), 0)
                            for sid in self.membership.get(phase, []))
            if allocated > self.effective_cap.get(phase, 0):
                within_cap = False
        within_supply = True
        for row in self.m.sources:
            sid = row["source_id"]
            total = sum(self.final.get((phase, sid), 0) for phase in self.phases)
            if total > self.canonical[sid]:
                within_supply = False
        groups = self.source_examples()
        materialized_match = True
        for row in self.m.sources:
            sid = row["source_id"]
            examples = groups.get(sid, [])
            if not examples:
                continue
            # An empty group (zero total token length) materializes zero by
            # the stage 12 rule, so it is not a checkable split.
            if sum(to_int(ex.get("token_length", "0")) for ex in examples) == 0:
                continue
            total = sum(self.final.get((phase, sid), 0) for phase in self.phases)
            placed = sum(self.materialized.get(ex["example_id"], 0) for ex in examples)
            if placed != total:
                materialized_match = False
        cap = self.m.max_seq_tokens
        batches_ok = True
        for _, members, seq in batches:
            if len(members) > 1 and seq > cap:
                batches_ok = False
        return [
            ("phase_within_cap", within_cap),
            ("source_within_supply", within_supply),
            ("materialized_sums_match", materialized_match),
            ("batches_respect_seq_cap", batches_ok),
        ]


# --------------------------------------------------------------------------
# serialization
# --------------------------------------------------------------------------

def dump_lines(rows):
    if not rows:
        return ""
    return "".join(
        json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
        for row in rows
    )


def dump_object(value):
    return json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def digest_bytes(data):
    return hashlib.sha256(data).hexdigest()


def main(argv):
    if len(argv) != 3:
        sys.stderr.write("usage: plan.py <DATA_DIR> <OUT_DIR>\n")
        return 2
    data_dir, out_dir = argv[1], argv[2]
    if not os.path.isdir(data_dir):
        sys.stderr.write("data directory not found: %s\n" % data_dir)
        return 2
    os.makedirs(out_dir, exist_ok=True)

    manifest = Manifest(data_dir)
    compiler = Compiler(manifest)
    artifacts, objective, summary = compiler.run()

    payloads = {}
    for name in LINES_FILES:
        payloads[name] = dump_lines(artifacts[name])

    program_source = os.path.abspath(__file__)
    delivered = os.path.join(out_dir, "plan.py")
    if os.path.abspath(delivered) != program_source:
        shutil.copyfile(program_source, delivered)
    with open(delivered, "rb") as handle:
        program_bytes = handle.read()

    payloads["objective_tuple.json"] = dump_object({
        "objective": objective,
        "order": list(OBJECTIVE_ORDER),
    })

    manifest_map = {}
    for name in LINES_FILES:
        manifest_map[name] = digest_bytes(payloads[name].encode("utf-8"))
    manifest_map["objective_tuple.json"] = digest_bytes(
        payloads["objective_tuple.json"].encode("utf-8"))
    manifest_map["plan.py"] = digest_bytes(program_bytes)
    payloads["integrity_manifest.json"] = dump_object(manifest_map)

    chain = "0" * 64
    for name in sorted(manifest_map):
        chain = digest_bytes((chain + manifest_map[name]).encode("utf-8"))
    payloads["final_state.json"] = dump_object({
        "chain": chain,
        "total_tokens": summary["total_tokens"],
        "global_clawbacks": compiler.counters["global_clawbacks"],
        "capacity_displacements": len(artifacts["capacity_binding_trace.jsonl"]),
        "cycle_edges_removed": compiler.counters["cycle_edges_removed"],
        "precedence_overturns": compiler.counters["precedence_overturns"],
        "microbatches": len(artifacts["microbatch_manifest.jsonl"]),
        "examples_placed": summary["examples_placed"],
        "quiet_branches": len(artifacts["exception_trace.jsonl"]),
        "objective": objective,
        "invariants_hold": summary["invariants_hold"],
    })

    for name in LINES_FILES + list(JSON_FILES):
        with open(os.path.join(out_dir, name), "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payloads[name])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
