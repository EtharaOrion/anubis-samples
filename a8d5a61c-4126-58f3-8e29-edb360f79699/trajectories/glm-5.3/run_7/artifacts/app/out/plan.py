#!/usr/bin/env python3
"""TrainMix allocation compiler.

    plan.py <DATA_DIR> <OUT_DIR>

Derives the weighted training plan, the phase allocations and the batch
manifests from a directory of manifests, following the specification shipped
alongside them. Every number is an integer or an exact rational; the only
rounding anywhere is round_half_even at the points the specification names it,
and no floating point value is computed, compared or serialized.
"""

import csv
import hashlib
import json
import os
import sys

# A domain maps to the phases a source of that domain competes in.
DOMAIN_PHASES = {
    "maint": ("PH1",),
    "manual": ("PH1", "PH2"),
    "incident": ("PH2",),
    "qa": ("PH2", "PH3"),
    "translated": ("PH1", "PH3"),
    "synthetic": ("PH3",),
    "reg": ("PH2",),
}

HARDNESS_RANK = {"soft": 0, "hard": 1}


def rhe(n, d):
    """round_half_even(n, d) over the integers, for d > 0."""
    q, r = divmod(n, d)
    if r == 0:
        return q
    r2 = r * 2
    if r2 > d:
        return q + 1
    if r2 < d:
        return q
    return q if q % 2 == 0 else q + 1


def rhe_tie(n, d):
    """(round_half_even(n, d), landed on an exact half)."""
    q, r = divmod(n, d)
    return rhe(n, d), r != 0 and r * 2 == d


def gcd(a, b):
    while b:
        a, b = b, a % b
    return a


def reduced(n, d):
    """Fully reduced fraction of the same value, denominator positive."""
    if d < 0:
        n, d = -n, -d
    g = gcd(abs(n), d)
    if g == 0:
        return 0, 1
    return n // g, d // g


def largest_remainder(total, weights):
    """Split an integer total over (key, weight) pairs with weight >= 0.

    Each key takes floor(total * weight / W) and the leftover units are handed
    out one at a time in descending remainder order, ties by ascending key.
    """
    wsum = sum(w for _, w in weights)
    if wsum <= 0 or total == 0:
        return {k: 0 for k, _ in weights}
    base = {k: (total * w) // wsum for k, w in weights}
    left = total - sum(base.values())
    keys = [k for k, _ in sorted(weights,
                                 key=lambda kw: (-((total * kw[1]) % wsum), kw[0]))]
    out = dict(base)
    i = 0
    while left > 0:
        out[keys[i % len(keys)]] += 1
        left -= 1
        i += 1
    return out


def jsonl_bytes(rows):
    """One compact JSON object per line, keys sorted, trailing newline."""
    return "".join(
        json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n" for r in rows
    ).encode("utf-8")


def json_bytes(obj):
    """One JSON document, keys sorted, two space indent, trailing newline."""
    return (json.dumps(obj, sort_keys=True, indent=2) + "\n").encode("utf-8")


def sha_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def write_file(path, payload):
    with open(path, "wb") as fh:
        fh.write(payload)


def load_json(data_dir, name):
    with open(os.path.join(data_dir, name), "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_csv(data_dir, name):
    with open(os.path.join(data_dir, name), "r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


class Data(object):
    """Every shipped record, indexed for canonical access."""

    def __init__(self, data_dir):
        self.unit_rules = load_json(data_dir, "unit_rules.json")
        self.quality_policy = load_json(data_dir, "quality_policy.json")
        self.language_policy = load_json(data_dir, "language_policy.json")
        self.safety_policy = load_json(data_dir, "safety_policy.json")
        self.priority_order = load_json(data_dir, "priority_order.json")
        self.scoring_weights = load_json(data_dir, "scoring_weights.json")
        self.batch_rules = load_json(data_dir, "batch_rules.json")
        self.curriculum = load_json(data_dir, "curriculum.json")

        self.sources = load_csv(data_dir, "sources.csv")
        self.quality = {r["source_id"]: r
                        for r in load_csv(data_dir, "quality.csv")}
        self.freshness = {r["source_id"]: r
                          for r in load_csv(data_dir, "freshness.csv")}
        self.examples = load_csv(data_dir, "examples.csv")
        self.capacity = {r["phase"]: r
                         for r in load_csv(data_dir, "capacity.csv")}
        self.carry = {r["phase"]: r
                      for r in load_csv(data_dir, "carry_state.csv")}

        # Canonical source order is first appearance in sources.csv.
        self.order = []
        self.row = {}
        for r in self.sources:
            sid = r["source_id"]
            if sid not in self.row:
                self.row[sid] = r
                self.order.append(sid)

        self.languages = self.language_policy["languages"]
        self.strata = self.safety_policy["strata"]
        self.scale = int(self.unit_rules["scale"])
        self.score_scale = int(self.scoring_weights["scale"])
        self.phases = list(self.curriculum["phases"])
        self.edges = list(self.curriculum["edges"])
        self.max_seq = int(self.batch_rules["max_seq_tokens"])
        self.adjacency = int(self.batch_rules["adjacency_limit"])
        self.min_quality = self.quality_policy["min_quality"]
        self.blocked_domains = self.quality_policy["blocked_domains"]

        # Examples grouped by source, ascending example id within a source.
        self.examples_of = {sid: [] for sid in self.order}
        self.example_order = list(self.order)
        for e in self.examples:
            sid = e["source_id"]
            if sid not in self.examples_of:
                self.examples_of[sid] = []
                self.example_order.append(sid)
            self.examples_of[sid].append(e)
        for sid in self.examples_of:
            self.examples_of[sid].sort(key=lambda e: e["example_id"])

    def budget(self, phase):
        return int(self.capacity.get(phase, {}).get("budget_tokens", 0))

    def cap(self, phase):
        return int(self.capacity.get(phase, {}).get("cap_tokens", 0))

    def initial_credit(self, phase):
        return int(self.carry.get(phase, {}).get("initial_credit", 0))

    def initial_debt(self, phase):
        return int(self.carry.get(phase, {}).get("initial_debt", 0))

    def displacement_order(self, member_ids):
        """Ascending stratum displacement rank, then larger priority class
        number first, then ascending source id."""
        rank = self.priority_order["stratum_displacement_rank"]
        return sorted(
            member_ids,
            key=lambda s: (
                int(rank[self.row[s]["safety_stratum"]]),
                -int(self.row[s]["priority"]),
                s,
            ),
        )


def transitive_closure(phases, edges):
    """Reachability over paths of one or more edges; self pairs on cycles."""
    adj = {p: set() for p in phases}
    for e in edges:
        f, t = e["from"], e["to"]
        if f in adj and t in adj:
            adj[f].add(t)
    reach = {p: set(adj[p]) for p in phases}
    changed = True
    while changed:
        changed = False
        for p in phases:
            grown = set(reach[p])
            for q in reach[p]:
                grown |= adj[q]
            if grown != reach[p]:
                reach[p] = grown
                changed = True
    return reach


def topo_sort(phases, edges):
    """Topological order of the surviving edges, ties by ascending phase name."""
    indeg = {p: 0 for p in phases}
    adj = {p: [] for p in phases}
    for e in edges:
        f, t = e["from"], e["to"]
        if f in indeg and t in indeg:
            adj[f].append(t)
            indeg[t] += 1
    ready = sorted(p for p in phases if indeg[p] == 0)
    out = []
    while ready:
        p = ready.pop(0)
        out.append(p)
        for t in sorted(adj[p]):
            indeg[t] -= 1
            if indeg[t] == 0:
                ready.append(t)
                ready.sort()
    out.extend(sorted(p for p in phases if p not in out))
    return out


class Compiler(object):
    """The fourteen stage chain, with its quiet branch trace."""

    def __init__(self, data):
        self.data = data
        self.notes = []

    def note(self, code, ref):
        self.notes.append({"code": code, "ref": str(ref)})

    def canonicalize(self):
        """Stage 1, manifest canonicalization."""
        d = self.data
        units = d.unit_rules["units"]
        canonical, rows = {}, []
        for sid in d.order:
            r = d.row[sid]
            u = units[r["raw_unit"]]
            tokens, tie = rhe_tie(int(r["raw_amount"]) * int(u["num"]),
                                  int(u["den"]))
            canonical[sid] = tokens
            if tie:
                self.note("B_UNIT_ROUND_TIE", sid)
            rows.append({
                "source_id": sid,
                "raw_amount": int(r["raw_amount"]),
                "raw_unit": r["raw_unit"],
                "canonical_tokens": tokens,
                "rounding_tie": tie,
            })
        return canonical, rows

    def eligibility(self, canonical):
        """Stage 2, eligibility projection, first failing test names the code."""
        d = self.data
        mq_num = int(d.min_quality["num"])
        mq_den = int(d.min_quality["den"])
        rows, eligible = [], []
        for sid in d.order:
            r = d.row[sid]
            q = d.quality.get(sid)
            q_num = int(q["q_num"]) if q else 0
            q_den = int(q["q_den"]) if q else 0
            if q_den == 0 or q_num * mq_den < mq_num * q_den:
                code = "E_LOW_QUALITY"
            elif r["language"] not in d.languages:
                code = "E_LANG_EXCLUDED"
            elif r["safety_stratum"] not in d.strata:
                code = "E_STRATUM_UNKNOWN"
            elif r["domain"] in d.blocked_domains:
                code = "E_DOMAIN_BLOCKED"
            elif canonical[sid] <= 0:
                code = "E_EMPTY_TOKENS"
            else:
                code = "E_OK"
            rows.append({"source_id": sid, "eligible": code == "E_OK",
                         "code": code})
            if code == "E_OK":
                eligible.append(sid)
        return rows, eligible

    def score(self, canonical, eligible):
        """Stage 3, demand scoring: four quantized steps in a fixed order."""
        d = self.data
        hl = int(d.scoring_weights["half_life_days"])
        pw = d.scoring_weights["priority_weights"]
        rows, score = [], {}
        for sid in eligible:
            r = d.row[sid]
            q = d.quality[sid]
            q_num, q_den = int(q["q_num"]), int(q["q_den"])
            quality_bp = rhe(q_num * d.score_scale, q_den) if q_den > 0 else 0
            age = int(d.freshness[sid]["age_days"])
            fresh_bp = rhe(hl * d.score_scale, hl + age)
            priority_factor = int(pw[str(r["priority"])])
            lang_balance_bp = int(d.languages[r["language"]]["balance_bp"])
            s1 = rhe(canonical[sid] * quality_bp, d.score_scale)
            s2 = rhe(s1 * fresh_bp, d.score_scale)
            s3 = s2 * priority_factor
            demand = rhe(s3 * lang_balance_bp, d.score_scale)
            score[sid] = demand
            rows.append({
                "source_id": sid,
                "quality_bp": quality_bp,
                "fresh_bp": fresh_bp,
                "priority_factor": priority_factor,
                "lang_balance_bp": lang_balance_bp,
                "demand_score": demand,
            })
        return rows, score

    def membership(self, eligible):
        """Stage 4, a source is a member of every declared phase its domain
        maps to. A phase the curriculum does not declare holds no members."""
        d = self.data
        members = {p: [] for p in d.phases}
        for sid in eligible:
            for p in DOMAIN_PHASES.get(d.row[sid]["domain"], ()):
                if p in members:
                    members[p].append(sid)
        for p in d.phases:
            members[p].sort()
        rows = [{"phase": p, "source_id": sid}
                for p in d.phases for sid in members[p]]
        return rows, members

    def proportional(self, members, score):
        """Stage 4, per source proportional target, never rescaled to budget."""
        d = self.data
        rows, target = [], {p: {} for p in d.phases}
        for p in d.phases:
            budget = d.budget(p)
            demand_sum = sum(score[s] for s in members[p] if score[s] > 0)
            for sid in members[p]:
                share_num = score[sid] * budget
                share_den = demand_sum
                t = rhe(share_num, share_den) if share_num > 0 and share_den > 0 else 0
                target[p][sid] = t
                rows.append({
                    "phase": p,
                    "source_id": sid,
                    "target_tokens": t,
                    "share_num": share_num,
                    "share_den": share_den,
                })
        return rows, target

    def floors(self, members):
        """Stage 5, the larger of the safety stratum and the language floor."""
        d = self.data
        rows, floor = [], {p: {} for p in d.phases}
        for p in d.phases:
            for sid in members[p]:
                r = d.row[sid]
                sf = int(d.strata[r["safety_stratum"]]["floor_tokens"])
                lf = int(d.languages[r["language"]]["floor_tokens"])
                f = sf if sf >= lf else lf
                floor[p][sid] = f
                rows.append({
                    "phase": p,
                    "source_id": sid,
                    "safety_floor": sf,
                    "language_floor": lf,
                    "floor": f,
                })
        return rows, floor

    def allocate(self, members, target, floor, topo):
        """Stages 6 and 9, binding displacement and carry, phase by phase."""
        d = self.data
        alloc = {p: {} for p in d.phases}
        pre = {p: {} for p in d.phases}
        trace = []
        carry_rows = []
        eff_cap = {}
        displacements = 0
        credit = {p: d.initial_credit(p) for p in d.phases}
        debt = {p: d.initial_debt(p) for p in d.phases}

        for i, p in enumerate(topo):
            ec = d.cap(p) + credit[p] - debt[p]
            eff_cap[p] = ec
            cur = {}
            raised = False
            for sid in members[p]:
                t, f = target[p][sid], floor[p][sid]
                cur[sid] = t if t >= f else f
                if f > t:
                    raised = True
            if raised:
                self.note("B_FLOOR_BIND", p)
            total = sum(cur.values())
            if total > ec:
                self.note("B_CAP_BIND", p)
                excess = total - ec
                order = d.displacement_order(members[p])
                for sid in order:
                    if excess <= 0:
                        break
                    room = cur[sid] - floor[p][sid]
                    if room > 0:
                        take = room if room < excess else excess
                        before = cur[sid]
                        cur[sid] -= take
                        excess -= take
                        trace.append({
                            "phase": p,
                            "source_id": sid,
                            "before": before,
                            "after": cur[sid],
                            "displaced": take,
                            "kind": "above_floor",
                        })
                        displacements += 1
                if excess > 0:
                    self.note("B_FLOOR_OVER_CAP", p)
                    for sid in order:
                        if excess <= 0:
                            break
                        take = cur[sid] if cur[sid] < excess else excess
                        if take > 0:
                            before = cur[sid]
                            cur[sid] -= take
                            excess -= take
                            trace.append({
                                "phase": p,
                                "source_id": sid,
                                "before": before,
                                "after": cur[sid],
                                "displaced": take,
                                "kind": "floor_cut",
                            })
                            displacements += 1
            alloc[p] = cur
            pre[p] = dict(cur)
            used = sum(cur.values())
            unused = ec - used if ec > used else 0
            deferred = sum(max(0, floor[p][s] - cur[s]) for s in members[p])
            if deferred > 0:
                self.note("B_CARRY_DEBT", p)
            carry_rows.append({
                "phase": p,
                "credit_in": credit[p],
                "debt_in": debt[p],
                "credit_out": unused,
                "debt_out": deferred,
            })
            if i + 1 < len(topo):
                credit[topo[i + 1]] += unused
                debt[topo[i + 1]] += deferred

        return alloc, pre, trace, carry_rows, eff_cap, displacements

    def reconcile(self, alloc, topo, canonical):
        """Stage 10, claw back to the canonical supply, later phase first."""
        d = self.data
        trace = []
        clawbacks = 0
        for sid in d.order:
            total = sum(alloc[p].get(sid, 0) for p in d.phases)
            if total <= canonical[sid]:
                continue
            self.note("B_GLOBAL_CLAWBACK", sid)
            clawbacks += 1
            rem = total - canonical[sid]
            for p in reversed(topo):
                if rem <= 0:
                    break
                before = alloc[p].get(sid, 0)
                if before <= 0:
                    continue
                take = before if before < rem else rem
                alloc[p][sid] = before - take
                rem -= take
                trace.append({
                    "source_id": sid,
                    "phase": p,
                    "before": before,
                    "after": before - take,
                    "clawed": take,
                })
        return trace, clawbacks

    def overturns(self, alloc, members, reach):
        """Stage 8, a positive allocation in a phase that got nothing in a
        phase it requires through the closure."""
        d = self.data
        rows = []
        for p in d.phases:
            for q in sorted(reach[p]):
                if q == p:
                    continue
                for sid in sorted(members[p]):
                    if sid in members[q] and alloc[q].get(sid, 0) == 0 \
                            and alloc[p].get(sid, 0) > 0:
                        rows.append({
                            "phase": p,
                            "prereq_phase": q,
                            "source_id": sid,
                        })
                        self.note("B_PRECEDENCE_OVERTURN", p)
        return rows

    def weights(self, totals):
        """Stages 11 and 12, rational weights and integer materialization."""
        d = self.data
        rational_rows, fixed_rows, residual_rows = [], [], []
        for sid in d.example_order:
            examples = d.examples_of[sid]
            total = int(totals.get(sid, 0))
            weights = [(e["example_id"], int(e["token_length"]))
                       for e in examples]
            group_den = sum(w for _, w in weights)
            if examples and group_den == 0 and sid in d.row:
                self.note("B_EMPTY_GROUP", sid)
            if group_den > 0:
                for eid, length in weights:
                    n, dn = reduced(total * length, group_den)
                    rational_rows.append({
                        "example_id": eid,
                        "source_id": sid,
                        "w_num": n,
                        "w_den": dn,
                    })
            split = largest_remainder(total, weights) if group_den > 0 else {}
            base = ({eid: (total * w) // group_den for eid, w in weights}
                    if group_den > 0 else {})
            for eid, _ in weights:
                materialized = int(split.get(eid, 0))
                fixed_rows.append({
                    "example_id": eid,
                    "source_id": sid,
                    "materialized_tokens": materialized,
                })
                if group_den > 0 and materialized > base[eid]:
                    residual_rows.append({
                        "example_id": eid,
                        "source_id": sid,
                        "base_tokens": base[eid],
                        "residual_unit": materialized - base[eid],
                    })
        return rational_rows, fixed_rows, residual_rows

    def microbatches(self, alloc, materialized):
        """Stage 13, pack positive examples in fixed order into batches."""
        d = self.data
        rows = []
        batch_no = 0
        for p in d.phases:
            seq = []
            for sid in sorted(s for s in alloc[p] if alloc[p][s] > 0):
                for e in d.examples_of.get(sid, []):
                    tokens = materialized.get(e["example_id"], 0)
                    if tokens > 0:
                        seq.append((e["example_id"], sid, tokens))
            batch, run, tail, tail_source = [], 0, 0, None
            for eid, sid, tokens in seq:
                if batch and (run + tokens > d.max_seq
                              or (sid == tail_source and tail >= d.adjacency)):
                    batch_no += 1
                    rows.append({
                        "batch_id": "BATCH%04d" % batch_no,
                        "phase": p,
                        "members": batch,
                        "seq_tokens": run,
                    })
                    batch, run, tail, tail_source = [], 0, 0, None
                if sid == tail_source:
                    tail += 1
                else:
                    tail, tail_source = 1, sid
                batch.append(eid)
                run += tokens
            if batch:
                batch_no += 1
                rows.append({
                    "batch_id": "BATCH%04d" % batch_no,
                    "phase": p,
                    "members": batch,
                    "seq_tokens": run,
                })
        return rows

    def run(self):
        """The full chain, stage by stage, in the specified order."""
        d = self.data

        canonical, canonical_rows = self.canonicalize()
        elig_rows, eligible = self.eligibility(canonical)
        score_rows, score = self.score(canonical, eligible)
        member_rows, members = self.membership(eligible)
        prop_rows, target = self.proportional(members, score)
        floor_rows, floor = self.floors(members)

        # Stage 7, precedence closure of the shipped curriculum.
        reach = transitive_closure(d.phases, d.edges)
        closure_rows = [{"phase": p, "requires": q}
                        for p in sorted(d.phases) for q in sorted(reach[p])]

        # Stage 8, cycle break. Among the edges whose endpoints both sit on a
        # cycle, rank soft below hard, then from, then to, then edge id, and
        # remove the single lowest ranked edge.
        surviving = list(d.edges)
        cycle_nodes = sorted(p for p in d.phases if p in reach[p])
        break_rows = []
        while True:
            live = transitive_closure(d.phases, surviving)
            cyclic = sorted(p for p in d.phases if p in live[p])
            if not cyclic:
                break
            cs = set(cyclic)
            candidates = sorted(
                (e for e in surviving if e["from"] in cs and e["to"] in cs),
                key=lambda e: (HARDNESS_RANK.get(e["hardness"], 2),
                               e["from"], e["to"], e["edge_id"]),
            )
            removed = candidates[0]
            self.note("B_CYCLE_BREAK", removed["edge_id"])
            break_rows.append({
                "edge_id": removed["edge_id"],
                "from": removed["from"],
                "to": removed["to"],
                "hardness": removed["hardness"],
            })
            surviving = [e for e in surviving if e is not removed]
        cycle_rows = [{"cycle_nodes": cycle_nodes}] if cycle_nodes else []

        topo = topo_sort(d.phases, surviving)
        alloc, pre, bind_trace, carry_rows, eff_cap, displacements = \
            self.allocate(members, target, floor, topo)
        claw_trace, clawbacks = self.reconcile(alloc, topo, canonical)

        # Stage 8 overturns need the spine, and their notes belong before the
        # stage 10 claw back notes in the exception trace.
        claw_notes = [n for n in self.notes if n["code"] == "B_GLOBAL_CLAWBACK"]
        self.notes = [n for n in self.notes if n["code"] != "B_GLOBAL_CLAWBACK"]
        overturn_rows = self.overturns(alloc, members, reach)
        self.notes.extend(claw_notes)

        totals = {sid: sum(alloc[p].get(sid, 0) for p in d.phases)
                  for sid in d.order}
        rational_rows, fixed_rows, residual_rows = self.weights(totals)
        materialized = {r["example_id"]: r["materialized_tokens"]
                        for r in fixed_rows}
        batch_rows = self.microbatches(alloc, materialized)

        return {
            "canonical": canonical,
            "canonical_rows": canonical_rows,
            "elig_rows": elig_rows,
            "score_rows": score_rows,
            "member_rows": member_rows,
            "prop_rows": prop_rows,
            "floor_rows": floor_rows,
            "closure_rows": closure_rows,
            "cycle_rows": cycle_rows,
            "break_rows": break_rows,
            "overturn_rows": overturn_rows,
            "carry_rows": carry_rows,
            "pre_rows": [{"phase": p, "source_id": sid, "tokens": pre[p][sid]}
                         for p in d.phases for sid in sorted(pre[p])],
            "bind_trace": bind_trace,
            "claw_trace": claw_trace,
            "final_rows": [{"phase": p, "source_id": sid,
                            "tokens": alloc[p][sid]}
                           for p in d.phases for sid in sorted(alloc[p])],
            "rational_rows": rational_rows,
            "fixed_rows": fixed_rows,
            "residual_rows": residual_rows,
            "batch_rows": batch_rows,
            "totals": totals,
            "eff_cap": eff_cap,
            "alloc": alloc,
            "displacements": displacements,
            "clawbacks": clawbacks,
            "cycle_edges_removed": len(break_rows),
            "notes": self.notes,
        }


def build_artifacts(data, res):
    """Stage 14, the summaries, the invariants and the certificate."""
    canonical = res["canonical"]
    totals = res["totals"]
    alloc = res["alloc"]
    total_tokens = sum(totals.values())

    lang_rows = []
    for lang in sorted(data.languages):
        pol = data.languages[lang]
        lang_rows.append({
            "language": lang,
            "tokens": sum(totals[s] for s in data.order
                          if data.row[s]["language"] == lang),
            "min_required": rhe(int(pol["min_share_bp"]) * total_tokens,
                                data.scale),
            "max_allowed": rhe(int(pol["max_share_bp"]) * total_tokens,
                                data.scale),
        })

    safety_rows = []
    for st in sorted(data.strata):
        safety_rows.append({
            "stratum": st,
            "tokens": sum(totals[s] for s in data.order
                          if data.row[s]["safety_stratum"] == st),
            "floor": int(data.strata[st]["floor_tokens"]),
        })

    source_rows = [{"source_id": sid, "tokens": totals[sid],
                    "canonical_tokens": canonical[sid]}
                   for sid in data.order]

    phase_rows = [{
        "phase": p,
        "allocated": sum(alloc[p].values()),
        "cap": data.cap(p),
        "budget": data.budget(p),
    } for p in data.phases]

    materialized_sums_ok = True
    for sid in data.order:
        group_den = sum(int(e["token_length"]) for e in data.examples_of[sid])
        if group_den > 0:
            got = sum(r["materialized_tokens"] for r in res["fixed_rows"]
                      if r["source_id"] == sid)
            if got != totals[sid]:
                materialized_sums_ok = False

    invariants = [
        {"invariant": "phase_within_cap",
         "holds": all(sum(alloc[p].values()) <= res["eff_cap"][p]
                      for p in data.phases)},
        {"invariant": "source_within_supply",
         "holds": all(totals[s] <= canonical[s] for s in data.order)},
        {"invariant": "materialized_sums_match", "holds": materialized_sums_ok},
        {"invariant": "batches_respect_seq_cap",
         "holds": all(r["seq_tokens"] <= data.max_seq
                      or len(r["members"]) == 1
                      for r in res["batch_rows"])},
    ]

    objective = [
        total_tokens,
        res["clawbacks"],
        res["cycle_edges_removed"],
        res["displacements"],
        len(res["batch_rows"]),
    ]

    artifacts = {
        "canonical_sources.jsonl": res["canonical_rows"],
        "eligibility_ledger.jsonl": res["elig_rows"],
        "demand_score_ledger.jsonl": res["score_rows"],
        "phase_membership.jsonl": res["member_rows"],
        "proportional_allocation.jsonl": res["prop_rows"],
        "protected_floor_ledger.jsonl": res["floor_rows"],
        "capacity_binding_trace.jsonl": res["bind_trace"],
        "precedence_closure.jsonl": res["closure_rows"],
        "cycle_report.jsonl": res["cycle_rows"],
        "cycle_break_decisions.jsonl": res["break_rows"],
        "precedence_overturn_trace.jsonl": res["overturn_rows"],
        "carry_state_ledger.jsonl": res["carry_rows"],
        "pre_reconciliation_ledger.jsonl": res["pre_rows"],
        "global_reconciliation_trace.jsonl": res["claw_trace"],
        "final_allocation.jsonl": res["final_rows"],
        "language_allocation_summary.jsonl": lang_rows,
        "safety_allocation_summary.jsonl": safety_rows,
        "source_allocation_summary.jsonl": source_rows,
        "rational_example_weights.jsonl": res["rational_rows"],
        "fixed_point_weights.jsonl": res["fixed_rows"],
        "rounding_residual_ledger.jsonl": res["residual_rows"],
        "microbatch_manifest.jsonl": res["batch_rows"],
        "phase_token_totals.jsonl": phase_rows,
        "invariant_report.jsonl": invariants,
        "exception_trace.jsonl": res["notes"],
        "objective_tuple.json": {
            "objective": objective,
            "order": ["total_tokens", "global_clawbacks", "cycle_edges_removed",
                      "capacity_displacements", "microbatches"],
        },
    }

    state = {
        "total_tokens": total_tokens,
        "global_clawbacks": res["clawbacks"],
        "capacity_displacements": res["displacements"],
        "cycle_edges_removed": res["cycle_edges_removed"],
        "precedence_overturns": len(res["overturn_rows"]),
        "microbatches": len(res["batch_rows"]),
        "examples_placed": sum(len(r["members"]) for r in res["batch_rows"]),
        "quiet_branches": len(res["notes"]),
        "objective": objective,
        "invariants_hold": all(r["holds"] for r in invariants),
    }
    return artifacts, state


def main(argv):
    if len(argv) != 3:
        sys.stderr.write("usage: plan.py <DATA_DIR> <OUT_DIR>\n")
        return 2
    data_dir, out_dir = argv[1], argv[2]
    os.makedirs(out_dir, exist_ok=True)

    data = Data(data_dir)
    res = Compiler(data).run()
    artifacts, state = build_artifacts(data, res)

    for name, rows in artifacts.items():
        payload = json_bytes(rows) if name.endswith(".json") else jsonl_bytes(rows)
        write_file(os.path.join(out_dir, name), payload)

    # The program ships beside its output, byte for byte.
    here = os.path.abspath(__file__)
    dest = os.path.abspath(os.path.join(out_dir, "plan.py"))
    if dest != here:
        with open(here, "rb") as fh:
            write_file(dest, fh.read())

    manifest = {}
    for name in list(artifacts) + ["plan.py"]:
        manifest[name] = sha_file(os.path.join(out_dir, name))
    write_file(os.path.join(out_dir, "integrity_manifest.json"),
               json_bytes(manifest))

    # The folded chain runs over the digests in ascending manifest name order.
    chain = "0" * 64
    for name in sorted(manifest):
        chain = hashlib.sha256((chain + manifest[name]).encode("utf-8")).hexdigest()
    state["chain"] = chain
    write_file(os.path.join(out_dir, "final_state.json"), json_bytes(state))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
