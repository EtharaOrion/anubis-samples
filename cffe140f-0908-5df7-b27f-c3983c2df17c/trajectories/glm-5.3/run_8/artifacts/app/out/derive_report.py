#!/usr/bin/env python3
"""Settle a sealed tactics-league season from its replay journals.

Usage:  python3 derive_report.py DATA_DIR OUT_DIR

Everything is derived from the material in DATA_DIR: the journal envelope and
guard contracts, the published policy rosters, the labelled calibration
controls, and the sealed captures (reference / control / season, told apart by
the capture identifier in the envelope, never by file name).
"""

import collections
import csv
import hashlib
import itertools
import json
import os
import re
import struct
import sys
from math import gcd

MASK32 = 1 << 32
MAX_SWEEPS = 4096

# Guard register bit assignments (replay-guards.md, in the published bit order).
GUARD_BITS = [
    (0, "keystream"),
    (1, "chain"),
    (2, "delta2"),
    (3, "driftkey"),
    (4, "segperm"),
    (5, "backref"),
    (6, "endian"),
    (7, "countbias"),
    (8, "shadow"),
    (9, "rotate"),
    (10, "alias"),
    (11, "escape"),
]

EVENT_KINDS = {1: "deploy", 2: "output", 3: "eliminate", 4: "disband",
               5: "concede", 6: "transfer", 8: "close"}
ATTRIBUTION_WIDTH = {0: 3, 1: 1, 2: 1}
LEDGER_ORDER = ["deploy", "output", "eliminate", "disband", "concede",
                "transfer", "close"]
TIER_ORDER = ["champion", "contender", "qualifier", "provisional"]


class DecodeError(Exception):
    """A candidate reading of the guards cannot decode a capture."""


# --------------------------------------------------------------------------
# document loading
# --------------------------------------------------------------------------

def read_text(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def read_csv_rows(path):
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def backticked(text):
    return re.findall(r"`([^`]*)`", text)


def find_roster(lines, label):
    """The backticked items of the first line that begins with `label`."""
    prefix = label.lower()
    for line in lines:
        if line.strip().lower().startswith(prefix):
            return backticked(line)
    raise KeyError("published roster not found: %s" % label)


def parse_int(item):
    item = item.strip()
    return int(item, 16) if item.lower().startswith("0x") else int(item, 10)


def load_documents(data_dir):
    names = sorted(os.listdir(data_dir))
    docs = {"dir": data_dir, "capture_files": {}}
    for name in names:
        if name.endswith(".rjnl"):
            docs["capture_files"][name] = os.path.join(data_dir, name)

    def md(name):
        return read_text(os.path.join(data_dir, name)).splitlines()

    standings = md("standings-policy.md")
    upkeep = md("upkeep-policy.md")
    guards = md("replay-guards.md")
    bracket = md("bracket-policy.md")

    docs["threshold_roster"] = [parse_int(v) for v in
                                find_roster(standings, "Promotion threshold roster")]
    docs["budget_roster"] = [parse_int(v) for v in
                             find_roster(upkeep, "Budget cap roster")]
    docs["engine_tag_roster"] = list(find_roster(guards, "Engine tag roster"))
    docs["window_depth_roster"] = [parse_int(v) for v in
                                   find_roster(guards, "Window depth roster")]
    docs["sentinel_roster"] = [parse_int(v) for v in
                               find_roster(guards, "Sentinel roster")]

    for key, label in [
        ("eligibility_floor", "Eligibility match floor"),
        ("prominence", "Ladder weight prominence"),
        ("standing_floor", "Standing points floor"),
        ("combined_weight_floor", "Combined ladder weight floor"),
        ("combined_gross_floor", "Combined gross output floor"),
        ("review_weight", "Review weight"),
        ("docket_size", "Docket size"),
        ("docket_cap", "Docket conference cap"),
    ]:
        docs[key] = parse_int(find_roster(bracket, label)[0])

    controls = []
    for line in md("calibration-controls.md"):
        match = re.match(r"\s*-\s*(C\d+)\s+(.+?)\s*:\s*(\d+)\s*$", line)
        if match:
            controls.append({"id": match.group(1), "figure": match.group(2),
                             "published": int(match.group(3))})
    docs["controls"] = controls
    docs["control_values"] = {c["id"]: c["published"] for c in controls}

    docs["registry"] = read_csv_rows(os.path.join(data_dir, "entrant-registry.csv"))
    docs["topology"] = read_csv_rows(os.path.join(data_dir, "division-topology.csv"))
    docs["licences"] = read_csv_rows(os.path.join(data_dir, "licence-classes.csv"))

    docs["registry_by_key"] = {r["squad_key"]: r for r in docs["registry"]}
    docs["topology_by_division"] = {r["division"]: r for r in docs["topology"]}
    docs["licence_by_class"] = {r["licence_class"]: r for r in docs["licences"]}
    docs["conferences"] = list(dict.fromkeys(r["conference"] for r in docs["topology"]))
    docs["segments"] = list(dict.fromkeys(r["segment"] for r in docs["licences"]))
    return docs


# --------------------------------------------------------------------------
# envelope and segment framing
# --------------------------------------------------------------------------

def guard_register(envelope):
    return struct.unpack_from("<H", envelope, 6)[0]


def engine_tag_bias(tag):
    return sum(ord(ch) for ch in tag) % 11


def permutation_step(nonce, nseg):
    """Smallest integer >= ((nonce >> 8) % nseg) + 2 sharing no factor with nseg."""
    step = ((nonce >> 8) % nseg) + 2
    while gcd(step, nseg) != 1:
        step += 1
    return step


def read_envelope(data, engine_tag):
    if len(data) < 36:
        raise DecodeError("capture is shorter than an envelope")
    if data[0:4] != b"RJNL":
        raise DecodeError("bad capture marker %r" % data[0:4])
    active = {name for bit, name in GUARD_BITS if (guard_register(data) >> bit) & 1}
    nonce = struct.unpack_from(">I" if "endian" in active else "<I", data, 8)[0]
    bias = engine_tag_bias(engine_tag) if "countbias" in active else 0
    env = {
        "envelope_version": data[4],
        "reserved": data[5],
        "guard_register": guard_register(data),
        "active_guards": active,
        "nonce": nonce,
        "count_bias": bias,
        "declared_segments": struct.unpack_from("<I", data, 12)[0] - bias,
        "declared_records": struct.unpack_from("<I", data, 16)[0] - bias,
        "base_tick": struct.unpack_from("<I", data, 20)[0],
        "capture_id": struct.unpack_from("<I", data, 24)[0],
        "declared_roster": struct.unpack_from("<I", data, 28)[0] - bias,
        "declared_checksum": struct.unpack_from("<I", data, 32)[0],
    }
    env["computed_checksum"] = sum(data[:32]) % MASK32
    env["checksum_agrees"] = env["declared_checksum"] == env["computed_checksum"]

    segments = []
    off = 36
    while off < len(data):
        if data[off:off + 4] != b"SEG0":
            raise DecodeError("bad segment marker at offset %d" % off)
        width = struct.unpack_from("<I", data, off + 20)[0]
        if off + 28 + width > len(data):
            raise DecodeError("truncated segment payload at offset %d" % off)
        segments.append({
            "storage_slot": struct.unpack_from("<I", data, off + 4)[0],
            "match_id": struct.unpack_from("<I", data, off + 8)[0],
            "declared_records": struct.unpack_from("<I", data, off + 12)[0] - bias,
            "written_salt": struct.unpack_from("<I", data, off + 16)[0],
            "payload_width": width,
            "declared_payload_checksum": struct.unpack_from("<I", data, off + 24)[0],
            "payload": data[off + 28:off + 28 + width],
        })
        off += 28 + width
    if not segments:
        raise DecodeError("no segments in capture")

    if "segperm" in active:
        nseg = len(segments)
        step = permutation_step(nonce, nseg)
        env["permutation_step"] = step
        by_slot = {seg["storage_slot"]: seg for seg in segments}
        if sorted(by_slot) != list(range(nseg)):
            raise DecodeError("storage slots do not form a permutation")
        ordered = [None] * nseg
        for slot in range(nseg):
            ordered[(nonce + slot * step) % nseg] = by_slot[slot]
        segments = ordered

    env["segments"] = segments
    env["payload_checksums_agree"] = all(
        sum(seg["payload"]) % MASK32 == seg["declared_payload_checksum"]
        for seg in segments)
    return env


# --------------------------------------------------------------------------
# record decoding
# --------------------------------------------------------------------------

def decode_capture(data, engine_tag, window_depth, sentinel, dictionary):
    """Decode a capture into its records, in logical order."""
    env = read_envelope(data, engine_tag)
    active = env["active_guards"]
    records = []
    recent = []           # rolling back-reference window, least recently seen first
    drift = 0             # running sum of value fields over logically earlier segments
    previous_tick = None  # chain mask source
    diagnostics = {"filler_mismatches": 0, "backward_ticks": 0}

    for logical_pos, seg in enumerate(env["segments"]):
        salt = ((seg["written_salt"] ^ drift) % MASK32) if "driftkey" in active \
            else seg["written_salt"]
        payload = seg["payload"]
        limit = len(payload)
        pos = 0
        segment_value_sum = 0
        first_difference = 0
        running_tick = 0

        for index in range(seg["declared_records"]):
            where = "segment %d record %d" % (logical_pos, index)
            if pos + 2 > limit:
                raise DecodeError("%s overruns the payload" % where)
            visible = payload[pos]
            shadow = payload[pos + 1]
            pos += 2
            width = ((shadow ^ (salt & 0xFF)) & 0xFF) if "shadow" in active else visible
            if width == 0:
                raise DecodeError("%s declares a zero width" % where)
            if pos + width > limit:
                raise DecodeError("%s overruns the payload" % where)
            blob = payload[pos:pos + width]
            pos += width

            if "keystream" in active:
                material = "%s:%d:%d:%d" % (engine_tag, seg["match_id"], salt, index)
                stream = hashlib.sha256(material.encode("utf-8")).digest()
                if width > len(stream):
                    raise DecodeError("%s is wider than the keystream" % where)
                blob = bytes(b ^ stream[i] for i, b in enumerate(blob))

            if "rotate" in active:
                turn = (salt + len(records)) % width
                blob = bytes(blob[(i - turn) % width] for i in range(width))

            cursor = 0
            directives = []
            if "escape" in active:
                for _ in range(2):
                    if cursor + 1 < len(blob) and blob[cursor] == sentinel:
                        directive = blob[cursor + 1]
                        directives.append((directive >> 4, directive & 0x0F))
                        cursor += 2
                    else:
                        break

            if cursor + 2 > len(blob):
                raise DecodeError("%s carries no body" % where)
            kind = blob[cursor]
            mode = blob[cursor + 1]
            cursor += 2
            if kind not in EVENT_KINDS:
                raise DecodeError("%s carries unknown event kind %d" % (where, kind))
            if mode not in ATTRIBUTION_WIDTH:
                raise DecodeError("%s carries unknown attribution mode %d" % (where, mode))
            width_of_field = ATTRIBUTION_WIDTH[mode]
            if cursor + width_of_field > len(blob):
                raise DecodeError("%s carries a short attribution field" % where)
            field = blob[cursor:cursor + width_of_field]
            cursor += width_of_field
            if mode == 0:
                try:
                    wire_key = field.decode("ascii")
                except UnicodeDecodeError:
                    raise DecodeError("%s carries a non ascii squad key" % where)
            elif mode == 1:
                position = field[0]
                if position >= len(recent):
                    raise DecodeError("%s back reference %d exceeds the window of %d"
                                      % (where, position, len(recent)))
                wire_key = recent[position]
            else:
                position = field[0]
                if position >= len(dictionary):
                    raise DecodeError("%s alias position %d exceeds the roster "
                                      "dictionary of %d" % (where, position, len(dictionary)))
                wire_key = dictionary[position]

            if cursor + 10 > len(blob):
                raise DecodeError("%s carries a truncated tail" % where)
            unit = struct.unpack_from("<H", blob, cursor)[0]
            cursor += 2
            stored_tick = struct.unpack_from("<I", blob, cursor)[0]
            cursor += 4
            stored_value = struct.unpack_from("<I", blob, cursor)[0]
            cursor += 4

            if "delta2" in active:
                difference = (stored_tick + first_difference) % MASK32
                tick = (running_tick + difference) % MASK32
                if difference > tick:
                    diagnostics["backward_ticks"] += 1
                first_difference = difference
                running_tick = tick
            else:
                tick = stored_tick
                if records and records[-1]["segment"] == logical_pos \
                        and tick < records[-1]["tick"]:
                    diagnostics["backward_ticks"] += 1

            if "chain" in active:
                mask = env["nonce"] if previous_tick is None else previous_tick
                value = (stored_value ^ mask) % MASK32
            else:
                value = stored_value

            # Directive pairs are weighed back to front: the pair nearest the body
            # is applied first, so an absolute redirect is then advanced by any
            # further operand. This is the reading the published controls and the
            # worked conference example both pin down.
            effective_key = wire_key
            for operation, operand in reversed(directives):
                if operation == 1:
                    if operand >= len(dictionary):
                        raise DecodeError("%s escape redirects past the roster "
                                          "dictionary" % where)
                    effective_key = dictionary[operand]
                elif operation == 2:
                    if effective_key in dictionary:
                        position = dictionary.index(effective_key)
                        effective_key = dictionary[(position + operand) % len(dictionary)]
                else:
                    raise DecodeError("%s carries escape operation %d" % (where, operation))

            records.append({
                "kind": kind, "kind_name": EVENT_KINDS[kind], "mode": mode,
                "wire_key": wire_key, "key": effective_key, "unit": unit,
                "tick": tick, "value": value, "segment": logical_pos,
                "index_in_segment": index, "match_id": seg["match_id"],
                "directives": directives,
            })
            segment_value_sum = (segment_value_sum + value) % MASK32

            if wire_key in recent:
                recent.remove(wire_key)
            recent.append(wire_key)
            if len(recent) > window_depth:
                recent.pop(0)
            previous_tick = tick

        drift = (drift + segment_value_sum) % MASK32
        # The filler width is keyed to the salt as written in the segment envelope, not to
        # the drift key effective salt: every segment of every shipped capture matches the
        # written salt exactly, while the effective salt breaks the check in later segments.
        if pos + 3 + (seg["written_salt"] % 5) != len(payload):
            diagnostics["filler_mismatches"] += 1

    env["records"] = records
    env["diagnostics"] = diagnostics
    return env


def build_dictionary(reference_env):
    seen = []
    for record in reference_env["records"]:
        if record["wire_key"] not in seen:
            seen.append(record["wire_key"])
    return seen


# --------------------------------------------------------------------------
# match settlement
# --------------------------------------------------------------------------

def settle_matches(env):
    """One match per segment, in logical order."""
    by_segment = collections.defaultdict(list)
    for record in env["records"]:
        by_segment[record["segment"]].append(record)

    matches = []
    for logical_pos, seg in enumerate(env["segments"]):
        records = by_segment.get(logical_pos, [])
        raw = {}
        closing_tick = None
        highest_tick = None
        escape_records = 0
        for record in records:
            if record["directives"]:
                escape_records += 1
            squad = raw.setdefault(record["key"], {
                "deploys": [], "eliminations": {}, "disbands": {},
                "concede_tick": None, "outputs": []})
            kind = record["kind"]
            if kind == 1:
                squad["deploys"].append(record)
            elif kind == 2:
                squad["outputs"].append(record)
            elif kind == 3:
                unit, tick = record["unit"], record["tick"]
                if unit not in squad["eliminations"] or tick < squad["eliminations"][unit]:
                    squad["eliminations"][unit] = tick
            elif kind == 4:
                unit, tick = record["unit"], record["tick"]
                if unit not in squad["disbands"] or tick < squad["disbands"][unit]:
                    squad["disbands"][unit] = tick
            elif kind == 5:
                tick = record["tick"]
                if squad["concede_tick"] is None or tick < squad["concede_tick"]:
                    squad["concede_tick"] = tick
            elif kind == 8 and closing_tick is None:
                closing_tick = record["tick"]
            if highest_tick is None or record["tick"] > highest_tick:
                highest_tick = record["tick"]
        if closing_tick is None:
            closing_tick = highest_tick if highest_tick is not None else 0

        stats = {}
        for key, squad in raw.items():
            gross = 0
            voided = 0
            concede = squad["concede_tick"]
            for record in squad["outputs"]:
                withdrawn = False
                if concede is not None and record["tick"] >= concede:
                    withdrawn = True
                unit = record["unit"]
                if unit in squad["eliminations"] \
                        and record["tick"] >= squad["eliminations"][unit]:
                    withdrawn = True
                if withdrawn:
                    voided += record["value"]
                else:
                    gross += record["value"]
            upkeep = 0
            for record in squad["deploys"]:
                start = record["tick"]
                unit = record["unit"]
                end = squad["disbands"].get(unit)
                if end is not None and end < start:
                    end = None
                if end is None:
                    end = closing_tick
                if end > start:
                    upkeep += record["value"] * (end - start)
            stats[key] = {"gross": gross, "voided": voided, "raw": upkeep,
                          "conceded": concede is not None}
        matches.append({
            "segment": logical_pos, "match_id": seg["match_id"], "squads": stats,
            "closing_tick": closing_tick, "escape_records": escape_records,
            "record_count": len(records),
        })
    return matches


def ladder_weight(docs, key):
    entry = docs["registry_by_key"].get(key)
    if entry is None:
        return 1
    division = docs["topology_by_division"].get(entry["division"])
    return int(division["ladder_weight"]) if division else 1


def levy_rate(docs, key):
    entry = docs["registry_by_key"].get(key)
    if entry is None:
        return 1
    licence = docs["licence_by_class"].get(entry["licence_class"])
    return int(licence["levy_rate"]) if licence else 1


def tier_of(score, threshold):
    if threshold is None:
        return "provisional"
    if score >= 3 * threshold:
        return "champion"
    if score >= 2 * threshold:
        return "contender"
    if score >= threshold:
        return "qualifier"
    return "provisional"


def settle_season(env, matches, docs, budget, threshold):
    """Score every squad, award standing points and streak credit, assign tiers."""
    squads = {}
    for key in sorted({record["key"] for record in env["records"]}):
        squads[key] = {"key": key, "gross": 0, "voided": 0, "raw": 0, "matches": 0,
                       "budget_left": budget, "charged": 0, "overflow": 0,
                       "appearances": [], "awards": [], "outright": []}

    for match in matches:
        scores = {}
        for key, stat in sorted(match["squads"].items()):
            squad = squads[key]
            squad["gross"] += stat["gross"]
            squad["voided"] += stat["voided"]
            squad["raw"] += stat["raw"]
            squad["matches"] += 1
            charged = min(squad["budget_left"], stat["raw"])
            overflow = stat["raw"] - charged
            squad["budget_left"] -= charged
            squad["charged"] += charged
            squad["overflow"] += overflow
            scores[key] = stat["gross"] * ladder_weight(docs, key) - charged * levy_rate(docs, key)
            squad["appearances"].append({
                "match_id": match["match_id"], "segment": match["segment"],
                "gross": stat["gross"], "voided": stat["voided"], "raw": stat["raw"],
                "charged": charged, "overflow": overflow, "score": scores[key],
                "conceded": stat["conceded"]})
        match["scores"] = scores
        contenders = [k for k in scores if not match["squads"][k]["conceded"]]
        winners = []
        if contenders:
            best = max(scores[k] for k in contenders)
            winners = [k for k in contenders if scores[k] == best]
        match["winners"] = winners
        match["outright_winner"] = winners[0] if len(winners) == 1 else None
        for key in scores:
            squad = squads[key]
            if match["squads"][key]["conceded"] or key not in winners:
                squad["awards"].append(0)
                squad["outright"].append(False)
            else:
                squad["awards"].append(3 if len(winners) == 1 else 1)
                squad["outright"].append(len(winners) == 1)

    for key, squad in squads.items():
        squad["season_score"] = sum(a["score"] for a in squad["appearances"])
        squad["match_awards"] = sum(squad["awards"])
        credit = 0
        run = 0
        for won in squad["outright"]:
            if won:
                run += 1
                if run > 1:
                    credit += 2
            else:
                run = 0
        squad["streak_credit"] = credit
        squad["standing_points"] = squad["match_awards"] + credit
        squad["tier"] = tier_of(squad["season_score"], threshold)
        squad["ladder_weight"] = ladder_weight(docs, key)
        squad["levy_rate"] = levy_rate(docs, key)
        entry = docs["registry_by_key"].get(key)
        if entry:
            division = docs["topology_by_division"].get(entry["division"], {})
            licence = docs["licence_by_class"].get(entry["licence_class"], {})
            squad.update({"entrant_id": entry["entrant_id"], "division": entry["division"],
                          "conference": division.get("conference"),
                          "licence_class": entry["licence_class"],
                          "segment": licence.get("segment"),
                          "is_ranked": entry.get("is_ranked", "").lower() == "yes",
                          "registered": True})
        else:
            squad.update({"entrant_id": None, "division": None, "conference": None,
                          "licence_class": None, "segment": None, "is_ranked": False,
                          "registered": False})
    return squads


# --------------------------------------------------------------------------
# effective ownership
# --------------------------------------------------------------------------

def handover_targets(env, dictionary):
    """Kind 6 events map a squad to the dictionary position it hands its entry to."""
    positions = {}
    for record in env["records"]:
        if record["kind"] == 6:
            positions[record["key"]] = record["value"] % len(dictionary) if dictionary else 0
    if not dictionary:
        return {}
    return {key: dictionary[position] for key, position in positions.items()}


def settle_ownership(env, dictionary):
    """Follow entry hand overs to their end by repeated sweeps."""
    handed_to = handover_targets(env, dictionary)
    keys = sorted({record["key"] for record in env["records"]} | set(handed_to))

    # A closed set of hand overs settles on the member whose key sorts first.
    closed_sets = []
    resolved = dict(handed_to)
    for start in keys:
        trail = []
        seen_at = {}
        node = start
        while node is not None and node in handed_to and node not in seen_at:
            seen_at[node] = len(trail)
            trail.append(node)
            node = handed_to[node]
        if node is not None and node in seen_at:
            closed = sorted(trail[seen_at[node]:])
            if closed not in closed_sets:
                closed_sets.append(closed)
            settled = min(closed)
            for member in closed:
                resolved[member] = settled
            resolved[settled] = settled

    owner = {key: key for key in keys}
    passes = 0
    while True:
        passes += 1
        updated = {key: resolved.get(owner[key], owner[key]) for key in keys}
        if updated == owner:
            break
        owner = updated
        if passes > MAX_SWEEPS:
            break

    return {"owners": owner, "passes": passes, "handovers": handed_to,
            "closed_sets": closed_sets, "keys": keys}


# --------------------------------------------------------------------------
# reports
# --------------------------------------------------------------------------

def entry_view(squad):
    return {
        "charged_upkeep": squad["charged"],
        "conference": squad["conference"],
        "division": squad["division"],
        "entrant_id": squad["entrant_id"],
        "gross_output": squad["gross"],
        "licence_class": squad["licence_class"],
        "matches": squad["matches"],
        "overflow_upkeep": squad["overflow"],
        "season_score": squad["season_score"],
        "segment": squad["segment"],
        "squad_key": squad["key"],
        "standing_points": squad["standing_points"],
        "streak_credit": squad["streak_credit"],
        "tier": squad["tier"],
        "voided_output": squad["voided"],
    }


def group_report(kind, name, squads, docs):
    entries = sorted((entry_view(s) for s in squads), key=lambda e: e["squad_key"])
    return {
        "charged_upkeep": sum(e["charged_upkeep"] for e in entries),
        "divisions": sorted({e["division"] for e in entries if e["division"]}),
        "entrants": len(entries),
        "entries": entries,
        "gross_output": sum(e["gross_output"] for e in entries),
        "group_kind": kind,
        "group_name": name,
        "overflow_upkeep": sum(e["overflow_upkeep"] for e in entries),
        "season_score": sum(e["season_score"] for e in entries),
        "standing_points": sum(e["standing_points"] for e in entries),
        "tier_counts": {t: sum(1 for e in entries if e["tier"] == t) for t in TIER_ORDER},
        "voided_output": sum(e["voided_output"] for e in entries),
    }


BRACKET_CONSTRAINTS = [
    ("prominence", "at least one member sits in a division whose ladder weight "
                   "reaches the prominence figure"),
    ("segments", "both entrant segments appear among the members"),
    ("standing", "every member holds at least the standing points floor"),
    ("conferences", "no two members share a conference"),
    ("ranked", "at least one member is a ranked entrant"),
    ("weight_sum", "the ladder weights of the members sum to at least the combined "
                   "ladder weight floor"),
    ("no_overflow", "no member carries any overflow upkeep"),
    ("champion", "at least one member holds the champion tier"),
    ("gross_sum", "the gross output of the members sums to at least the combined "
                  "gross output floor"),
    ("no_handover", "no member has handed its entry to another squad"),
]


def bracket_checks(members, docs, handovers):
    member_segments = {m["segment"] for m in members if m["segment"]}
    conferences = [m["conference"] for m in members if m["conference"]]
    return {
        "prominence": any(m["ladder_weight"] >= docs["prominence"] for m in members),
        "segments": all(segment in member_segments for segment in docs["segments"]),
        "standing": all(m["standing_points"] >= docs["standing_floor"] for m in members),
        "conferences": len(conferences) == len(set(conferences)),
        "ranked": any(m["is_ranked"] for m in members),
        "weight_sum": sum(m["ladder_weight"] for m in members) >= docs["combined_weight_floor"],
        "no_overflow": all(m["overflow"] == 0 for m in members),
        "champion": any(m["tier"] == "champion" for m in members),
        "gross_sum": sum(m["gross"] for m in members) >= docs["combined_gross_floor"],
        "no_handover": all(m["key"] not in handovers for m in members),
    }


def bracket_member_view(m):
    return {
        "squad_key": m["key"], "entrant_id": m["entrant_id"], "conference": m["conference"],
        "division": m["division"], "segment": m["segment"], "tier": m["tier"],
        "is_ranked": m["is_ranked"], "ladder_weight": m["ladder_weight"],
        "matches": m["matches"], "gross_output": m["gross"],
        "voided_output": m["voided"], "charged_upkeep": m["charged"],
        "overflow_upkeep": m["overflow"], "season_score": m["season_score"],
        "standing_points": m["standing_points"], "streak_credit": m["streak_credit"],
    }


def elect_bracket(squads, docs, ownership):
    pool = [squads[key] for key in sorted(squads)
            if ownership["owners"].get(key, key) == key
            and squads[key]["matches"] >= docs["eligibility_floor"]]
    space = list(itertools.combinations(pool, 3))
    best = None
    for combo in space:
        members = sorted(combo, key=lambda s: s["key"])
        checks = bracket_checks(members, docs, ownership["handovers"])
        satisfied = sum(1 for value in checks.values() if value)
        charged = sum(m["charged"] for m in members)
        key = (-satisfied, charged, tuple(m["key"] for m in members))
        if best is None or key < best[0]:
            best = (key, members, checks, satisfied)
    _, members, checks, satisfied = best
    return {
        "members": [bracket_member_view(m) for m in members],
        "constraints": [{"id": cid, "requirement": text, "satisfied": checks[cid]}
                        for cid, text in BRACKET_CONSTRAINTS],
        "satisfied_constraint_count": satisfied,
        "total_charged_upkeep": sum(m["charged"] for m in members),
        "total_gross_output": sum(m["gross"] for m in members),
        "total_season_score": sum(m["season_score"] for m in members),
        "combined_ladder_weight": sum(m["ladder_weight"] for m in members),
        "eligibility_floor": docs["eligibility_floor"],
        "eligible_pool_size": len(pool),
        "candidate_space_size": len(space),
    }


def match_conference(match, squads):
    """Conference of the registered squad reporting the most gross output."""
    best_key = None
    best_gross = None
    for key in sorted(match["squads"]):
        if not squads[key]["registered"]:
            continue
        gross = match["squads"][key]["gross"]
        if best_gross is None or gross > best_gross:
            best_gross = gross
            best_key = key
    if best_key is None:
        return None, None
    return squads[best_key]["conference"], best_key


def elect_docket(matches, squads, docs):
    weight = docs["review_weight"]
    size = docs["docket_size"]
    cap = docs["docket_cap"]
    decorated = []
    for match in matches:
        conference, decider = match_conference(match, squads)
        voided = sum(stat["voided"] for stat in match["squads"].values())
        decorated.append({"match_id": match["match_id"], "segment": match["segment"],
                          "conference": conference, "deciding_squad": decider,
                          "voided_output": voided,
                          "escape_directive_records": match["escape_records"],
                          "review_index": voided + weight * match["escape_records"]})

    by_conference = collections.defaultdict(list)
    pool = []
    for match in decorated:
        if match["conference"] is None:
            pool.append(match)
        else:
            by_conference[match["conference"]].append(match)
    allowed = 0
    for conference in sorted(by_conference):
        ordered = sorted(by_conference[conference],
                         key=lambda m: (-m["review_index"], m["match_id"]))
        allowed += min(cap, len(ordered))
        pool.extend(ordered[:cap])

    take = min(size, len(pool))
    best = None
    for combo in itertools.combinations(sorted(pool, key=lambda m: m["match_id"]), take):
        counts = collections.Counter(m["conference"] for m in combo if m["conference"])
        if any(value > cap for value in counts.values()):
            continue
        total = sum(m["review_index"] for m in combo)
        ids = tuple(m["match_id"] for m in combo)
        if best is None or total > best[0] or (total == best[0] and ids < best[1]):
            best = (total, ids, combo)
    total, ids, combo = best
    return {
        "matches": [{"match_id": m["match_id"], "segment": m["segment"],
                     "conference": m["conference"],
                     "deciding_squad": m["deciding_squad"],
                     "voided_output": m["voided_output"],
                     "escape_directive_records": m["escape_directive_records"],
                     "review_index": m["review_index"]} for m in combo],
        "total_review_index": total,
        "conference_spread": dict(sorted(collections.Counter(
            m["conference"] for m in combo if m["conference"]).items())),
        "docket_size": size,
        "conference_cap": cap,
        "review_weight": weight,
        "candidate_space_size": allowed,
    }


# --------------------------------------------------------------------------
# calibration search
# --------------------------------------------------------------------------

CONTROL_TARGETS = [
    ("C1", "matches opened", "matches"),
    ("C2", "records recovered", "records"),
    ("C3", "distinct squads", "squads"),
    ("C4", "gross output", "gross"),
    ("C5", "voided output", "voided"),
    ("C6", "raw upkeep", "raw"),
    ("C7", "charged upkeep", "charged"),
    ("C8", "overflow upkeep", "overflow"),
    ("C9", "champion entrants", "champion"),
    ("C10", "qualifier entrants", "qualifier"),
    ("C11", "provisional entrants", "provisional"),
]


def capture_figures(env, matches, squads):
    return {
        "matches": len(matches),
        "records": len(env["records"]),
        "squads": len(squads),
        "gross": sum(s["gross"] for s in squads.values()),
        "voided": sum(s["voided"] for s in squads.values()),
        "raw": sum(s["raw"] for s in squads.values()),
        "charged": sum(s["charged"] for s in squads.values()),
        "overflow": sum(s["overflow"] for s in squads.values()),
        "champion": sum(1 for s in squads.values() if s["tier"] == "champion"),
        "contender": sum(1 for s in squads.values() if s["tier"] == "contender"),
        "qualifier": sum(1 for s in squads.values() if s["tier"] == "qualifier"),
        "provisional": sum(1 for s in squads.values() if s["tier"] == "provisional"),
    }


def first_control_failure(figures, published, targets):
    for cid, label, field in targets:
        if cid not in published:
            continue
        if figures[field] != published[cid]:
            return "%s %s (published %d, reproduced %d)" % (
                cid, label, published[cid], figures[field])
    return None


def resolve_parameters(docs, reference_bytes, control_bytes):
    """Pin the unstated quantities against the published control figures."""
    published = docs["control_values"]
    readings = []
    survivors = []
    for tag in docs["engine_tag_roster"]:
        for depth in docs["window_depth_roster"]:
            for sentinel in docs["sentinel_roster"]:
                reading = {"engine_tag": tag, "window_depth": depth,
                           "sentinel": sentinel,
                           "candidate": "%s / window %d / sentinel 0x%02X"
                                        % (tag, depth, sentinel)}
                try:
                    reference_env = decode_capture(reference_bytes, tag, depth,
                                                   sentinel, [])
                    dictionary = build_dictionary(reference_env)
                    env = decode_capture(control_bytes, tag, depth, sentinel, dictionary)
                    matches = settle_matches(env)
                    squads = settle_season(env, matches, docs, budget=0, threshold=None)
                    figures = capture_figures(env, matches, squads)
                    failure = first_control_failure(figures, published,
                                                    CONTROL_TARGETS[:6])
                except DecodeError as exc:
                    reading["verdict"] = "rejected"
                    reading["falsifier"] = "the control capture does not decode under " \
                                           "this reading: %s" % exc
                    readings.append(reading)
                    continue
                if failure:
                    reading["verdict"] = "rejected"
                    reading["falsifier"] = failure
                else:
                    reading["verdict"] = "accepted"
                    reading["falsifier"] = None
                    survivors.append({"tag": tag, "depth": depth, "sentinel": sentinel,
                                      "dictionary": dictionary, "env": env,
                                      "matches": matches, "reading": reading})
                readings.append(reading)
    if not survivors:
        raise SystemExit("no reading of the published guard rosters reproduces the "
                         "control figures")

    winner = survivors[0]
    control_env = winner["env"]
    control_matches = winner["matches"]

    def sweep(roster, stage, kwargs_builder):
        space = []
        keep = []
        for candidate in roster:
            figures = capture_figures(control_env, control_matches,
                                      settle_season(control_env, control_matches, docs,
                                                    **kwargs_builder(candidate)))
            failure = first_control_failure(figures, published, stage)
            if failure:
                space.append({"candidate": candidate, "verdict": "rejected",
                              "falsifier": failure})
            else:
                space.append({"candidate": candidate, "verdict": "accepted",
                              "falsifier": None})
                keep.append(candidate)
        return space, keep

    budget_space, budgets = sweep(docs["budget_roster"], CONTROL_TARGETS[6:8],
                                  lambda b: {"budget": b, "threshold": None})
    if not budgets:
        raise SystemExit("no figure on the budget cap roster reproduces the control figures")
    budget = budgets[0]

    threshold_space, thresholds = sweep(docs["threshold_roster"], CONTROL_TARGETS[8:],
                                        lambda t: {"budget": budget, "threshold": t})
    if not thresholds:
        raise SystemExit("no figure on the promotion threshold roster reproduces the "
                         "control figures")
    threshold = thresholds[0]

    return {
        "engine_tag": winner["tag"], "window_depth": winner["depth"],
        "sentinel": winner["sentinel"], "budget": budget, "threshold": threshold,
        "dictionary": winner["dictionary"], "control_env": control_env,
        "control_matches": control_matches, "readings": readings,
        "survivor_labels": [s["reading"]["candidate"] for s in survivors],
        "budget_space": budget_space, "threshold_space": threshold_space,
        "surviving_budgets": budgets, "surviving_thresholds": thresholds,
    }


def per_quantity_space(readings, field, fmt):
    space = []
    for value in dict.fromkeys(r[field] for r in readings):
        entries = [r for r in readings if r[field] == value]
        accepted = [r for r in entries if r["verdict"] == "accepted"]
        rejected = [r for r in entries if r["verdict"] == "rejected"]
        space.append({
            "candidate": fmt(value),
            "verdict": "accepted" if accepted else "rejected",
            "falsifier": None if accepted else rejected[0]["falsifier"],
            "readings_tested": len(entries),
        })
    return space


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def classify_captures(docs):
    roles = {}
    for name, path in sorted(docs["capture_files"].items()):
        with open(path, "rb") as fh:
            head = fh.read(36)
        if head[0:4] != b"RJNL":
            continue
        capture_id = struct.unpack_from("<I", head, 24)[0]
        roles[capture_id % 10] = {"file": name, "path": path}
    return roles


def write_json(out_dir, name, payload):
    with open(os.path.join(out_dir, name), "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
        fh.write("\n")


def safe_name(text):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(text))


def main(argv):
    if len(argv) != 3:
        sys.stderr.write("usage: %s DATA_DIR OUT_DIR\n" % argv[0])
        return 2
    data_dir, out_dir = argv[1], argv[2]
    os.makedirs(out_dir, exist_ok=True)

    docs = load_documents(data_dir)
    roles = classify_captures(docs)
    for role, label in [(1, "reference"), (2, "control"), (3, "season")]:
        if role not in roles:
            raise SystemExit("the data directory must hold a capture whose identifier "
                             "ends in %d (%s)" % (role, label))
    blobs = {role: open(info["path"], "rb").read() for role, info in roles.items()}

    resolved = resolve_parameters(docs, blobs[1], blobs[2])
    tag = resolved["engine_tag"]
    depth = resolved["window_depth"]
    sentinel = resolved["sentinel"]
    budget = resolved["budget"]
    threshold = resolved["threshold"]
    dictionary = resolved["dictionary"]

    season_env = decode_capture(blobs[3], tag, depth, sentinel, dictionary)
    season_matches = settle_matches(season_env)
    squads = settle_season(season_env, season_matches, docs, budget, threshold)
    ownership = settle_ownership(season_env, dictionary)
    reference_env = decode_capture(blobs[1], tag, depth, sentinel, dictionary)
    control_env = resolved["control_env"]

    registered = [s for s in squads.values() if s["registered"]]
    conference_reports = {conference: group_report("conference", conference,
                                                   [s for s in registered
                                                    if s["conference"] == conference], docs)
                          for conference in docs["conferences"]}
    segment_reports = {segment: group_report("segment", segment,
                                             [s for s in registered
                                              if s["segment"] == segment], docs)
                       for segment in docs["segments"]}

    bracket = elect_bracket(squads, docs, ownership)
    docket = elect_docket(season_matches, squads, docs)
    ranking = sorted(squads.values(),
                     key=lambda s: (-s["standing_points"], -s["season_score"], s["key"]))
    for position, squad in enumerate(ranking, start=1):
        squad["rank"] = position

    season_gross = sum(s["gross"] for s in squads.values())
    season_voided = sum(s["voided"] for s in squads.values())
    raw_upkeep = sum(s["raw"] for s in squads.values())
    charged_upkeep = sum(s["charged"] for s in squads.values())
    overflow_upkeep = sum(s["overflow"] for s in squads.values())
    reported_output = sum(r["value"] for r in season_env["records"] if r["kind"] == 2)
    conference_gross = sum(r["gross_output"] for r in conference_reports.values())
    segment_gross = sum(r["gross_output"] for r in segment_reports.values())

    registry_keys = set(docs["registry_by_key"])
    unregistered = sorted(set(squads) - registry_keys)
    absent = sorted(registry_keys - {r["key"] for r in season_env["records"]}
                    - set(ownership["handovers"]))

    def ledger(env):
        counts = collections.Counter(r["kind_name"] for r in env["records"])
        return {name: counts.get(name, 0) for name in LEDGER_ORDER}

    capture_quality = []
    for role, label in [(1, "reference"), (2, "control"), (3, "season")]:
        env = {1: reference_env, 2: control_env, 3: season_env}[role]
        seen = {r["key"] for r in env["records"]}
        capture_quality.append({
            "file": roles[role]["file"],
            "role": label,
            "capture_identifier": env["capture_id"],
            "guard_register": env["guard_register"],
            "active_guards": sorted(env["active_guards"]),
            "declared_segment_count": env["declared_segments"],
            "recovered_segment_count": len(env["segments"]),
            "segment_count_residual": len(env["segments"]) - env["declared_segments"],
            "declared_record_count": env["declared_records"],
            "recovered_record_count": len(env["records"]),
            "record_count_residual": len(env["records"]) - env["declared_records"],
            "declared_roster_size": env["declared_roster"],
            "distinct_squads_recovered": len(seen),
            "registered_squads_recovered": len(seen & registry_keys),
            "roster_size_residual": len(seen) - env["declared_roster"],
            "unregistered_squads": sorted(seen - registry_keys),
            "envelope_checksum_agrees": env["checksum_agrees"],
            "segment_payload_checksums_agree": env["payload_checksums_agree"],
            "filler_width_mismatches": env["diagnostics"]["filler_mismatches"],
            "backward_tick_steps": env["diagnostics"]["backward_ticks"],
            "kind_ledger": ledger(env),
        })

    data_quality = {
        "captures": capture_quality,
        "season": {
            "matches": len(season_matches),
            "records_recovered": len(season_env["records"]),
            "distinct_squads": len(squads),
            "registered_squads": len(registered),
            "unregistered_squads": unregistered,
            "absent_entrants": absent,
            "kind_ledger": ledger(season_env),
        },
        "residuals": {
            "attribution_residual": season_gross - conference_gross,
            "segment_coverage_residual": segment_gross - season_gross,
            "output_ledger_residual": (season_gross + season_voided) - reported_output,
            "upkeep_ledger_residual": (charged_upkeep + overflow_upkeep) - raw_upkeep,
        },
        "residual_notes": {
            "attribution_residual": ("season gross output minus the sum of the conference "
                                     "gross outputs; it is carried by squads that appear "
                                     "in the capture but not in the entrant registry"),
            "segment_coverage_residual": ("sum of the segment gross outputs minus the "
                                          "season gross output; the same squads seen from "
                                          "the segment side"),
            "output_ledger_residual": "gross output plus voided output minus all output "
                                      "reported in the capture",
            "upkeep_ledger_residual": "charged upkeep plus overflow upkeep minus raw "
                                      "upkeep",
        },
        "declared_versus_recovered": {
            "segment_counts_match": all(c["segment_count_residual"] == 0
                                        for c in capture_quality),
            "record_counts_match": all(c["record_count_residual"] == 0
                                       for c in capture_quality),
            "roster_sizes_match": all(c["roster_size_residual"] == 0
                                      for c in capture_quality),
        },
    }

    findings = []
    if data_quality["residuals"]["attribution_residual"] != 0:
        findings.append({
            "finding": "attribution residual is not zero",
            "detail": "the season gross output exceeds the sum of the conference gross "
                      "outputs by %d" % data_quality["residuals"]["attribution_residual"],
            "squad_keys": unregistered,
            "cause": "squads seen on the wire that the entrant registry never lists are "
                     "settled in the season totals but belong to no conference and no "
                     "segment"})
    if data_quality["residuals"]["segment_coverage_residual"] != 0:
        findings.append({
            "finding": "segment coverage residual is not zero",
            "detail": "the segment reports account for %d of the %d gross output reported "
                      "in the season" % (segment_gross, season_gross),
            "squad_keys": unregistered,
            "cause": "the same unregistered squads carry the difference"})
    if data_quality["residuals"]["output_ledger_residual"] != 0:
        findings.append({
            "finding": "output ledger residual is not zero",
            "detail": "gross output plus voided output is %d against %d output reported"
                      % (season_gross + season_voided, reported_output),
            "squad_keys": unregistered,
            "cause": "reported output was not partitioned by the withdrawal rules"})
    if data_quality["residuals"]["upkeep_ledger_residual"] != 0:
        findings.append({
            "finding": "upkeep ledger residual is not zero",
            "detail": "charged upkeep plus overflow upkeep is %d against raw upkeep %d"
                      % (charged_upkeep + overflow_upkeep, raw_upkeep),
            "squad_keys": unregistered,
            "cause": "the season budget did not partition the raw upkeep"})
    for capture in capture_quality:
        if capture["roster_size_residual"] != 0:
            findings.append({
                "finding": "declared roster size does not match the squads recovered",
                "detail": "%s declares a roster size of %d against %d distinct squads "
                          "recovered" % (capture["file"], capture["declared_roster_size"],
                                         capture["distinct_squads_recovered"]),
                "squad_keys": capture["unregistered_squads"],
                "cause": "the declared roster size counts only squads that appear in the "
                         "entrant registry, so the residual is the number of squads that "
                         "played without an entry"})
        if capture["record_count_residual"] != 0:
            findings.append({
                "finding": "declared record count does not match the records recovered",
                "detail": "%s declares %d records against %d recovered"
                          % (capture["file"], capture["declared_record_count"],
                             capture["recovered_record_count"]),
                "squad_keys": [],
                "cause": "records were lost or duplicated on the wire"})
        if capture["segment_count_residual"] != 0:
            findings.append({
                "finding": "declared segment count does not match the segments recovered",
                "detail": "%s declares %d segments against %d recovered"
                          % (capture["file"], capture["declared_segment_count"],
                             capture["recovered_segment_count"]),
                "squad_keys": [],
                "cause": "segments were lost or duplicated on the wire"})
        if capture["backward_tick_steps"]:
            findings.append({
                "finding": "ticks moved backwards inside a segment",
                "detail": "%s shows %d backward tick steps"
                          % (capture["file"], capture["backward_tick_steps"]),
                "squad_keys": [],
                "cause": "the stream continuity contract is broken"})
    if absent:
        findings.append({
            "finding": "absent entrants",
            "detail": "%d squads are listed in the entrant registry but appear in no "
                      "capture" % len(absent),
            "squad_keys": absent,
            "cause": "they are listed and contribute nothing"})
    data_quality["findings"] = findings

    # ---- policy audit -------------------------------------------------
    audit = {
        "unstated_quantities": [
            {"quantity": "engine tag",
             "source": "Engine tag roster published in replay-guards.md",
             "published_roster": docs["engine_tag_roster"],
             "candidate_space": per_quantity_space(resolved["readings"], "engine_tag",
                                                   str),
             "elected": tag},
            {"quantity": "window depth",
             "source": "Window depth roster published in replay-guards.md",
             "published_roster": docs["window_depth_roster"],
             "candidate_space": per_quantity_space(resolved["readings"], "window_depth",
                                                   str),
             "elected": depth},
            {"quantity": "escape sentinel",
             "source": "Sentinel roster published in replay-guards.md",
             "published_roster": ["0x%02X" % s for s in docs["sentinel_roster"]],
             "candidate_space": per_quantity_space(resolved["readings"], "sentinel",
                                                   lambda v: "0x%02X" % v),
             "elected": "0x%02X" % sentinel},
            {"quantity": "season budget",
             "source": "Budget cap roster published in upkeep-policy.md",
             "published_roster": docs["budget_roster"],
             "candidate_space": resolved["budget_space"],
             "elected": budget},
            {"quantity": "promotion threshold",
             "source": "Promotion threshold roster published in standings-policy.md",
             "published_roster": docs["threshold_roster"],
             "candidate_space": resolved["threshold_space"],
             "elected": threshold},
        ],
        "guard_reading_space": resolved["readings"],
        "surviving_guard_readings": resolved["survivor_labels"],
        "attribution_settlement": {
            "passes": ownership["passes"],
            "handovers": {k: ownership["handovers"][k]
                          for k in sorted(ownership["handovers"])},
            "closed_handover_sets": [list(c) for c in ownership["closed_sets"]],
            "effective_owners": {k: ownership["owners"][k]
                                 for k in sorted(ownership["owners"])},
            "squads_no_longer_their_own_owner": sorted(
                k for k in ownership["owners"] if ownership["owners"][k] != k),
        },
        "bracket": {
            "candidate_space_size": bracket["candidate_space_size"],
            "best_satisfied_constraint_count": bracket["satisfied_constraint_count"],
            "eligible_pool_size": bracket["eligible_pool_size"],
            "members": [m["squad_key"] for m in bracket["members"]],
            "constraints": bracket["constraints"],
        },
        "docket": {
            "candidate_space_size": docket["candidate_space_size"],
            "total_review_index": docket["total_review_index"],
            "conference_spread": docket["conference_spread"],
            "members": [m["match_id"] for m in docket["matches"]],
        },
        "calibration": [],
    }

    control_squads = settle_season(control_env, resolved["control_matches"], docs,
                                   budget, threshold)
    control_figures = capture_figures(control_env, resolved["control_matches"],
                                      control_squads)
    for cid, figure, field in CONTROL_TARGETS:
        if cid not in docs["control_values"]:
            continue
        audit["calibration"].append({
            "control": cid,
            "figure": figure,
            "published": docs["control_values"][cid],
            "reproduced": control_figures[field],
            "agrees": control_figures[field] == docs["control_values"][cid],
        })

    # ---- summary ------------------------------------------------------
    summary = {
        "captures": {
            label: {"file": roles[role]["file"],
                    "capture_identifier": env["capture_id"],
                    "active_guards": sorted(env["active_guards"])}
            for role, label, env in [(1, "reference", reference_env),
                                     (2, "control", control_env),
                                     (3, "season", season_env)]},
        "resolved_quantities": {
            "engine_tag": tag,
            "window_depth": depth,
            "escape_sentinel": "0x%02X" % sentinel,
            "season_budget": budget,
            "promotion_threshold": threshold,
            "count_bias": season_env["count_bias"],
            "roster_dictionary": dictionary,
        },
        "season": {
            "matches": len(season_matches),
            "records_recovered": len(season_env["records"]),
            "distinct_squads": len(squads),
            "registered_squads": len(registered),
            "unregistered_squads": unregistered,
            "absent_entrants": absent,
            "gross_output": season_gross,
            "voided_output": season_voided,
            "raw_upkeep": raw_upkeep,
            "charged_upkeep": charged_upkeep,
            "overflow_upkeep": overflow_upkeep,
            "season_score_total": sum(s["season_score"] for s in squads.values()),
            "standing_points_total": sum(s["standing_points"] for s in squads.values()),
            "tier_counts": {t: sum(1 for s in squads.values() if s["tier"] == t)
                            for t in TIER_ORDER},
            "kind_ledger": ledger(season_env),
        },
        "ranking_rule": "standing points descending, then season score descending, "
                        "then squad key ascending",
        "ladder": [{
            "rank": s["rank"], "squad_key": s["key"], "entrant_id": s["entrant_id"],
            "conference": s["conference"], "division": s["division"],
            "segment": s["segment"], "registered": s["registered"],
            "is_ranked": s["is_ranked"], "ladder_weight": s["ladder_weight"],
            "levy_rate": s["levy_rate"], "matches": s["matches"],
            "gross_output": s["gross"], "voided_output": s["voided"],
            "raw_upkeep": s["raw"], "charged_upkeep": s["charged"],
            "overflow_upkeep": s["overflow"], "season_score": s["season_score"],
            "match_awards": s["match_awards"], "streak_credit": s["streak_credit"],
            "standing_points": s["standing_points"], "tier": s["tier"],
            "effective_owner": ownership["owners"].get(s["key"], s["key"]),
            "is_own_effective_owner": ownership["owners"].get(s["key"], s["key"]) == s["key"],
        } for s in ranking],
        "finals_bracket": bracket,
        "review_docket": docket,
        "attribution_settlement": {
            "passes": ownership["passes"],
            "handovers": {k: ownership["handovers"][k]
                          for k in sorted(ownership["handovers"])},
            "closed_handover_sets": [list(c) for c in ownership["closed_sets"]],
        },
        "conference_reports": {name: {"gross_output": r["gross_output"],
                                      "entrants": r["entrants"]}
                               for name, r in conference_reports.items()},
        "segment_reports": {name: {"gross_output": r["gross_output"],
                                   "entrants": r["entrants"]}
                            for name, r in segment_reports.items()},
    }

    write_json(out_dir, "summary.json", summary)
    write_json(out_dir, "data_quality.json", data_quality)
    write_json(out_dir, "policy_audit.json", audit)
    for conference, report in conference_reports.items():
        write_json(out_dir, "region_%s.json" % safe_name(conference), report)
    for segment, report in segment_reports.items():
        write_json(out_dir, "segment_%s.json" % safe_name(segment), report)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
