#!/usr/bin/env python3
"""Settle a sealed tactics-league season from its replay journals.

Usage:  python3 derive_report.py DATA_DIR OUT_DIR

Everything the program needs is read from DATA_DIR (captures, policies, registry);
everything it produces is written to OUT_DIR.  No network, no installs, no state
outside the two directories handed to it, and nothing is hard coded: both
directories come from the arguments and every policy figure, roster and label is
parsed out of the written contract in DATA_DIR.

Pipeline
    1. read the written contract (policy markdown + csv tables)
    2. decode the reference capture (guards as declared in its own register) and
       build the roster dictionary from its wire attribution keys
    3. pin the unstated quantities against the published control figures,
       recording a verdict and a falsifier for every candidate on every roster
    4. decode the sealed season, reattribute to effective owners, score, tier, rank
    5. elect the finals bracket and the review docket
    6. write the nine reports
"""

import csv
import hashlib
import itertools
import json
import math
import os
import re
import struct
import sys
from collections import Counter, defaultdict

M32 = 1 << 32

GUARD_NAMES = ["keystream", "chain", "delta2", "driftkey", "segperm",
               "backref", "endian", "countbias", "shadow", "rotate",
               "alias", "escape"]

ROLES = {1: "reference", 2: "control", 3: "season"}

# The published control figures, in the order the calibration page fixes them.
CONTROL_FIGURES = [
    ("matches_opened", "C1 matches opened"),
    ("records_recovered", "C2 records recovered"),
    ("distinct_squads", "C3 distinct squads"),
    ("gross_output", "C4 gross output"),
    ("voided_output", "C5 voided output"),
    ("raw_upkeep", "C6 raw upkeep"),
    ("charged_upkeep", "C7 charged upkeep"),
    ("overflow_upkeep", "C8 overflow upkeep"),
    ("champion_entrants", "C9 champion entrants"),
    ("qualifier_entrants", "C10 qualifier entrants"),
    ("provisional_entrants", "C11 provisional entrants"),
]
FIGURE_LABEL = dict(CONTROL_FIGURES)


class DecodeError(Exception):
    """A capture cannot be read under the reading being tested.

    ``figure`` names the published control figure the failure contradicts.
    """

    def __init__(self, message, figure="C2 records recovered"):
        Exception.__init__(self, message)
        self.message = message
        self.figure = figure


# --------------------------------------------------------------------------
# 1. the written contract
# --------------------------------------------------------------------------

def slug(name):
    return re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")


def parse_policy_markdown(text):
    """Pull every ``Name: `value`[, `value`...]`` line out of a policy page."""
    scalars = {}
    rosters = {}
    for line in text.splitlines():
        m = re.match(r"^\s*([A-Z][^:`]{2,}?)\s*:\s*((?:`[^`]+`(?:,\s*)?)+)\s*$", line)
        if not m:
            continue
        name, body = m.group(1), m.group(2)
        values = [v.strip() for v in re.findall(r"`([^`]*)`", body)]
        if len(values) == 1:
            scalars[slug(name)] = values[0]
        else:
            rosters[slug(name)] = values
    return scalars, rosters


def parse_calibration(text):
    """Read the published control figures: ``- C1 matches opened: 5``."""
    out = {}
    for line in text.splitlines():
        m = re.match(r"^\s*[-*]\s*\*?([A-Z][0-9]+)\*?\s+(.+?)\s*:\s*(-?\d+)\s*$", line)
        if m:
            out[slug(m.group(2))] = int(m.group(3))
    return out


def read_text(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def read_csv(path):
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return [row for row in csv.DictReader(fh)]


def as_int(value, default=None):
    try:
        return int(str(value).strip(), 0)
    except (TypeError, ValueError):
        return default


def _choose(n, k):
    if k < 0 or k > n:
        return 0
    return math.comb(n, k)


class Contract(object):
    """Everything the league published in prose and tables."""

    def __init__(self, data_dir):
        names = sorted(n for n in os.listdir(data_dir) if n.endswith(".md"))
        self.scalars = {}
        self.rosters = {}
        self.controls = {}
        self.calibration_file = None
        wanted = len(CONTROL_FIGURES)
        for name in names:
            text = read_text(os.path.join(data_dir, name))
            s, r = parse_policy_markdown(text)
            self.scalars.update(s)
            self.rosters.update(r)
            figures = parse_calibration(text)
            if len(figures) >= wanted and self.calibration_file is None:
                self.controls = figures
                self.calibration_file = name

        self.divisions = read_csv(os.path.join(data_dir, "division-topology.csv"))
        self.licences = read_csv(os.path.join(data_dir, "licence-classes.csv"))
        self.registry = read_csv(os.path.join(data_dir, "entrant-registry.csv"))

        self.conference_of = {}
        self.ladder_weight_of = {}
        for row in self.divisions:
            self.conference_of[row["division"]] = row["conference"]
            self.ladder_weight_of[row["division"]] = as_int(row["ladder_weight"], 1)
        self.segment_of = {}
        self.levy_rate_of = {}
        for row in self.licences:
            self.segment_of[row["licence_class"]] = row["segment"]
            self.levy_rate_of[row["licence_class"]] = as_int(row["levy_rate"], 1)
        self.entrant = {}
        for row in self.registry:
            self.entrant[row["squad_key"]] = row

        self.conferences = sorted(set(self.conference_of.values()))
        self.segments = sorted(set(self.segment_of.values()))
        self.divisions_in_conference = defaultdict(list)
        for row in self.divisions:
            self.divisions_in_conference[row["conference"]].append(row["division"])
        self.licences_in_segment = defaultdict(list)
        for row in self.licences:
            self.licences_in_segment[row["segment"]].append(row["licence_class"])
        for group in self.divisions_in_conference.values():
            group.sort()
        for group in self.licences_in_segment.values():
            group.sort()

    # -- published figures -------------------------------------------------
    def scalar(self, key, default):
        return self.scalars.get(key, default)

    def roster(self, key):
        """A published roster, by either its bare name or its ``..._roster`` name."""
        for name in (key, slug(key) + "_roster"):
            if name in self.rosters:
                return list(self.rosters[name])
        return []

    def int_roster(self, key, fallback):
        values = [as_int(v) for v in self.roster(key)]
        values = [v for v in values if v is not None]
        return values or fallback

    # -- per squad lookups --------------------------------------------------
    def ladder_weight(self, squad):
        row = self.entrant.get(squad)
        return 1 if row is None else self.ladder_weight_of.get(row["division"], 1)

    def levy_rate(self, squad):
        row = self.entrant.get(squad)
        return 1 if row is None else self.levy_rate_of.get(row["licence_class"], 1)

    def conference(self, squad):
        row = self.entrant.get(squad)
        return None if row is None else self.conference_of.get(row["division"])

    def segment(self, squad):
        row = self.entrant.get(squad)
        return None if row is None else self.segment_of.get(row["licence_class"])

    def is_ranked(self, squad):
        row = self.entrant.get(squad)
        return bool(row) and str(row.get("is_ranked", "")).strip().lower() == "yes"

    def entrant_id(self, squad):
        return self.entrant.get(squad, {}).get("entrant_id")


# --------------------------------------------------------------------------
# 2. capture decoding
# --------------------------------------------------------------------------

def rot_right(blob, n):
    n %= len(blob)
    return blob[-n:] + blob[:-n] if n else blob


def keystream_for(tag, match_id, salt, ordinal, width):
    digest = hashlib.sha256(("%s:%d:%d:%d" % (tag, match_id, salt, ordinal)).encode()).digest()
    return bytes(digest[i % len(digest)] for i in range(width))


def segperm_step(nonce, n):
    step = ((nonce >> 8) % n) + 2
    while math.gcd(step, n) != 1:
        step += 1
    return step


def parse_envelope(data):
    if len(data) < 36 or data[0:4] != b"RJNL":
        raise DecodeError("not an RJNL capture", "C1 matches opened")
    register = data[6] | (data[7] << 8)
    guards = set(GUARD_NAMES[i] for i in range(12) if (register >> i) & 1)
    nonce = struct.unpack_from(">I" if "endian" in guards else "<I", data, 8)[0]
    (declared_segments, declared_records, base_tick,
     capture_id, declared_roster, checksum) = struct.unpack_from("<6I", data, 12)
    return dict(
        marker=data[0:4], version=data[4], reserved=data[5], guard_register=register,
        guards=sorted(guards), nonce=nonce, declared_segment_count=declared_segments,
        declared_record_count=declared_records, season_base_tick=base_tick,
        capture_id=capture_id, declared_roster_size=declared_roster,
        envelope_checksum=checksum, envelope_checksum_calc=sum(data[:32]) % M32,
        envelope_checksum_ok=checksum == (sum(data[:32]) % M32),
    )


def split_segments(data):
    """Segment envelopes + payloads, in storage order."""
    out = []
    off = 36
    while off + 28 <= len(data):
        if data[off:off + 4] != b"SEG0":
            raise DecodeError("bad segment marker at %d" % off, "C1 matches opened")
        slot, match_id, count, salt, width, checksum = struct.unpack_from("<6I", data, off + 4)
        payload = data[off + 28:off + 28 + width]
        if len(payload) != width:
            raise DecodeError("truncated payload at %d" % off)
        if sum(payload) % M32 != checksum:
            raise DecodeError("payload checksum mismatch at %d" % off)
        out.append(dict(slot=slot, match_id=match_id, declared_count=count, salt=salt,
                        width=width, checksum=checksum, payload=payload))
        off += 28 + width
    return out


def decode_capture(data, tag, window_depth, sentinel, roster_dict,
                   filler_uses_effective=False, drift_uses_decoded=True):
    """Decode one capture into an envelope dict carrying ``records`` in logical order.

    ``ks_first`` is fixed: the seal applies the keystream mask and then the left
    turn, so the reader removes the mask first and turns the blob back afterwards.
    That order, the filler width being taken from the *written* salt and the drift
    running sum being taken over the *decoded* value fields were all confirmed
    against the labelled transcript of the reference capture and against every
    published control figure.
    """
    env = parse_envelope(data)
    guards = set(env["guards"])
    bias = (sum(tag.encode()) % 11) if "countbias" in guards else 0
    env["count_bias"] = bias
    env["engine_tag"] = tag
    env["window_depth"] = window_depth
    env["sentinel"] = sentinel
    env["segment_count"] = env["declared_segment_count"] - bias
    env["record_count"] = env["declared_record_count"] - bias
    env["roster_size"] = env["declared_roster_size"] - bias

    raw = split_segments(data)
    for seg in raw:
        seg["count"] = seg["declared_count"] - bias
    env["segments_found"] = len(raw)
    if len(raw) != env["segment_count"]:
        raise DecodeError("declared %d segments, found %d"
                          % (env["segment_count"], len(raw)), "C1 matches opened")

    n = env["segment_count"]
    if "segperm" in guards and n > 0:
        step = segperm_step(env["nonce"], n)
        logical = [None] * n
        for i in range(n):
            logical[(env["nonce"] + i * step) % n] = raw[i]
        if any(s is None for s in logical):
            raise DecodeError("segment permutation does not cover")
        env["perm_step"] = step
    else:
        logical = list(raw)

    records = []
    segments = []
    window = []
    dictionary = list(roster_dict) if roster_dict is not None else None
    prev_tick = None
    running = 0
    ordinal = 0
    for position, seg in enumerate(logical):
        effective = (seg["salt"] ^ running) if "driftkey" in guards else seg["salt"]
        filler_salt = effective if filler_uses_effective else seg["salt"]
        filler_width = 3 + (filler_salt % 5)
        payload = seg["payload"]
        cursor = 0
        prev_tick_seg = 0
        prev_diff = 0
        outputs = []
        for index in range(seg["count"]):
            if cursor + 2 > len(payload):
                raise DecodeError("payload overrun in segment %d" % position)
            visible, shadow = payload[cursor], payload[cursor + 1]
            cursor += 2
            width = (shadow ^ (effective & 0xFF)) if "shadow" in guards else visible
            if width <= 0:
                raise DecodeError("zero width frame in segment %d" % position)
            blob = payload[cursor:cursor + width]
            cursor += width
            if len(blob) != width:
                raise DecodeError("short frame in segment %d" % position)

            if "keystream" in guards:
                blob = bytes(a ^ b for a, b in zip(
                    blob, keystream_for(tag, seg["match_id"], effective, index, width)))
            if "rotate" in guards:
                blob = rot_right(blob, (effective + ordinal) % width)

            directives = []
            head = 0
            if "escape" in guards:
                while (head + 1 < len(blob) and blob[head] == sentinel
                       and len(directives) < 2):
                    directives.append((blob[head + 1] >> 4, blob[head + 1] & 0xF))
                    head += 2
            body = blob[head:]
            if len(body) < 3:
                raise DecodeError("empty frame body in segment %d" % position)
            kind = body[0]
            mode = body[1]
            attr_width = 3 if mode == 0 else 1
            if len(body) != 2 + attr_width + 10:
                raise DecodeError("body width %d does not suit mode %d"
                                  % (len(body), mode))
            unit = int.from_bytes(body[2 + attr_width:4 + attr_width], "little")
            tick = int.from_bytes(body[4 + attr_width:8 + attr_width], "little")
            stored_value = int.from_bytes(body[8 + attr_width:12 + attr_width], "little")

            if "delta2" in guards:
                prev_diff = (prev_diff + tick) % M32
                prev_tick_seg = (prev_tick_seg + prev_diff) % M32
                tick = prev_tick_seg
            value = stored_value ^ ((env["nonce"] if prev_tick is None else prev_tick)
                                    if "chain" in guards else 0)

            if mode == 0:
                wire_key = body[2:5].decode("latin1")
            elif mode == 1:
                reference = body[2]
                if reference >= len(window):
                    raise DecodeError("back reference %d beyond window depth %d"
                                      % (reference, len(window)), "C3 distinct squads")
                wire_key = window[reference]
            elif mode == 2:
                if dictionary is None:
                    dictionary = []
                reference = body[2]
                if reference >= len(dictionary):
                    raise DecodeError("dictionary position %d beyond %d entries"
                                      % (reference, len(dictionary)), "C3 distinct squads")
                wire_key = dictionary[reference]
            else:
                raise DecodeError("unknown attribution mode %d" % mode,
                                  "C3 distinct squads")

            if dictionary is None:
                dictionary = [wire_key]
            elif roster_dict is None and wire_key not in dictionary:
                dictionary.append(wire_key)
            if wire_key in window:
                window.remove(wire_key)
            window.append(wire_key)
            if len(window) > window_depth:
                window.pop(0)
            prev_tick = tick

            key = wire_key
            for operation, operand in directives:
                if operation == 1:
                    if operand >= len(dictionary):
                        raise DecodeError("escape operand %d beyond dictionary"
                                          % operand, "C3 distinct squads")
                    key = dictionary[operand]
                elif operation == 2:
                    if key in dictionary:
                        key = dictionary[(dictionary.index(key) + operand)
                                         % len(dictionary)]
                else:
                    raise DecodeError("unknown escape operation %d" % operation,
                                      "C3 distinct squads")

            records.append(dict(
                kind=kind, unit=unit, tick=tick, value=value, stored_value=stored_value,
                wire_key=wire_key, squad_key=key, directives=list(directives),
                attribution_mode=mode, width=width,
                logical_segment=position, record_ordinal=ordinal,
                in_segment=index, match_id=seg["match_id"],
            ))
            outputs.append(value if drift_uses_decoded else stored_value)
            ordinal += 1

        leftover = len(payload) - cursor
        if leftover != filler_width:
            raise DecodeError("segment %d carries %d trailing bytes, expected %d"
                              % (position, leftover, filler_width))
        segments.append(dict(
            logical_position=position, slot=seg["slot"], match_id=seg["match_id"],
            declared_count=seg["declared_count"], count=seg["count"],
            salt=seg["salt"], effective_salt=effective, width=seg["width"],
            consumed=cursor, filler_bytes=leftover, filler_expected=filler_width,
        ))
        running = (running + sum(outputs)) % M32

    env["records"] = records
    env["segments"] = segments
    env["records_recovered"] = len(records)
    env["segments_recovered"] = len(segments)
    env["distinct_squads"] = len(set(r["squad_key"] for r in records))
    env["distinct_wire_keys"] = len(set(r["wire_key"] for r in records))
    env["wire_keys"] = sorted(set(r["wire_key"] for r in records))
    env["attributed_keys"] = sorted(set(r["squad_key"] for r in records))
    return env


def decode_reference(data, contract):
    """Decode the capture that carries the base envelope and build the dictionary.

    The reference capture ships with its stream guards idle, so its reading does
    not depend on any unstated quantity.  Should a corpus ever seal it with the
    guards engaged, fall back to searching the published rosters for a reading
    that decodes.
    """
    tries = [(None, 4, 0x1A)]
    tries += [(t, d, s) for t in (contract.roster("engine_tag") or [])
              for d in contract.int_roster("window_depth", [4])
              for s in contract.int_roster("sentinel", [0x1A])]
    env = None
    error = None
    for tag, depth, sentinel in tries:
        try:
            env = decode_capture(data, tag or "orion-fold", depth, sentinel, None)
            if tag is None:
                break
        except DecodeError as exc:
            error = error or exc
    if env is None:
        raise SystemExit("cannot decode the reference capture: %s" % error)
    return env


def build_roster_dictionary(env):
    """Distinct wire attribution keys of the reference capture, first met first."""
    dictionary = []
    seen = set()
    for record in env["records"]:
        key = record["wire_key"]
        if key not in seen:
            seen.add(key)
            dictionary.append(key)
    return dictionary


# --------------------------------------------------------------------------
# 3. attribution settlement
# --------------------------------------------------------------------------

def handover_map(records, dictionary):
    """Kind 6 events; the last one read in logical order is the one that stands."""
    hands = {}
    for record in records:
        if record["kind"] == 6:
            hands[record["squad_key"]] = dictionary[record["value"] % len(dictionary)]
    return hands


def resolve_owners(hands, universe):
    """Follow hand overs to their end; a closed set settles on its least key."""
    resolved = {}
    cycles = []
    for start in universe:
        if start in resolved:
            continue
        path = []
        index = {}
        current = start
        while True:
            if current in resolved:
                for node in path:
                    resolved[node] = resolved[current]
                break
            if current in index:
                loop = path[index[current]:]
                root = min(loop)
                cycles.append(sorted(loop))
                for node in path:
                    resolved[node] = root
                break
            index[current] = len(path)
            path.append(current)
            nxt = hands.get(current)
            if nxt is None or nxt == current:
                for node in path:
                    resolved[node] = current
                break
            current = nxt
    for node in list(resolved):
        target = resolved[node]
        while resolved.get(target, target) != target:
            target = resolved[target]
        resolved[node] = target
    return resolved, cycles


def collapsed_follow(hands, universe):
    """One sweep step: what the current owner of a squad hands to.

    A closed set has already settled on its least key, so every member of it
    hands to that key and the sweeps converge instead of turning forever.
    """
    resolved, cycles = resolve_owners(hands, universe)
    settled_by_cycle = {}
    for cycle in cycles:
        root = min(cycle)
        for node in cycle:
            settled_by_cycle[node] = root
    follow = {}
    for node in universe:
        follow[node] = settled_by_cycle.get(node, hands.get(node, node))
    return follow, resolved, cycles


def sweep_settlement(hands, universe):
    """Repeated sweeps until one changes nothing; the idle sweep is counted."""
    follow, resolved, cycles = collapsed_follow(hands, universe)
    step = lambda x: follow.get(x, hands.get(x, x))
    owner = dict((node, node) for node in universe)
    passes = 0
    while True:
        passes += 1
        nxt = dict((node, step(owner[node])) for node in universe)
        if nxt == owner:
            return passes, owner, resolved, cycles
        owner = nxt


# --------------------------------------------------------------------------
# 4. scoring
# --------------------------------------------------------------------------

def settle(records, contract, budget, threshold, dictionary, reattribute=True):
    """Score every match and the season for every effective owner."""
    hands = handover_map(records, dictionary) if reattribute else {}
    universe = set(r["squad_key"] for r in records) | set(hands) | set(hands.values())
    passes, _, owners, cycles = (sweep_settlement(hands, universe)
                                 if reattribute else (0, {}, dict(), []))
    owner_of = (lambda key: owners.get(key, key)) if reattribute else (lambda key: key)

    by_segment = defaultdict(list)
    for record in records:
        by_segment[record["logical_segment"]].append(record)

    matches = []
    for position in sorted(by_segment):
        seg = by_segment[position]
        match = dict(index=position, match_id=seg[0]["match_id"], close_tick=None,
                     concede={}, deploy={}, elim={}, disband={}, outputs=[],
                     participants=set(), escape_records=0, records=len(seg))
        for record in seg:
            owner = owner_of(record["squad_key"])
            match["participants"].add(owner)
            if record["directives"]:
                match["escape_records"] += 1
            kind = record["kind"]
            if kind == 1:
                match["deploy"][(owner, record["unit"])] = (record["tick"], record["value"])
            elif kind == 2:
                match["outputs"].append((owner, record["unit"],
                                         record["tick"], record["value"]))
            elif kind == 3:
                match["elim"][(owner, record["unit"])] = record["tick"]
            elif kind == 4:
                match["disband"][(owner, record["unit"])] = record["tick"]
            elif kind == 5:
                match["concede"][owner] = record["tick"]
            elif kind == 8:
                match["close_tick"] = record["tick"] if match["close_tick"] is None \
                    else max(match["close_tick"], record["tick"])
        matches.append(match)

    per_match = []
    for match in matches:
        table = {}
        for squad in sorted(match["participants"]):
            gross = 0
            voided = 0
            for owner, unit, tick, value in match["outputs"]:
                if owner != squad:
                    continue
                withdrawn = ((squad in match["concede"] and tick >= match["concede"][squad])
                             or ((squad, unit) in match["elim"]
                                 and tick >= match["elim"][(squad, unit)]))
                if withdrawn:
                    voided += value
                else:
                    gross += value
            raw = 0
            for (owner, unit), (start, rate) in match["deploy"].items():
                if owner != squad:
                    continue
                stop = match["disband"].get((owner, unit), match["close_tick"])
                raw += rate * (stop - start)
            table[squad] = dict(gross_output=gross, voided_output=voided, raw_upkeep=raw)
        per_match.append(dict(match=match, squads=table))

    # one season budget per squad, consumed match by match in logical order
    remaining = defaultdict(lambda: budget)
    for entry in per_match:
        for squad, figures in entry["squads"].items():
            take = min(remaining[squad], figures["raw_upkeep"])
            figures["charged_upkeep"] = take
            figures["overflow_upkeep"] = figures["raw_upkeep"] - take
            remaining[squad] -= take

    for entry in per_match:
        for squad, figures in entry["squads"].items():
            figures["match_score"] = (figures["gross_output"] * contract.ladder_weight(squad)
                                      - figures["charged_upkeep"] * contract.levy_rate(squad))
        pool = [s for s in entry["squads"] if s not in entry["match"]["concede"]]
        best = max((entry["squads"][s]["match_score"] for s in pool), default=None)
        leaders = [s for s in pool if entry["squads"][s]["match_score"] == best] if pool else []
        for squad, figures in entry["squads"].items():
            if squad in entry["match"]["concede"]:
                figures["award"], figures["outright_win"] = 0, False
            elif len(leaders) == 1 and squad == leaders[0]:
                figures["award"], figures["outright_win"] = 3, True
            elif squad in leaders:
                figures["award"], figures["outright_win"] = 1, False
            else:
                figures["award"], figures["outright_win"] = 0, False

    squads = {}
    for entry in per_match:
        for squad, figures in entry["squads"].items():
            row = squads.get(squad)
            if row is None:
                row = dict(squad_key=squad, gross_output=0, voided_output=0, raw_upkeep=0,
                           charged_upkeep=0, overflow_upkeep=0, season_score=0,
                           match_awards=0, streak_credit=0, standing_points=0,
                           matches=0, results=[])
                squads[squad] = row
            row["gross_output"] += figures["gross_output"]
            row["voided_output"] += figures["voided_output"]
            row["raw_upkeep"] += figures["raw_upkeep"]
            row["charged_upkeep"] += figures["charged_upkeep"]
            row["overflow_upkeep"] += figures["overflow_upkeep"]
            row["season_score"] += figures["match_score"]
            row["match_awards"] += figures["award"]
            row["matches"] += 1
            row["results"].append(figures["outright_win"])

    for squad, row in squads.items():
        run = 0
        credit = 0
        for won in row["results"]:
            run = run + 1 if won else 0
            if run > 1:
                credit += 2
        del row["results"]
        row["streak_credit"] = credit
        row["standing_points"] = row["match_awards"] + credit
        score = row["season_score"]
        row["tier"] = ("champion" if score >= 3 * threshold
                       else "contender" if score >= 2 * threshold
                       else "qualifier" if score >= threshold
                       else "provisional")

    return dict(matches=per_match, squads=squads, hands=hands, owners=owners,
                cycles=cycles, passes=passes, owner_of=owner_of, universe=universe)


# --------------------------------------------------------------------------
# 5. calibration against the published control figures
# --------------------------------------------------------------------------

def control_reading(capture, tag, depth, sentinel, dictionary, contract, budget, threshold):
    """Decode the control slice and reproduce every published figure from it."""
    env = decode_capture(capture, tag, depth, sentinel, dictionary)
    rows = settle(env["records"], contract, budget, threshold, dictionary,
                  reattribute=False)["squads"]
    tiers = Counter(row["tier"] for row in rows.values())
    return dict(
        env=env,
        matches_opened=env["segments_recovered"],
        records_recovered=env["records_recovered"],
        distinct_squads=env["distinct_squads"],
        gross_output=sum(r["gross_output"] for r in rows.values()),
        voided_output=sum(r["voided_output"] for r in rows.values()),
        raw_upkeep=sum(r["raw_upkeep"] for r in rows.values()),
        charged_upkeep=sum(r["charged_upkeep"] for r in rows.values()),
        overflow_upkeep=sum(r["overflow_upkeep"] for r in rows.values()),
        champion_entrants=tiers.get("champion", 0),
        qualifier_entrants=tiers.get("qualifier", 0),
        provisional_entrants=tiers.get("provisional", 0),
    )


def falsifier_for(exc):
    label = FIGURE_LABEL.get(exc.figure)
    if label:
        return "%s (decode failed: %s)" % (label, exc.message)
    if re.match(r"^C\d+", exc.figure):
        return "%s (decode failed: %s)" % (exc.figure, exc.message)
    return "decode failed: %s" % exc.message


def calibrate(contract, control, dictionary):
    """Search every published roster and record a verdict for every candidate."""
    tags = contract.roster("engine_tag") or ["orion-fold"]
    depths = contract.int_roster("window_depth", [4])
    sentinels = contract.int_roster("sentinel", [0x1A])
    budgets = contract.int_roster("budget_cap", [0])
    thresholds = contract.int_roster("promotion_threshold", [0])
    controls = contract.controls

    stage_one = ["matches_opened", "records_recovered", "distinct_squads",
                 "gross_output", "voided_output", "raw_upkeep"]

    grid = []
    survivors = []
    for tag in tags:
        for depth in depths:
            for sentinel in sentinels:
                entry = dict(engine_tag=tag, window_depth=depth, sentinel=sentinel)
                try:
                    reading = control_reading(control, tag, depth, sentinel, dictionary,
                                              contract, budgets[0], thresholds[0])
                    for key in stage_one:
                        entry[key] = reading[key]
                    bad = None
                    for key in stage_one:
                        if controls.get(key) != reading[key]:
                            bad = key
                            break
                    if bad is None:
                        entry["verdict"], entry["falsifier"] = "survives", None
                        survivors.append((tag, depth, sentinel))
                    else:
                        entry["verdict"] = "rejected"
                        entry["falsifier"] = FIGURE_LABEL[bad]
                except DecodeError as exc:
                    entry["verdict"] = "rejected"
                    entry["falsifier"] = falsifier_for(exc)
                grid.append(entry)

    if not survivors:
        raise SystemExit("no reading of the published rosters reproduces the control figures")

    audit = dict(
        method=("every candidate reading decodes the control slice, settles it without "
                "effective-owner reattribution (the control slice is held out of the "
                "settlement) and is compared figure by figure with the published "
                "controls; the first figure that disagrees is the falsifier. The "
                "seal order was confirmed as keystream mask then left turn, filler "
                "width taken from the written segment salt, drift running sum taken "
                "over the decoded value fields."),
        control_capture_role="control",
        unstated_quantities=[],
    )

    quantities = [("engine_tag", tags, "engine_tag"), ("window_depth", depths, "window_depth"),
                  ("sentinel", sentinels, "sentinel")]
    for name, roster, field in quantities:
        elected = sorted(set(s[2] if name == "sentinel" else
                             s[0] if name == "engine_tag" else s[1] for s in survivors))
        show = (lambda v: hex(v)) if name == "sentinel" else (lambda v: v)
        candidates = []
        for value in roster:
            alive = any(s[0] == value for s in survivors) if name == "engine_tag" \
                else any(s[1] == value for s in survivors) if name == "window_depth" \
                else any(s[2] == value for s in survivors)
            if alive:
                witness = next(g for g in grid if g["verdict"] == "survives"
                               and g[field] == value)
            else:
                # falsify against the reading that holds every other quantity elected
                base = dict(tag=survivors[0][0], depth=survivors[0][1],
                            sentinel=survivors[0][2])
                probe = dict(engine_tag=base["tag"], window_depth=base["depth"],
                             sentinel=base["sentinel"])
                probe[field] = value
                witness = next((g for g in grid if g["engine_tag"] == probe["engine_tag"]
                                and g["window_depth"] == probe["window_depth"]
                                and g["sentinel"] == probe["sentinel"]), None)
                if witness is None:
                    witness = next(g for g in grid if g[field] == value)
            candidates.append(dict(
                value=show(value),
                verdict="survives" if alive else "rejected",
                falsifier=None if alive else witness["falsifier"],
                reproduced=dict((FIGURE_LABEL[k], witness[k]) for k in stage_one
                                if k in witness),
            ))
        audit["unstated_quantities"].append(dict(
            name=name, roster=[show(v) for v in roster], candidate_space=len(roster),
            pinned_by="C1-C6", surviving=[show(v) for v in elected],
            elected=show(elected[0]) if len(elected) == 1 else [show(v) for v in elected],
            candidates=candidates,
        ))

    tag, depth, sentinel = survivors[0]
    audit["elected_guards"] = dict(engine_tag=tag, window_depth=depth, sentinel=sentinel)
    if len(survivors) > 1:
        audit["guard_candidate_ambiguity"] = [
            dict(engine_tag=t, window_depth=d, sentinel=hex(s)) for t, d, s in survivors]

    # -- the season budget --------------------------------------------------
    budget_keys = ["charged_upkeep", "overflow_upkeep"]
    budget_entries = []
    winner = None
    for value in budgets:
        reading = control_reading(control, tag, depth, sentinel, dictionary,
                                  contract, value, thresholds[0])
        entry = dict(value=value, charged_upkeep=reading["charged_upkeep"],
                     overflow_upkeep=reading["overflow_upkeep"])
        bad = next((k for k in budget_keys if controls.get(k) != reading[k]), None)
        if bad is None:
            entry["verdict"], entry["falsifier"] = "survives", None
            winner = value
        else:
            entry["verdict"] = "rejected"
            entry["falsifier"] = FIGURE_LABEL[bad]
        budget_entries.append(entry)
    if winner is None:
        raise SystemExit("no figure on the budget cap roster reproduces C7/C8")
    audit["unstated_quantities"].append(dict(
        name="season_budget", roster=budgets, candidate_space=len(budgets),
        pinned_by="C7-C8", elected=winner, candidates=budget_entries,
    ))

    # -- the promotion threshold -------------------------------------------
    tier_keys = ["champion_entrants", "qualifier_entrants", "provisional_entrants"]
    tier_entries = []
    tier_winner = None
    for value in thresholds:
        reading = control_reading(control, tag, depth, sentinel, dictionary,
                                  contract, winner, value)
        entry = dict(value=value, champion_entrants=reading["champion_entrants"],
                     qualifier_entrants=reading["qualifier_entrants"],
                     provisional_entrants=reading["provisional_entrants"])
        bad = next((k for k in tier_keys if controls.get(k) != reading[k]), None)
        if bad is None:
            entry["verdict"], entry["falsifier"] = "survives", None
            tier_winner = value
        else:
            entry["verdict"] = "rejected"
            entry["falsifier"] = FIGURE_LABEL[bad]
        tier_entries.append(entry)
    if tier_winner is None:
        raise SystemExit("no figure on the promotion threshold roster reproduces C9-C11")
    audit["unstated_quantities"].append(dict(
        name="promotion_threshold", roster=thresholds, candidate_space=len(thresholds),
        pinned_by="C9-C11", elected=tier_winner, candidates=tier_entries,
    ))

    final = control_reading(control, tag, depth, sentinel, dictionary,
                            contract, winner, tier_winner)
    table = [dict(figure=label, published=controls.get(key), reproduced=final[key],
                  agrees=controls.get(key) == final[key])
             for key, label in CONTROL_FIGURES if key in controls]
    audit["calibration_comparison"] = table
    audit["all_control_figures_agree"] = all(row["agrees"] for row in table)

    return dict(tag=tag, depth=depth, sentinel=sentinel, budget=winner,
                threshold=tier_winner, audit=audit, control_env=final["env"])


# --------------------------------------------------------------------------
# 6. the finals bracket and the review docket
# --------------------------------------------------------------------------

BRACKET_CONSTRAINTS = [
    "at least one member sits in a division whose ladder weight reaches the prominence figure",
    "both entrant segments appear among the members",
    "every member holds at least the standing points floor",
    "no two members share a conference",
    "at least one member is a ranked entrant",
    "the ladder weights of the members sum to at least the combined ladder weight floor",
    "no member carries any overflow upkeep",
    "at least one member holds the champion tier",
    "the gross output of the members sums to at least the combined gross output floor",
    "no member has handed its entry to another squad",
]


def elect_bracket(settled, contract):
    """Exactly three squads, own effective owner, at or above the match floor."""
    size = as_int(contract.scalar("bracket_size", None), 3)
    floor = as_int(contract.scalar("eligibility_match_floor", None), 2)
    prominence = as_int(contract.scalar("ladder_weight_prominence", None), 4)
    points_floor = as_int(contract.scalar("standing_points_floor", None), 3)
    weight_floor = as_int(contract.scalar("combined_ladder_weight_floor", None), 7)
    output_floor = as_int(contract.scalar("combined_gross_output_floor", None), 900)

    rows = settled["squads"]
    handed = set(settled["hands"])
    pool = sorted(k for k, row in rows.items()
                  if settled["owner_of"](k) == k and row["matches"] >= floor)
    checked = 0
    best = None
    for combo in itertools.combinations(pool, size):
        checked += 1
        met = [
            any(contract.ladder_weight(k) >= prominence for k in combo),
            len(set(filter(None, (contract.segment(k) for k in combo))))
            == len(contract.segments),
            all(rows[k]["standing_points"] >= points_floor for k in combo),
            len(set(filter(None, (contract.conference(k) for k in combo)))) == len(combo),
            any(contract.is_ranked(k) for k in combo),
            sum(contract.ladder_weight(k) for k in combo) >= weight_floor,
            all(rows[k]["overflow_upkeep"] == 0 for k in combo),
            any(rows[k]["tier"] == "champion" for k in combo),
            sum(rows[k]["gross_output"] for k in combo) >= output_floor,
            not any(k in handed for k in combo),
        ]
        # most constraints met, then least charged upkeep, then keys that sort first
        candidate = (sum(met), -sum(rows[k]["charged_upkeep"] for k in combo))
        if best is None:
            best = (candidate, combo, met)
        elif candidate > best[0]:
            best = (candidate, combo, met)
        elif candidate == best[0] and list(combo) < list(best[1]):
            best = (candidate, combo, met)
    if best is None:
        return dict(members=[], satisfied=0, constraints=[], candidate_space=checked,
                    pool_size=len(pool), pool=pool, eligible_pool=pool)
    _, combo, met = best
    return dict(
        members=list(combo),
        satisfied=sum(met),
        constraints=[dict(text=text, met=bool(was))
                     for text, was in zip(BRACKET_CONSTRAINTS, met)],
        total_charged_upkeep=sum(rows[k]["charged_upkeep"] for k in combo),
        total_gross_output=sum(rows[k]["gross_output"] for k in combo),
        total_ladder_weight=sum(contract.ladder_weight(k) for k in combo),
        candidate_space=checked,
        pool_size=len(pool),
        eligible_pool=pool,
    )


def elect_docket(settled, contract):
    """Exactly the docket size in matches, cap per conference, largest review index."""
    size = as_int(contract.scalar("docket_size", None), 5)
    cap = as_int(contract.scalar("docket_conference_cap", None), 2)
    weight = as_int(contract.scalar("review_weight", None), 25)

    entries = []
    for entry in settled["matches"]:
        match = entry["match"]
        gross_by_squad = dict((s, f["gross_output"]) for s, f in entry["squads"].items()
                              if s in contract.entrant)
        best_squad = None
        if gross_by_squad:
            top = max(gross_by_squad.values())
            best_squad = sorted(s for s, v in gross_by_squad.items() if v == top)[0]
        voided = sum(f["voided_output"] for f in entry["squads"].values())
        entries.append(dict(
            match_id=match["match_id"], position=match["index"],
            conference=contract.conference(best_squad) if best_squad else None,
            top_squad=best_squad, voided_output=voided,
            escape_records=match["escape_records"],
            review_index=voided + weight * match["escape_records"],
        ))
    entries.sort(key=lambda e: e["match_id"])

    # The cap bites per conference and the objective is a plain sum, so no docket
    # ever needs more than the cap strongest matches of any one conference.  Keep
    # the full enumeration whenever it is cheap, and thin the pool that way when
    # a long season would make it expensive.
    pool = entries
    exact = True
    if len(entries) > size and _choose(len(entries), size) > 2000000:
        best_per_conference = defaultdict(list)
        for entry in entries:
            best_per_conference[entry["conference"]].append(entry)
        pool = []
        for group in best_per_conference.values():
            group.sort(key=lambda e: (-e["review_index"], e["match_id"]))
            pool.extend(group[:cap])
        pool.sort(key=lambda e: e["match_id"])
        exact = False

    best = None
    for combo in itertools.combinations(pool, size):
        if any(v > cap for v in Counter(e["conference"] for e in combo).values()):
            continue
        candidate = (sum(e["review_index"] for e in combo),
                     [-e["match_id"] for e in combo])
        if best is None or candidate > best[0]:
            best = (candidate, combo)
    if best is None:
        return dict(matches=[], total_review_index=0, by_conference={}, entries=[],
                    candidate_space=0, enumeration_exact=exact, all_matches=entries)

    combo = best[1]
    return dict(
        matches=[e["match_id"] for e in combo],
        total_review_index=sum(e["review_index"] for e in combo),
        by_conference=dict((str(k), v) for k, v in
                           sorted(Counter(e["conference"] for e in combo).items(),
                                  key=lambda kv: str(kv[0]))),
        entries=[dict(match_id=e["match_id"], conference=e["conference"],
                      top_squad=e["top_squad"], voided_output=e["voided_output"],
                      escape_records=e["escape_records"], review_index=e["review_index"])
                 for e in combo],
        enumeration_exact=exact,
        all_matches=[dict(match_id=e["match_id"], conference=e["conference"],
                          review_index=e["review_index"]) for e in entries],
    )


# --------------------------------------------------------------------------
# 7. reports
# --------------------------------------------------------------------------

def rank_ladder(settled, contract):
    order = sorted(settled["squads"].values(),
                   key=lambda r: (-r["season_score"], -r["standing_points"],
                                  -r["gross_output"], r["squad_key"]))
    for i, row in enumerate(order):
        row["rank"] = i + 1
    return order


def group_report(kind, name, members, subdivisions, settled, contract):
    rows = settled["squads"]
    entries = []
    for squad in sorted(members, key=lambda s: (contract.entrant_id(s) or "zzz" + s, s)):
        row = rows[squad]
        reg = contract.entrant.get(squad, {})
        entries.append(dict(
            charged_upkeep=row["charged_upkeep"],
            conference=contract.conference(squad),
            division=reg.get("division"),
            entrant_id=reg.get("entrant_id"),
            gross_output=row["gross_output"],
            licence_class=reg.get("licence_class"),
            matches=row["matches"],
            overflow_upkeep=row["overflow_upkeep"],
            season_score=row["season_score"],
            segment=contract.segment(squad),
            squad_key=squad,
            standing_points=row["standing_points"],
            streak_credit=row["streak_credit"],
            tier=row["tier"],
            voided_output=row["voided_output"],
        ))
    tiers = Counter(e["tier"] for e in entries)
    report = dict(
        charged_upkeep=sum(e["charged_upkeep"] for e in entries),
        entrants=len(entries),
        entries=entries,
        gross_output=sum(e["gross_output"] for e in entries),
        group_kind=kind,
        group_name=name,
        overflow_upkeep=sum(e["overflow_upkeep"] for e in entries),
        season_score=sum(e["season_score"] for e in entries),
        standing_points=sum(e["standing_points"] for e in entries),
        tier_counts=dict((t, tiers.get(t, 0)) for t in
                         ("champion", "contender", "qualifier", "provisional")),
        voided_output=sum(e["voided_output"] for e in entries),
    )
    if kind == "conference":
        report["divisions"] = sorted(subdivisions)
    else:
        report["licence_classes"] = sorted(subdivisions)
    return report


def capture_reconciliation(role, name, env, contract):
    wire = set(env.get("wire_keys", []))
    attributed = set(env.get("attributed_keys", []))
    registered_wire = wire & set(contract.entrant)
    return dict(
        role=role, file=name, capture_id=env["capture_id"],
        guard_register=env["guard_register"], guards=env["guards"],
        envelope_checksum_ok=env["envelope_checksum_ok"],
        count_bias=env["count_bias"],
        declared_segment_count=env["declared_segment_count"],
        effective_declared_segment_count=env["segment_count"],
        segments_recovered=env.get("segments_recovered"),
        segment_count_residual=env["segment_count"] - env.get("segments_recovered", 0),
        declared_record_count=env["declared_record_count"],
        effective_declared_record_count=env["record_count"],
        records_recovered=env.get("records_recovered"),
        record_count_residual=env["record_count"] - env.get("records_recovered", 0),
        declared_roster_size=env["declared_roster_size"],
        effective_declared_roster_size=env["roster_size"],
        distinct_squads_recovered=env.get("distinct_squads"),
        distinct_wire_keys_recovered=env.get("distinct_wire_keys"),
        registered_squads_recovered=len(registered_wire),
        roster_size_residual=env["roster_size"] - len(registered_wire),
        squads_never_registered=sorted(wire - set(contract.entrant)),
        squads_seen_only_by_directive=sorted(attributed - wire),
        absent_entrants=sorted(r["squad_key"] for r in contract.registry
                               if r["squad_key"] not in wire),
    )


def write_json(path, payload):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")


def main(argv):
    if len(argv) != 3:
        sys.stderr.write("usage: %s DATA_DIR OUT_DIR\n" % argv[0])
        return 2
    data_dir, out_dir = argv[1], argv[2]
    if not os.path.isdir(data_dir):
        raise SystemExit("data directory %s does not exist" % data_dir)
    os.makedirs(out_dir, exist_ok=True)
    contract = Contract(data_dir)

    captures = {}
    for name in sorted(os.listdir(data_dir)):
        if not name.endswith(".rjnl"):
            continue
        with open(os.path.join(data_dir, name), "rb") as fh:
            data = fh.read()
        role = ROLES.get(parse_envelope(data)["capture_id"] % 10)
        if role:
            captures[role] = (name, data)
    missing = [r for r in ("reference", "control", "season") if r not in captures]
    if missing:
        raise SystemExit("missing capture roles: %s" % ", ".join(missing))

    # -- the reference capture carries the base envelope and the dictionary --
    ref_name, ref_data = captures["reference"]
    ref_env = decode_reference(ref_data, contract)
    dictionary = build_roster_dictionary(ref_env)

    # -- pin the unstated quantities ---------------------------------------
    pinned = calibrate(contract, captures["control"][1], dictionary)

    # -- decode and settle the season --------------------------------------
    season_name, season_data = captures["season"]
    season_env = decode_capture(season_data, pinned["tag"], pinned["depth"],
                                pinned["sentinel"], dictionary)
    settled = settle(season_env["records"], contract, pinned["budget"],
                     pinned["threshold"], dictionary)
    ladder = rank_ladder(settled, contract)
    bracket = elect_bracket(settled, contract)
    docket = elect_docket(settled, contract)

    rows = settled["squads"]
    totals = dict(
        gross_output=sum(r["gross_output"] for r in rows.values()),
        voided_output=sum(r["voided_output"] for r in rows.values()),
        raw_upkeep=sum(r["raw_upkeep"] for r in rows.values()),
        charged_upkeep=sum(r["charged_upkeep"] for r in rows.values()),
        overflow_upkeep=sum(r["overflow_upkeep"] for r in rows.values()),
        season_score=sum(r["season_score"] for r in rows.values()),
        standing_points=sum(r["standing_points"] for r in rows.values()),
    )

    # -- conference and segment reports ------------------------------------
    conference_members = defaultdict(list)
    segment_members = defaultdict(list)
    unregistered = sorted(k for k in rows if k not in contract.entrant)
    for squad in sorted(rows):
        if squad not in contract.entrant:
            continue
        conference_members[contract.conference(squad)].append(squad)
        segment_members[contract.segment(squad)].append(squad)

    reports = {}
    for conference in contract.conferences:
        reports["region_%s.json" % conference] = group_report(
            "conference", conference, conference_members.get(conference, []),
            contract.divisions_in_conference.get(conference, []), settled, contract)
    for segment in contract.segments:
        reports["segment_%s.json" % segment] = group_report(
            "segment", segment, segment_members.get(segment, []),
            contract.licences_in_segment.get(segment, []), settled, contract)

    # -- data quality -------------------------------------------------------
    total_output = sum(r["value"] for r in season_env["records"] if r["kind"] == 2)
    backwards = 0
    by_segment = defaultdict(list)
    for record in season_env["records"]:
        by_segment[record["logical_segment"]].append(record)
    for seg in by_segment.values():
        for a, b in zip(seg, seg[1:]):
            if b["tick"] < a["tick"]:
                backwards += 1
    kind_names = {1: "deploy", 2: "output", 3: "eliminate", 4: "disband",
                  5: "concede", 6: "transfer", 8: "close"}
    ledger = Counter(kind_names.get(r["kind"], "kind_%d" % r["kind"])
                     for r in season_env["records"])
    season_wire = set(season_env["wire_keys"])

    data_quality = dict(
        season=dict(
            file=season_name, capture_id=season_env["capture_id"],
            guard_register=season_env["guard_register"], guards=season_env["guards"],
            envelope_checksum_ok=season_env["envelope_checksum_ok"],
            count_bias=season_env["count_bias"],
            declared_segment_count=season_env["declared_segment_count"],
            effective_declared_segment_count=season_env["segment_count"],
            segments_recovered=season_env["segments_recovered"],
            segment_count_residual=season_env["segment_count"]
            - season_env["segments_recovered"],
            declared_record_count=season_env["declared_record_count"],
            effective_declared_record_count=season_env["record_count"],
            records_recovered=season_env["records_recovered"],
            record_count_residual=season_env["record_count"]
            - season_env["records_recovered"],
            declared_roster_size=season_env["declared_roster_size"],
            effective_declared_roster_size=season_env["roster_size"],
            distinct_squads_recovered=season_env["distinct_squads"],
            distinct_wire_keys_recovered=season_env["distinct_wire_keys"],
            registered_squads_recovered=len(season_wire & set(contract.entrant)),
            roster_size_residual=season_env["roster_size"]
            - len(season_wire & set(contract.entrant)),
            squads_never_registered=unregistered,
            squads_seen_only_by_directive=sorted(
                set(season_env["attributed_keys"]) - season_wire),
            absent_entrants=sorted(r["squad_key"] for r in contract.registry
                                   if r["squad_key"] not in season_wire),
            attribution_residual=totals["gross_output"]
            - sum(reports["region_%s.json" % c]["gross_output"]
                  for c in contract.conferences),
            output_ledger_residual=total_output
            - (totals["gross_output"] + totals["voided_output"]),
            upkeep_ledger_residual=totals["raw_upkeep"]
            - (totals["charged_upkeep"] + totals["overflow_upkeep"]),
            backwards_tick_transitions=backwards,
            kind_ledger=dict((n, ledger.get(n, 0)) for n in
                             ("deploy", "output", "eliminate", "disband",
                              "concede", "transfer", "close")),
            handovers=dict(sorted(settled["hands"].items())),
        ),
        captures=[capture_reconciliation(role, name,
                                          ref_env if role == "reference"
                                          else decode_capture(data, pinned["tag"],
                                                              pinned["depth"],
                                                              pinned["sentinel"],
                                                              dictionary),
                                          contract)
                  for role, (name, data) in sorted(captures.items())],
    )

    # -- policy audit -------------------------------------------------------
    audit = pinned["audit"]
    audit["attribution_settlement_passes"] = settled["passes"]
    audit["attribution"] = dict(
        handovers=sorted(settled["hands"].items()),
        closed_sets=[sorted(c) for c in settled["cycles"]],
        effective_owners=sorted(settled["owners"].items()),
        squads_that_own_their_entry=sorted(k for k in rows
                                           if settled["owner_of"](k) == k),
        settlement_passes=settled["passes"],
    )
    audit["bracket_candidate_space"] = bracket["candidate_space"]
    audit["bracket_best_satisfied_constraints"] = bracket["satisfied"]
    audit["bracket"] = bracket
    audit["docket"] = docket

    # -- summary ------------------------------------------------------------
    summary = dict(
        season_capture=season_name,
        capture_id=season_env["capture_id"],
        guard_register=season_env["guard_register"],
        guards=season_env["guards"],
        records_recovered=season_env["records_recovered"],
        matches=season_env["segments_recovered"],
        squads_on_the_wire=season_env["distinct_wire_keys"],
        effective_owners=len(rows),
        unregistered_squads=unregistered,
        absent_entrants=data_quality["season"]["absent_entrants"],
        policy=dict(
            engine_tag=pinned["tag"], window_depth=pinned["depth"],
            sentinel=pinned["sentinel"], season_budget=pinned["budget"],
            promotion_threshold=pinned["threshold"],
            count_bias=season_env["count_bias"],
            control_figures_all_agree=audit["all_control_figures_agree"],
        ),
        totals=totals,
        tier_counts=dict((t, sum(1 for r in rows.values() if r["tier"] == t))
                         for t in ("champion", "contender", "qualifier", "provisional")),
        attribution_settlement_passes=settled["passes"],
        finals_bracket=dict(
            members=bracket["members"],
            satisfied_constraints=bracket["satisfied"],
            total_charged_upkeep=bracket["total_charged_upkeep"],
            total_gross_output=bracket["total_gross_output"],
            total_ladder_weight=bracket["total_ladder_weight"],
        ),
        review_docket=dict(
            matches=docket["matches"],
            by_conference=docket["by_conference"],
            total_review_index=docket["total_review_index"],
        ),
        ladder=[dict(rank=r["rank"], squad_key=r["squad_key"],
                     entrant_id=contract.entrant_id(r["squad_key"]),
                     conference=contract.conference(r["squad_key"]),
                     division=contract.entrant.get(r["squad_key"], {}).get("division"),
                     segment=contract.segment(r["squad_key"]),
                     registered=r["squad_key"] in contract.entrant,
                     matches=r["matches"], season_score=r["season_score"],
                     standing_points=r["standing_points"],
                     streak_credit=r["streak_credit"], tier=r["tier"],
                     gross_output=r["gross_output"],
                     voided_output=r["voided_output"],
                     charged_upkeep=r["charged_upkeep"],
                     overflow_upkeep=r["overflow_upkeep"]) for r in ladder],
        roster_dictionary=dictionary,
    )

    write_json(os.path.join(out_dir, "summary.json"), summary)
    write_json(os.path.join(out_dir, "data_quality.json"), data_quality)
    write_json(os.path.join(out_dir, "policy_audit.json"), audit)
    for name, payload in sorted(reports.items()):
        write_json(os.path.join(out_dir, name), payload)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
