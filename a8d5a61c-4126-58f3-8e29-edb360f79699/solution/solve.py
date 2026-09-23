#!/usr/bin/env python3
"""TrainMix allocation compiler (reference oracle).

Independent implementation written against the specification documents shipped
in the data directory (compiler_spec.md, scoring_rules.md, allocation_rules.md,
weight_rules.md, serialization_rules.md, artifact_contract.md, reason_codes.md).
It shares no code with the verifier: it re-derives the fourteen stage chain from
the written rules and reads its policy constants out of the manifest files rather
than hardcoding them. Every value is an integer or an exact rational; nothing is
ever a float. Usage: plan.py <DATA_DIR> <OUT_DIR>.
"""
from __future__ import annotations

import json
import sys
from fractions import Fraction
from hashlib import sha256
from pathlib import Path

# Fixed phase iteration order and the domain to phase membership table are the
# only structural facts the spec states in prose rather than in a data file.
PHASE_SEQUENCE = ("PH1", "PH2", "PH3")
DOMAIN_TO_PHASES = {
    "maint": ("PH1",),
    "manual": ("PH1", "PH2"),
    "incident": ("PH2",),
    "qa": ("PH2", "PH3"),
    "translated": ("PH1", "PH3"),
    "synthetic": ("PH3",),
    "reg": ("PH2",),
}
JSONL_ORDER = (
    "canonical_sources", "eligibility_ledger", "demand_score_ledger",
    "phase_membership", "proportional_allocation", "protected_floor_ledger",
    "capacity_binding_trace", "precedence_closure", "cycle_report",
    "cycle_break_decisions", "precedence_overturn_trace", "carry_state_ledger",
    "pre_reconciliation_ledger", "global_reconciliation_trace", "final_allocation",
    "language_allocation_summary", "safety_allocation_summary",
    "source_allocation_summary", "rational_example_weights", "fixed_point_weights",
    "rounding_residual_ledger", "microbatch_manifest", "phase_token_totals",
    "invariant_report", "exception_trace",
)
ZERO_CHAIN = "0" * 64


def bankers(numerator, denominator):
    """Round numerator/denominator half to even; a non positive denominator yields 0."""
    if denominator <= 0:
        return 0
    whole, leftover = divmod(numerator, denominator)
    twice = leftover << 1
    if twice > denominator or (twice == denominator and whole & 1):
        return whole + 1
    return whole


def as_line(record):
    return json.dumps(record, separators=(",", ":"), sort_keys=True)


def as_document(record):
    return json.dumps(record, indent=2, sort_keys=True) + "\n"


def lines_blob(records):
    parts = [as_line(rec) + "\n" for rec in records]
    return "".join(parts)


def load_table(path):
    handle = Path(path)
    if not handle.is_file():
        return []
    body = [row for row in handle.read_text(encoding="utf-8").splitlines() if row.strip()]
    if not body:
        return []
    columns = body[0].split(",")
    return [dict(zip(columns, row.split(","))) for row in body[1:]]


def load_document(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


class Inputs:
    """Everything the agent is handed, parsed once."""

    def __init__(self, data_dir):
        base = Path(data_dir)
        self.units = load_document(base / "unit_rules.json")
        self.scoring = load_document(base / "scoring_weights.json")
        self.quality_policy = load_document(base / "quality_policy.json")
        self.language_policy = load_document(base / "language_policy.json")
        self.safety_policy = load_document(base / "safety_policy.json")
        self.curriculum = load_document(base / "curriculum.json")
        self.batch_rules = load_document(base / "batch_rules.json")
        self.priority_order = load_document(base / "priority_order.json")

        quality_lookup = {
            row["source_id"]: (int(row["q_num"]), int(row["q_den"]))
            for row in load_table(base / "quality.csv")
        }
        age_lookup = {
            row["source_id"]: int(row["age_days"])
            for row in load_table(base / "freshness.csv")
        }

        self.order = []
        self.by_id = {}
        for row in load_table(base / "sources.csv"):
            sid = row["source_id"]
            if sid in self.by_id:
                continue
            qnum, qden = quality_lookup.get(sid, (0, 1))
            record = {
                "source_id": sid,
                "language": row["language"],
                "domain": row["domain"],
                "source_class": row["source_class"],
                "safety_stratum": row["safety_stratum"],
                "priority": int(row["priority"]),
                "raw_amount": int(row["raw_amount"]),
                "raw_unit": row["raw_unit"],
                "q_num": qnum,
                "q_den": qden,
                "age_days": age_lookup.get(sid, 0),
            }
            self.order.append(sid)
            self.by_id[sid] = record

        self.examples = [
            {
                "example_id": row["example_id"],
                "source_id": row["source_id"],
                "stratum": row["stratum"],
                "token_length": int(row["token_length"]),
            }
            for row in load_table(base / "examples.csv")
        ]

        self.capacity = {}
        for row in load_table(base / "capacity.csv"):
            self.capacity[row["phase"]] = {
                "budget": int(row["budget_tokens"]),
                "cap": int(row["cap_tokens"]),
            }
        self.carry = {}
        for row in load_table(base / "carry_state.csv"):
            self.carry[row["phase"]] = {
                "credit": int(row["initial_credit"]),
                "debt": int(row["initial_debt"]),
            }

    @property
    def scale(self):
        return self.units["scale"]

    @property
    def half_life(self):
        return self.scoring["half_life_days"]

    @property
    def languages(self):
        return self.language_policy["languages"]

    @property
    def strata(self):
        return self.safety_policy["strata"]

    def stratum_rank(self, stratum):
        return self.priority_order["stratum_displacement_rank"][stratum]

    def initial_carry(self, phase):
        entry = self.carry.get(phase, {})
        return {"credit": entry.get("credit", 0), "debt": entry.get("debt", 0)}


class Compiler:
    def __init__(self, inputs):
        self.inp = inputs
        self.docs = {}

    # -- stage 1 -----------------------------------------------------------
    def canonicalize(self):
        scale_units = self.inp.units["units"]
        canon = {}
        tie_ids = []
        rows = []
        for sid in self.inp.order:
            src = self.inp.by_id[sid]
            conv = scale_units[src["raw_unit"]]
            numerator = src["raw_amount"] * conv["num"]
            denom = conv["den"]
            tokens = bankers(numerator, denom)
            canon[sid] = tokens
            is_tie = 1 if (denom > 0 and (numerator % denom) * 2 == denom) else 0
            if is_tie:
                tie_ids.append(sid)
            rows.append(dict(source_id=sid, raw_amount=src["raw_amount"],
                             raw_unit=src["raw_unit"], canonical_tokens=tokens,
                             rounding_tie=is_tie))
        self.docs["canonical_sources"] = rows
        return canon, tie_ids

    # -- stage 2 -----------------------------------------------------------
    def project_eligibility(self, canon):
        floor_ratio = self.inp.quality_policy["min_quality"]
        blocked = set(self.inp.quality_policy.get("blocked_domains", []))
        known_langs = set(self.inp.languages.keys())
        known_strata = set(self.inp.strata.keys())
        eligible = {}
        rows = []
        for sid in self.inp.order:
            src = self.inp.by_id[sid]
            below_quality = (
                src["q_den"] == 0
                or src["q_num"] * floor_ratio["den"] < floor_ratio["num"] * src["q_den"]
            )
            if below_quality:
                verdict = "E_LOW_QUALITY"
            elif src["language"] not in known_langs:
                verdict = "E_LANG_EXCLUDED"
            elif src["safety_stratum"] not in known_strata:
                verdict = "E_STRATUM_UNKNOWN"
            elif src["domain"] in blocked:
                verdict = "E_DOMAIN_BLOCKED"
            elif canon[sid] <= 0:
                verdict = "E_EMPTY_TOKENS"
            else:
                verdict = "E_OK"
            passed = verdict == "E_OK"
            eligible[sid] = passed
            rows.append(dict(source_id=sid, eligible=passed, code=verdict))
        self.docs["eligibility_ledger"] = rows
        return eligible

    # -- stage 3 -----------------------------------------------------------
    def score_demand(self, canon, eligible):
        scale = self.inp.scale
        half_life = self.inp.half_life
        weights = self.inp.scoring["priority_weights"]
        langs = self.inp.languages
        demand = {}
        rows = []
        for sid in self.inp.order:
            if not eligible[sid]:
                continue
            src = self.inp.by_id[sid]
            quality_bp = bankers(src["q_num"] * scale, src["q_den"])
            fresh_bp = bankers(half_life * scale, half_life + src["age_days"])
            priority_factor = weights[str(src["priority"])]
            balance_bp = langs[src["language"]]["balance_bp"]
            step1 = bankers(canon[sid] * quality_bp, scale)
            step2 = bankers(step1 * fresh_bp, scale)
            step3 = step2 * priority_factor
            final = bankers(step3 * balance_bp, scale)
            demand[sid] = final
            rows.append(dict(source_id=sid, quality_bp=quality_bp, fresh_bp=fresh_bp,
                             priority_factor=priority_factor, lang_balance_bp=balance_bp,
                             demand_score=final))
        self.docs["demand_score_ledger"] = rows
        return demand

    # -- stage 4 membership + proportional --------------------------------
    def assign_phases(self, eligible):
        membership = {phase: [] for phase in PHASE_SEQUENCE}
        rows = []
        for sid in self.inp.order:
            if not eligible[sid]:
                continue
            for phase in DOMAIN_TO_PHASES[self.inp.by_id[sid]["domain"]]:
                membership[phase].append(sid)
                rows.append(dict(phase=phase, source_id=sid))
        self.docs["phase_membership"] = rows
        for phase in PHASE_SEQUENCE:
            membership[phase].sort()
        return membership

    def propose_shares(self, demand, membership):
        proposal = {}
        rows = []
        for phase in PHASE_SEQUENCE:
            budget = self.inp.capacity[phase]["budget"]
            contributors = [s for s in membership[phase] if demand.get(s, 0) > 0]
            demand_sum = sum(demand[s] for s in contributors)
            for sid in membership[phase]:
                if demand.get(sid, 0) > 0 and demand_sum > 0:
                    weighted = demand[sid] * budget
                    target = bankers(weighted, demand_sum)
                else:
                    weighted = 0
                    target = 0
                proposal[(phase, sid)] = target
                rows.append(dict(phase=phase, source_id=sid, target_tokens=target,
                                 share_num=weighted, share_den=demand_sum))
        self.docs["proportional_allocation"] = rows
        return proposal

    # -- stage 5 floors ----------------------------------------------------
    def protected_floors(self, membership):
        strata = self.inp.strata
        langs = self.inp.languages
        floors = {}
        rows = []
        for phase in PHASE_SEQUENCE:
            for sid in membership[phase]:
                src = self.inp.by_id[sid]
                stratum_floor = strata.get(src["safety_stratum"], {}).get("floor_tokens", 0)
                language_floor = langs.get(src["language"], {}).get("floor_tokens", 0)
                combined = max(stratum_floor, language_floor)
                floors[(phase, sid)] = combined
                rows.append(dict(phase=phase, source_id=sid, safety_floor=stratum_floor,
                                 language_floor=language_floor, floor=combined))
        self.docs["protected_floor_ledger"] = rows
        return floors

    def _displacement_sequence(self, keys):
        def rank(key):
            src = self.inp.by_id[key[1]]
            return (self.inp.stratum_rank(src["safety_stratum"]), -src["priority"], key[1])
        return sorted(keys, key=rank)

    # -- stage 6 capacity binding -----------------------------------------
    def bind_capacity(self, membership, proposal, floors, carry_in):
        allocation = {}
        trace = []
        branch_events = []
        leftover_credit = {}
        deferred_debt = {}
        for phase in PHASE_SEQUENCE:
            keys = [(phase, sid) for sid in membership[phase]]
            for key in keys:
                allocation[key] = max(proposal[key], floors[key])
            effective = (self.inp.capacity[phase]["cap"]
                         + carry_in[phase]["credit"] - carry_in[phase]["debt"])
            lifted = any(allocation[key] > proposal[key] for key in keys)
            if lifted:
                branch_events.append(dict(phase=phase, code="B_FLOOR_BIND"))
            placed = sum(allocation[key] for key in keys)
            if placed > effective:
                branch_events.append(dict(phase=phase, code="B_CAP_BIND"))
                surplus = placed - effective
                for key in self._displacement_sequence(keys):
                    if surplus <= 0:
                        break
                    headroom = allocation[key] - floors[key]
                    if headroom > 0:
                        taken = min(headroom, surplus)
                        prev = allocation[key]
                        allocation[key] -= taken
                        surplus -= taken
                        trace.append(dict(phase=phase, source_id=key[1], before=prev,
                                          after=allocation[key], displaced=taken,
                                          kind="above_floor"))
                if surplus > 0:
                    branch_events.append(dict(phase=phase, code="B_FLOOR_OVER_CAP"))
                    for key in self._displacement_sequence(keys):
                        if surplus <= 0:
                            break
                        if allocation[key] > 0:
                            taken = min(allocation[key], surplus)
                            prev = allocation[key]
                            allocation[key] -= taken
                            surplus -= taken
                            trace.append(dict(phase=phase, source_id=key[1], before=prev,
                                              after=allocation[key], displaced=taken,
                                              kind="floor_cut"))
            consumed = sum(allocation[key] for key in keys)
            leftover_credit[phase] = max(0, effective - consumed)
            deferred_debt[phase] = sum(max(0, floors[key] - allocation[key]) for key in keys)
        self.docs["capacity_binding_trace"] = trace
        return allocation, trace, branch_events, leftover_credit, deferred_debt

    # -- stage 7 closure ---------------------------------------------------
    def close_precedence(self):
        nodes = list(self.inp.curriculum["phases"])
        reach = {node: set() for node in nodes}
        for edge in self.inp.curriculum["edges"]:
            reach[edge["from"]].add(edge["to"])
        growing = True
        while growing:
            growing = False
            for node in nodes:
                extra = set()
                for target in list(reach[node]):
                    extra |= reach[target]
                if not extra <= reach[node]:
                    reach[node] |= extra
                    growing = True
        rows = []
        for node in sorted(nodes):
            for target in sorted(reach[node]):
                rows.append(dict(phase=node, requires=target))
        self.docs["precedence_closure"] = rows
        return reach

    # -- stage 8 cycle break ----------------------------------------------
    def break_cycles(self, reach):
        edges = [dict(edge) for edge in self.inp.curriculum["edges"]]
        hardness_value = {"hard": 2, "soft": 1}
        nodes = list(self.inp.curriculum["phases"])
        on_cycle = sorted(node for node in nodes if node in reach[node])
        cut = []
        cycle_rows = []
        if on_cycle:
            cycle_rows.append(dict(cycle_nodes=on_cycle))
            candidates = [e for e in edges if e["from"] in on_cycle and e["to"] in on_cycle]
            victim = min(candidates, key=lambda e: (hardness_value.get(e["hardness"], 0),
                                                     e["from"], e["to"], e["edge_id"]))
            cut.append(victim)
        survivors = [edge for edge in edges if edge not in cut]
        removed_rows = [{key: edge[key] for key in ("edge_id", "from", "to", "hardness")}
                        for edge in cut]
        self.docs["cycle_report"] = cycle_rows
        self.docs["cycle_break_decisions"] = removed_rows
        return survivors, removed_rows

    def topological(self, survivors):
        nodes = list(self.inp.curriculum["phases"])
        indegree = {node: 0 for node in nodes}
        successors = {node: [] for node in nodes}
        for edge in survivors:
            successors[edge["from"]].append(edge["to"])
            indegree[edge["to"]] += 1
        ready = sorted(node for node in nodes if indegree[node] == 0)
        ordering = []
        while ready:
            head = ready.pop(0)
            ordering.append(head)
            for nxt in sorted(successors[head]):
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    ready.append(nxt)
            ready.sort()
        for node in nodes:
            if node not in ordering:
                ordering.append(node)
        return ordering

    # -- stage 8 overturn --------------------------------------------------
    def record_overturns(self, membership, allocation, reach):
        rows = []
        events = []
        for phase in PHASE_SEQUENCE:
            for sid in membership[phase]:
                for required in sorted(reach.get(phase, set())):
                    if sid not in membership.get(required, []):
                        continue
                    upstream = allocation.get((required, sid), 0)
                    here = allocation.get((phase, sid), 0)
                    if upstream == 0 and here > 0:
                        rows.append(dict(phase=phase, prereq_phase=required, source_id=sid))
                        events.append(dict(phase=phase, code="B_PRECEDENCE_OVERTURN"))
        self.docs["precedence_overturn_trace"] = rows
        return events

    # -- stage 9 carry -----------------------------------------------------
    def carry_ledger(self, ordering, carry_in, leftover_credit, deferred_debt):
        running = {phase: dict(carry_in[phase]) for phase in carry_in}
        rows = []
        events = []
        for index, phase in enumerate(ordering):
            credit_in = running[phase]["credit"]
            debt_in = running[phase]["debt"]
            credit_out = leftover_credit[phase]
            debt_out = deferred_debt[phase]
            if debt_out > 0:
                events.append(dict(phase=phase, code="B_CARRY_DEBT"))
            rows.append(dict(phase=phase, credit_in=credit_in, debt_in=debt_in,
                             credit_out=credit_out, debt_out=debt_out))
            if index + 1 < len(ordering):
                nxt = ordering[index + 1]
                running[nxt] = {
                    "credit": running[nxt]["credit"] + credit_out,
                    "debt": running[nxt]["debt"] + debt_out,
                }
        self.docs["carry_state_ledger"] = rows
        return events

    # -- stage 10 reconciliation ------------------------------------------
    def reconcile(self, canon, allocation, ordering):
        repaired = dict(allocation)
        trace = []
        events = []
        position = {phase: idx for idx, phase in enumerate(ordering)}
        for sid in self.inp.order:
            present = [(phase, sid) for phase in PHASE_SEQUENCE if (phase, sid) in repaired]
            granted = sum(repaired[key] for key in present)
            supply = canon.get(sid, 0)
            if granted <= supply:
                continue
            events.append(dict(source_id=sid, code="B_GLOBAL_CLAWBACK"))
            surplus = granted - supply
            latest_first = sorted((key[0] for key in present), key=lambda ph: -position[ph])
            for phase in latest_first:
                if surplus <= 0:
                    break
                key = (phase, sid)
                take = min(repaired[key], surplus)
                if take > 0:
                    prev = repaired[key]
                    repaired[key] -= take
                    surplus -= take
                    trace.append(dict(source_id=sid, phase=phase, before=prev,
                                      after=repaired[key], clawed=take))
        self.docs["global_reconciliation_trace"] = trace
        return repaired, trace, events

    # -- stage 11/12 example weights --------------------------------------
    def _examples_by_source(self):
        grouped = {}
        for example in self.inp.examples:
            grouped.setdefault(example["source_id"], []).append(example)
        return grouped

    def rational_weights(self, final_alloc, grouped):
        totals = {}
        for (phase, sid), value in final_alloc.items():
            totals[sid] = totals.get(sid, 0) + value
        rows = []
        empty_sources = []
        for sid in self.inp.order:
            examples = sorted(grouped.get(sid, []), key=lambda e: e["example_id"])
            total = totals.get(sid, 0)
            denom = sum(e["token_length"] for e in examples)
            if denom == 0:
                if examples:
                    empty_sources.append(sid)
                continue
            for example in examples:
                weight = Fraction(total * example["token_length"], denom)
                rows.append(dict(example_id=example["example_id"], source_id=sid,
                                 w_num=weight.numerator, w_den=weight.denominator))
        self.docs["rational_example_weights"] = rows
        return totals, empty_sources

    def materialize(self, totals, grouped):
        placement = {}
        rows = []
        residuals = []
        for sid in self.inp.order:
            examples = sorted(grouped.get(sid, []), key=lambda e: e["example_id"])
            total = totals.get(sid, 0)
            ids = [e["example_id"] for e in examples]
            length_of = {e["example_id"]: e["token_length"] for e in examples}
            denom = sum(length_of.values())
            if denom == 0 or total == 0 or not ids:
                for example in examples:
                    placement[example["example_id"]] = 0
                    rows.append(dict(example_id=example["example_id"], source_id=sid,
                                     materialized_tokens=0))
                continue
            shares = self._largest_remainder(total, length_of, ids)
            for example in examples:
                eid = example["example_id"]
                floor_share = (total * length_of[eid]) // denom
                placement[eid] = shares[eid]
                rows.append(dict(example_id=eid, source_id=sid,
                                 materialized_tokens=shares[eid]))
                if shares[eid] > floor_share:
                    residuals.append(dict(example_id=eid, source_id=sid,
                                          base_tokens=floor_share, residual_unit=1))
        self.docs["fixed_point_weights"] = rows
        self.docs["rounding_residual_ledger"] = residuals
        return placement

    @staticmethod
    def _largest_remainder(total, weight_of, ids):
        weight_sum = sum(weight_of[i] for i in ids)
        shares = {i: 0 for i in ids}
        if weight_sum <= 0 or total <= 0:
            return shares
        remainder = {}
        handed = 0
        for i in ids:
            quotient, rem = divmod(total * weight_of[i], weight_sum)
            shares[i] = quotient
            remainder[i] = rem
            handed += quotient
        spare = total - handed
        ranked = sorted(ids, key=lambda i: (-remainder[i], i))
        for step in range(spare):
            shares[ranked[step % len(ranked)]] += 1
        return shares

    # -- stage 13 microbatch ----------------------------------------------
    def build_microbatches(self, final_alloc, membership, placement, grouped):
        cap_tokens = self.inp.batch_rules["max_seq_tokens"]
        run_limit = self.inp.batch_rules["adjacency_limit"]
        rows = []
        counter = 0
        for phase in PHASE_SEQUENCE:
            active = sorted(s for s in membership[phase] if final_alloc.get((phase, s), 0) > 0)
            queue = []
            for sid in active:
                for example in sorted(grouped.get(sid, []), key=lambda e: e["example_id"]):
                    if placement.get(example["example_id"], 0) > 0:
                        queue.append((sid, example["example_id"], placement[example["example_id"]]))
            pack = []
            pack_tokens = 0
            run_source = None
            run_length = 0
            for sid, eid, tokens in queue:
                exceeds_cap = bool(pack) and pack_tokens + tokens > cap_tokens
                exceeds_run = sid == run_source and run_length >= run_limit
                if exceeds_cap or exceeds_run:
                    counter += 1
                    rows.append(dict(batch_id="BATCH%04d" % counter, phase=phase,
                                     members=[item[1] for item in pack], seq_tokens=pack_tokens))
                    pack = []
                    pack_tokens = 0
                    run_source = None
                    run_length = 0
                pack.append((sid, eid, tokens))
                pack_tokens += tokens
                if sid == run_source:
                    run_length += 1
                else:
                    run_source = sid
                    run_length = 1
            if pack:
                counter += 1
                rows.append(dict(batch_id="BATCH%04d" % counter, phase=phase,
                                 members=[item[1] for item in pack], seq_tokens=pack_tokens))
        self.docs["microbatch_manifest"] = rows
        return rows

    # -- summaries + objective --------------------------------------------
    def summarize(self, canon, final_alloc, totals):
        langs = self.inp.languages
        strata = self.inp.strata
        scale = self.inp.scale
        grand_total = sum(final_alloc.values())

        per_language = {}
        per_stratum = {}
        for (phase, sid), value in final_alloc.items():
            src = self.inp.by_id[sid]
            per_language[src["language"]] = per_language.get(src["language"], 0) + value
            per_stratum[src["safety_stratum"]] = per_stratum.get(src["safety_stratum"], 0) + value

        language_rows = []
        for language in sorted(set(per_language) | set(langs)):
            policy = langs.get(language, {})
            language_rows.append(dict(
                language=language,
                tokens=per_language.get(language, 0),
                min_required=bankers(policy.get("min_share_bp", 0) * grand_total, scale),
                max_allowed=bankers(policy.get("max_share_bp", scale) * grand_total, scale),
            ))
        self.docs["language_allocation_summary"] = language_rows

        stratum_rows = []
        for stratum in sorted(set(per_stratum) | set(strata)):
            stratum_rows.append(dict(
                stratum=stratum,
                tokens=per_stratum.get(stratum, 0),
                floor=strata.get(stratum, {}).get("floor_tokens", 0),
            ))
        self.docs["safety_allocation_summary"] = stratum_rows

        self.docs["source_allocation_summary"] = [
            dict(source_id=sid, tokens=totals.get(sid, 0), canonical_tokens=canon.get(sid, 0))
            for sid in self.inp.order
        ]

        phase_rows = [
            dict(phase=phase,
                 allocated=sum(final_alloc[(phase, sid)] for sid in membership_phase),
                 cap=self.inp.capacity[phase]["cap"],
                 budget=self.inp.capacity[phase]["budget"])
            for phase, membership_phase in ((p, self._membership[p]) for p in PHASE_SEQUENCE)
        ]
        self.docs["phase_token_totals"] = phase_rows
        return grand_total, phase_rows

    # -- driver ------------------------------------------------------------
    def run(self, program_bytes):
        canon, tie_ids = self.canonicalize()
        eligible = self.project_eligibility(canon)
        demand = self.score_demand(canon, eligible)
        membership = self.assign_phases(eligible)
        self._membership = membership
        proposal = self.propose_shares(demand, membership)
        floors = self.protected_floors(membership)
        carry_in = {phase: self.inp.initial_carry(phase) for phase in PHASE_SEQUENCE}
        bound, bind_trace, bind_events, credit_out, debt_out = self.bind_capacity(
            membership, proposal, floors, carry_in)
        reach = self.close_precedence()
        survivors, removed_rows = self.break_cycles(reach)
        ordering = self.topological(survivors)
        overturn_events = self.record_overturns(membership, bound, reach)
        carry_events = self.carry_ledger(ordering, carry_in, credit_out, debt_out)

        self.docs["pre_reconciliation_ledger"] = [
            dict(phase=phase, source_id=sid, tokens=bound[(phase, sid)])
            for phase in PHASE_SEQUENCE for sid in membership[phase]
        ]
        final_alloc, claw_trace, claw_events = self.reconcile(canon, bound, ordering)
        self.docs["final_allocation"] = [
            dict(phase=phase, source_id=sid, tokens=final_alloc[(phase, sid)])
            for phase in PHASE_SEQUENCE for sid in membership[phase]
        ]

        grouped = self._examples_by_source()
        totals, empty_sources = self.rational_weights(final_alloc, grouped)
        placement = self.materialize(totals, grouped)
        batches = self.build_microbatches(final_alloc, membership, placement, grouped)
        grand_total, phase_rows = self.summarize(canon, final_alloc, totals)

        self.docs["exception_trace"] = self._exception_trace(
            tie_ids, bind_events, overturn_events, removed_rows, carry_events,
            claw_events, empty_sources)

        invariants = self._invariants(canon, totals, placement, grouped, phase_rows,
                                      carry_in, batches)
        self.docs["invariant_report"] = invariants

        objective = [grand_total, len(claw_trace), len(removed_rows), len(bind_trace),
                     len(batches)]
        placed = sum(len(batch["members"]) for batch in batches)
        return self._render(program_bytes, objective, grand_total, claw_trace,
                            bind_trace, removed_rows, overturn_events, batches, placed,
                            invariants)

    def _exception_trace(self, tie_ids, bind_events, overturn_events, removed_rows,
                         carry_events, claw_events, empty_sources):
        entries = []
        for sid in tie_ids:
            entries.append(dict(code="B_UNIT_ROUND_TIE", ref=sid))
        for event in bind_events:
            entries.append(dict(code=event["code"], ref=event["phase"]))
        for event in overturn_events:
            entries.append(dict(code=event["code"], ref=event["phase"]))
        for edge in removed_rows:
            entries.append(dict(code="B_CYCLE_BREAK", ref=edge["edge_id"]))
        for event in carry_events:
            entries.append(dict(code="B_CARRY_DEBT", ref=event["phase"]))
        for event in claw_events:
            entries.append(dict(code="B_GLOBAL_CLAWBACK", ref=event["source_id"]))
        for sid in empty_sources:
            entries.append(dict(code="B_EMPTY_GROUP", ref=sid))
        return entries

    def _invariants(self, canon, totals, placement, grouped, phase_rows, carry_in, batches):
        cap_ok = all(row["allocated"] <= row["cap"] + carry_in[row["phase"]]["credit"]
                     for row in phase_rows)
        supply_ok = all(totals.get(sid, 0) <= canon.get(sid, 0) for sid in self.inp.order)

        def materialized(sid):
            examples = grouped.get(sid, [])
            if sum(e["token_length"] for e in examples) <= 0:
                return 0
            return sum(placement[e["example_id"]] for e in examples)

        mat_ok = all(materialized(sid) == totals.get(sid, 0)
                     for sid in self.inp.order if totals.get(sid, 0) > 0)
        cap_seq = self.inp.batch_rules["max_seq_tokens"]
        batch_ok = all(batch["seq_tokens"] <= cap_seq or len(batch["members"]) == 1
                       for batch in batches)
        return [
            dict(invariant="phase_within_cap", holds=cap_ok),
            dict(invariant="source_within_supply", holds=supply_ok),
            dict(invariant="materialized_sums_match", holds=mat_ok),
            dict(invariant="batches_respect_seq_cap", holds=batch_ok),
        ]

    def _render(self, program_bytes, objective, grand_total, claw_trace, bind_trace,
                removed_rows, overturn_events, batches, placed, invariants):
        artifacts = {}
        for key in JSONL_ORDER:
            artifacts[key + ".jsonl"] = lines_blob(self.docs[key])
        artifacts["objective_tuple.json"] = as_document({
            "objective": objective,
            "order": ["total_tokens", "global_clawbacks", "cycle_edges_removed",
                      "capacity_displacements", "microbatches"],
        })

        manifest = {}
        for name in sorted(artifacts):
            manifest[name] = sha256(artifacts[name].encode("utf-8")).hexdigest()
        manifest["plan.py"] = sha256(program_bytes).hexdigest()
        artifacts["integrity_manifest.json"] = as_document(manifest)

        chain = ZERO_CHAIN
        for name in sorted(manifest):
            chain = sha256((chain + manifest[name]).encode("utf-8")).hexdigest()
        artifacts["final_state.json"] = as_document({
            "chain": chain,
            "total_tokens": grand_total,
            "global_clawbacks": len(claw_trace),
            "capacity_displacements": len(bind_trace),
            "cycle_edges_removed": len(removed_rows),
            "precedence_overturns": len(overturn_events),
            "microbatches": len(batches),
            "examples_placed": placed,
            "quiet_branches": len(self.docs["exception_trace"]),
            "objective": objective,
            "invariants_hold": all(item["holds"] for item in invariants),
        })
        return artifacts


def compile_certificate(data_dir, out_dir, program_bytes=b""):
    artifacts = Compiler(Inputs(data_dir)).run(program_bytes)
    destination = Path(out_dir)
    destination.mkdir(parents=True, exist_ok=True)
    for name, text in artifacts.items():
        (destination / name).write_text(text, encoding="utf-8", newline="")
    return artifacts


def main(argv):
    if len(argv) != 3:
        raise SystemExit("usage: plan.py <DATA_DIR> <OUT_DIR>")
    compile_certificate(argv[1], argv[2], Path(__file__).read_bytes())


if __name__ == "__main__":
    main(sys.argv)
