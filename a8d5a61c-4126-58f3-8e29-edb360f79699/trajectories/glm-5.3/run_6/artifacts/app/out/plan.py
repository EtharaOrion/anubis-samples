#!/usr/bin/env python3
"""TrainMix allocation compiler.

Derives the training mixture certificate from a data directory of source,
quality, freshness, example, capacity, carry state, policy and curriculum
records.  Usage:

    python3 plan.py <DATA_DIR> <OUT_DIR>

Every number is an integer or an exact rational.  No float is constructed,
compared or serialized anywhere: all divisions go through the half to even
quantization of `unit_rules.json` (or a documented exact branch).  Runs are
deterministic and the data directory is only ever read.
"""

import csv
import hashlib
import json
import os
import sys

# ---------------------------------------------------------------------------
# exact rational helpers
# ---------------------------------------------------------------------------


def rhe(p, d):
    """round_half_even(p, d) for integer p over positive integer d.

    Quotient and remainder, round up when twice the remainder exceeds d,
    on an exact half round to the even quotient.  A zero denominator never
    yields a rounded value; callers branch on it before getting here.
    """
    q, r = divmod(p, d)
    twice = 2 * r
    if twice > d:
        return q + 1
    if twice < d:
        return q
    return q if q % 2 == 0 else q + 1


def rhe_tie(p, d):
    """True when p over d lands on an exact half."""
    return 2 * (p % d) == d


def gcd(a, b):
    while b:
        a, b = b, a % b
    return a


def reduced(n, d):
    """Fully reduced fraction with a positive denominator."""
    if d < 0:
        n, d = -n, -d
    g = gcd(abs(n), d) or 1
    return n // g, d // g


# ---------------------------------------------------------------------------
# fixed rule constants
# ---------------------------------------------------------------------------

PHASE_ORDER = ["PH1", "PH2", "PH3"]

# fixed domain to phase mapping of stage 4
DOMAIN_PHASES = {
    "maint": ["PH1"],
    "manual": ["PH1", "PH2"],
    "incident": ["PH2"],
    "qa": ["PH2", "PH3"],
    "translated": ["PH1", "PH3"],
    "synthetic": ["PH3"],
    "reg": ["PH2"],
}

# quiet branch codes in the order the stages activate them
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


def read_csv_rows(path):
    rows = []
    with open(path, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            rows.append({k: (v.strip() if isinstance(v, str) else v) for k, v in row.items()})
    return rows


def to_int(text):
    text = (text or "").strip()
    return 0 if text == "" else int(text)


def write_jsonl(out_dir, name, rows):
    """Compact separators, sorted keys, one object per line, trailing newline.

    An empty document is the empty string.
    """
    lines = [
        json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        for row in rows
    ]
    write_text(out_dir, name, "".join(line + "\n" for line in lines))


def write_json_object(out_dir, name, obj):
    """Sorted keys, two space indentation, one trailing newline."""
    text = json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False)
    write_text(out_dir, name, text + "\n")


def write_text(out_dir, name, text):
    with open(os.path.join(out_dir, name), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def digest_file(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


class Compiler:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.load_inputs()
        self.exceptions = []  # (code, ref) in activation order
        self.capacity_trace = []
        self.overturn_rows = []
        self.cycle_break_rows = []
        self.recon_rows = []

    # -- inputs -----------------------------------------------------------

    def load_json(self, name):
        with open(os.path.join(self.data_dir, name), encoding="utf-8") as handle:
            return json.load(handle)

    def load_inputs(self):
        d = self.data_dir
        self.unit_rules = self.load_json("unit_rules.json")
        self.scale = int(self.unit_rules["scale"])
        self.quality_policy = self.load_json("quality_policy.json")
        self.lang_policy = self.load_json("language_policy.json")
        self.languages = self.lang_policy["languages"]
        self.safety_policy = self.load_json("safety_policy.json")
        self.strata = self.safety_policy["strata"]
        self.scoring = self.load_json("scoring_weights.json")
        self.priority_order = self.load_json("priority_order.json")
        self.stratum_rank = self.priority_order["stratum_displacement_rank"]
        self.curriculum = self.load_json("curriculum.json")
        self.batch_rules = self.load_json("batch_rules.json")
        self.blocked_domains = list(self.quality_policy.get("blocked_domains") or [])
        self.min_quality = self.quality_policy["min_quality"]

        self.sources = read_csv_rows(os.path.join(d, "sources.csv"))
        self.quality = {}
        for row in read_csv_rows(os.path.join(d, "quality.csv")):
            self.quality[row["source_id"]] = (to_int(row["q_num"]), to_int(row["q_den"]))
        self.freshness = {}
        for row in read_csv_rows(os.path.join(d, "freshness.csv")):
            self.freshness[row["source_id"]] = to_int(row["age_days"])
        self.capacity = {}
        for row in read_csv_rows(os.path.join(d, "capacity.csv")):
            self.capacity[row["phase"]] = (
                to_int(row["budget_tokens"]),
                to_int(row["cap_tokens"]),
            )
        self.initial_carry = {}
        for row in read_csv_rows(os.path.join(d, "carry_state.csv")):
            self.initial_carry[row["phase"]] = (
                to_int(row["initial_credit"]),
                to_int(row["initial_debt"]),
            )
        self.examples = read_csv_rows(os.path.join(d, "examples.csv"))

        self.source_ids = [row["source_id"] for row in self.sources]
        self.source = {row["source_id"]: row for row in self.sources}
        self.phases = list(self.curriculum.get("phases") or PHASE_ORDER)
        self.example_token = {}
        self.examples_by_source = {}
        for row in self.examples:
            eid = row["example_id"]
            self.example_token[eid] = to_int(row["token_length"])
            self.examples_by_source.setdefault(row["source_id"], []).append(eid)
        for sid in self.examples_by_source:
            self.examples_by_source[sid].sort()

    # -- quiet branches ----------------------------------------------------

    def branch(self, code, ref):
        """Record a quiet branch activation, in the order it happens."""
        self.exceptions.append((code, ref))

    # -- stage 1, manifest canonicalization --------------------------------

    def stage1(self):
        self.canonical = {}
        self.rounding_tie = {}
        self.canonical_rows = []
        for sid in self.source_ids:
            row = self.source[sid]
            amount = to_int(row["raw_amount"])
            unit = row["raw_unit"]
            rational = self.unit_rules["units"].get(unit)
            if rational is None:
                num, den = 1, 1
            else:
                num, den = int(rational["num"]), int(rational["den"])
            if den == 0:
                # a zero unit denominator yields no rounded value; the
                # canonical count stays empty, which fails stage 2 on tokens
                tokens = 0
                tie = False
            else:
                tokens = rhe(amount * num, den)
                tie = rhe_tie(amount * num, den)
            self.canonical[sid] = tokens
            self.rounding_tie[sid] = tie
            self.canonical_rows.append(
                {
                    "source_id": sid,
                    "raw_amount": amount,
                    "raw_unit": unit,
                    "canonical_tokens": tokens,
                    "rounding_tie": tie,
                }
            )
            if tie:
                self.branch("B_UNIT_ROUND_TIE", sid)

    # -- stage 2, eligibility projection -----------------------------------

    def stage2(self):
        self.eligible = {}
        self.eligibility_rows = []
        m_num, m_den = int(self.min_quality["num"]), int(self.min_quality["den"])
        for sid in self.source_ids:
            row = self.source[sid]
            q_num, q_den = self.quality.get(sid, (0, 0))
            language = row["language"]
            stratum = row["safety_stratum"]
            domain = row["domain"]
            if q_den == 0 or q_num * m_den < m_num * q_den:
                code = "E_LOW_QUALITY"
            elif language not in self.languages:
                code = "E_LANG_EXCLUDED"
            elif stratum not in self.strata:
                code = "E_STRATUM_UNKNOWN"
            elif domain in self.blocked_domains:
                code = "E_DOMAIN_BLOCKED"
            elif self.canonical[sid] <= 0:
                code = "E_EMPTY_TOKENS"
            else:
                code = "E_OK"
            self.eligible[sid] = code == "E_OK"
            self.eligibility_rows.append(
                {"source_id": sid, "eligible": code == "E_OK", "code": code}
            )

    # -- stage 3, demand scoring -------------------------------------------

    def stage3(self):
        half_life = int(self.scoring["half_life_days"])
        priority_weights = {
            int(k): int(v) for k, v in self.scoring["priority_weights"].items()
        }
        self.demand = {}
        self.demand_rows = []
        for sid in self.source_ids:
            if not self.eligible[sid]:
                continue
            row = self.source[sid]
            q_num, q_den = self.quality.get(sid, (0, 0))
            age = self.freshness.get(sid, 0)
            fresh_den = half_life + age
            quality_bp = rhe(q_num * self.scale, q_den) if q_den > 0 else 0
            fresh_bp = rhe(half_life * self.scale, fresh_den) if fresh_den > 0 else 0
            priority_factor = priority_weights.get(int(row["priority"]), 0)
            lang_balance_bp = int(
                self.languages.get(row["language"], {}).get("balance_bp", 0)
            )
            s1 = rhe(self.canonical[sid] * quality_bp, self.scale)
            s2 = rhe(s1 * fresh_bp, self.scale)
            s3 = s2 * priority_factor
            score = rhe(s3 * lang_balance_bp, self.scale)
            self.demand[sid] = score
            self.demand_rows.append(
                {
                    "source_id": sid,
                    "quality_bp": quality_bp,
                    "fresh_bp": fresh_bp,
                    "priority_factor": priority_factor,
                    "lang_balance_bp": lang_balance_bp,
                    "demand_score": score,
                }
            )

    # -- stage 4, phase membership and proportional allocation -------------

    def stage4(self):
        self.members = {p: [] for p in self.phases}
        self.membership_rows = []
        self.target = {}
        self.share_num = {}
        self.share_den = {}
        self.proportional_rows = []
        self.demand_sum = {}
        for sid in self.source_ids:
            if not self.eligible[sid]:
                continue
            for phase in DOMAIN_PHASES.get(self.source[sid]["domain"], []):
                self.members[phase].append(sid)
                self.membership_rows.append({"phase": phase, "source_id": sid})
        for phase in self.phases:
            self.members[phase].sort()
            budget = self.capacity.get(phase, (0, 0))[0]
            scored = [s for s in self.members[phase] if self.demand.get(s, 0) > 0]
            total = sum(self.demand.get(s, 0) for s in scored)
            self.demand_sum[phase] = total
            for sid in self.members[phase]:
                if self.demand.get(sid, 0) > 0 and total > 0:
                    num = self.demand[sid] * budget
                    target = rhe(num, total)
                else:
                    num = 0
                    target = 0
                self.target[(phase, sid)] = target
                self.share_num[(phase, sid)] = num
                self.share_den[(phase, sid)] = total
                self.proportional_rows.append(
                    {
                        "phase": phase,
                        "source_id": sid,
                        "target_tokens": target,
                        "share_num": num,
                        "share_den": total,
                    }
                )

    # -- stage 5, protected floors -----------------------------------------

    def stage5(self):
        self.floor = {}
        self.floor_rows = []
        self.floored = {}
        for phase in self.phases:
            for sid in self.members[phase]:
                row = self.source[sid]
                safety_floor = int(
                    self.strata.get(row["safety_stratum"], {}).get("floor_tokens", 0)
                )
                language_floor = int(
                    self.languages.get(row["language"], {}).get("floor_tokens", 0)
                )
                value = safety_floor if safety_floor > language_floor else language_floor
                self.floor[(phase, sid)] = value
                self.floored[(phase, sid)] = max(self.target[(phase, sid)], value)
                self.floor_rows.append(
                    {
                        "phase": phase,
                        "source_id": sid,
                        "safety_floor": safety_floor,
                        "language_floor": language_floor,
                        "floor": value,
                    }
                )

    # -- stage 7, curriculum precedence closure ----------------------------

    def build_reach(self, edges):
        """Forward reachability: reach[p] is every phase p can walk to.

        An edge from A to B means A precedes B, so B requires A.  The
        requires relation of a phase is therefore the set of phases that can
        walk to it.
        """
        adj = {p: set() for p in self.phases}
        for e in edges:
            adj.setdefault(e["from"], set()).add(e["to"])
        reach = {}
        for p in self.phases:
            seen = set()
            stack = list(adj.get(p, ()))
            while stack:
                n = stack.pop()
                if n in seen:
                    continue
                seen.add(n)
                stack.extend(adj.get(n, ()))
            reach[p] = seen
        return adj, reach

    def requires_of(self, edges):
        """Closure of the requires relation: who must run before each phase.

        An edge from A to B means A precedes B, so B requires A, and the
        closure is unfiltered: a phase on a cycle requires itself, which is
        how a cycle node is recognised downstream.
        """
        _adj, reach = self.build_reach(edges)
        requires = {p: set() for p in self.phases}
        for p in self.phases:
            for q in self.phases:
                if p in reach[q]:
                    requires[p].add(q)
        return requires

    def stage7(self):
        self.edges = list(self.curriculum.get("edges") or [])
        self.adj, self.reach = self.build_reach(self.edges)
        self.closure = self.requires_of(self.edges)
        self.closure_rows = []
        for p in sorted(self.closure):
            for r in sorted(self.closure[p]):
                self.closure_rows.append({"phase": p, "requires": r})

    def topo_order(self, edges):
        """Kahn order over the surviving edges, ties broken by phase name."""
        indeg = {p: 0 for p in self.phases}
        adj = {p: [] for p in self.phases}
        for e in edges:
            adj.setdefault(e["from"], []).append(e["to"])
            indeg[e["to"]] = indeg.get(e["to"], 0) + 1
        ready = sorted(p for p in self.phases if indeg[p] == 0)
        order = []
        while ready:
            node = ready.pop(0)
            order.append(node)
            for t in sorted(adj[node]):
                indeg[t] -= 1
                if indeg[t] == 0:
                    ready.append(t)
            ready.sort()
        for p in sorted(self.phases):
            if p not in order:
                order.append(p)
        return order

    # -- stage 8, cycle break ----------------------------------------------

    def stage8(self):
        # a phase reachable from itself sits on a cycle
        cycle_nodes = sorted(p for p in self.phases if p in self.reach[p])
        self.cycle_report_rows = [{"cycle_nodes": cycle_nodes}]
        self.removed_edge = None
        self.cycle_break_rows = []
        if cycle_nodes:
            hard_rank = {"soft": 0, "hard": 1}
            candidates = [
                e
                for e in self.edges
                if e["from"] in cycle_nodes and e["to"] in cycle_nodes
            ]
            candidates.sort(
                key=lambda e: (
                    hard_rank.get(e["hardness"], 0),
                    e["from"],
                    e["to"],
                    e["edge_id"],
                )
            )
            if candidates:
                self.removed_edge = candidates[0]
                self.cycle_break_rows = [
                    {
                        "edge_id": self.removed_edge["edge_id"],
                        "from": self.removed_edge["from"],
                        "to": self.removed_edge["to"],
                        "hardness": self.removed_edge["hardness"],
                    }
                ]
                self.branch("B_CYCLE_BREAK", self.removed_edge["edge_id"])
        self.surviving = [e for e in self.edges if e is not self.removed_edge]
        self.cycle_edges_removed = 1 if self.removed_edge is not None else 0

    # -- stage 6, capacity binding displacement ----------------------------

    def displacement_order(self, phase):
        def key(sid):
            row = self.source[sid]
            rank = int(
                self.stratum_rank.get(row["safety_stratum"], len(self.stratum_rank))
            )
            return (rank, -int(row["priority"]), sid)

        return sorted(self.members[phase], key=key)

    def stage6(self, phase, eff_cap):
        alloc = {sid: self.floored[(phase, sid)] for sid in self.members[phase]}
        if any(
            self.floored[(phase, sid)] > self.target[(phase, sid)]
            for sid in self.members[phase]
        ):
            self.branch("B_FLOOR_BIND", phase)
        excess = sum(alloc.values()) - eff_cap
        if excess > 0:
            self.branch("B_CAP_BIND", phase)
        order = self.displacement_order(phase)
        for sid in order:
            if excess <= 0:
                break
            room = alloc[sid] - self.floor[(phase, sid)]
            if room <= 0:
                continue
            take = room if room < excess else excess
            before = alloc[sid]
            alloc[sid] = before - take
            excess -= take
            self.capacity_trace.append(
                {
                    "phase": phase,
                    "source_id": sid,
                    "before": before,
                    "after": alloc[sid],
                    "displaced": take,
                    "kind": "above_floor",
                }
            )
        if excess > 0:
            self.branch("B_FLOOR_OVER_CAP", phase)
            for sid in order:
                if excess <= 0:
                    break
                room = alloc[sid]
                if room <= 0:
                    continue
                take = room if room < excess else excess
                before = alloc[sid]
                alloc[sid] = before - take
                excess -= take
                self.capacity_trace.append(
                    {
                        "phase": phase,
                        "source_id": sid,
                        "before": before,
                        "after": alloc[sid],
                        "displaced": take,
                        "kind": "floor_cut",
                    }
                )
        return alloc

    # -- stages 6, 8, 9 and 10 over the phase graph -------------------------

    def allocate_phases(self):
        self.order = self.topo_order(self.surviving)
        self.surviving_requires = self.requires_of(self.surviving)
        credit_in = {p: 0 for p in self.phases}
        debt_in = {p: 0 for p in self.phases}
        for p in self.phases:
            initial_credit, initial_debt = self.initial_carry.get(p, (0, 0))
            credit_in[p] += initial_credit
            debt_in[p] += initial_debt
        self.alloc = {}
        self.carry_rows = []
        self.pre_recon_rows = []
        self.eff_cap_of = {}
        for index, phase in enumerate(self.order):
            credit, debt = credit_in[phase], debt_in[phase]
            eff_cap = self.capacity.get(phase, (0, 0))[1] + credit - debt
            self.eff_cap_of[phase] = eff_cap
            alloc = self.stage6(phase, eff_cap)
            self.alloc[phase] = alloc
            for sid in self.members[phase]:
                self.pre_recon_rows.append(
                    {"phase": phase, "source_id": sid, "tokens": alloc[sid]}
                )
            used = sum(alloc.values())
            credit_out = eff_cap - used
            if credit_out < 0:
                credit_out = 0
            deferred = sum(
                self.floor[(phase, sid)] - alloc[sid]
                for sid in self.members[phase]
                if alloc[sid] < self.floor[(phase, sid)]
            )
            debt_out = deferred
            if debt_out > 0:
                self.branch("B_CARRY_DEBT", phase)
            self.carry_rows.append(
                {
                    "phase": phase,
                    "credit_in": credit,
                    "debt_in": debt,
                    "credit_out": credit_out,
                    "debt_out": debt_out,
                }
            )
            if index + 1 < len(self.order):
                nxt = self.order[index + 1]
                credit_in[nxt] += credit_out
                debt_in[nxt] += debt_out
        self.stage8_overturn()
        self.reconcile()

    def stage8_overturn(self):
        """A positive allocation where a required phase gave the source zero."""
        self.overturn_rows = []
        for phase in self.phases:
            for sid in self.members[phase]:
                if self.alloc[phase][sid] <= 0:
                    continue
                for prereq in sorted(self.surviving_requires.get(phase, ())):
                    if sid not in self.members.get(prereq, ()):
                        continue
                    if self.alloc[prereq][sid] > 0:
                        continue
                    row = {"phase": phase, "prereq_phase": prereq, "source_id": sid}
                    if row not in self.overturn_rows:
                        self.overturn_rows.append(row)
                        self.branch("B_PRECEDENCE_OVERTURN", phase)
        self.precedence_overturns = len(self.overturn_rows)

    def reconcile(self):
        """Stage 10, claw the excess back from the later phases first."""
        self.recon_rows = []
        self.final = {p: dict(self.alloc[p]) for p in self.phases}
        for sid in self.source_ids:
            phases_of = [p for p in self.order if sid in self.members[p]]
            supply = self.canonical[sid]
            excess = sum(self.final[p][sid] for p in phases_of) - supply
            if excess <= 0:
                continue
            self.branch("B_GLOBAL_CLAWBACK", sid)
            for phase in reversed(phases_of):
                if excess <= 0:
                    break
                have = self.final[phase][sid]
                if have <= 0:
                    continue
                take = have if have < excess else excess
                self.final[phase][sid] = have - take
                excess -= take
                self.recon_rows.append(
                    {
                        "source_id": sid,
                        "phase": phase,
                        "before": have,
                        "after": self.final[phase][sid],
                        "clawed": take,
                    }
                )

    # -- stage 11, rational example weights --------------------------------

    def stage11(self):
        self.total_of = {}
        for sid in self.source_ids:
            self.total_of[sid] = sum(
                self.final[p][sid] for p in self.phases if sid in self.members[p]
            )
        self.rational_rows = []
        for sid in self.source_ids:
            eids = self.examples_by_source.get(sid, [])
            if not eids:
                continue
            group_den = sum(self.example_token[e] for e in eids)
            if group_den == 0:
                self.branch("B_EMPTY_GROUP", sid)
                continue
            total = self.total_of[sid]
            for eid in eids:
                num, den = reduced(total * self.example_token[eid], group_den)
                self.rational_rows.append(
                    {
                        "example_id": eid,
                        "source_id": sid,
                        "w_num": num,
                        "w_den": den,
                    }
                )

    # -- stage 12, integer weight materialization --------------------------

    def stage12(self):
        """Largest remainder split of a source total over its examples.

        Each example takes the integer quotient of total times its length
        over the group denominator, and the leftover is handed out one unit
        at a time in descending remainder order, ties by ascending example
        id.  An empty group, a source with no examples or a source with a
        zero token total materializes zero for each of its examples.
        """
        self.materialized = {}
        self.fixed_rows = []
        self.residual_rows = []
        for sid in self.source_ids:
            eids = self.examples_by_source.get(sid, [])
            lengths = {e: self.example_token[e] for e in eids}
            weight_sum = sum(lengths.values())
            total = self.total_of[sid]
            base = {}
            remainders = {}
            for eid in eids:
                product = total * lengths[eid]
                base[eid] = product // weight_sum if weight_sum > 0 else 0
                remainders[eid] = product % weight_sum if weight_sum > 0 else 0
            leftover = total - sum(base.values()) if weight_sum > 0 else 0
            order = (
                sorted(eids, key=lambda e: (-remainders[e], e))
                if weight_sum > 0
                else []
            )
            given = set()
            for eid in order:
                if leftover <= 0:
                    break
                given.add(eid)
                leftover -= 1
            for eid in eids:
                self.materialized[eid] = base[eid] + 1 if eid in given else base[eid]
                self.fixed_rows.append(
                    {
                        "example_id": eid,
                        "source_id": sid,
                        "materialized_tokens": self.materialized[eid],
                    }
                )
                if eid in given:
                    self.residual_rows.append(
                        {
                            "example_id": eid,
                            "source_id": sid,
                            "base_tokens": base[eid],
                            "residual_unit": 1,
                        }
                    )

    # -- stage 13, microbatch construction ---------------------------------

    def stage13(self):
        cap = int(self.batch_rules["max_seq_tokens"])
        adjacency = int(self.batch_rules["adjacency_limit"])
        self.batches = []
        for phase in self.phases:
            entries = []
            for sid in self.members[phase]:
                if self.final[phase].get(sid, 0) <= 0:
                    continue
                for eid in self.examples_by_source.get(sid, []):
                    if self.materialized.get(eid, 0) > 0:
                        entries.append(eid)
            members = []
            run_source = []
            run_total = 0
            for eid in entries:
                sid = self.example_source[eid]
                tok = self.materialized[eid]
                run_len = len(run_source) + 1 if run_source and run_source[-1] == sid else 1
                if members and (run_total + tok > cap or run_len > adjacency):
                    self.batches.append((phase, list(members), run_total))
                    members = []
                    run_source = []
                    run_total = 0
                    run_len = 1
                members.append(eid)
                run_source.append(sid)
                run_total += tok
            if members:
                self.batches.append((phase, list(members), run_total))
        self.microbatch_rows = [
            {
                "batch_id": "BATCH" + str(i + 1).zfill(4),
                "phase": phase,
                "members": members,
                "seq_tokens": total,
            }
            for i, (phase, members, total) in enumerate(self.batches)
        ]
        self.microbatches = len(self.microbatch_rows)

    # -- stage 14, summaries, invariants, certificate ----------------------

    def stage14(self):
        self.final_rows = []
        for phase in self.phases:
            for sid in self.members[phase]:
                self.final_rows.append(
                    {
                        "phase": phase,
                        "source_id": sid,
                        "tokens": self.final[phase][sid],
                    }
                )
        self.total_tokens = sum(row["tokens"] for row in self.final_rows)

        self.language_rows = []
        for language in sorted(self.languages):
            policy = self.languages[language]
            tokens = sum(
                self.total_of[sid]
                for sid in self.source_ids
                if self.source[sid]["language"] == language
            )
            self.language_rows.append(
                {
                    "language": language,
                    "tokens": tokens,
                    "min_required": rhe(
                        int(policy["min_share_bp"]) * self.total_tokens, self.scale
                    ),
                    "max_allowed": rhe(
                        int(policy["max_share_bp"]) * self.total_tokens, self.scale
                    ),
                }
            )

        self.safety_rows = []
        for stratum in sorted(self.strata):
            tokens = sum(
                self.total_of[sid]
                for sid in self.source_ids
                if self.source[sid]["safety_stratum"] == stratum
            )
            self.safety_rows.append(
                {
                    "stratum": stratum,
                    "tokens": tokens,
                    "floor": int(self.strata[stratum]["floor_tokens"]),
                }
            )

        self.source_rows = [
            {
                "source_id": sid,
                "tokens": self.total_of[sid],
                "canonical_tokens": self.canonical[sid],
            }
            for sid in self.source_ids
        ]

        self.phase_total_rows = []
        for phase in self.phases:
            budget, cap = self.capacity.get(phase, (0, 0))
            self.phase_total_rows.append(
                {
                    "phase": phase,
                    "allocated": sum(
                        self.final[phase][sid] for sid in self.members[phase]
                    ),
                    "cap": cap,
                    "budget": budget,
                }
            )

    def build_invariants(self):
        cap = int(self.batch_rules["max_seq_tokens"])
        phase_within_cap = all(
            sum(self.final[p].values()) <= self.eff_cap_of[p] for p in self.phases
        )
        source_within_supply = all(
            self.total_of[sid] <= self.canonical[sid] for sid in self.source_ids
        )
        materialized_sums_match = True
        for sid in self.source_ids:
            eids = self.examples_by_source.get(sid) or []
            if not eids:
                continue
            if sum(self.example_token[e] for e in eids) == 0:
                # an empty group materializes zero for each example by rule
                continue
            if sum(self.materialized[e] for e in eids) != self.total_of[sid]:
                materialized_sums_match = False
        batches_respect_seq_cap = all(
            total <= cap or len(members) == 1 for _p, members, total in self.batches
        )
        self.invariant_rows = [
            {"invariant": "phase_within_cap", "holds": phase_within_cap},
            {"invariant": "source_within_supply", "holds": source_within_supply},
            {"invariant": "materialized_sums_match", "holds": materialized_sums_match},
            {"invariant": "batches_respect_seq_cap", "holds": batches_respect_seq_cap},
        ]
        self.invariants_hold = all(row["holds"] for row in self.invariant_rows)

    # -- driver ------------------------------------------------------------

    def run(self, out_dir):
        self.stage1()
        self.stage2()
        self.stage3()
        self.stage4()
        self.stage5()
        self.stage7()
        self.stage8()
        self.example_source = {row["example_id"]: row["source_id"] for row in self.examples}
        self.allocate_phases()
        self.stage11()
        self.stage12()
        self.stage13()
        self.stage14()
        self.build_invariants()
        self.exception_rows = [{"code": code, "ref": ref} for code, ref in self.exceptions]

        jsonl_files = [
            ("canonical_sources.jsonl", self.canonical_rows),
            ("eligibility_ledger.jsonl", self.eligibility_rows),
            ("demand_score_ledger.jsonl", self.demand_rows),
            ("phase_membership.jsonl", self.membership_rows),
            ("proportional_allocation.jsonl", self.proportional_rows),
            ("protected_floor_ledger.jsonl", self.floor_rows),
            ("capacity_binding_trace.jsonl", self.capacity_trace),
            ("precedence_closure.jsonl", self.closure_rows),
            ("cycle_report.jsonl", self.cycle_report_rows),
            ("cycle_break_decisions.jsonl", self.cycle_break_rows),
            ("precedence_overturn_trace.jsonl", self.overturn_rows),
            ("carry_state_ledger.jsonl", self.carry_rows),
            ("pre_reconciliation_ledger.jsonl", self.pre_recon_rows),
            ("global_reconciliation_trace.jsonl", self.recon_rows),
            ("final_allocation.jsonl", self.final_rows),
            ("language_allocation_summary.jsonl", self.language_rows),
            ("safety_allocation_summary.jsonl", self.safety_rows),
            ("source_allocation_summary.jsonl", self.source_rows),
            ("rational_example_weights.jsonl", self.rational_rows),
            ("fixed_point_weights.jsonl", self.fixed_rows),
            ("rounding_residual_ledger.jsonl", self.residual_rows),
            ("microbatch_manifest.jsonl", self.microbatch_rows),
            ("phase_token_totals.jsonl", self.phase_total_rows),
            ("invariant_report.jsonl", self.invariant_rows),
            ("exception_trace.jsonl", self.exception_rows),
        ]
        for name, rows in jsonl_files:
            write_jsonl(out_dir, name, rows)

        objective = [
            self.total_tokens,
            len(self.recon_rows),
            self.cycle_edges_removed,
            len(self.capacity_trace),
            self.microbatches,
        ]
        order_names = [
            "total_tokens",
            "global_clawbacks",
            "cycle_edges_removed",
            "capacity_displacements",
            "microbatches",
        ]
        write_json_object(
            out_dir, "objective_tuple.json", {"objective": objective, "order": order_names}
        )

        manifest = {}
        for name, _rows in jsonl_files:
            manifest[name] = digest_file(os.path.join(out_dir, name))
        manifest["objective_tuple.json"] = digest_file(
            os.path.join(out_dir, "objective_tuple.json")
        )
        # the program writes its own source into the output directory too
        with open(os.path.abspath(__file__), "rb") as handle:
            program_bytes = handle.read()
        program_dest = os.path.join(out_dir, "plan.py")
        if os.path.abspath(program_dest) != os.path.abspath(__file__):
            with open(program_dest, "wb") as handle:
                handle.write(program_bytes)
        manifest["plan.py"] = digest_file(program_dest)
        write_json_object(out_dir, "integrity_manifest.json", manifest)

        chain = "0" * 64
        for name in sorted(manifest):
            chain = hashlib.sha256((chain + manifest[name]).encode("utf-8")).hexdigest()
        write_json_object(
            out_dir,
            "final_state.json",
            {
                "chain": chain,
                "total_tokens": self.total_tokens,
                "global_clawbacks": len(self.recon_rows),
                "capacity_displacements": len(self.capacity_trace),
                "cycle_edges_removed": self.cycle_edges_removed,
                "precedence_overturns": self.precedence_overturns,
                "microbatches": self.microbatches,
                "examples_placed": sum(len(m) for _p, m, _t in self.batches),
                "quiet_branches": len(self.exceptions),
                "objective": objective,
                "invariants_hold": self.invariants_hold,
            },
        )


def main(argv):
    if len(argv) != 3:
        sys.stderr.write("usage: plan.py <DATA_DIR> <OUT_DIR>\n")
        return 2
    data_dir, out_dir = argv[1], argv[2]
    os.makedirs(out_dir, exist_ok=True)
    Compiler(data_dir).run(out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
