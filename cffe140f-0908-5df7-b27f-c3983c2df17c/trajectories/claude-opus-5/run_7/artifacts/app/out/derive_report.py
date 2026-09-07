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

M32 = 0xFFFFFFFF

# ---------------------------------------------------------------------------
# document parsing -- rosters and policy figures come out of the markdown so
# that a differently-published corpus is honoured rather than memorised.
# ---------------------------------------------------------------------------

LABEL_LINE = re.compile(
    r"^\s*[-*]?\s*([A-Za-z][A-Za-z0-9 '\-]*?)\s*:\s*"
    r"(`[^`]*`(?:\s*,\s*`[^`]*`)*)\s*$"
)
CONTROL_LINE = re.compile(r"^\s*[-*]\s*(C\d+)\s+([^:]+?)\s*:\s*(-?\d+)\s*$")


def norm(label):
    return re.sub(r"[^a-z0-9]+", " ", label.lower()).strip()


def read_documents(data_dir):
    """Harvest every ``Label: `a`, `b`` line and every ``- Cn label: value`` line."""
    figures = {}
    controls = []
    for name in sorted(os.listdir(data_dir)):
        if not name.lower().endswith((".md", ".txt")):
            continue
        with open(os.path.join(data_dir, name), "r", encoding="utf-8") as fh:
            for line in fh:
                mc = CONTROL_LINE.match(line)
                if mc:
                    controls.append(
                        {
                            "control_id": mc.group(1),
                            "label": mc.group(2).strip(),
                            "published": int(mc.group(3)),
                        }
                    )
                    continue
                ml = LABEL_LINE.match(line)
                if ml:
                    vals = re.findall(r"`([^`]*)`", ml.group(2))
                    figures.setdefault(norm(ml.group(1)), vals)
    controls.sort(key=lambda c: int(c["control_id"][1:]))
    return figures, controls


def figure_list(figures, *keywords_sets):
    for keys in keywords_sets:
        for label, vals in figures.items():
            if all(k in label for k in keys):
                return vals
    return None


def as_int(text):
    text = text.strip()
    return int(text, 16) if text.lower().startswith("0x") else int(text)


def load_policy_rosters(figures):
    def ints(vals, default):
        return [as_int(v) for v in vals] if vals else default

    def one(vals, default):
        return as_int(vals[0]) if vals else default

    tags = figure_list(figures, ("engine", "tag", "roster")) or [
        "orion-fold", "wren-cadence", "tessel-drift", "halcyon-mark",
        "vector-plume", "umbral-stack", "quill-lattice", "nimbus-gable",
    ]
    return {
        "engine_tag_roster": list(tags),
        "window_depth_roster": ints(figure_list(figures, ("window", "depth", "roster")), [4, 6, 8, 10]),
        "sentinel_roster": ints(figure_list(figures, ("sentinel", "roster")), [0x1A, 0x1D, 0x20, 0x7F]),
        "budget_roster": ints(figure_list(figures, ("budget", "roster")), [20, 220, 440, 640, 860, 1060]),
        "threshold_roster": ints(figure_list(figures, ("threshold", "roster")), [20, 140, 200, 360, 700, 1300]),
        "eligibility_match_floor": one(figure_list(figures, ("eligibility", "match", "floor")), 2),
        "ladder_weight_prominence": one(figure_list(figures, ("ladder", "weight", "prominence")), 4),
        "standing_points_floor": one(figure_list(figures, ("standing", "points", "floor")), 3),
        "combined_ladder_weight_floor": one(figure_list(figures, ("combined", "ladder", "weight", "floor")), 7),
        "combined_gross_output_floor": one(figure_list(figures, ("combined", "gross", "output", "floor")), 900),
        "review_weight": one(figure_list(figures, ("review", "weight")), 25),
        "docket_size": one(figure_list(figures, ("docket", "size")), 5),
        "docket_conference_cap": one(figure_list(figures, ("docket", "conference", "cap")), 2),
        "bracket_size": 3,
    }


# ---------------------------------------------------------------------------
# reference tables
# ---------------------------------------------------------------------------


def load_tables(data_dir):
    registry, topology, licences = {}, {}, {}
    for name in sorted(os.listdir(data_dir)):
        if not name.lower().endswith(".csv"):
            continue
        with open(os.path.join(data_dir, name), newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        if not rows:
            continue
        cols = set(rows[0])
        if {"squad_key", "division", "licence_class"} <= cols:
            for r in rows:
                registry[r["squad_key"]] = r
        elif {"division", "conference", "ladder_weight"} <= cols:
            for r in rows:
                topology[r["division"]] = r
        elif {"licence_class", "segment", "levy_rate"} <= cols:
            for r in rows:
                licences[r["licence_class"]] = r
    return registry, topology, licences


class Book(object):
    """Registry lookups with the unregistered-squad defaults folded in."""

    def __init__(self, registry, topology, licences):
        self.registry = registry
        self.topology = topology
        self.licences = licences

    def registered(self, key):
        return key in self.registry

    def division(self, key):
        return self.registry[key]["division"] if key in self.registry else None

    def licence(self, key):
        return self.registry[key]["licence_class"] if key in self.registry else None

    def entrant_id(self, key):
        return self.registry[key].get("entrant_id") if key in self.registry else None

    def ranked(self, key):
        if key not in self.registry:
            return False
        return str(self.registry[key].get("is_ranked", "")).strip().lower() in ("yes", "true", "1", "y")

    def ladder_weight(self, key):
        d = self.division(key)
        if d is not None and d in self.topology:
            return int(self.topology[d]["ladder_weight"])
        return 1

    def levy_rate(self, key):
        c = self.licence(key)
        if c is not None and c in self.licences:
            return int(self.licences[c]["levy_rate"])
        return 1

    def conference(self, key):
        d = self.division(key)
        if d is not None and d in self.topology:
            return self.topology[d]["conference"]
        return None

    def segment(self, key):
        c = self.licence(key)
        if c is not None and c in self.licences:
            return self.licences[c]["segment"]
        return None


# ---------------------------------------------------------------------------
# journal decoding
# ---------------------------------------------------------------------------

GUARD_NAMES = [
    "keystream", "chain", "delta2", "driftkey", "segperm", "backref",
    "endian", "countbias", "shadow", "rotate", "alias", "escape",
]


class DecodeError(Exception):
    def __init__(self, message, blames="C2"):
        Exception.__init__(self, message)
        self.blames = blames


def rot_left(blob, n):
    if not blob:
        return blob
    n %= len(blob)
    return blob[n:] + blob[:n]


def read_envelope(data):
    if data[0:4] != b"RJNL":
        raise DecodeError("envelope marker missing", "C1")
    guard = struct.unpack_from("<H", data, 6)[0]
    if (guard >> 6) & 1:
        nonce = struct.unpack_from(">I", data, 8)[0]
    else:
        nonce = struct.unpack_from("<I", data, 8)[0]
    segs, recs, base, cid, roster, checksum = struct.unpack_from("<6I", data, 12)
    return {
        "version": data[4],
        "guard_register": guard,
        "guards_active": [n for i, n in enumerate(GUARD_NAMES) if (guard >> i) & 1],
        "nonce": nonce,
        "declared_segments": segs,
        "declared_records": recs,
        "base_tick": base,
        "capture_id": cid,
        "declared_roster_size": roster,
        "envelope_checksum": checksum,
        "envelope_checksum_ok": sum(data[:32]) % (1 << 32) == checksum,
    }


def count_bias(guard, engine_tag):
    if not (guard >> 7) & 1:
        return 0
    return sum(bytearray(engine_tag.encode("utf-8"))) % 11


def permutation_step(nonce, nsegs):
    bound = ((nonce >> 8) % nsegs) + 2
    step = bound
    while gcd(step, nsegs) != 1:
        step += 1
    return step


def decode_capture(data, engine_tag, window_depth, sentinel, roster_dict):
    """Return (envelope, segments, records, meta) for one sealed capture."""
    env = read_envelope(data)
    guard = env["guard_register"]
    bias = count_bias(guard, engine_tag)
    nsegs = env["declared_segments"] - bias
    nrecs = env["declared_records"] - bias
    nroster = env["declared_roster_size"] - bias
    if nsegs <= 0:
        raise DecodeError("non positive segment count under bias %d" % bias, "C1")

    segments = []
    off = 36
    while off < len(data):
        if data[off:off + 4] != b"SEG0":
            raise DecodeError("segment marker missing at %d" % off, "C1")
        slot, mid, rc, salt, width, pck = struct.unpack_from("<6I", data, off + 4)
        off += 28
        payload = data[off:off + width]
        off += width
        if len(payload) != width:
            raise DecodeError("segment payload truncated", "C1")
        segments.append({
            "slot": slot, "match_id": mid, "declared_records": rc,
            "records": rc - bias, "salt": salt, "payload_width": width,
            "payload_checksum": pck,
            "payload_checksum_ok": sum(bytearray(payload)) % (1 << 32) == pck,
            "payload": payload,
        })
    if len(segments) != nsegs:
        raise DecodeError("recovered %d segments, declared %d" % (len(segments), nsegs), "C1")

    if (guard >> 4) & 1:
        step = permutation_step(env["nonce"], nsegs)
        logical_to_slot = [None] * nsegs
        for i in range(nsegs):
            logical_to_slot[(env["nonce"] + i * step) % nsegs] = i
        if any(x is None for x in logical_to_slot):
            raise DecodeError("segment permutation is not a bijection", "C1")
    else:
        step = None
        logical_to_slot = list(range(nsegs))

    records = []
    prev_tick = env["nonce"] & M32
    capture_ordinal = 0
    running_value = 0
    lru = []
    backwards_ticks = 0

    for logical in range(nsegs):
        seg = segments[logical_to_slot[logical]]
        seg["logical_index"] = logical
        if (guard >> 3) & 1:
            eff_salt = (seg["salt"] ^ running_value) & M32
        else:
            eff_salt = seg["salt"]
        seg["effective_salt"] = eff_salt
        payload = seg["payload"]
        pos = 0
        first_diff = 0
        tick = 0
        seg_value_sum = 0
        if seg["records"] < 0:
            raise DecodeError("negative segment record count", "C2")
        for ordinal in range(seg["records"]):
            if pos + 2 > len(payload):
                raise DecodeError("frame header past payload", "C2")
            visible = payload[pos]
            shadow = payload[pos + 1]
            width = (shadow ^ (eff_salt & 0xFF)) if (guard >> 8) & 1 else visible
            blob = payload[pos + 2:pos + 2 + width]
            pos += 2 + width
            if len(blob) != width or width == 0:
                raise DecodeError("record blob truncated", "C2")

            if (guard >> 0) & 1:
                seed = ("%s:%d:%d:%d" % (engine_tag, seg["match_id"], eff_salt, ordinal)).encode("utf-8")
                stream = hashlib.sha256(seed).digest()
                while len(stream) < width:
                    stream = stream + hashlib.sha256(stream).digest()
                blob = bytes(bytearray(a ^ b for a, b in zip(bytearray(blob), bytearray(stream[:width]))))
            if (guard >> 9) & 1:
                turn = (eff_salt + capture_ordinal) % width
                blob = rot_left(blob, (width - turn) % width)

            directives = []
            cut = 0
            if (guard >> 11) & 1:
                while cut + 1 < len(blob) and blob[cut] == sentinel and len(directives) < 2:
                    directives.append(blob[cut + 1])
                    cut += 2
            body = blob[cut:]
            if len(body) < 3:
                raise DecodeError("record body too short", "C2")
            kind = body[0]
            mode = body[1]
            if mode == 0:
                head = 5
                wire_field = body[2:5].decode("latin-1")
            elif mode in (1, 2):
                head = 3
                wire_field = body[2]
            else:
                raise DecodeError("unknown attribution mode %d" % mode, "C2")
            if len(body) != head + 10:
                raise DecodeError("record body width %d unexpected" % len(body), "C2")
            unit = struct.unpack_from("<H", body, head)[0]
            raw_tick = struct.unpack_from("<I", body, head + 2)[0]
            raw_value = struct.unpack_from("<I", body, head + 6)[0]

            if (guard >> 2) & 1:
                first_diff = (first_diff + raw_tick) & M32
                new_tick = (tick + first_diff) & M32
            else:
                new_tick = raw_tick
            if ordinal > 0 and new_tick < tick:
                backwards_ticks += 1
            tick = new_tick

            value = (raw_value ^ prev_tick) & M32 if (guard >> 1) & 1 else raw_value
            prev_tick = tick

            if mode == 0:
                wire_key = wire_field
            elif mode == 1:
                if not (guard >> 5) & 1:
                    raise DecodeError("back reference without the guard", "C2")
                if wire_field >= len(lru):
                    raise DecodeError("back reference %d beyond window" % wire_field, "C2")
                wire_key = lru[wire_field]
            else:
                if not roster_dict:
                    raise DecodeError("alias without a roster dictionary", "C2")
                wire_key = roster_dict[wire_field % len(roster_dict)]

            if (guard >> 5) & 1:
                if wire_key in lru:
                    lru.remove(wire_key)
                lru.append(wire_key)
                if len(lru) > window_depth:
                    del lru[0]

            # Directive pairs sit ahead of the body; the pair nearest the body
            # was laid down last, so it is weighed first.
            key = wire_key
            for directive in reversed(directives):
                op, operand = directive >> 4, directive & 0x0F
                if op == 1 and roster_dict:
                    key = roster_dict[operand % len(roster_dict)]
                elif op == 2 and roster_dict and key in roster_dict:
                    key = roster_dict[(roster_dict.index(key) + operand) % len(roster_dict)]

            records.append({
                "kind": kind, "key": key, "wire_key": wire_key, "unit": unit,
                "tick": tick, "value": value, "match_id": seg["match_id"],
                "logical_segment": logical, "segment_ordinal": ordinal,
                "capture_ordinal": capture_ordinal, "escapes": len(directives),
            })
            seg_value_sum = (seg_value_sum + value) & M32
            capture_ordinal += 1

        filler = len(payload) - pos
        if filler != (seg["salt"] % 5) + 3:
            raise DecodeError("filler width %d, expected %d" % (filler, (seg["salt"] % 5) + 3), "C2")
        running_value = (running_value + seg_value_sum) & M32

    if len(records) != nrecs:
        raise DecodeError("recovered %d records, declared %d" % (len(records), nrecs), "C2")

    meta = {
        "count_bias": bias,
        "segments_expected": nsegs,
        "records_expected": nrecs,
        "roster_size_expected": nroster,
        "permutation_step": step,
        "logical_to_slot": logical_to_slot,
        "backwards_ticks": backwards_ticks,
    }
    for seg in segments:
        seg.pop("payload", None)
    return env, segments, records, meta


# ---------------------------------------------------------------------------
# settlement
# ---------------------------------------------------------------------------

KIND_NAMES = {1: "deploy", 2: "output", 3: "eliminate", 4: "disband",
              5: "concede", 6: "transfer", 8: "close"}


def settle_ownership(records, squads, roster_dict):
    """Follow the hand overs to a fixpoint by repeated sweeps."""
    hand = {}
    for rec in records:
        if rec["kind"] == 6 and roster_dict:
            hand[rec["key"]] = roster_dict[rec["value"] % len(roster_dict)]

    universe = sorted(set(squads) | set(hand) | set(hand.values()))

    # A closed set of hand overs settles on its first-sorting member.
    step = dict((k, hand.get(k, k)) for k in universe)
    colour = {}
    for start in universe:
        if start in colour:
            continue
        path, node = [], start
        while node not in colour and node not in path:
            path.append(node)
            node = step[node]
        if node in path:
            cycle = path[path.index(node):]
            winner = min(cycle)
            for member in cycle:
                step[member] = winner
            step[winner] = winner
        for member in path:
            colour[member] = True

    owner = dict((k, k) for k in universe)
    passes = 0
    while True:
        passes += 1
        moved = dict((k, step.get(owner[k], owner[k])) for k in universe)
        if moved == owner:
            break
        owner = moved
        if passes > 4 * len(universe) + 8:
            break
    return hand, owner, passes


def settle_matches(records, book, budget):
    """Per-match output, upkeep and score, then the season budget draw-down."""
    by_segment = {}
    for rec in records:
        by_segment.setdefault(rec["logical_segment"], []).append(rec)

    matches = []
    for logical in sorted(by_segment):
        rows = by_segment[logical]
        closes = [r["tick"] for r in rows if r["kind"] == 8]
        close_tick = max(closes) if closes else max(r["tick"] for r in rows)
        elim, conc, deploy, disband = {}, {}, {}, {}
        for r in rows:
            key = r["key"]
            if r["kind"] == 3:
                elim.setdefault((key, r["unit"]), r["tick"])
            elif r["kind"] == 5:
                if key not in conc or r["tick"] < conc[key]:
                    conc[key] = r["tick"]
            elif r["kind"] == 1:
                deploy.setdefault((key, r["unit"]), r)
            elif r["kind"] == 4:
                disband.setdefault((key, r["unit"]), r["tick"])

        stats = {}

        def slot(key):
            if key not in stats:
                stats[key] = {"gross_output": 0, "voided_output": 0, "raw_upkeep": 0}
            return stats[key]

        for r in rows:
            box = slot(r["key"])
            if r["kind"] != 2:
                continue
            unit_key = (r["key"], r["unit"])
            dead = unit_key in elim and r["tick"] >= elim[unit_key]
            gone = r["key"] in conc and r["tick"] >= conc[r["key"]]
            box["voided_output" if (dead or gone) else "gross_output"] += r["value"]

        for (key, unit), rec in deploy.items():
            end = disband.get((key, unit), close_tick)
            slot(key)["raw_upkeep"] += rec["value"] * max(0, end - rec["tick"])

        matches.append({
            "logical_index": logical,
            "match_id": rows[0]["match_id"],
            "records": len(rows),
            "close_tick": close_tick,
            "participants": sorted(stats),
            "conceded": sorted(conc),
            "stats": stats,
            "escape_records": sum(1 for r in rows if r["escapes"]),
            "voided_output": sum(b["voided_output"] for b in stats.values()),
            "gross_output": sum(b["gross_output"] for b in stats.values()),
        })

    remaining = {}
    for match in matches:
        for key in sorted(match["stats"]):
            box = match["stats"][key]
            left = remaining.get(key, budget)
            charged = min(box["raw_upkeep"], left)
            remaining[key] = left - charged
            box["charged_upkeep"] = charged
            box["overflow_upkeep"] = box["raw_upkeep"] - charged
            box["match_score"] = (box["gross_output"] * book.ladder_weight(key)
                                  - charged * book.levy_rate(key))
    return matches


def settle_standings(matches):
    awards, streak, history = {}, {}, {}
    for match in matches:
        scores = dict((k, match["stats"][k]["match_score"])
                      for k in match["participants"] if k not in match["conceded"])
        winners = []
        if scores:
            top = max(scores.values())
            winners = sorted(k for k, v in scores.items() if v == top)
            for key in winners:
                awards[key] = awards.get(key, 0) + (3 if len(winners) == 1 else 1)
        outright = winners if len(winners) == 1 else []
        for key in match["participants"]:
            history.setdefault(key, []).append("W" if key in outright else "-")
    for key, marks in history.items():
        run = 0
        for mark in marks:
            if mark == "W":
                run += 1
                if run > 1:
                    streak[key] = streak.get(key, 0) + 2
            else:
                run = 0
    return awards, streak, history


def tier_of(score, threshold):
    if score >= 3 * threshold:
        return "champion"
    if score >= 2 * threshold:
        return "contender"
    if score >= threshold:
        return "qualifier"
    return "provisional"


def build_squads(matches, book, threshold, owner, hand):
    awards, streak, history = settle_standings(matches)
    keys = sorted(set(k for m in matches for k in m["stats"]))
    squads = {}
    for key in keys:
        agg = {"gross_output": 0, "voided_output": 0, "raw_upkeep": 0,
               "charged_upkeep": 0, "overflow_upkeep": 0, "season_score": 0}
        for match in matches:
            box = match["stats"].get(key)
            if box is None:
                continue
            for field in ("gross_output", "voided_output", "raw_upkeep",
                          "charged_upkeep", "overflow_upkeep"):
                agg[field] += box[field]
            agg["season_score"] += box["match_score"]
        agg.update({
            "squad_key": key,
            "entrant_id": book.entrant_id(key),
            "registered": book.registered(key),
            "division": book.division(key),
            "conference": book.conference(key),
            "licence_class": book.licence(key),
            "segment": book.segment(key),
            "ladder_weight": book.ladder_weight(key),
            "levy_rate": book.levy_rate(key),
            "is_ranked": book.ranked(key),
            "matches": len(history.get(key, [])),
            "match_awards": awards.get(key, 0),
            "streak_credit": streak.get(key, 0),
            "standing_points": awards.get(key, 0) + streak.get(key, 0),
            "tier": tier_of(agg["season_score"], threshold),
            "effective_owner": owner.get(key, key),
            "handed_over": key in hand,
        })
        squads[key] = agg
    order = sorted(keys, key=lambda k: (-squads[k]["season_score"],
                                        -squads[k]["standing_points"], k))
    for rank, key in enumerate(order, 1):
        squads[key]["rank"] = rank
    return squads, order


# ---------------------------------------------------------------------------
# calibration: pin the unstated quantities against the control figures
# ---------------------------------------------------------------------------


def control_figures(matches, records, squads_seen):
    gross = sum(m["gross_output"] for m in matches)
    voided = sum(m["voided_output"] for m in matches)
    raw = sum(b["raw_upkeep"] for m in matches for b in m["stats"].values())
    return {
        "matches opened": len(matches),
        "records recovered": len(records),
        "distinct squads": len(squads_seen),
        "gross output": gross,
        "voided output": voided,
        "raw upkeep": raw,
    }


def label_key(text):
    return norm(text)


def pick_control(controls, *keywords):
    for c in controls:
        lab = label_key(c["label"])
        if all(k in lab for k in keywords):
            return c
    return None


def evaluate_guard_candidate(ref_bytes, ctl_bytes, tag, window, sentinel):
    """Decode reference then control; return (figures, roster_dict, error)."""
    try:
        _, _, ref_recs, _ = decode_capture(ref_bytes, tag, window, sentinel, [])
    except DecodeError as exc:
        return None, None, None, ("reference capture would not decode: %s" % exc, exc.blames)
    roster_dict = []
    for rec in ref_recs:
        if rec["wire_key"] not in roster_dict:
            roster_dict.append(rec["wire_key"])
    try:
        _, _, recs, _ = decode_capture(ctl_bytes, tag, window, sentinel, roster_dict)
    except DecodeError as exc:
        return None, roster_dict, None, (str(exc), exc.blames)
    return recs, roster_dict, None, None


# ---------------------------------------------------------------------------
# bracket and docket
# ---------------------------------------------------------------------------


def bracket_constraints(members, squads, policy, all_segments=None):
    rows = squads
    weights = [rows[m]["ladder_weight"] for m in members]
    confs = [rows[m]["conference"] for m in members if rows[m]["conference"] is not None]
    segs = set(rows[m]["segment"] for m in members if rows[m]["segment"] is not None)
    all_segs = set(all_segments) if all_segments else set(
        s["segment"] for s in rows.values() if s["segment"] is not None)
    checks = [
        ("prominent_division",
         any(w >= policy["ladder_weight_prominence"] for w in weights)),
        ("both_segments_present", len(all_segs) > 0 and segs >= all_segs),
        ("standing_points_floor",
         all(rows[m]["standing_points"] >= policy["standing_points_floor"] for m in members)),
        ("distinct_conferences", len(confs) == len(set(confs))),
        ("ranked_entrant_present", any(rows[m]["is_ranked"] for m in members)),
        ("combined_ladder_weight",
         sum(weights) >= policy["combined_ladder_weight_floor"]),
        ("no_overflow_upkeep", all(rows[m]["overflow_upkeep"] == 0 for m in members)),
        ("champion_present", any(rows[m]["tier"] == "champion" for m in members)),
        ("combined_gross_output",
         sum(rows[m]["gross_output"] for m in members) >= policy["combined_gross_output_floor"]),
        ("no_handed_entry", not any(rows[m]["handed_over"] for m in members)),
    ]
    return checks


def elect_bracket(squads, policy, all_segments=None):
    size = policy["bracket_size"]
    pool = sorted(k for k, s in squads.items()
                  if s["effective_owner"] == k and s["matches"] >= policy["eligibility_match_floor"])
    best = None
    best_count = -1
    considered = 0
    for members in itertools.combinations(pool, size):
        considered += 1
        checks = bracket_constraints(list(members), squads, policy, all_segments)
        met = sum(1 for _, ok in checks if ok)
        upkeep = sum(squads[m]["charged_upkeep"] for m in members)
        candidate = (-met, upkeep, list(members))
        if best is None or candidate < best:
            best = candidate
            best_count = met
    if best is None:
        return {"pool": pool, "pool_size": len(pool), "candidate_space": considered,
                "best_satisfied_constraints": 0, "members": [], "constraints": []}
    members = best[2]
    checks = bracket_constraints(members, squads, policy, all_segments)
    return {
        "pool": pool,
        "pool_size": len(pool),
        "candidate_space": considered,
        "best_satisfied_constraints": best_count,
        "members": members,
        "combined_charged_upkeep": best[1],
        "combined_gross_output": sum(squads[m]["gross_output"] for m in members),
        "combined_ladder_weight": sum(squads[m]["ladder_weight"] for m in members),
        "constraints": [{"constraint": n, "satisfied": bool(ok)} for n, ok in checks],
        "constraints_satisfied": best_count,
        "constraints_total": len(checks),
    }


def match_conference(match, book):
    best_key, best_gross = None, None
    for key in sorted(match["stats"]):
        if not book.registered(key):
            continue
        gross = match["stats"][key]["gross_output"]
        if best_gross is None or gross > best_gross:
            best_key, best_gross = key, gross
    return (book.conference(best_key) if best_key else None), best_key


def elect_docket(matches, book, policy):
    size = policy["docket_size"]
    cap = policy["docket_conference_cap"]
    rows = []
    for match in matches:
        conf, owner_key = match_conference(match, book)
        rows.append({
            "match_id": match["match_id"],
            "logical_index": match["logical_index"],
            "conference": conf,
            "leading_squad": owner_key,
            "voided_output": match["voided_output"],
            "escape_records": match["escape_records"],
            "review_index": match["voided_output"] + policy["review_weight"] * match["escape_records"],
        })
    index = dict((r["match_id"], r) for r in rows)
    ids = sorted(index)
    size = min(size, len(ids))

    def feasible(combo):
        seen = {}
        for mid in combo:
            conf = index[mid]["conference"]
            if conf is None:
                continue
            seen[conf] = seen.get(conf, 0) + 1
            if seen[conf] > cap:
                return False
        return True

    best = None
    space = 0
    # Exhaustive when small; otherwise search per-conference count vectors.
    if len(ids) <= 30:
        for combo in itertools.combinations(ids, size):
            space += 1
            if not feasible(combo):
                continue
            total = sum(index[m]["review_index"] for m in combo)
            cand = (-total, list(combo))
            if best is None or cand < best:
                best = cand
    else:
        groups = {}
        for r in rows:
            groups.setdefault(r["conference"], []).append(r["match_id"])
        for conf in groups:
            groups[conf].sort(key=lambda m: (-index[m]["review_index"], m))
        names = sorted(groups, key=lambda c: (c is None, c))
        limits = [len(groups[c]) if c is None else min(cap, len(groups[c])) for c in names]
        for counts in itertools.product(*[range(l + 1) for l in limits]):
            if sum(counts) != size:
                continue
            space += 1
            combo = []
            for c, n in zip(names, counts):
                combo.extend(groups[c][:n])
            combo.sort()
            total = sum(index[m]["review_index"] for m in combo)
            cand = (-total, combo)
            if best is None or cand < best:
                best = cand
    if best is None:
        return {"matches": [], "total_review_index": 0, "conference_spread": {},
                "candidate_space": space, "match_review": rows}
    chosen = best[1]
    spread = {}
    for mid in chosen:
        conf = index[mid]["conference"]
        name = conf if conf is not None else "unassigned"
        spread[name] = spread.get(name, 0) + 1
    return {
        "matches": [index[m] for m in chosen],
        "match_ids": list(chosen),
        "total_review_index": -best[0],
        "conference_spread": spread,
        "candidate_space": space,
        "match_review": rows,
    }


# ---------------------------------------------------------------------------
# reports
# ---------------------------------------------------------------------------

ENTRY_FIELDS = ["charged_upkeep", "conference", "division", "entrant_id",
                "gross_output", "licence_class", "matches", "overflow_upkeep",
                "season_score", "segment", "squad_key", "standing_points",
                "streak_credit", "tier", "voided_output"]


def group_report(kind, name, members, squads):
    entries = []
    for key in sorted(members):
        row = squads[key]
        entries.append(dict((f, row[f]) for f in ENTRY_FIELDS))
    total = lambda f: sum(e[f] for e in entries)
    counts = {"champion": 0, "contender": 0, "qualifier": 0, "provisional": 0}
    for e in entries:
        counts[e["tier"]] = counts.get(e["tier"], 0) + 1
    report = {
        "group_kind": kind,
        "group_name": name,
        "entrants": len(entries),
        "entries": entries,
        "divisions": sorted(set(e["division"] for e in entries if e["division"])),
        "gross_output": total("gross_output"),
        "voided_output": total("voided_output"),
        "charged_upkeep": total("charged_upkeep"),
        "overflow_upkeep": total("overflow_upkeep"),
        "season_score": total("season_score"),
        "standing_points": total("standing_points"),
        "tier_counts": counts,
    }
    if kind == "segment":
        report["licence_classes"] = sorted(set(e["licence_class"] for e in entries if e["licence_class"]))
    return report


def write_json(path, payload):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def classify_captures(data_dir):
    found = {}
    for name in sorted(os.listdir(data_dir)):
        path = os.path.join(data_dir, name)
        if not os.path.isfile(path):
            continue
        with open(path, "rb") as fh:
            head = fh.read(4)
        if head != b"RJNL":
            continue
        with open(path, "rb") as fh:
            data = fh.read()
        env = read_envelope(data)
        role = {1: "reference", 2: "control", 3: "season"}.get(env["capture_id"] % 10)
        if role is None:
            continue
        found[role] = {"file": name, "bytes": data, "envelope": env}
    return found


def load_transcript(data_dir):
    for name in sorted(os.listdir(data_dir)):
        if not name.lower().endswith(".jsonl"):
            continue
        rows = []
        with open(os.path.join(data_dir, name), "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        if rows:
            return name, rows
    return None, None


def main(argv):
    if len(argv) != 3:
        sys.stderr.write("usage: derive_report.py DATA_DIR OUT_DIR\n")
        return 2
    data_dir, out_dir = os.path.abspath(argv[1]), os.path.abspath(argv[2])
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)

    figures, controls = read_documents(data_dir)
    policy = load_policy_rosters(figures)
    registry, topology, licences = load_tables(data_dir)
    book = Book(registry, topology, licences)
    captures = classify_captures(data_dir)
    for role in ("reference", "control", "season"):
        if role not in captures:
            raise SystemExit("no %s capture found under %s" % (role, data_dir))

    ref_bytes = captures["reference"]["bytes"]
    ctl_bytes = captures["control"]["bytes"]
    season_bytes = captures["season"]["bytes"]

    c_matches = pick_control(controls, "matches", "opened")
    c_records = pick_control(controls, "records", "recovered")
    c_squads = pick_control(controls, "distinct", "squads")
    c_gross = pick_control(controls, "gross", "output")
    c_voided = pick_control(controls, "voided", "output")
    c_raw = pick_control(controls, "raw", "upkeep")
    c_charged = pick_control(controls, "charged", "upkeep")
    c_overflow = pick_control(controls, "overflow", "upkeep")
    tier_controls = {}
    for tier in ("champion", "contender", "qualifier", "provisional"):
        hit = pick_control(controls, tier)
        if hit:
            tier_controls[tier] = hit

    stage_one = [(c_matches, "matches opened"), (c_records, "records recovered"),
                 (c_squads, "distinct squads"), (c_gross, "gross output"),
                 (c_voided, "voided output"), (c_raw, "raw upkeep")]
    stage_one = [(c, k) for c, k in stage_one if c is not None]

    # ---- stage one: the three guard quantities -----------------------------
    joint = []
    accepted = None
    cache = {}
    for tag in policy["engine_tag_roster"]:
        for window in policy["window_depth_roster"]:
            for sentinel in policy["sentinel_roster"]:
                recs, roster_dict, _, err = evaluate_guard_candidate(
                    ref_bytes, ctl_bytes, tag, window, sentinel)
                entry = {"engine_tag": tag, "window_depth": window,
                         "sentinel": "0x%02X" % sentinel}
                if err is not None:
                    blamed = None
                    for ctrl, _field in stage_one:
                        if ctrl["control_id"] == err[1]:
                            blamed = ctrl
                            break
                    entry["verdict"] = "rejected"
                    entry["falsifier"] = {
                        "control_id": err[1],
                        "label": blamed["label"] if blamed else None,
                        "published": blamed["published"] if blamed else None,
                        "reproduced": None,
                        "reason": "capture would not decode: %s" % err[0],
                    }
                    joint.append(entry)
                    continue
                mm = settle_matches(recs, book, policy["budget_roster"][0])
                seen = set(r["key"] for r in recs)
                got = control_figures(mm, recs, seen)
                bad = None
                for ctrl, field in stage_one:
                    if got[field] != ctrl["published"]:
                        bad = (ctrl, field, got[field])
                        break
                if bad is None:
                    entry["verdict"] = "accepted"
                    if accepted is None:
                        accepted = (tag, window, sentinel, roster_dict)
                else:
                    ctrl, field, value = bad
                    entry["verdict"] = "rejected"
                    entry["falsifier"] = {
                        "control_id": ctrl["control_id"],
                        "label": ctrl["label"],
                        "published": ctrl["published"],
                        "reproduced": value,
                    }
                joint.append(entry)
                cache[(tag, window, sentinel)] = (recs, roster_dict, mm)

    if accepted is None:
        raise SystemExit("no guard reading reproduces the published control figures")
    engine_tag, window_depth, sentinel, roster_dict = accepted

    def per_quantity(name, values, fmt=str):
        rows = []
        for value in values:
            hits = [j for j in joint if j[name] == fmt(value)]
            ok = [j for j in hits if j["verdict"] == "accepted"]
            if ok:
                rows.append({"candidate": fmt(value), "verdict": "accepted",
                             "supporting_readings": len(ok)})
            else:
                # Name the control figure that kills the candidate when every
                # other quantity is read at its most favourable: the deepest
                # control figure any reading on this candidate reached.
                fals = None
                depth = -1
                for j in hits:
                    f = j.get("falsifier")
                    if not f:
                        continue
                    try:
                        here = int(str(f["control_id"])[1:])
                    except ValueError:
                        here = 0
                    if here > depth:
                        fals, depth = f, here
                rows.append({"candidate": fmt(value), "verdict": "rejected",
                             "falsifier": fals,
                             "note": "no reading on this candidate reproduces every "
                                     "control figure of the guard stage"})
        return rows

    ctl_recs, _, _ = cache[(engine_tag, window_depth, sentinel)]

    # ---- stage two: the season budget --------------------------------------
    budget_rows = []
    budget = None
    for cand in policy["budget_roster"]:
        mm = settle_matches(ctl_recs, book, cand)
        charged = sum(b["charged_upkeep"] for m in mm for b in m["stats"].values())
        overflow = sum(b["overflow_upkeep"] for m in mm for b in m["stats"].values())
        bad = None
        for ctrl, value in ((c_charged, charged), (c_overflow, overflow)):
            if ctrl is not None and ctrl["published"] != value:
                bad = (ctrl, value)
                break
        if bad is None:
            budget_rows.append({"candidate": cand, "verdict": "accepted"})
            if budget is None:
                budget = cand
        else:
            ctrl, value = bad
            budget_rows.append({"candidate": cand, "verdict": "rejected",
                                "falsifier": {"control_id": ctrl["control_id"],
                                              "label": ctrl["label"],
                                              "published": ctrl["published"],
                                              "reproduced": value}})
    if budget is None:
        raise SystemExit("no season budget reproduces the published upkeep split")

    # ---- stage three: the promotion threshold ------------------------------
    ctl_matches = settle_matches(ctl_recs, book, budget)
    ctl_hand, ctl_owner, ctl_passes = settle_ownership(
        ctl_recs, set(r["key"] for r in ctl_recs), roster_dict)
    threshold_rows = []
    threshold = None
    for cand in policy["threshold_roster"]:
        squads, _ = build_squads(ctl_matches, book, cand, ctl_owner, ctl_hand)
        counts = {}
        for s in squads.values():
            if s["registered"]:
                counts[s["tier"]] = counts.get(s["tier"], 0) + 1
        bad = None
        for tier, ctrl in sorted(tier_controls.items(), key=lambda kv: int(kv[1]["control_id"][1:])):
            if counts.get(tier, 0) != ctrl["published"]:
                bad = (ctrl, counts.get(tier, 0))
                break
        if bad is None:
            threshold_rows.append({"candidate": cand, "verdict": "accepted"})
            if threshold is None:
                threshold = cand
        else:
            ctrl, value = bad
            threshold_rows.append({"candidate": cand, "verdict": "rejected",
                                   "falsifier": {"control_id": ctrl["control_id"],
                                                 "label": ctrl["label"],
                                                 "published": ctrl["published"],
                                                 "reproduced": value}})
    if threshold is None:
        raise SystemExit("no promotion threshold reproduces the published tier counts")

    # ---- baseline verification --------------------------------------------
    ref_env, ref_segs, ref_recs, ref_meta = decode_capture(
        ref_bytes, engine_tag, window_depth, sentinel, [])
    tname, transcript = load_transcript(data_dir)
    verification = {"transcript_file": tname, "checked": False}
    if transcript:
        mismatches = []
        for i, (got, want) in enumerate(zip(ref_recs, transcript)):
            pair = (
                (got["kind"], got["wire_key"], got["unit"], got["tick"], got["value"], got["match_id"]),
                (want.get("kind"), want.get("squad_key"), want.get("unit"),
                 want.get("tick"), want.get("value"), want.get("match_id")),
            )
            if pair[0] != pair[1]:
                mismatches.append({"index": i, "decoded": pair[0], "transcript": pair[1]})
        verification = {
            "transcript_file": tname,
            "checked": True,
            "transcript_records": len(transcript),
            "decoded_records": len(ref_recs),
            "mismatches": len(mismatches),
            "byte_exact": len(mismatches) == 0 and len(ref_recs) == len(transcript),
            "first_mismatches": mismatches[:5],
        }

    # ---- the season --------------------------------------------------------
    env, segs, recs, meta = decode_capture(
        season_bytes, engine_tag, window_depth, sentinel, roster_dict)
    matches = settle_matches(recs, book, budget)
    seen_keys = set(r["key"] for r in recs)
    hand, owner, passes = settle_ownership(recs, seen_keys, roster_dict)
    squads, order = build_squads(matches, book, threshold, owner, hand)

    conferences = sorted(set(t["conference"] for t in topology.values()))
    segments = sorted(set(l["segment"] for l in licences.values()))
    bracket = elect_bracket(squads, policy, segments)
    docket = elect_docket(matches, book, policy)

    totals = {
        "matches": len(matches),
        "records": len(recs),
        "distinct_squads": len(squads),
        "registered_squads": sum(1 for s in squads.values() if s["registered"]),
        "unregistered_squads": sum(1 for s in squads.values() if not s["registered"]),
        "gross_output": sum(s["gross_output"] for s in squads.values()),
        "voided_output": sum(s["voided_output"] for s in squads.values()),
        "raw_upkeep": sum(s["raw_upkeep"] for s in squads.values()),
        "charged_upkeep": sum(s["charged_upkeep"] for s in squads.values()),
        "overflow_upkeep": sum(s["overflow_upkeep"] for s in squads.values()),
        "season_score": sum(s["season_score"] for s in squads.values()),
        "standing_points": sum(s["standing_points"] for s in squads.values()),
    }
    totals["total_output"] = totals["gross_output"] + totals["voided_output"]

    ledger = dict((n, 0) for n in KIND_NAMES.values())
    ledger["unknown"] = 0
    for r in recs:
        ledger[KIND_NAMES.get(r["kind"], "unknown")] += 1

    tier_counts = {"champion": 0, "contender": 0, "qualifier": 0, "provisional": 0}
    for s in squads.values():
        tier_counts[s["tier"]] = tier_counts.get(s["tier"], 0) + 1

    resolved = {
        "engine_tag": engine_tag,
        "window_depth": window_depth,
        "sentinel": "0x%02X" % sentinel,
        "season_budget": budget,
        "promotion_threshold": threshold,
        "count_bias": meta["count_bias"],
        "eligibility_match_floor": policy["eligibility_match_floor"],
        "ladder_weight_prominence": policy["ladder_weight_prominence"],
        "standing_points_floor": policy["standing_points_floor"],
        "combined_ladder_weight_floor": policy["combined_ladder_weight_floor"],
        "combined_gross_output_floor": policy["combined_gross_output_floor"],
        "review_weight": policy["review_weight"],
        "docket_size": policy["docket_size"],
        "docket_conference_cap": policy["docket_conference_cap"],
    }

    summary = {
        "capture": {
            "file": captures["season"]["file"],
            "role": "season",
            "capture_id": env["capture_id"],
            "envelope_version": env["version"],
            "guard_register": env["guard_register"],
            "guards_active": env["guards_active"],
            "capture_nonce": env["nonce"],
            "season_base_tick": env["base_tick"],
            "permutation_step": meta["permutation_step"],
            "declared": {
                "segments": env["declared_segments"],
                "records": env["declared_records"],
                "roster_size": env["declared_roster_size"],
            },
            "recovered": {
                "segments": len(segs),
                "records": len(recs),
                "distinct_squads": len(squads),
            },
        },
        "policy": resolved,
        "roster_dictionary": roster_dict,
        "totals": totals,
        "kind_ledger": ledger,
        "tier_counts": tier_counts,
        "conferences": conferences,
        "segments": segments,
        "ranking_rule": "season score descending, then standing points descending, then squad key ascending",
        "standings": [dict(squads[k], rank=squads[k]["rank"]) for k in order],
        "matches": [{
            "match_id": m["match_id"],
            "logical_index": m["logical_index"],
            "records": m["records"],
            "close_tick": m["close_tick"],
            "participants": m["participants"],
            "conceded": m["conceded"],
            "gross_output": m["gross_output"],
            "voided_output": m["voided_output"],
            "escape_records": m["escape_records"],
            "review_index": m["voided_output"] + policy["review_weight"] * m["escape_records"],
            "conference": match_conference(m, book)[0],
            "leading_squad": match_conference(m, book)[1],
            "scores": dict((k, m["stats"][k]["match_score"]) for k in m["participants"]),
        } for m in matches],
        "ownership": {
            "hand_overs": hand,
            "effective_owners": dict((k, v) for k, v in sorted(owner.items()) if k != v),
            "settlement_passes": passes,
        },
        "finals_bracket": {
            "members": bracket["members"],
            "satisfied_constraints": bracket.get("constraints_satisfied", 0),
            "total_constraints": bracket.get("constraints_total", 0),
            "constraints": bracket.get("constraints", []),
            "combined_charged_upkeep": bracket.get("combined_charged_upkeep", 0),
            "combined_gross_output": bracket.get("combined_gross_output", 0),
            "combined_ladder_weight": bracket.get("combined_ladder_weight", 0),
            "eligible_pool": bracket["pool"],
        },
        "review_docket": {
            "match_ids": docket.get("match_ids", []),
            "total_review_index": docket["total_review_index"],
            "conference_spread": docket["conference_spread"],
            "matches": docket["matches"],
        },
    }

    # ---- reconciliation ----------------------------------------------------
    conf_members = dict((c, [k for k, s in squads.items() if s["conference"] == c])
                        for c in conferences)
    seg_members = dict((g, [k for k, s in squads.items() if s["segment"] == g])
                       for g in segments)
    conf_gross = sum(squads[k]["gross_output"] for c in conferences for k in conf_members[c])
    seg_gross = sum(squads[k]["gross_output"] for g in segments for k in seg_members[g])
    unregistered = sorted(k for k, s in squads.items() if not s["registered"])
    wire_only = sorted(set(r["wire_key"] for r in recs) - set(registry))
    absent = sorted(k for k in registry if k not in squads)

    capture_rows = []
    for role in ("reference", "control", "season"):
        cap = captures[role]
        if role == "reference":
            e, sg, rc, mt = ref_env, ref_segs, ref_recs, ref_meta
        elif role == "control":
            e = cap["envelope"]
            e, sg, rc, mt = decode_capture(cap["bytes"], engine_tag, window_depth, sentinel, roster_dict)
        else:
            e, sg, rc, mt = env, segs, recs, meta
        keys = set(r["key"] for r in rc)
        registered_seen = sorted(k for k in keys if k in registry)
        capture_rows.append({
            "role": role,
            "file": cap["file"],
            "capture_id": e["capture_id"],
            "guard_register": e["guard_register"],
            "guards_active": e["guards_active"],
            "envelope_checksum_ok": e["envelope_checksum_ok"],
            "segment_payload_checksums_ok": all(s["payload_checksum_ok"] for s in sg),
            "count_bias_applied": mt["count_bias"],
            "declared_segments_raw": e["declared_segments"],
            "declared_segments": mt["segments_expected"],
            "recovered_segments": len(sg),
            "segment_residual": len(sg) - mt["segments_expected"],
            "declared_records_raw": e["declared_records"],
            "declared_records": mt["records_expected"],
            "recovered_records": len(rc),
            "record_residual": len(rc) - mt["records_expected"],
            "declared_roster_size_raw": e["declared_roster_size"],
            "declared_roster_size": mt["roster_size_expected"],
            "distinct_squads_recovered": len(keys),
            "registered_squads_recovered": len(registered_seen),
            "roster_residual": len(registered_seen) - mt["roster_size_expected"],
            "roster_exposure_residual": len(keys) - mt["roster_size_expected"],
            "unregistered_squads": sorted(k for k in keys if k not in registry),
            "backwards_ticks": mt["backwards_ticks"],
        })

    findings = []
    season_row = capture_rows[-1]
    if season_row["record_residual"]:
        findings.append("season records recovered differ from the declared count by %d"
                        % season_row["record_residual"])
    if season_row["segment_residual"]:
        findings.append("season segments recovered differ from the declared count by %d"
                        % season_row["segment_residual"])
    for row in capture_rows:
        if row["roster_exposure_residual"] and not row["unregistered_squads"]:
            findings.append(
                "%s capture: %d distinct squads recovered against a declared roster size of %d "
                "with no unregistered squad on the wire to account for the gap"
                % (row["role"], row["distinct_squads_recovered"], row["declared_roster_size"]))
        if row["roster_residual"]:
            findings.append(
                "%s capture: %d registered squads recovered against a declared roster size of %d "
                "(residual %+d)" % (row["role"], row["registered_squads_recovered"],
                                    row["declared_roster_size"], row["roster_residual"]))
    if unregistered:
        findings.append("squads on the wire with no entry in the registry: %s"
                        % ", ".join(unregistered))
    if absent:
        findings.append("registered entrants absent from the season capture: %s"
                        % ", ".join(absent))
    attribution_residual = totals["gross_output"] - conf_gross
    if attribution_residual:
        findings.append(
            "attribution residual of %d gross output sits outside the conference reports, "
            "carried by %s" % (attribution_residual, ", ".join(unregistered) or "no squad"))
    if season_row["backwards_ticks"]:
        findings.append("%d records step the tick backwards inside their segment"
                        % season_row["backwards_ticks"])

    data_quality = {
        "captures": capture_rows,
        "baseline_verification": verification,
        "season": {
            "kind_ledger": ledger,
            "ledger_residuals": {
                "output_split": (totals["gross_output"] + totals["voided_output"]) - totals["total_output"],
                "upkeep_split": (totals["charged_upkeep"] + totals["overflow_upkeep"]) - totals["raw_upkeep"],
                "record_count": season_row["record_residual"],
                "segment_count": season_row["segment_residual"],
                "roster_size": season_row["roster_residual"],
                "roster_exposure": season_row["roster_exposure_residual"],
                "backwards_ticks": season_row["backwards_ticks"],
            },
            "attribution_residual": {
                "season_gross_output": totals["gross_output"],
                "conference_gross_output": conf_gross,
                "conference_residual": attribution_residual,
                "segment_gross_output": seg_gross,
                "segment_residual": totals["gross_output"] - seg_gross,
            },
            "unregistered_squads": [{
                "squad_key": k,
                "matches": squads[k]["matches"],
                "gross_output": squads[k]["gross_output"],
                "voided_output": squads[k]["voided_output"],
                "raw_upkeep": squads[k]["raw_upkeep"],
                "charged_upkeep": squads[k]["charged_upkeep"],
                "overflow_upkeep": squads[k]["overflow_upkeep"],
                "season_score": squads[k]["season_score"],
                "standing_points": squads[k]["standing_points"],
            } for k in unregistered],
            "unregistered_wire_keys": wire_only,
            "absent_entrants": absent,
            "totals": totals,
        },
        "findings": findings,
    }

    # ---- audit -------------------------------------------------------------
    calibration = []
    ctl_squads, _ = build_squads(ctl_matches, book, threshold, ctl_owner, ctl_hand)
    ctl_tiers = {}
    for s in ctl_squads.values():
        if s["registered"]:
            ctl_tiers[s["tier"]] = ctl_tiers.get(s["tier"], 0) + 1
    reproduced = control_figures(ctl_matches, ctl_recs, set(r["key"] for r in ctl_recs))
    reproduced["charged upkeep"] = sum(b["charged_upkeep"] for m in ctl_matches for b in m["stats"].values())
    reproduced["overflow upkeep"] = sum(b["overflow_upkeep"] for m in ctl_matches for b in m["stats"].values())
    for tier in ("champion", "contender", "qualifier", "provisional"):
        reproduced["%s entrants" % tier] = ctl_tiers.get(tier, 0)
    for ctrl in controls:
        lab = label_key(ctrl["label"])
        value = None
        for name, got in reproduced.items():
            if label_key(name) == lab:
                value = got
                break
        calibration.append({
            "control_id": ctrl["control_id"],
            "label": ctrl["label"],
            "published": ctrl["published"],
            "reproduced": value,
            "agrees": value == ctrl["published"],
        })

    audit = {
        "resolved": resolved,
        "unstated_quantities": {
            "engine_tag": {
                "roster": policy["engine_tag_roster"],
                "candidates": per_quantity("engine_tag", policy["engine_tag_roster"]),
                "resolved": engine_tag,
                "pinned_by": [c["control_id"] for c, _ in stage_one],
            },
            "window_depth": {
                "roster": policy["window_depth_roster"],
                "candidates": per_quantity("window_depth", policy["window_depth_roster"], lambda v: v),
                "resolved": window_depth,
                "pinned_by": [c["control_id"] for c, _ in stage_one],
            },
            "sentinel": {
                "roster": ["0x%02X" % s for s in policy["sentinel_roster"]],
                "candidates": per_quantity("sentinel", policy["sentinel_roster"], lambda v: "0x%02X" % v),
                "resolved": "0x%02X" % sentinel,
                "pinned_by": [c["control_id"] for c, _ in stage_one],
            },
            "season_budget": {
                "roster": policy["budget_roster"],
                "candidates": budget_rows,
                "resolved": budget,
                "pinned_by": [c["control_id"] for c in (c_charged, c_overflow) if c],
            },
            "promotion_threshold": {
                "roster": policy["threshold_roster"],
                "candidates": threshold_rows,
                "resolved": threshold,
                "pinned_by": [c["control_id"] for c in tier_controls.values()],
            },
        },
        "joint_guard_search": {
            "space_size": len(joint),
            "dimensions": {
                "engine_tag": len(policy["engine_tag_roster"]),
                "window_depth": len(policy["window_depth_roster"]),
                "sentinel": len(policy["sentinel_roster"]),
            },
            "accepted": [j for j in joint if j["verdict"] == "accepted"],
            "readings": joint,
        },
        "attribution_settlement": {
            "hand_overs": hand,
            "cycles_resolved_to_first_sorting_key": True,
            "passes": passes,
            "control_passes": ctl_passes,
        },
        "bracket_search": {
            "eligibility_rule": "own effective owner and at least %d matches"
                                % policy["eligibility_match_floor"],
            "eligible_pool": bracket["pool"],
            "eligible_pool_size": bracket["pool_size"],
            "bracket_size": policy["bracket_size"],
            "candidate_space": bracket["candidate_space"],
            "constraints_total": bracket.get("constraints_total", 0),
            "best_satisfied_constraint_count": bracket["best_satisfied_constraints"],
            "elected": bracket["members"],
            "constraints": bracket.get("constraints", []),
            "tie_breaks": ["least combined charged upkeep", "member keys sort first"],
        },
        "docket_search": {
            "docket_size": policy["docket_size"],
            "conference_cap": policy["docket_conference_cap"],
            "candidate_space": docket["candidate_space"],
            "elected": docket.get("match_ids", []),
            "total_review_index": docket["total_review_index"],
            "conference_spread": docket["conference_spread"],
            "match_review_index": docket["match_review"],
        },
        "calibration": calibration,
        "calibration_agrees": all(row["agrees"] for row in calibration),
    }

    write_json(os.path.join(out_dir, "summary.json"), summary)
    write_json(os.path.join(out_dir, "data_quality.json"), data_quality)
    write_json(os.path.join(out_dir, "policy_audit.json"), audit)
    for conf in conferences:
        write_json(os.path.join(out_dir, "region_%s.json" % conf),
                   group_report("conference", conf, conf_members[conf], squads))
    for seg in segments:
        write_json(os.path.join(out_dir, "segment_%s.json" % seg),
                   group_report("segment", seg, seg_members[seg], squads))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
