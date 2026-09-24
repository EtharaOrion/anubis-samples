#!/usr/bin/env python3
"""TrainMix allocation compiler.

Derives the training mixture certificate from the manifest set in a data
directory, following the fourteen stage chain in the shipped specification.
All arithmetic is integer or exact rational; no floating point value is
produced, compared or serialized.  Usage:

    python3 plan.py <DATA_DIR> <OUT_DIR>
"""

import csv
import hashlib
import json
import os
import sys
from fractions import Fraction

DOMAIN_PHASES = {
    "maint": ("PH1",),
    "manual": ("PH1", "PH2"),
    "incident": ("PH2",),
    "qa": ("PH2", "PH3"),
    "translated": ("PH1", "PH3"),
    "synthetic": ("PH3",),
    "reg": ("PH2",),
}


# ---------------------------------------------------------------------------
# exact arithmetic
# ---------------------------------------------------------------------------

def rhe(p, d):
    """round_half_even(p, d) for integer p over positive denominator d."""
    q, r = divmod(p, d)
    twice = 2 * r
    if twice > d:
        return q + 1
    if twice == d:
        return q if q % 2 == 0 else q + 1
    return q


def is_half(p, d):
    return 2 * (p % d) == d


def largest_remainder_split(total, weights):
    """Split integer total across keys in proportion to the given weights.

    weights is a sequence of (key, weight) pairs; returns {key: tokens}.
    Each key gets the integer quotient of total*weight over the weight sum,
    then leftovers go one unit at a time to keys in descending remainder
    order, ties by ascending key.
    """
    out = {k: 0 for k, _ in weights}
    if total == 0:
        return out
    wsum = sum(w for _, w in weights)
    if wsum == 0:
        return out
    rem = {}
    for k, w in weights:
        num = total * w
        out[k] = num // wsum
        rem[k] = num % wsum
    leftover = total - sum(out.values())
    order = sorted(weights, key=lambda kw: (-rem[kw[0]], kw[0]))
    for i in range(leftover):
        out[order[i][0]] += 1
    return out


# ---------------------------------------------------------------------------
# input loading
# ---------------------------------------------------------------------------

def read_csv(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return [row for row in csv.DictReader(fh) if row]


def read_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


class Inputs(object):
    def __init__(self, data_dir):
        j = lambda n: read_json(os.path.join(data_dir, n))
        self.unit_rules = j("unit_rules.json")
        self.batch_rules = j("batch_rules.json")
        self.quality_policy = j("quality_policy.json")
        self.scoring = j("scoring_weights.json")
        self.priority_order = j("priority_order.json")
        self.lang_policy = j("language_policy.json")
        self.safety_policy = j("safety_policy.json")
        self.curriculum = j("curriculum.json")
        self.capacity = {}
        for row in read_csv(os.path.join(data_dir, "capacity.csv")):
            self.capacity[row["phase"]] = {"budget": int(row["budget_tokens"]),
                                           "cap": int(row["cap_tokens"])}
        self.carry = {}
        for row in read_csv(os.path.join(data_dir, "carry_state.csv")):
            self.carry[row["phase"]] = {"credit": int(row["initial_credit"]),
                                        "debt": int(row["initial_debt"])}
        self.quality = {}
        for row in read_csv(os.path.join(data_dir, "quality.csv")):
            self.quality[row["source_id"]] = {"num": int(row["q_num"]),
                                              "den": int(row["q_den"])}
        self.freshness = {}
        for row in read_csv(os.path.join(data_dir, "freshness.csv")):
            self.freshness[row["source_id"]] = int(row["age_days"])
        self.sources = []
        for row in read_csv(os.path.join(data_dir, "sources.csv")):
            self.sources.append({
                "source_id": row["source_id"],
                "language": row["language"],
                "domain": row["domain"],
                "source_class": row["source_class"],
                "safety_stratum": row["safety_stratum"],
                "priority": int(row["priority"]),
                "raw_amount": int(row["raw_amount"]),
                "raw_unit": row["raw_unit"],
            })
        self.examples = []
        for row in read_csv(os.path.join(data_dir, "examples.csv")):
            self.examples.append({
                "example_id": row["example_id"],
                "source_id": row["source_id"],
                "stratum": row["stratum"],
                "token_length": int(row["token_length"]),
            })


class Compiler(object):
    def __init__(self, data_dir):
        self.inp = Inputs(data_dir)
        self.rows = {}
        self.exceptions = []      # (code, ref) in stage activation order
        self.canonical = {}       # source_id -> canonical tokens
        self.eligible = {}
        self.demand = {}
        self.members = {p: [] for p in self.inp.curriculum["phases"]}
        self.phase_order = list(self.inp.curriculum["phases"])
        self.target = {}          # (phase, source) -> target tokens
        self.floor = {}           # (phase, source) -> floor tokens
        self.alloc = {}           # (phase, source) -> stage 6 allocation
        self.final_alloc = {}
        self.eff_cap = {}
        self.credit_in = {}
        self.debt_in = {}
        self.credit_out = {}
        self.debt_out = {}
        self.topo = []
        self.global_clawbacks = 0
        self.cycle_edges_removed = 0
        self.precedence_overturns = 0
        self.microbatches = 0
        self.examples_placed = 0

    # quiet branch codes keyed by the stage that activates them; the trace is
    # written in stage order, and within a stage in emission order
    BRANCH_STAGE = {
        "B_UNIT_ROUND_TIE": 1,
        "B_FLOOR_BIND": 6,
        "B_CAP_BIND": 6,
        "B_FLOOR_OVER_CAP": 6,
        "B_CYCLE_BREAK": 8,
        "B_PRECEDENCE_OVERTURN": 8,
        "B_CARRY_DEBT": 9,
        "B_GLOBAL_CLAWBACK": 10,
        "B_EMPTY_GROUP": 11,
    }

    def note(self, code, ref):
        self.exceptions.append({"code": code, "ref": ref})

    # -- stage 1 ----------------------------------------------------------
    def stage1(self):
        rows = []
        units = self.inp.unit_rules["units"]
        for src in self.inp.sources:
            unit = units[src["raw_unit"]]
            p = src["raw_amount"] * unit["num"]
            d = unit["den"]
            tokens = rhe(p, d)
            tie = is_half(p, d)
            if tie:
                self.note("B_UNIT_ROUND_TIE", src["source_id"])
            self.canonical[src["source_id"]] = tokens
            rows.append({"source_id": src["source_id"],
                         "raw_amount": src["raw_amount"],
                         "raw_unit": src["raw_unit"],
                         "canonical_tokens": tokens,
                         "rounding_tie": tie})
        self.rows["canonical_sources"] = rows

    # -- stage 2 ----------------------------------------------------------
    def stage2(self):
        pol = self.inp.quality_policy
        mq = pol["min_quality"]
        langs = self.inp.lang_policy["languages"]
        strata = self.inp.safety_policy["strata"]
        blocked = pol["blocked_domains"]
        rows = []
        for src in self.inp.sources:
            sid = src["source_id"]
            q = self.inp.quality.get(sid, {"num": 0, "den": 0})
            code = "E_OK"
            if q["den"] == 0 or q["num"] * mq["den"] < q["den"] * mq["num"]:
                code = "E_LOW_QUALITY"
            elif src["language"] not in langs:
                code = "E_LANG_EXCLUDED"
            elif src["safety_stratum"] not in strata:
                code = "E_STRATUM_UNKNOWN"
            elif src["domain"] in blocked:
                code = "E_DOMAIN_BLOCKED"
            elif self.canonical[sid] <= 0:
                code = "E_EMPTY_TOKENS"
            self.eligible[sid] = (code == "E_OK")
            rows.append({"source_id": sid, "eligible": code == "E_OK",
                         "code": code})
        self.rows["eligibility_ledger"] = rows

    # -- stage 3 ----------------------------------------------------------
    def stage3(self):
        scale = self.inp.scoring["scale"]
        half_life = self.inp.scoring["half_life_days"]
        pw = self.inp.scoring["priority_weights"]
        langs = self.inp.lang_policy["languages"]
        rows = []
        for src in self.inp.sources:
            sid = src["source_id"]
            if not self.eligible[sid]:
                continue
            q = self.inp.quality[sid]
            quality_bp = rhe(q["num"] * scale, q["den"])
            age = self.inp.freshness.get(sid, 0)
            fresh_bp = rhe(half_life * scale, half_life + age)
            priority_factor = pw[str(src["priority"])]
            balance_bp = langs[src["language"]]["balance_bp"]
            s1 = rhe(self.canonical[sid] * quality_bp, scale)
            s2 = rhe(s1 * fresh_bp, scale)
            s3 = s2 * priority_factor
            score = rhe(s3 * balance_bp, scale)
            self.demand[sid] = score
            rows.append({"source_id": sid, "quality_bp": quality_bp,
                         "fresh_bp": fresh_bp, "priority_factor": priority_factor,
                         "lang_balance_bp": balance_bp, "demand_score": score})
        self.rows["demand_score_ledger"] = rows

    # -- stage 4 ----------------------------------------------------------
    def stage4(self):
        member_rows = []
        for src in self.inp.sources:
            sid = src["source_id"]
            if not self.eligible[sid]:
                continue
            for phase in DOMAIN_PHASES.get(src["domain"], ()):
                self.members[phase].append(sid)
        for phase in self.phase_order:
            for sid in self.members[phase]:
                member_rows.append({"phase": phase, "source_id": sid})
        self.rows["phase_membership"] = member_rows

        prop_rows = []
        for phase in self.phase_order:
            budget = self.inp.capacity[phase]["budget"]
            ids = self.members[phase]
            demand_sum = sum(self.demand[s] for s in ids if self.demand[s] > 0)
            for sid in ids:
                score = self.demand[sid]
                share_num = score * budget
                share_den = demand_sum
                target = rhe(share_num, share_den) if (score > 0 and demand_sum > 0) else 0
                self.target[(phase, sid)] = target
                prop_rows.append({"phase": phase, "source_id": sid,
                                  "target_tokens": target,
                                  "share_num": share_num,
                                  "share_den": share_den})
        self.rows["proportional_allocation"] = prop_rows

    # -- stage 5 ----------------------------------------------------------
    def stage5(self):
        strata = self.inp.safety_policy["strata"]
        langs = self.inp.lang_policy["languages"]
        src_by_id = {s["source_id"]: s for s in self.inp.sources}
        rows = []
        for phase in self.phase_order:
            for sid in self.members[phase]:
                src = src_by_id[sid]
                sf = strata[src["safety_stratum"]]["floor_tokens"]
                lf = langs[src["language"]]["floor_tokens"]
                fl = max(sf, lf)
                self.floor[(phase, sid)] = fl
                rows.append({"phase": phase, "source_id": sid,
                             "safety_floor": sf, "language_floor": lf,
                             "floor": fl})
        self.rows["protected_floor_ledger"] = rows

    # -- stage 7 ----------------------------------------------------------
    def closure(self):
        phases = self.inp.curriculum["phases"]
        edges = self.inp.curriculum["edges"]
        adj = {p: set() for p in phases}
        for e in edges:
            adj[e["from"]].add(e["to"])
        reach = {}
        for p in phases:
            seen = set()
            stack = list(adj[p])
            while stack:
                n = stack.pop()
                if n in seen:
                    continue
                seen.add(n)
                stack.extend(adj[n])
            reach[p] = seen
        return reach, adj

    def stage7(self, reach):
        rows = []
        for phase in sorted(reach):
            for req in sorted(reach[phase]):
                if req != phase:
                    rows.append({"phase": phase, "requires": req})
        self.rows["precedence_closure"] = rows

    def topo_order(self, edges, phases):
        adj = {p: set() for p in phases}
        indeg = {p: 0 for p in phases}
        for e in edges:
            f, t = e["from"], e["to"]
            if t not in adj[f]:
                adj[f].add(t)
                indeg[t] += 1
        ready = sorted(p for p in phases if indeg[p] == 0)
        order = []
        while ready:
            node = ready.pop(0)
            order.append(node)
            for nxt in sorted(adj[node]):
                indeg[nxt] -= 1
                if indeg[nxt] == 0:
                    ready.append(nxt)
            ready.sort()
        for p in sorted(phases):
            if p not in order:
                order.append(p)
        return order

    def stage8(self, reach, edges):
        cycle_nodes = sorted(p for p in reach if p in reach[p])
        self.rows["cycle_report"] = [{"cycle_nodes": cycle_nodes}] if cycle_nodes else []
        self.rows["cycle_break_decisions"] = []
        if cycle_nodes:
            cset = set(cycle_nodes)
            rank = {"soft": 0, "hard": 1}
            candidates = sorted(
                (e for e in edges if e["from"] in cset and e["to"] in cset),
                key=lambda e: (rank.get(e["hardness"], 2), e["from"], e["to"],
                               e["edge_id"]))
            if candidates:
                removed = candidates[0]
                self.rows["cycle_break_decisions"] = [{
                    "edge_id": removed["edge_id"], "from": removed["from"],
                    "to": removed["to"], "hardness": removed["hardness"]}]
                self.note("B_CYCLE_BREAK", removed["edge_id"])
                self.cycle_edges_removed = 1
                return [e for e in edges if e["edge_id"] != removed["edge_id"]]
        return edges

    # -- stages 6 and 9 ---------------------------------------------------
    def bind_phase(self, phase, credit_in, debt_in):
        ids = self.members[phase]
        src_by_id = {s["source_id"]: s for s in self.inp.sources}
        ranks = self.inp.priority_order["stratum_displacement_rank"]
        eff = self.inp.capacity[phase]["cap"] + credit_in - debt_in
        self.eff_cap[phase] = eff
        floored = {s: max(self.target[(phase, s)], self.floor[(phase, s)])
                   for s in ids}
        if any(floored[s] > self.target[(phase, s)] for s in ids):
            self.note("B_FLOOR_BIND", phase)
        alloc = dict(floored)
        excess = sum(alloc.values()) - eff
        if excess > 0:
            self.note("B_CAP_BIND", phase)
            order = sorted(ids, key=lambda s: (
                ranks[src_by_id[s]["safety_stratum"]],
                -src_by_id[s]["priority"], s))
            for sid in order:
                if excess <= 0:
                    break
                room = alloc[sid] - self.floor[(phase, sid)]
                if room > 0:
                    take = min(room, excess)
                    before = alloc[sid]
                    alloc[sid] -= take
                    excess -= take
                    self.binding_trace.append(
                        {"phase": phase, "source_id": sid, "before": before,
                         "after": alloc[sid], "displaced": take,
                         "kind": "above_floor"})
            if excess > 0:
                self.note("B_FLOOR_OVER_CAP", phase)
                for sid in order:
                    if excess <= 0:
                        break
                    if alloc[sid] > 0:
                        take = min(alloc[sid], excess)
                        before = alloc[sid]
                        alloc[sid] -= take
                        excess -= take
                        self.binding_trace.append(
                            {"phase": phase, "source_id": sid, "before": before,
                             "after": alloc[sid], "displaced": take,
                             "kind": "floor_cut"})
        for sid in ids:
            self.alloc[(phase, sid)] = alloc[sid]
        self.credit_in[phase] = credit_in
        self.debt_in[phase] = debt_in
        self.credit_out[phase] = max(0, eff - sum(alloc.values()))
        self.debt_out[phase] = sum(max(0, self.floor[(phase, s)] - alloc[s])
                                   for s in ids)
        if self.debt_out[phase] > 0:
            self.note("B_CARRY_DEBT", phase)

    def stages6and9(self, surviving):
        self.binding_trace = []
        order = self.topo_order(surviving, self.inp.curriculum["phases"])
        self.topo = order
        succ = {p: set() for p in order}
        for e in surviving:
            succ[e["from"]].add(e["to"])
        for phase in order:
            credit = self.inp.carry.get(phase, {"credit": 0, "debt": 0})["credit"]
            debt = self.inp.carry.get(phase, {"credit": 0, "debt": 0})["debt"]
            for prev in order[:order.index(phase)]:
                if phase in succ[prev]:
                    credit += self.credit_out[prev]
                    debt += self.debt_out[prev]
            self.bind_phase(phase, credit, debt)
        self.rows["capacity_binding_trace"] = self.binding_trace
        self.rows["carry_state_ledger"] = [
            {"phase": p, "credit_in": self.credit_in[p],
             "debt_in": self.debt_in[p], "credit_out": self.credit_out[p],
             "debt_out": self.debt_out[p]} for p in order]

    # -- stage 10 ---------------------------------------------------------
    def stage10(self):
        rows = []
        for phase in self.phase_order:
            for sid in self.members[phase]:
                rows.append({"phase": phase, "source_id": sid,
                             "tokens": self.alloc[(phase, sid)]})
        self.rows["pre_reconciliation_ledger"] = rows

        totals = {}
        for (phase, sid), v in self.alloc.items():
            totals[sid] = totals.get(sid, 0) + v
        trace = []
        alloc = dict(self.alloc)
        for src in self.inp.sources:
            sid = src["source_id"]
            supply = self.canonical[sid]
            excess = totals.get(sid, 0) - supply
            if excess <= 0:
                continue
            self.note("B_GLOBAL_CLAWBACK", sid)
            self.global_clawbacks += 1
            for phase in reversed(self.topo):
                if excess <= 0:
                    break
                before = alloc.get((phase, sid), 0)
                if before <= 0:
                    continue
                take = min(before, excess)
                alloc[(phase, sid)] = before - take
                excess -= take
                trace.append({"source_id": sid, "phase": phase,
                              "before": before, "after": before - take,
                              "clawed": take})
        self.rows["global_reconciliation_trace"] = trace
        self.global_clawbacks = len(trace)
        self.final_alloc = alloc
        self.rows["final_allocation"] = [
            {"phase": phase, "source_id": sid,
             "tokens": alloc.get((phase, sid), 0)}
            for phase in self.phase_order for sid in self.members[phase]]

    # -- stage 8b, precedence overturn over the final matrix --------------
    def stage8_overturn(self, reach):
        rows = []
        for phase in self.phase_order:
            for prereq in sorted(reach[phase]):
                if prereq == phase:
                    continue
                for sid in self.members[phase]:
                    if sid not in self.members.get(prereq, []):
                        continue
                    if (self.final_alloc.get((phase, sid), 0) > 0 and
                            self.final_alloc.get((prereq, sid), 0) == 0):
                        rows.append({"phase": phase,
                                     "prereq_phase": prereq,
                                     "source_id": sid})
        self.rows["precedence_overturn_trace"] = rows
        self.precedence_overturns = len(rows)
        for row in rows:
            self.note("B_PRECEDENCE_OVERTURN", row["phase"])

    # -- stages 11 and 12 -------------------------------------------------
    def stages11and12(self):
        totals = {}
        for (phase, sid), v in self.final_alloc.items():
            totals[sid] = totals.get(sid, 0) + v
        self.source_totals = totals
        by_source = {}
        for ex in self.inp.examples:
            by_source.setdefault(ex["source_id"], []).append(ex)
        for sid in by_source:
            by_source[sid].sort(key=lambda e: e["example_id"])
        self.examples_by_source = by_source

        rat_rows, mat_rows, res_rows = [], [], []
        self.materialized = {}
        self.group_den = {}
        for src in self.inp.sources:
            sid = src["source_id"]
            examples = by_source.get(sid, [])
            total = totals.get(sid, 0)
            den = sum(e["token_length"] for e in examples)
            if examples and den == 0:
                self.note("B_EMPTY_GROUP", sid)
            if den > 0:
                for ex in examples:
                    frac = Fraction(total * ex["token_length"], den)
                    rat_rows.append({"example_id": ex["example_id"],
                                     "source_id": sid,
                                     "w_num": frac.numerator,
                                     "w_den": frac.denominator})
            split = largest_remainder_split(
                total, [(e["example_id"], e["token_length"]) for e in examples])
            for ex in examples:
                eid = ex["example_id"]
                base = (total * ex["token_length"]) // den if den > 0 else 0
                got = split.get(eid, 0)
                self.materialized[eid] = got
                mat_rows.append({"example_id": eid, "source_id": sid,
                                 "materialized_tokens": got})
                if got > base:
                    res_rows.append({"example_id": eid, "source_id": sid,
                                     "base_tokens": base, "residual_unit": 1})
        self.rows["rational_example_weights"] = rat_rows
        self.rows["fixed_point_weights"] = mat_rows
        self.rows["rounding_residual_ledger"] = res_rows

    # -- stage 13 ---------------------------------------------------------
    def stage13(self):
        max_seq = self.inp.batch_rules["max_seq_tokens"]
        adj_limit = self.inp.batch_rules["adjacency_limit"]
        rows = []
        batch_num = 0
        for phase in self.phase_order:
            for sid in sorted(self.members[phase]):
                if self.final_alloc.get((phase, sid), 0) <= 0:
                    continue
                for ex in self.examples_by_source.get(sid, []):
                    eid = ex["example_id"]
                    tokens = self.materialized.get(eid, 0)
                    if tokens <= 0:
                        continue
                    need_new = True
                    if rows and rows[-1]["phase"] == phase:
                        last = rows[-1]
                        run = 0
                        last_src = None
                        for m in reversed(last["members"]):
                            src_of = self.example_source[m]
                            if last_src is None or src_of == last_src:
                                last_src = src_of
                                run += 1
                            else:
                                break
                        if (last["seq_tokens"] + tokens > max_seq or
                                (last_src == sid and run >= adj_limit)):
                            need_new = True
                        else:
                            need_new = False
                    if need_new:
                        batch_num += 1
                        rows.append({"batch_id": "BATCH%04d" % batch_num,
                                     "phase": phase, "members": [eid],
                                     "seq_tokens": tokens})
                    else:
                        last["members"].append(eid)
                        last["seq_tokens"] += tokens
        self.rows["microbatch_manifest"] = rows
        self.microbatches = len(rows)
        self.examples_placed = sum(len(r["members"]) for r in rows)

    # -- summaries and invariants -----------------------------------------
    def summaries(self):
        scale = self.inp.unit_rules["scale"]
        langs = self.inp.lang_policy["languages"]
        total = sum(self.final_alloc.values())
        self.total_tokens = total
        lang_tokens = {}
        stratum_tokens = {}
        for src in self.inp.sources:
            sid = src["source_id"]
            lang_tokens[src["language"]] = lang_tokens.get(src["language"], 0) + \
                self.source_totals.get(sid, 0)
            stratum_tokens[src["safety_stratum"]] = \
                stratum_tokens.get(src["safety_stratum"], 0) + \
                self.source_totals.get(sid, 0)
        rows = []
        for lang in sorted(langs):
            pol = langs[lang]
            rows.append({"language": lang,
                         "tokens": lang_tokens.get(lang, 0),
                         "min_required": rhe(pol["min_share_bp"] * total, scale),
                         "max_allowed": rhe(pol["max_share_bp"] * total, scale)})
        self.rows["language_allocation_summary"] = rows

        strata = self.inp.safety_policy["strata"]
        self.rows["safety_allocation_summary"] = [
            {"stratum": s, "tokens": stratum_tokens.get(s, 0),
             "floor": strata[s]["floor_tokens"]} for s in sorted(strata)]

        self.rows["source_allocation_summary"] = [
            {"source_id": s["source_id"],
             "tokens": self.source_totals.get(s["source_id"], 0),
             "canonical_tokens": self.canonical[s["source_id"]]}
            for s in self.inp.sources]

        self.rows["phase_token_totals"] = [
            {"phase": p,
             "allocated": sum(v for (ph, _), v in self.final_alloc.items()
                              if ph == p),
             "cap": self.inp.capacity[p]["cap"],
             "budget": self.inp.capacity[p]["budget"]}
            for p in self.phase_order]

    def invariants(self):
        ok_cap = all(
            sum(v for (ph, _), v in self.final_alloc.items() if ph == p)
            <= self.eff_cap[p] for p in self.members)
        ok_supply = all(
            self.source_totals.get(s["source_id"], 0)
            <= self.canonical[s["source_id"]] for s in self.inp.sources)
        ok_mat = True
        for src in self.inp.sources:
            sid = src["source_id"]
            examples = self.examples_by_source.get(sid, [])
            if not examples or self.group_den.get(sid, 0) <= 0:
                # no examples, or an empty group whose split materializes zero
                continue
            if sum(self.materialized[e["example_id"]] for e in examples) != \
                    self.source_totals.get(sid, 0):
                ok_mat = False
        max_seq = self.inp.batch_rules["max_seq_tokens"]
        ok_batch = all(r["seq_tokens"] <= max_seq or len(r["members"]) == 1
                       for r in self.rows["microbatch_manifest"])
        order = ["phase_within_cap", "source_within_supply",
                 "materialized_sums_match", "batches_respect_seq_cap"]
        values = {"phase_within_cap": ok_cap, "source_within_supply": ok_supply,
                  "materialized_sums_match": ok_mat,
                  "batches_respect_seq_cap": ok_batch}
        self.rows["invariant_report"] = [{"invariant": k, "holds": values[k]}
                                         for k in order]
        self.invariants_hold = all(values[k] for k in order)

    # -- driver -----------------------------------------------------------
    def run(self):
        self.example_source = {e["example_id"]: e["source_id"]
                               for e in self.inp.examples}
        self.stage1()
        self.stage2()
        self.stage3()
        self.stage4()
        self.stage5()
        reach, _ = self.closure()
        self.stage7(reach)
        surviving = self.stage8(reach, self.inp.curriculum["edges"])
        self.stages6and9(surviving)
        self.stage10()
        self.stage8_overturn(reach)
        self.stages11and12()
        self.stage13()
        self.summaries()
        self.invariants()
        return self


# ---------------------------------------------------------------------------
# serialization
# ---------------------------------------------------------------------------

JSONL_FILES = [
    "canonical_sources", "eligibility_ledger", "demand_score_ledger",
    "phase_membership", "proportional_allocation", "protected_floor_ledger",
    "capacity_binding_trace", "precedence_closure", "cycle_report",
    "cycle_break_decisions", "precedence_overturn_trace", "carry_state_ledger",
    "pre_reconciliation_ledger", "global_reconciliation_trace",
    "final_allocation", "language_allocation_summary",
    "safety_allocation_summary", "source_allocation_summary",
    "rational_example_weights", "fixed_point_weights",
    "rounding_residual_ledger", "microbatch_manifest", "phase_token_totals",
    "invariant_report", "exception_trace",
]


def dump_jsonl(rows):
    return "".join(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n"
                   for r in rows)


def dump_json(obj):
    return json.dumps(obj, sort_keys=True, indent=2) + "\n"


def write(path, data):
    mode = "wb" if isinstance(data, bytes) else "w"
    kwargs = {} if isinstance(data, bytes) else {"encoding": "utf-8",
                                                 "newline": ""}
    with open(path, mode, **kwargs) as fh:
        fh.write(data)
    os.chmod(path, 0o644)


def main(argv):
    if len(argv) != 3:
        sys.stderr.write("usage: plan.py <DATA_DIR> <OUT_DIR>\n")
        return 2
    data_dir, out_dir = argv[1], argv[2]
    os.makedirs(out_dir, exist_ok=True)
    comp = Compiler(data_dir).run()

    stage = comp.BRANCH_STAGE
    comp.rows["exception_trace"] = sorted(
        comp.exceptions, key=lambda r: stage[r["code"]])
    payloads = {name + ".jsonl": dump_jsonl(comp.rows.get(name, []))
                for name in JSONL_FILES}
    objective = [comp.total_tokens, comp.global_clawbacks,
                 comp.cycle_edges_removed,
                 len(comp.rows["capacity_binding_trace"]), comp.microbatches]
    order = ["total_tokens", "global_clawbacks", "cycle_edges_removed",
             "capacity_displacements", "microbatches"]
    payloads["objective_tuple.json"] = dump_json(
        {"objective": objective, "order": order})

    with open(__file__, "rb") as fh:
        payloads["plan.py"] = fh.read()

    for name in sorted(payloads):
        write(os.path.join(out_dir, name), payloads[name])

    manifest = {}
    for name in sorted(payloads):
        with open(os.path.join(out_dir, name), "rb") as fh:
            manifest[name] = hashlib.sha256(fh.read()).hexdigest()
    write(os.path.join(out_dir, "integrity_manifest.json"), dump_json(manifest))
    chain = "0" * 64
    for name in sorted(manifest):
        chain = hashlib.sha256((chain + manifest[name]).encode("utf-8")).hexdigest()
    final_state = {
        "chain": chain,
        "total_tokens": comp.total_tokens,
        "global_clawbacks": comp.global_clawbacks,
        "capacity_displacements": len(comp.rows["capacity_binding_trace"]),
        "cycle_edges_removed": comp.cycle_edges_removed,
        "precedence_overturns": comp.precedence_overturns,
        "microbatches": comp.microbatches,
        "examples_placed": comp.examples_placed,
        "quiet_branches": len(comp.exceptions),
        "objective": objective,
        "invariants_hold": comp.invariants_hold,
    }
    write(os.path.join(out_dir, "final_state.json"), dump_json(final_state))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
