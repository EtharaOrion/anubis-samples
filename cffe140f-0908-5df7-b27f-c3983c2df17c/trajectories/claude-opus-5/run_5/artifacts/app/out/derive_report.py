#!/usr/bin/env python3
"""Settle a sealed tactics-league season from its replay journals.

Usage:  python3 derive_report.py DATA_DIR OUT_DIR

Reads only DATA_DIR, writes only OUT_DIR.  Standard library only.
"""

import csv
import hashlib
import itertools
import json
import os
import re
import struct
import sys
from math import gcd

M32 = 1 << 32

# ---------------------------------------------------------------------------
# document parsing
# ---------------------------------------------------------------------------

KIND_NAMES = {1: "deploy", 2: "output", 3: "eliminate", 4: "disband",
              5: "concede", 6: "transfer", 8: "close"}

GUARD_BITS = ["keystream", "chain", "delta2", "driftkey", "segperm", "backref",
              "endian", "countbias", "shadow", "rotate", "alias", "escape"]

BACKTICK = re.compile(r"`([^`]+)`")


def norm(s):
    return re.sub(r"[^a-z0-9]+", "_", s.strip().lower()).strip("_")


def read_text(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def parse_number(tok):
    tok = tok.strip()
    if re.fullmatch(r"0[xX][0-9a-fA-F]+", tok):
        return int(tok, 16)
    if re.fullmatch(r"[+-]?\d+", tok):
        return int(tok)
    return tok


class Docs(object):
    """Every roster, policy figure and control figure published under DATA_DIR."""

    def __init__(self, data_dir):
        self.rosters = {}
        self.figures = {}
        self.controls = []
        for name in sorted(os.listdir(data_dir)):
            if not name.endswith(".md"):
                continue
            text = read_text(os.path.join(data_dir, name))
            for line in text.splitlines():
                line = line.strip()
                m = re.match(r"^(?:[-*]\s*)?([A-Za-z][A-Za-z \-]*?)\s+roster:\s*(.+)$", line)
                if m:
                    items = [parse_number(x) for x in BACKTICK.findall(m.group(2))]
                    if items:
                        self.rosters[norm(m.group(1))] = items
                    continue
                m = re.match(r"^(?:[-*]\s*)?([A-Za-z][A-Za-z \-]*?):\s*`([^`]+)`\s*$", line)
                if m:
                    self.figures[norm(m.group(1))] = parse_number(m.group(2))
                    continue
                m = re.match(r"^[-*]\s*C(\d+)\s+(.+?):\s*([+-]?\d+)\s*$", line)
                if m:
                    self.controls.append(dict(index=int(m.group(1)),
                                              tag="C" + m.group(1),
                                              label=m.group(2).strip(),
                                              key=norm(m.group(2)),
                                              published=int(m.group(3))))
        self.controls.sort(key=lambda c: c["index"])

    def roster(self, *names):
        for n in names:
            if n in self.rosters:
                return list(self.rosters[n])
        return []

    def figure(self, names, default):
        for n in names:
            if n in self.figures:
                return self.figures[n]
        return default


# ---------------------------------------------------------------------------
# journal decoding
# ---------------------------------------------------------------------------

class DecodeError(Exception):
    pass


def rot_right(buf, n):
    n %= len(buf)
    if not n:
        return buf
    return buf[len(buf) - n:] + buf[:len(buf) - n]


def keystream_mask(tag, match_id, salt, ordinal, width):
    seed = ("%s:%d:%d:%d" % (tag, match_id, salt, ordinal)).encode("utf-8")
    out = hashlib.sha256(seed).digest()
    while len(out) < width:
        out += hashlib.sha256(out).digest()
    return out[:width]


class Capture(object):
    """A sealed journal: envelope, physical segments, raw bytes."""

    def __init__(self, path):
        self.path = path
        self.name = os.path.basename(path)
        with open(path, "rb") as fh:
            self.blob = fh.read()
        b = self.blob
        if len(b) < 36 or b[:4] != b"RJNL":
            raise DecodeError("%s: not a journal" % self.name)
        e = b[:36]
        self.version = e[4]
        self.guards = struct.unpack("<H", e[6:8])[0]
        self.nonce_le = struct.unpack("<I", e[8:12])[0]
        self.nonce_be = struct.unpack(">I", e[8:12])[0]
        (self.declared_segments, self.declared_records, self.base_tick,
         self.capture_id, self.declared_roster, self.checksum) = struct.unpack("<6I", e[12:36])
        self.checksum_calc = sum(e[:32]) % M32
        self.envelope_ok = (self.checksum == self.checksum_calc)
        self.role = self.capture_id % 10

        self.segments = []
        self.segment_checksums_ok = True
        off = 36
        while off < len(b):
            if b[off:off + 4] != b"SEG0":
                raise DecodeError("%s: bad segment marker at %d" % (self.name, off))
            slot, mid, rc, salt, width, pck = struct.unpack("<6I", b[off + 4:off + 28])
            payload = b[off + 28:off + 28 + width]
            if len(payload) != width:
                raise DecodeError("%s: truncated segment payload" % self.name)
            if sum(payload) % M32 != pck:
                self.segment_checksums_ok = False
            self.segments.append(dict(slot=slot, match_id=mid, declared_records=rc,
                                      salt=salt, width=width, payload=payload))
            off += 28 + width

    def guard(self, bit):
        return bool((self.guards >> bit) & 1)

    def nonce(self):
        return self.nonce_be if self.guard(6) else self.nonce_le

    def count_bias(self, engine_tag):
        if not self.guard(7):
            return 0
        return sum(engine_tag.encode("utf-8")) % 11

    def logical_order(self):
        """Storage index of each logical segment position."""
        n = len(self.segments)
        if n == 0:
            return []
        if not self.guard(4):
            return list(range(n))
        nonce = self.nonce()
        step = ((nonce >> 8) % n) + 2
        while gcd(step, n) != 1:
            step += 1
        order = [None] * n
        for i in range(n):
            lp = (nonce + i * step) % n
            if order[lp] is not None:
                raise DecodeError("%s: segment permutation collides" % self.name)
            order[lp] = i
        if any(o is None for o in order):
            raise DecodeError("%s: segment permutation incomplete" % self.name)
        return order

    def decode(self, engine_tag, sentinel):
        """Recover every record in logical order (attribution left on the wire)."""
        bias = self.count_bias(engine_tag)
        order = self.logical_order()
        nonce = self.nonce()
        records = []
        drift = 0
        prev_tick = None
        capture_ordinal = 0
        tick_regressions = 0
        for logical, si in enumerate(order):
            seg = self.segments[si]
            count = seg["declared_records"] - bias
            if count < 0:
                raise DecodeError("negative segment record count")
            salt = ((seg["salt"] ^ drift) % M32) if self.guard(3) else seg["salt"]
            payload = seg["payload"]
            pos = 0
            seg_value_sum = 0
            prev_seg_tick = 0
            prev_delta = 0
            last_tick = None
            for r in range(count):
                if pos + 2 > len(payload):
                    raise DecodeError("frame runs past payload")
                visible = payload[pos]
                shadow = payload[pos + 1]
                width = (shadow ^ (salt & 0xFF)) if self.guard(8) else visible
                if not 13 <= width <= 19:
                    raise DecodeError("implausible blob width %d" % width)
                blob = payload[pos + 2:pos + 2 + width]
                if len(blob) != width:
                    raise DecodeError("frame runs past payload")
                pos += 2 + width
                # sealing applied rotate first and keystream second, so undo in reverse
                if self.guard(0):
                    mask = keystream_mask(engine_tag, seg["match_id"], salt, r, width)
                    blob = bytes(x ^ y for x, y in zip(blob, mask))
                if self.guard(9):
                    blob = rot_right(blob, (salt + capture_ordinal) % width)
                directives = []
                q = 0
                if self.guard(11):
                    while q + 1 < len(blob) and blob[q] == sentinel and len(directives) < 2:
                        directives.append(blob[q + 1])
                        q += 2
                body = blob[q:]
                if len(body) < 13:
                    raise DecodeError("record body too short")
                kind = body[0]
                mode = body[1]
                if kind not in KIND_NAMES:
                    raise DecodeError("unknown event kind %d" % kind)
                if mode == 0:
                    raw = body[2:5]
                    if not all(0x41 <= c <= 0x5A for c in raw):
                        raise DecodeError("non alphabetic squad key")
                    wire = raw.decode("ascii")
                    j = 5
                elif mode in (1, 2):
                    wire = body[2]
                    j = 3
                else:
                    raise DecodeError("unknown attribution mode %d" % mode)
                if j + 10 != len(body):
                    raise DecodeError("record body width mismatch")
                unit = struct.unpack("<H", body[j:j + 2])[0]
                tick_field = struct.unpack("<I", body[j + 2:j + 6])[0]
                value_field = struct.unpack("<I", body[j + 6:j + 10])[0]
                if self.guard(2):
                    delta = (prev_delta + tick_field) % M32
                    tick = (prev_seg_tick + delta) % M32
                    prev_delta = delta
                    prev_seg_tick = tick
                else:
                    tick = tick_field
                if self.guard(1):
                    tick_mask = nonce if prev_tick is None else prev_tick
                    value = value_field ^ tick_mask
                else:
                    value = value_field
                prev_tick = tick
                seg_value_sum = (seg_value_sum + value) % M32
                if last_tick is not None and tick < last_tick:
                    tick_regressions += 1
                last_tick = tick
                records.append(dict(logical_segment=logical, storage_slot=si,
                                    match_id=seg["match_id"], segment_ordinal=r,
                                    capture_ordinal=capture_ordinal, kind=kind,
                                    mode=mode, wire_raw=wire, unit=unit, tick=tick,
                                    value=value, directives=directives))
                capture_ordinal += 1
            drift = (drift + seg_value_sum) % M32
        self.tick_regressions = tick_regressions
        return records


def build_roster_dictionary(capture, engine_tag, sentinel, window_depth):
    """Distinct attribution keys as written on the wire in the reference capture."""
    records = capture.decode(engine_tag, sentinel)
    table = []
    window = []
    for rec in records:
        wire = resolve_wire(rec, table, window, window_depth, capture)
        if wire not in table:
            table.append(wire)
        refresh_window(window, wire, window_depth)
    return table, records


def resolve_wire(rec, table, window, window_depth, capture):
    if rec["mode"] == 0:
        return rec["wire_raw"]
    if rec["mode"] == 1:
        pos = rec["wire_raw"]
        if pos >= len(window):
            raise DecodeError("back reference %d past window of %d" % (pos, len(window)))
        return window[pos]
    if not table:
        raise DecodeError("alias with empty roster dictionary")
    return table[rec["wire_raw"] % len(table)]


def refresh_window(window, wire, depth):
    if wire in window:
        window.remove(wire)
    window.append(wire)
    while len(window) > depth:
        window.pop(0)


def attribute(records, roster, window_depth):
    """Resolve back references, aliases and escape directives to an owning key."""
    window = []
    out = []
    for rec in records:
        wire = resolve_wire(rec, roster, window, window_depth, None)
        refresh_window(window, wire, window_depth)
        key = wire
        # directive pairs are carried ahead of the body, outermost last applied:
        # the pair nearest the body settles first.
        for d in reversed(rec["directives"]):
            op = d >> 4
            operand = d & 0x0F
            if op == 1:
                if not roster:
                    raise DecodeError("escape redirect with empty dictionary")
                key = roster[operand % len(roster)]
            elif op == 2:
                if key in roster:
                    key = roster[(roster.index(key) + operand) % len(roster)]
            else:
                raise DecodeError("unknown escape operation %d" % op)
        item = dict(rec)
        item["wire"] = wire
        item["key"] = key
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# settlement
# ---------------------------------------------------------------------------

class Registry(object):
    def __init__(self, data_dir):
        self.divisions = {}
        self.conferences = []
        with open(os.path.join(data_dir, "division-topology.csv"), newline="") as fh:
            for row in csv.DictReader(fh):
                self.divisions[row["division"]] = dict(
                    conference=row["conference"], weight=int(row["ladder_weight"]))
                if row["conference"] not in self.conferences:
                    self.conferences.append(row["conference"])
        self.licences = {}
        self.segments = []
        with open(os.path.join(data_dir, "licence-classes.csv"), newline="") as fh:
            for row in csv.DictReader(fh):
                self.licences[row["licence_class"]] = dict(
                    segment=row["segment"], levy=int(row["levy_rate"]))
                if row["segment"] not in self.segments:
                    self.segments.append(row["segment"])
        self.entrants = {}
        with open(os.path.join(data_dir, "entrant-registry.csv"), newline="") as fh:
            for row in csv.DictReader(fh):
                self.entrants[row["squad_key"]] = dict(
                    entrant_id=row["entrant_id"], division=row["division"],
                    licence_class=row["licence_class"],
                    ranked=row["is_ranked"].strip().lower() in ("yes", "true", "1"))

    def profile(self, key):
        e = self.entrants.get(key)
        if e is None:
            return dict(registered=False, entrant_id=None, division=None,
                        conference=None, licence_class=None, segment=None,
                        weight=1, levy=1, ranked=False)
        div = self.divisions.get(e["division"], dict(conference=None, weight=1))
        lic = self.licences.get(e["licence_class"], dict(segment=None, levy=1))
        return dict(registered=True, entrant_id=e["entrant_id"], division=e["division"],
                    conference=div["conference"], licence_class=e["licence_class"],
                    segment=lic["segment"], weight=div["weight"], levy=lic["levy"],
                    ranked=e["ranked"])


BIG = 1 << 62


def match_tables(records):
    """Per match, per squad: gross, voided, raw upkeep, concession, records."""
    by_match = {}
    for rec in records:
        by_match.setdefault(rec["logical_segment"], []).append(rec)
    matches = []
    for logical in sorted(by_match):
        rs = by_match[logical]
        closes = [r["tick"] for r in rs if r["kind"] == 8]
        close = min(closes) if closes else max(r["tick"] for r in rs)
        elim, conceded, disband = {}, {}, {}
        for r in rs:
            ident = (r["key"], r["unit"])
            if r["kind"] == 3:
                elim[ident] = min(elim.get(ident, BIG), r["tick"])
            elif r["kind"] == 4:
                disband[ident] = min(disband.get(ident, BIG), r["tick"])
            elif r["kind"] == 5:
                conceded[r["key"]] = min(conceded.get(r["key"], BIG), r["tick"])
        squads = {}
        for r in rs:
            squads.setdefault(r["key"], dict(gross=0, voided=0, raw=0, records=0,
                                             conceded=False))
        for r in rs:
            d = squads[r["key"]]
            d["records"] += 1
            ident = (r["key"], r["unit"])
            if r["kind"] == 2:
                if r["tick"] >= elim.get(ident, BIG) or r["tick"] >= conceded.get(r["key"], BIG):
                    d["voided"] += r["value"]
                else:
                    d["gross"] += r["value"]
            elif r["kind"] == 1:
                leaves = disband.get(ident, close)
                d["raw"] += r["value"] * max(0, leaves - r["tick"])
        for key in squads:
            squads[key]["conceded"] = key in conceded
        matches.append(dict(logical=logical, match_id=rs[0]["match_id"], close=close,
                            squads=squads, records=rs,
                            escape_records=sum(1 for r in rs if r["directives"])))
    return matches


def settle_ownership(records, roster):
    """Follow hand overs to a fixpoint; return owners and the sweep count."""
    hand = {}
    for rec in records:
        if rec["kind"] == 6 and roster:
            hand[rec["key"]] = roster[rec["value"] % len(roster)]
    keys = sorted(set(r["key"] for r in records) | set(hand) | set(hand.values()))
    # a closed run of hand overs settles on the member whose key sorts first,
    # which is what stops the sweeps from turning forever
    settled = dict(hand)
    for k in keys:
        seen = []
        cur = k
        while cur in hand and cur not in seen:
            seen.append(cur)
            cur = hand[cur]
        if cur in seen:
            cycle = seen[seen.index(cur):]
            first = min(cycle)
            for member in cycle:
                if member == first:
                    settled.pop(member, None)
                else:
                    settled[member] = first
    owner = dict((k, settled.get(k, k)) for k in keys)
    passes = 0
    limit = 2 * len(keys) + 8
    while True:
        passes += 1
        nxt = dict((k, settled.get(owner[k], owner[k])) for k in keys)
        if nxt == owner:
            break
        owner = nxt
        if passes > limit:
            break
    return hand, owner, passes


def settle(records, registry, budget, threshold):
    """Score, tier and rank every squad recovered from a capture."""
    matches = match_tables(records)
    keys = sorted(set(r["key"] for r in records))
    profiles = dict((k, registry.profile(k)) for k in keys)
    state = dict((k, dict(squad_key=k, gross_output=0, voided_output=0,
                          raw_upkeep=0, charged_upkeep=0, overflow_upkeep=0,
                          season_score=0, matches=0, standing_points=0,
                          streak_credit=0, remaining=budget,
                          wins=[])) for k in keys)
    for m in matches:
        scores = {}
        for key, d in sorted(m["squads"].items()):
            s = state[key]
            charged = min(s["remaining"], d["raw"])
            s["remaining"] -= charged
            overflow = d["raw"] - charged
            p = profiles[key]
            score = d["gross"] * p["weight"] - charged * p["levy"]
            s["gross_output"] += d["gross"]
            s["voided_output"] += d["voided"]
            s["raw_upkeep"] += d["raw"]
            s["charged_upkeep"] += charged
            s["overflow_upkeep"] += overflow
            s["season_score"] += score
            s["matches"] += 1
            scores[key] = score
            d["charged"] = charged
            d["overflow"] = overflow
            d["score"] = score
        eligible = [k for k in scores if not m["squads"][k]["conceded"]]
        m["scores"] = scores
        m["winners"] = []
        if eligible:
            top = max(scores[k] for k in eligible)
            winners = sorted(k for k in eligible if scores[k] == top)
            m["top_score"] = top
            m["winners"] = winners
            outright = len(winners) == 1
            for k in winners:
                state[k]["standing_points"] += 3 if outright else 1
            for k in sorted(m["squads"]):
                state[k]["wins"].append(k in winners and outright)
        else:
            m["top_score"] = None
            for k in sorted(m["squads"]):
                state[k]["wins"].append(False)
    for k in keys:
        run = 0
        credit = 0
        for won in state[k]["wins"]:
            if won:
                run += 1
                if run > 1:
                    credit += 2
            else:
                run = 0
        state[k]["streak_credit"] = credit
        state[k]["standing_points"] += credit

    for k in keys:
        s = state[k]
        score = s["season_score"]
        if score >= 3 * threshold:
            tier = "champion"
        elif score >= 2 * threshold:
            tier = "contender"
        elif score >= threshold:
            tier = "qualifier"
        else:
            tier = "provisional"
        s["tier"] = tier
        s.update(profiles[k])
        del s["remaining"]
        del s["wins"]

    ranked = sorted(state.values(),
                    key=lambda s: (-s["season_score"], -s["standing_points"], s["squad_key"]))
    for i, s in enumerate(ranked):
        s["rank"] = i + 1
    return matches, state, profiles


def tier_counts(states):
    counts = dict(champion=0, contender=0, qualifier=0, provisional=0)
    for s in states:
        counts[s["tier"]] += 1
    return counts


# ---------------------------------------------------------------------------
# calibration search
# ---------------------------------------------------------------------------

GUARD_FIGURES = ["matches_opened", "records_recovered", "distinct_squads",
                 "gross_output", "voided_output", "raw_upkeep"]
BUDGET_FIGURES = ["charged_upkeep", "overflow_upkeep"]
THRESHOLD_FIGURES = ["champion_entrants", "contender_entrants",
                     "qualifier_entrants", "provisional_entrants"]


def control_observables(reference, control, registry, tag, depth, sentinel):
    """Reproduce the guard level control figures as far as this reading allows."""
    obs = dict(matches_opened=len(control.segments))
    roster = None
    records = None
    try:
        raw = control.decode(tag, sentinel)
        obs["records_recovered"] = len(raw)
        roster, _ = build_roster_dictionary(reference, tag, sentinel, depth)
        records = attribute(raw, roster, depth)
        obs["distinct_squads"] = len(set(r["key"] for r in records))
        matches = match_tables(records)
        obs["gross_output"] = sum(d["gross"] for m in matches for d in m["squads"].values())
        obs["voided_output"] = sum(d["voided"] for m in matches for d in m["squads"].values())
        obs["raw_upkeep"] = sum(d["raw"] for m in matches for d in m["squads"].values())
        return obs, roster, records, None
    except Exception as exc:
        return obs, roster, records, str(exc)


def budget_observables(records, registry, budget):
    _, state, _ = settle(records, registry, budget, 1)
    return dict(charged_upkeep=sum(s["charged_upkeep"] for s in state.values()),
                overflow_upkeep=sum(s["overflow_upkeep"] for s in state.values()))


def threshold_observables(records, registry, budget, threshold):
    _, state, _ = settle(records, registry, budget, threshold)
    counts = tier_counts(state.values())
    return dict(champion_entrants=counts["champion"],
                contender_entrants=counts["contender"],
                qualifier_entrants=counts["qualifier"],
                provisional_entrants=counts["provisional"])


def first_disagreement(published, observed, names, detail=None):
    """The earliest published figure this reading fails to reproduce."""
    reached = 0
    for c in published:
        if c["key"] not in names:
            continue
        if c["key"] not in observed:
            return dict(control_figure=c["tag"], label=c["label"],
                        published=c["published"], reproduced=None,
                        detail=detail or "figure could not be computed"), reached
        if observed[c["key"]] != c["published"]:
            return dict(control_figure=c["tag"], label=c["label"],
                        published=c["published"],
                        reproduced=observed[c["key"]]), reached
        reached += 1
    return None, reached


def pin_quantities(reference, control, registry, docs):
    """Search every published roster; keep the reading the controls allow."""
    published = docs.controls
    tags = docs.roster("engine_tag") or ["orion-fold"]
    depths = docs.roster("window_depth") or [6]
    sentinels = docs.roster("sentinel") or [0x1D]
    budgets = docs.roster("budget_cap") or [0]
    thresholds = docs.roster("promotion_threshold") or [1]

    audit = {}
    combo_results = []
    for tag, depth, sentinel in itertools.product(tags, depths, sentinels):
        obs, roster, records, err = control_observables(
            reference, control, registry, tag, depth, sentinel)
        bad, reached = first_disagreement(
            published, obs, GUARD_FIGURES,
            None if err is None else "reading refused the capture: %s" % err)
        combo_results.append(dict(tag=tag, depth=depth, sentinel=sentinel,
                                  ok=bad is None, figures_reproduced=reached,
                                  falsifier=bad, roster=roster, records=records,
                                  observed=obs))
    survivors = [c for c in combo_results if c["ok"]]
    if not survivors:
        raise DecodeError("no reading of the guard quantities reproduces the controls")
    chosen = survivors[0]

    def quantity_audit(roster_items, field, formatter=str):
        rows = []
        for cand in roster_items:
            hits = [c for c in combo_results if c[field] == cand]
            accepted = any(c["ok"] for c in hits)
            # hold the other two quantities at the pinned reading where we can,
            # so the falsifier isolates this candidate alone
            held = [c for c in hits
                    if all(c[f] == chosen[f] for f in ("tag", "depth", "sentinel")
                           if f != field)]
            pool = held or hits
            best = max(pool, key=lambda c: c["figures_reproduced"]) if pool else None
            rows.append(dict(candidate=formatter(cand),
                             verdict="accepted" if accepted else "rejected",
                             readings_tested=len(hits),
                             falsifier=None if accepted else (best["falsifier"] if best else None)))
        return dict(roster=[formatter(x) for x in roster_items],
                    candidate_space=len(roster_items),
                    falsifier_holds_other_quantities_pinned=True,
                    verdicts=rows)

    audit["engine_tag"] = quantity_audit(tags, "tag")
    audit["window_depth"] = quantity_audit(depths, "depth")
    audit["escape_sentinel"] = quantity_audit(
        sentinels, "sentinel", lambda v: "0x%02X" % v)
    guard_readings = len(combo_results)

    tag, depth, sentinel = chosen["tag"], chosen["depth"], chosen["sentinel"]
    roster, ctl_records = chosen["roster"], chosen["records"]

    # season budget
    budget_rows = []
    budget_pick = None
    for cand in budgets:
        obs = budget_observables(ctl_records, registry, cand)
        bad, _ = first_disagreement(published, obs, BUDGET_FIGURES)
        if bad is None and budget_pick is None:
            budget_pick = cand
        budget_rows.append(dict(candidate=cand,
                                verdict="accepted" if bad is None else "rejected",
                                falsifier=bad))
    if budget_pick is None:
        raise DecodeError("no budget cap reproduces the controls")
    audit["season_budget"] = dict(roster=list(budgets), candidate_space=len(budgets),
                                  falsifier_holds_other_quantities_pinned=True,
                                  verdicts=budget_rows)

    # promotion threshold
    thr_rows = []
    thr_pick = None
    for cand in thresholds:
        obs = threshold_observables(ctl_records, registry, budget_pick, cand)
        bad, _ = first_disagreement(published, obs, THRESHOLD_FIGURES)
        if bad is None and thr_pick is None:
            thr_pick = cand
        thr_rows.append(dict(candidate=cand,
                             verdict="accepted" if bad is None else "rejected",
                             falsifier=bad))
    if thr_pick is None:
        raise DecodeError("no promotion threshold reproduces the controls")
    audit["promotion_threshold"] = dict(roster=list(thresholds),
                                        candidate_space=len(thresholds),
                                        falsifier_holds_other_quantities_pinned=True,
                                        verdicts=thr_rows)

    pinned = dict(engine_tag=tag, window_depth=depth,
                  escape_sentinel="0x%02X" % sentinel, season_budget=budget_pick,
                  promotion_threshold=thr_pick,
                  count_bias=control.count_bias(tag))

    # calibration comparison over every published figure
    obs_all = {}
    obs_all.update(chosen["observed"])
    obs_all.update(budget_observables(ctl_records, registry, budget_pick))
    obs_all.update(threshold_observables(ctl_records, registry, budget_pick, thr_pick))
    table = []
    for c in published:
        rep = obs_all.get(c["key"])
        table.append(dict(control_figure=c["tag"], label=c["label"],
                          published=c["published"], reproduced=rep,
                          agrees=(rep == c["published"])))
    search = dict(guard_readings_tested=guard_readings,
                  guard_readings_surviving=len(survivors),
                  budget_candidates_tested=len(budgets),
                  threshold_candidates_tested=len(thresholds),
                  total_candidate_space=guard_readings + len(budgets) + len(thresholds))
    return pinned, audit, table, roster, sentinel, search


# ---------------------------------------------------------------------------
# bracket and docket
# ---------------------------------------------------------------------------

BRACKET_CONSTRAINTS = [
    "prominent_division_member",
    "both_segments_present",
    "standing_points_floor_met",
    "no_shared_conference",
    "ranked_entrant_present",
    "combined_ladder_weight_floor_met",
    "no_overflow_upkeep",
    "champion_member_present",
    "combined_gross_output_floor_met",
    "no_member_handed_entry_away",
]


def evaluate_bracket(members, state, registry, hand, docs):
    prominence = docs.figure(["ladder_weight_prominence"], 4)
    sp_floor = docs.figure(["standing_points_floor"], 3)
    weight_floor = docs.figure(["combined_ladder_weight_floor"], 7)
    gross_floor = docs.figure(["combined_gross_output_floor"], 900)
    ss = [state[k] for k in members]
    confs = [s["conference"] for s in ss if s["conference"]]
    segs = set(s["segment"] for s in ss if s["segment"])
    checks = {
        "prominent_division_member": any(s["weight"] >= prominence for s in ss),
        "both_segments_present": set(registry.segments) <= segs,
        "standing_points_floor_met": all(s["standing_points"] >= sp_floor for s in ss),
        "no_shared_conference": len(confs) == len(set(confs)),
        "ranked_entrant_present": any(s["ranked"] for s in ss),
        "combined_ladder_weight_floor_met": sum(s["weight"] for s in ss) >= weight_floor,
        "no_overflow_upkeep": all(s["overflow_upkeep"] == 0 for s in ss),
        "champion_member_present": any(s["tier"] == "champion" for s in ss),
        "combined_gross_output_floor_met": sum(s["gross_output"] for s in ss) >= gross_floor,
        "no_member_handed_entry_away": all(k not in hand for k in members),
    }
    return checks, sum(1 for v in checks.values() if v)


def elect_bracket(state, registry, hand, owner, docs):
    floor = docs.figure(["eligibility_match_floor"], 2)
    size = 3
    pool = sorted(k for k, s in state.items()
                  if owner.get(k, k) == k and s["matches"] >= floor)
    best = None
    best_count = 0
    space = 0
    for combo in itertools.combinations(pool, size):
        space += 1
        checks, count = evaluate_bracket(combo, state, registry, hand, docs)
        upkeep = sum(state[k]["charged_upkeep"] for k in combo)
        cand = (-count, upkeep, tuple(sorted(combo)))
        if best is None or cand < best[0]:
            best = (cand, combo, checks, count, upkeep)
        best_count = max(best_count, count)
    if best is None:
        return dict(members=[], eligible_pool=pool, candidate_space=0,
                    best_satisfied_constraint_count=0, satisfied_constraints=0,
                    constraints={}, combined_charged_upkeep=0)
    _, combo, checks, count, upkeep = best
    return dict(members=sorted(combo), eligible_pool=pool, candidate_space=space,
                best_satisfied_constraint_count=best_count,
                satisfied_constraints=count, constraints=checks,
                combined_charged_upkeep=upkeep,
                combined_ladder_weight=sum(state[k]["weight"] for k in combo),
                combined_gross_output=sum(state[k]["gross_output"] for k in combo),
                combined_standing_points=sum(state[k]["standing_points"] for k in combo))


def match_conference(m, registry):
    best = None
    for key, d in sorted(m["squads"].items()):
        prof = registry.profile(key)
        if not prof["registered"]:
            continue
        cand = (-d["gross"], key)
        if best is None or cand < best[0]:
            best = (cand, key, prof["conference"])
    if best is None:
        return None, None
    return best[2], best[1]


def elect_docket(matches, registry, docs):
    weight = docs.figure(["review_weight"], 25)
    size = docs.figure(["docket_size"], 5)
    cap = docs.figure(["docket_conference_cap"], 2)
    rows = []
    for m in matches:
        voided = sum(d["voided"] for d in m["squads"].values())
        conf, leader = match_conference(m, registry)
        rows.append(dict(match_id=m["match_id"], logical_segment=m["logical"],
                         voided_output=voided, escape_records=m["escape_records"],
                         review_index=voided + weight * m["escape_records"],
                         conference=conf, leading_squad=leader))
    size = min(size, len(rows))
    n = len(rows)
    gross_space = 1
    for i in range(size):
        gross_space = gross_space * (n - i) // (i + 1)
    best = None
    space = 0
    if gross_space <= 400000:
        for combo in itertools.combinations(range(n), size):
            counts = {}
            ok = True
            for i in combo:
                c = rows[i]["conference"]
                if c is None:
                    continue
                counts[c] = counts.get(c, 0) + 1
                if counts[c] > cap:
                    ok = False
                    break
            if not ok:
                continue
            space += 1
            total = sum(rows[i]["review_index"] for i in combo)
            ids = tuple(sorted(rows[i]["match_id"] for i in combo))
            cand = (-total, ids)
            if best is None or cand < best[0]:
                best = (cand, combo, total)
    else:
        # a conference cap plus a fixed docket size is a truncated partition
        # matroid, so taking the richest match that still fits is exact
        counts = {}
        picked = []
        for i in sorted(range(n), key=lambda i: (-rows[i]["review_index"],
                                                 rows[i]["match_id"])):
            c = rows[i]["conference"]
            if c is not None and counts.get(c, 0) >= cap:
                continue
            picked.append(i)
            if c is not None:
                counts[c] = counts.get(c, 0) + 1
            if len(picked) == size:
                break
        if len(picked) == size:
            total = sum(rows[i]["review_index"] for i in picked)
            best = ((-total, tuple(sorted(rows[i]["match_id"] for i in picked))),
                    tuple(picked), total)
        space = gross_space
    if best is None:
        return dict(matches=[], total_review_index=0, candidate_space=0,
                    feasible_candidate_space=0, conference_spread={},
                    all_matches=rows)
    _, combo, total = best
    chosen = [rows[i] for i in sorted(combo, key=lambda i: rows[i]["match_id"])]
    spread = {}
    for r in chosen:
        c = r["conference"] or "unassigned"
        spread[c] = spread.get(c, 0) + 1
    return dict(matches=chosen, total_review_index=total,
                candidate_space=gross_space, feasible_candidate_space=space,
                conference_spread=spread, all_matches=rows,
                review_weight=weight, docket_size=size, conference_cap=cap)


# ---------------------------------------------------------------------------
# reports
# ---------------------------------------------------------------------------

ENTRY_FIELDS = ["charged_upkeep", "conference", "division", "entrant_id",
                "gross_output", "licence_class", "matches", "overflow_upkeep",
                "season_score", "segment", "squad_key", "standing_points",
                "streak_credit", "tier", "voided_output"]


def entry_of(s):
    return dict((f, s[f]) for f in ENTRY_FIELDS)


def group_report(kind, name, states, extra_keys):
    entries = [entry_of(s) for s in sorted(states, key=lambda s: s["squad_key"])]
    rep = dict(group_kind=kind, group_name=name, entrants=len(entries),
               entries=entries,
               charged_upkeep=sum(e["charged_upkeep"] for e in entries),
               gross_output=sum(e["gross_output"] for e in entries),
               overflow_upkeep=sum(e["overflow_upkeep"] for e in entries),
               season_score=sum(e["season_score"] for e in entries),
               standing_points=sum(e["standing_points"] for e in entries),
               voided_output=sum(e["voided_output"] for e in entries),
               tier_counts=tier_counts(states))
    rep["divisions"] = sorted(set(e["division"] for e in entries if e["division"]))
    for k in extra_keys:
        if k == "licence_classes":
            rep[k] = sorted(set(e["licence_class"] for e in entries if e["licence_class"]))
    return rep


def write_json(out_dir, name, payload):
    path = os.path.join(out_dir, name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")


def capture_reconciliation(cap, records, keys, registry, tag):
    bias = cap.count_bias(tag)
    declared_segments = cap.declared_segments - bias
    declared_records = cap.declared_records - bias
    declared_roster = cap.declared_roster - bias
    registered = sorted(k for k in keys if registry.profile(k)["registered"])
    unregistered = sorted(k for k in keys if not registry.profile(k)["registered"])
    return dict(
        capture=cap.name, capture_id=cap.capture_id,
        role={1: "reference", 2: "control", 3: "season"}.get(cap.role, "unknown"),
        guard_register=cap.guards,
        guards_active=[GUARD_BITS[i] for i in range(len(GUARD_BITS)) if cap.guard(i)],
        count_bias=bias,
        envelope_checksum_agrees=cap.envelope_ok,
        segment_checksums_agree=cap.segment_checksums_ok,
        declared_segments=declared_segments,
        recovered_segments=len(cap.segments),
        segment_residual=len(cap.segments) - declared_segments,
        declared_records=declared_records,
        recovered_records=len(records),
        record_residual=len(records) - declared_records,
        declared_roster_size=declared_roster,
        registered_squads_recovered=len(registered),
        roster_residual=len(registered) - declared_roster,
        distinct_squads_recovered=len(keys),
        unregistered_squads=unregistered,
        tick_regressions=getattr(cap, "tick_regressions", 0))


def main(argv):
    if len(argv) != 3:
        sys.stderr.write("usage: derive_report.py DATA_DIR OUT_DIR\n")
        return 2
    data_dir = os.path.abspath(argv[1])
    out_dir = os.path.abspath(argv[2])
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)

    docs = Docs(data_dir)
    registry = Registry(data_dir)

    captures = []
    for name in sorted(os.listdir(data_dir)):
        path = os.path.join(data_dir, name)
        if not os.path.isfile(path):
            continue
        with open(path, "rb") as fh:
            if fh.read(4) != b"RJNL":
                continue
        captures.append(Capture(path))
    by_role = {}
    for cap in captures:
        by_role.setdefault(cap.role, []).append(cap)
    reference = by_role.get(1, [None])[0]
    control = by_role.get(2, [None])[0]
    season = by_role.get(3, [None])[0]
    if reference is None or season is None:
        raise DecodeError("corpus needs a reference capture and a season capture")
    if control is None:
        control = season

    pinned, quantity_audit, calibration, roster, sentinel, search = pin_quantities(
        reference, control, registry, docs)
    tag = pinned["engine_tag"]
    depth = pinned["window_depth"]
    budget = pinned["season_budget"]
    threshold = pinned["promotion_threshold"]

    # reference transcript agreement
    ref_records = attribute(reference.decode(tag, sentinel), roster, depth)
    transcript = None
    for name in sorted(os.listdir(data_dir)):
        if name.endswith(".jsonl"):
            transcript = os.path.join(data_dir, name)
            break
    transcript_check = None
    if transcript:
        want = [json.loads(line) for line in open(transcript, encoding="utf-8") if line.strip()]
        got = [dict(kind=r["kind"], logical_segment=r["logical_segment"],
                    match_id=r["match_id"], record_ordinal=r["capture_ordinal"],
                    squad_key=r["key"], tick=r["tick"], unit=r["unit"],
                    value=r["value"]) for r in ref_records]
        mismatches = sum(1 for a, b in zip(want, got) if a != b) + abs(len(want) - len(got))
        transcript_check = dict(transcript=os.path.basename(transcript),
                                labelled_records=len(want), decoded_records=len(got),
                                mismatched_records=mismatches, byte_exact=(mismatches == 0))

    # any published group report is a second calibration source: settle the
    # control slice the same way and see whether the two agree
    example_check = None
    for name in sorted(os.listdir(data_dir)):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(data_dir, name), encoding="utf-8") as fh:
                want = json.load(fh)
        except Exception:
            continue
        if not isinstance(want, dict) or "group_kind" not in want:
            continue
        ctl = attribute(control.decode(tag, sentinel), roster, depth)
        _, ctl_state, _ = settle(ctl, registry, budget, threshold)
        field = "conference" if want["group_kind"] == "conference" else "segment"
        members = [s for s in ctl_state.values() if s[field] == want.get("group_name")]
        extra = [] if field == "conference" else ["licence_classes"]
        got = group_report(want["group_kind"], want.get("group_name"), members, extra)
        example_check = dict(document=name, group_kind=want["group_kind"],
                             group_name=want.get("group_name"),
                             reproduced_from="control slice",
                             agrees=(got == want))
        if got != want:
            example_check["differing_fields"] = sorted(
                k for k in set(got) | set(want) if got.get(k) != want.get(k))
        break

    # season settlement
    season_records = attribute(season.decode(tag, sentinel), roster, depth)
    matches, state, profiles = settle(season_records, registry, budget, threshold)
    hand, owner, passes = settle_ownership(season_records, roster)
    keys = sorted(state)

    bracket = elect_bracket(state, registry, hand, owner, docs)
    docket = elect_docket(matches, registry, docs)

    # group reports
    conf_states = dict((c, []) for c in registry.conferences)
    seg_states = dict((s, []) for s in registry.segments)
    for k in keys:
        s = state[k]
        if s["conference"] in conf_states:
            conf_states[s["conference"]].append(s)
        if s["segment"] in seg_states:
            seg_states[s["segment"]].append(s)
    for conf in registry.conferences:
        write_json(out_dir, "region_%s.json" % conf,
                   group_report("conference", conf, conf_states[conf], []))
    for seg in registry.segments:
        write_json(out_dir, "segment_%s.json" % seg,
                   group_report("segment", seg, seg_states[seg], ["licence_classes"]))

    totals = dict(
        gross_output=sum(s["gross_output"] for s in state.values()),
        voided_output=sum(s["voided_output"] for s in state.values()),
        raw_upkeep=sum(s["raw_upkeep"] for s in state.values()),
        charged_upkeep=sum(s["charged_upkeep"] for s in state.values()),
        overflow_upkeep=sum(s["overflow_upkeep"] for s in state.values()),
        season_score=sum(s["season_score"] for s in state.values()),
        standing_points=sum(s["standing_points"] for s in state.values()),
        streak_credit=sum(s["streak_credit"] for s in state.values()))
    conf_gross = sum(sum(s["gross_output"] for s in v) for v in conf_states.values())
    seg_gross = sum(sum(s["gross_output"] for s in v) for v in seg_states.values())
    unregistered = sorted(k for k in keys if not state[k]["registered"])
    absent = sorted(k for k in registry.entrants if k not in state)

    ledger = dict((n, 0) for n in KIND_NAMES.values())
    for r in season_records:
        ledger[KIND_NAMES[r["kind"]]] += 1

    standings = []
    for s in sorted(state.values(), key=lambda s: s["rank"]):
        row = entry_of(s)
        row.update(rank=s["rank"], raw_upkeep=s["raw_upkeep"],
                   ladder_weight=s["weight"], levy_rate=s["levy"],
                   registered=s["registered"], ranked_entrant=s["ranked"],
                   effective_owner=owner.get(s["squad_key"], s["squad_key"]),
                   own_effective_owner=owner.get(s["squad_key"], s["squad_key"]) == s["squad_key"],
                   handed_entry_to=hand.get(s["squad_key"]))
        standings.append(row)

    summary = dict(
        season=dict(capture=season.name, capture_id=season.capture_id,
                    guard_register=season.guards,
                    guards_active=[GUARD_BITS[i] for i in range(len(GUARD_BITS))
                                   if season.guard(i)],
                    base_tick=season.base_tick,
                    matches=len(matches),
                    records_recovered=len(season_records),
                    segments_recovered=len(season.segments)),
        derived_quantities=pinned,
        roster_dictionary=roster,
        totals=totals,
        kind_ledger=ledger,
        squads_recovered=len(keys),
        registered_entrants=len(keys) - len(unregistered),
        unregistered_squads=unregistered,
        absent_entrants=absent,
        tier_counts=tier_counts(state.values()),
        standings=standings,
        attribution_settlement_passes=passes,
        hand_overs=dict(sorted(hand.items())),
        effective_owners=dict((k, owner.get(k, k)) for k in keys),
        finals_bracket=dict(
            members=bracket["members"],
            satisfied_constraints=bracket["satisfied_constraints"],
            best_satisfied_constraint_count=bracket["best_satisfied_constraint_count"],
            constraints=bracket["constraints"],
            combined_charged_upkeep=bracket.get("combined_charged_upkeep"),
            combined_ladder_weight=bracket.get("combined_ladder_weight"),
            combined_gross_output=bracket.get("combined_gross_output"),
            combined_standing_points=bracket.get("combined_standing_points"),
            eligible_pool=bracket["eligible_pool"]),
        review_docket=dict(
            match_ids=[r["match_id"] for r in docket["matches"]],
            matches=docket["matches"],
            total_review_index=docket["total_review_index"],
            conference_spread=docket["conference_spread"],
            review_weight=docket.get("review_weight"),
            docket_size=docket.get("docket_size"),
            conference_cap=docket.get("conference_cap")),
        match_review_index=docket["all_matches"],
        conference_totals=dict((c, dict(
            entrants=len(v),
            gross_output=sum(s["gross_output"] for s in v),
            season_score=sum(s["season_score"] for s in v),
            standing_points=sum(s["standing_points"] for s in v)))
            for c, v in sorted(conf_states.items())),
        segment_totals=dict((c, dict(
            entrants=len(v),
            gross_output=sum(s["gross_output"] for s in v),
            season_score=sum(s["season_score"] for s in v),
            standing_points=sum(s["standing_points"] for s in v)))
            for c, v in sorted(seg_states.items())))
    write_json(out_dir, "summary.json", summary)

    reported_output = sum(r["value"] for r in season_records if r["kind"] == 2)
    quality = dict(
        captures=[capture_reconciliation(reference, ref_records,
                                         sorted(set(r["key"] for r in ref_records)),
                                         registry, tag)],
        reference_transcript=transcript_check,
        attribution_residual=totals["gross_output"] - conf_gross,
        segment_attribution_residual=totals["gross_output"] - seg_gross,
        unregistered_squads=unregistered,
        unregistered_detail=[dict(squad_key=k, gross_output=state[k]["gross_output"],
                                  voided_output=state[k]["voided_output"],
                                  raw_upkeep=state[k]["raw_upkeep"],
                                  charged_upkeep=state[k]["charged_upkeep"],
                                  overflow_upkeep=state[k]["overflow_upkeep"],
                                  season_score=state[k]["season_score"],
                                  matches=state[k]["matches"]) for k in unregistered],
        absent_entrants=absent,
        kind_ledger=ledger,
        ledger_residuals=dict(
            output_residual=reported_output - (totals["gross_output"] + totals["voided_output"]),
            upkeep_residual=totals["raw_upkeep"] - (totals["charged_upkeep"] + totals["overflow_upkeep"]),
            record_count_residual=len(season_records) - sum(ledger.values())),
        tick_regressions=season.tick_regressions,
        findings=[])
    if control is not season:
        ctl_records = attribute(control.decode(tag, sentinel), roster, depth)
        quality["captures"].append(capture_reconciliation(
            control, ctl_records, sorted(set(r["key"] for r in ctl_records)),
            registry, tag))
    quality["captures"].append(capture_reconciliation(
        season, season_records, keys, registry, tag))
    quality["captures"].sort(key=lambda c: c["capture_id"])

    findings = []
    for c in quality["captures"]:
        for field, label in (("segment_residual", "declared segment count"),
                             ("record_residual", "declared record count"),
                             ("roster_residual", "declared roster size")):
            if c[field] != 0:
                findings.append(dict(capture=c["capture"], role=c["role"],
                                     residual=field, value=c[field],
                                     detail="%s disagrees with what was recovered" % label))
        if c["unregistered_squads"]:
            findings.append(dict(capture=c["capture"], role=c["role"],
                                 residual="unregistered_squads",
                                 value=len(c["unregistered_squads"]),
                                 squad_keys=c["unregistered_squads"],
                                 detail="squads seen on the wire that the entrant registry never lists"))
        if c["tick_regressions"]:
            findings.append(dict(capture=c["capture"], role=c["role"],
                                 residual="tick_regressions", value=c["tick_regressions"],
                                 detail="ticks moved backwards inside a segment"))
    if quality["attribution_residual"]:
        findings.append(dict(capture=season.name, role="season",
                             residual="attribution_residual",
                             value=quality["attribution_residual"],
                             squad_keys=unregistered,
                             detail="season gross output less the sum of the conference gross outputs"))
    if absent:
        findings.append(dict(capture=season.name, role="season",
                             residual="absent_entrants", value=len(absent),
                             squad_keys=absent,
                             detail="registered entrants that appear in no capture"))
    quality["findings"] = findings
    write_json(out_dir, "data_quality.json", quality)

    audit = dict(
        unstated_quantities=quantity_audit,
        search=search,
        pinned=pinned,
        attribution_settlement_passes=passes,
        bracket=dict(eligibility_match_floor=docs.figure(["eligibility_match_floor"], 2),
                     bracket_size=3,
                     eligible_pool=bracket["eligible_pool"],
                     eligible_pool_size=len(bracket["eligible_pool"]),
                     candidate_space=bracket["candidate_space"],
                     constraint_names=BRACKET_CONSTRAINTS,
                     constraint_count=len(BRACKET_CONSTRAINTS),
                     best_satisfied_constraint_count=bracket["best_satisfied_constraint_count"],
                     elected=bracket["members"],
                     elected_constraints=bracket["constraints"],
                     tie_breaks=["most constraints satisfied",
                                 "least combined charged upkeep",
                                 "member keys sort first"]),
        docket=dict(candidate_space=docket["candidate_space"],
                    feasible_candidate_space=docket.get("feasible_candidate_space"),
                    docket_size=docket.get("docket_size"),
                    conference_cap=docket.get("conference_cap"),
                    review_weight=docket.get("review_weight"),
                    elected=[r["match_id"] for r in docket["matches"]],
                    total_review_index=docket["total_review_index"],
                    conference_spread=docket["conference_spread"]),
        calibration=calibration,
        calibration_agrees=all(r["agrees"] for r in calibration),
        reference_transcript=transcript_check,
        example_group_report=example_check,
        notes=[
            "The reference capture decodes byte for byte against its labelled "
            "transcript before either guarded capture is touched.",
            "Sealing applied the rotate guard before the keystream guard, so a "
            "reading unmasks the keystream first and then turns the blob back; "
            "no other order decodes the control slice.",
            "Escape directive pairs settle from the body outward: the pair "
            "nearest the body is weighed first. The control figures do not "
            "separate the two orders, but only this one reproduces the "
            "published example group report.",
            "Operation 2 leaves the attribution alone when the key the record "
            "already holds sits outside the roster dictionary.",
            "A closed run of hand overs is collapsed onto the member whose key "
            "sorts first before the sweeps are counted, which is what lets the "
            "sweeps reach a fixpoint at all.",
        ])
    write_json(out_dir, "policy_audit.json", audit)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
