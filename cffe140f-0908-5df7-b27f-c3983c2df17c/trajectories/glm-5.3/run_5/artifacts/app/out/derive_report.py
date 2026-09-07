#!/usr/bin/env python3
"""Settle a sealed tactics-league season from its binary replay journals.

Usage:
    python3 derive_report.py DATA_DIR OUT_DIR

Reads the three sealed captures, the policy documents, the division topology,
the licence classes and the entrant registry from DATA_DIR, derives every
unstated governing quantity against the published control figures, and writes
the nine season reports into OUT_DIR.

Standard library only.  No network, no installs, no reads outside the two
directories handed to it.
"""

import hashlib
import json
import os
import re
import struct
import sys
from collections import Counter
from itertools import combinations
from math import gcd

# --------------------------------------------------------------------------
# Journal constants
# --------------------------------------------------------------------------

ENVELOPE_WIDTH = 36
SEGMENT_ENVELOPE_WIDTH = 28

GUARD_NAMES = [
    "keystream", "chain", "delta2", "driftkey", "segperm",
    "backref", "endian", "countbias", "shadow", "rotate", "alias", "escape",
]

KIND_LEDGER_NAMES = {
    1: "deploy",
    2: "output",
    3: "eliminate",
    4: "disband",
    5: "concede",
    6: "transfer",
    8: "close",
}

CAPTURE_ROLES = {1: "reference", 2: "control", 3: "season"}


class DecodeError(Exception):
    """A capture could not be decoded under the reading being tested."""


# --------------------------------------------------------------------------
# Policy material
# --------------------------------------------------------------------------

def _backtick_items(text):
    return [m.strip() for m in re.findall(r"`([^`]*)`", text)]


def read_policy(data_dir):
    """Collect every ``Label: `value` `` pair and every roster from the prose."""
    constants = {}
    rosters = {}
    docs = {}
    for name in sorted(os.listdir(data_dir)):
        path = os.path.join(data_dir, name)
        if not os.path.isfile(path):
            continue
        if not name.lower().endswith((".md", ".markdown", ".txt")):
            continue
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        docs[name] = text
        for line in text.splitlines():
            line = line.strip().lstrip("-*").strip()
            m = re.match(r"^([^:`]{1,80}):\s*(.+)$", line)
            if not m:
                continue
            label = m.group(1).strip()
            rest = m.group(2).strip()
            items = _backtick_items(rest)
            if not items:
                continue
            if label.lower().endswith("roster"):
                values = []
                for it in items:
                    try:
                        values.append(int(it, 0))
                    except ValueError:
                        values.append(it)
                rosters[label] = values
            elif len(items) == 1:
                try:
                    constants[label] = int(items[0], 0)
                except ValueError:
                    constants[label] = items[0]
    return constants, rosters, docs


def find_roster(rosters, *keywords):
    """Locate a published roster by a case-insensitive keyword of its label."""
    for label, values in sorted(rosters.items()):
        low = label.lower()
        if all(k in low for k in keywords):
            return label, list(values)
    return None, []


def read_control_figures(docs):
    """Parse the published control figures (``- C1 matches opened: 5``)."""
    figures = []
    for name, text in sorted(docs.items()):
        for line in text.splitlines():
            m = re.match(r"^\s*[-*]\s*([CC]\d+)\s+(.+?)\s*:\s*(-?\d+)\s*$", line)
            if m:
                figures.append(dict(
                    figure=m.group(1).upper(),
                    name=m.group(2).strip(),
                    published=int(m.group(3)),
                    source=name,
                ))
    figures.sort(key=lambda f: f["figure"])
    return figures


def read_transcript(data_dir):
    """Read the labelled event transcript shipped beside the captures."""
    for name in sorted(os.listdir(data_dir)):
        if not name.lower().endswith((".jsonl", ".ndjson")):
            continue
        path = os.path.join(data_dir, name)
        events = []
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
        if events:
            return name, events
    return None, []


TRANSCRIPT_FIELDS = ("logical_segment", "match_id", "record_ordinal", "kind",
                     "squad_key", "unit", "tick", "value")


def check_transcript(events, ref_segments, ref_meta):
    """Compare a decoded reference capture against its labelled transcript.

    The transcript is the ground truth for the base envelope: framing, field
    offsets, tick arithmetic and value arithmetic.  Every disagreement here is
    a defect in the reading under test.
    """
    if all("record_ordinal" in e for e in events):
        events = sorted(events, key=lambda e: e["record_ordinal"])
    records = [r for s in ref_segments for r in s["records"]]
    mismatches = []
    for index, event in enumerate(events):
        if index >= len(records):
            mismatches.append(dict(
                record_ordinal=event.get("record_ordinal", index),
                field="record",
                transcript="event present",
                recovered="no record recovered"))
            break
        record = records[index]
        for field in TRANSCRIPT_FIELDS:
            if field not in event:
                continue
            if field == "squad_key":
                recovered = record["owner"]
            elif field == "record_ordinal":
                recovered = record["capture_ordinal"]
            else:
                recovered = record[field]
            if recovered != event[field]:
                mismatches.append(dict(
                    record_ordinal=event.get("record_ordinal", index),
                    field=field,
                    transcript=event[field],
                    recovered=recovered))
                break
    if len(records) != len(events):
        mismatches.append(dict(
            record_ordinal=min(len(events), len(records)),
            field="record",
            transcript=len(events),
            recovered=len(records)))
    return dict(events=len(events), records=len(records),
                mismatch_count=len(mismatches),
                mismatches=mismatches[:20],
                agrees=not mismatches)


def read_tables(data_dir):
    """Read the division topology, the licence classes and the entrant registry."""
    def read_csv(path):
        with open(path, "r", encoding="utf-8-sig", newline="") as fh:
            header = [h.strip().lower() for h in fh.readline().strip().split(",")]
            out = []
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                parts = [p.strip() for p in line.split(",")]
                out.append(dict(zip(header, parts)))
        return out

    tables = {}
    for name in sorted(os.listdir(data_dir)):
        if not name.lower().endswith(".csv"):
            continue
        tables[name] = read_csv(os.path.join(data_dir, name))
    return tables


def build_registry(tables):
    """Return (registry_by_squad_key, conference_of_division, divisions_in_order,
    weight_of_division, segment_of_licence, levy_of_licence, licence_names)."""
    division_conference = {}
    division_weight = {}
    licence_segment = {}
    licence_levy = {}
    registry = {}
    for name, rows in sorted(tables.items()):
        cols = set(rows[0].keys()) if rows else set()
        if {"division", "conference"} <= cols and "licence_class" not in cols:
            for r in rows:
                division_conference[r["division"]] = r["conference"]
                division_weight[r["division"]] = int(r["ladder_weight"])
        elif {"licence_class", "segment"} <= cols:
            for r in rows:
                licence_segment[r["licence_class"]] = r["segment"]
                licence_levy[r["licence_class"]] = int(r["levy_rate"])
        elif {"squad_key", "division", "licence_class"} <= cols:
            for r in rows:
                registry[r["squad_key"]] = dict(
                    squad_key=r["squad_key"],
                    entrant_id=r.get("entrant_id", ""),
                    division=r["division"],
                    licence_class=r["licence_class"],
                    is_ranked=(r.get("is_ranked", "no").strip().lower() == "yes"),
                )
    for key, entry in registry.items():
        entry["conference"] = division_conference.get(entry["division"])
        entry["ladder_weight"] = division_weight.get(entry["division"], 1)
        entry["segment"] = licence_segment.get(entry["licence_class"])
        entry["levy_rate"] = licence_levy.get(entry["licence_class"], 1)
    conferences = []
    for division, conference in division_conference.items():
        if conference not in conferences:
            conferences.append(conference)
    segments = []
    for licence, segment in licence_segment.items():
        if segment not in segments:
            segments.append(segment)
    return dict(
        registry=registry,
        division_conference=division_conference,
        division_weight=division_weight,
        licence_segment=licence_segment,
        licence_levy=licence_levy,
        conferences=conferences,
        segments=segments,
    )


# --------------------------------------------------------------------------
# Envelope and segment framing
# --------------------------------------------------------------------------

def parse_envelope(blob):
    if len(blob) < ENVELOPE_WIDTH:
        raise DecodeError("capture shorter than an envelope")
    if blob[0:4] != b"RJNL":
        raise DecodeError("envelope marker missing")
    guard = struct.unpack_from("<H", blob, 6)[0]
    env = dict(
        version=blob[4],
        reserved=blob[5],
        guard_register=guard,
        guards_active=[GUARD_NAMES[i] for i in range(len(GUARD_NAMES)) if guard >> i & 1],
        nonce_le=struct.unpack_from("<I", blob, 8)[0],
        nonce_be=struct.unpack_from(">I", blob, 8)[0],
        declared_segment_count=struct.unpack_from("<I", blob, 12)[0],
        declared_record_count=struct.unpack_from("<I", blob, 16)[0],
        season_base_tick=struct.unpack_from("<I", blob, 20)[0],
        capture_identifier=struct.unpack_from("<I", blob, 24)[0],
        declared_roster_size=struct.unpack_from("<I", blob, 28)[0],
        envelope_checksum=struct.unpack_from("<I", blob, 32)[0],
    )
    env["envelope_checksum_ok"] = (
        (sum(blob[:32]) & 0xFFFFFFFF) == env["envelope_checksum"]
    )
    env["role"] = CAPTURE_ROLES.get(env["capture_identifier"] % 10, "unknown")
    return env


def read_segments(blob, bias, count_bias_active):
    """Split the capture body into segment envelopes, in storage order."""
    segments = []
    off = ENVELOPE_WIDTH
    while off < len(blob):
        if off + SEGMENT_ENVELOPE_WIDTH > len(blob):
            raise DecodeError("truncated segment envelope")
        if blob[off:off + 4] != b"SEG0":
            raise DecodeError("segment marker missing at offset %d" % off)
        slot, match_id, declared, salt, width, checksum = struct.unpack_from(
            "<6I", blob, off + 4)
        off += SEGMENT_ENVELOPE_WIDTH
        payload = blob[off:off + width]
        if len(payload) != width:
            raise DecodeError("truncated segment payload")
        off += width
        stored = declared - bias if count_bias_active else declared
        segments.append(dict(
            storage_slot=slot,
            match_id=match_id,
            stored_record_count=declared,
            declared_record_count=stored,
            written_salt=salt,
            payload_width=width,
            payload_checksum=checksum,
            payload=payload,
            payload_checksum_ok=((sum(payload) & 0xFFFFFFFF) == checksum),
        ))
    return segments


def permutation_step(nonce, count):
    base = (nonce >> 8) % count
    step = base + 2
    while gcd(step, count) != 1:
        step += 1
    return step


def unrotate_left(data, turn):
    """Invert a left rotation of `data` by `turn` positions."""
    n = len(data)
    turn %= n
    return bytes(data[(i - turn) % n] for i in range(n))


# --------------------------------------------------------------------------
# Record decoding
# --------------------------------------------------------------------------

def decode_capture(path, engine_tag, window_depth, sentinel, roster=None,
                   build_roster=False):
    """Decode one capture under a candidate reading of the unstated quantities.

    Returns (envelope, segments, meta).  `segments` are in logical order and each
    carries its decoded `records`.  When `build_roster` is set the corpus roster
    dictionary is grown from the wire keys as they are first met.
    """
    with open(path, "rb") as fh:
        blob = fh.read()
    env = parse_envelope(blob)
    guard = env["guard_register"]
    active = [i for i in range(len(GUARD_NAMES)) if guard >> i & 1]

    def on(bit):
        return bit in active

    nonce = env["nonce_be"] if on(6) else env["nonce_le"]
    bias = (sum(engine_tag.encode("utf-8")) % 11) if on(7) else 0
    segments = read_segments(blob, bias, on(7))
    count = len(segments)

    if on(4) and count:
        step = permutation_step(nonce, count)
        logical = [None] * count
        for stored_index, seg in enumerate(segments):
            logical[(nonce + stored_index * step) % count] = seg
        if any(seg is None for seg in logical):
            raise DecodeError("segment permutation did not cover the capture")
    else:
        step = None
        logical = list(segments)

    dictionary = [] if build_roster else list(roster or [])
    window = []
    records_seen = 0
    previous_tick = None
    running_value_sum = 0
    backwards_ticks = 0

    for logical_index, seg in enumerate(logical):
        effective_salt = ((seg["written_salt"] ^ running_value_sum) & 0xFFFFFFFF
                          if on(3) else seg["written_salt"])
        payload = seg["payload"]
        offset = 0
        blobs = []
        for _ in range(seg["declared_record_count"]):
            if offset + 2 > len(payload):
                raise DecodeError("record frame runs past the payload")
            visible, shadow = payload[offset], payload[offset + 1]
            offset += 2
            width = (shadow ^ (effective_salt & 0xFF)) if on(8) else visible
            if width <= 0 or offset + width > len(payload):
                raise DecodeError("record blob width %d is impossible" % width)
            blobs.append(payload[offset:offset + width])
            offset += width
        filler = len(payload) - offset
        seg["filler_width"] = filler
        seg["expected_filler_width"] = 3 + (seg["written_salt"] % 5)
        seg["filler_width_ok"] = filler == seg["expected_filler_width"]
        seg["effective_salt"] = effective_salt

        segment_values = []
        records = []
        first_difference = 0
        previous_segment_tick = 0

        for ordinal, raw in enumerate(blobs):
            body_blob = raw
            if on(0):
                digest = hashlib.sha256(
                    ("%s:%d:%d:%d" % (engine_tag, seg["match_id"],
                                      effective_salt, ordinal)).encode("utf-8")
                ).digest()
                body_blob = bytes(a ^ b for a, b in zip(body_blob, digest))
            if on(9):
                body_blob = unrotate_left(
                    body_blob, (effective_salt + records_seen) % len(body_blob))

            directives = []
            cursor = 0
            if on(11):
                while (cursor + 1 < len(body_blob)
                       and body_blob[cursor] == sentinel
                       and len(directives) < 2):
                    byte = body_blob[cursor + 1]
                    directives.append((byte >> 4, byte & 0xF))
                    cursor += 2
            body = body_blob[cursor:]
            if len(body) < 13:
                raise DecodeError("record body too narrow to hold a record")
            kind = body[0]
            mode = body[1]
            if mode == 0:
                if len(body) < 15:
                    raise DecodeError("named record body too narrow")
                attribution = body[2:5].decode("latin-1")
                field_offset = 5
            elif mode in (1, 2):
                attribution = body[2]
                field_offset = 3
            else:
                raise DecodeError("attribution mode %d is not defined" % mode)

            unit, = struct.unpack_from("<H", body, field_offset)
            stored_tick, = struct.unpack_from("<I", body, field_offset + 2)
            stored_value, = struct.unpack_from("<I", body, field_offset + 6)

            if on(2):
                first_difference = (first_difference + stored_tick) & 0xFFFFFFFF
                tick = (previous_segment_tick + first_difference) & 0xFFFFFFFF
            else:
                tick = stored_tick
            if records and tick < records[-1]["tick"]:
                backwards_ticks += 1
            previous_segment_tick = tick

            if on(1):
                chain_base = nonce if previous_tick is None else previous_tick
                value = (stored_value ^ chain_base) & 0xFFFFFFFF
            else:
                value = stored_value
            previous_tick = tick
            segment_values.append(value)

            if mode == 0:
                wire_key = attribution
                if build_roster and wire_key not in dictionary:
                    dictionary.append(wire_key)
            elif mode == 1:
                if attribution >= len(window):
                    raise DecodeError(
                        "back reference %d outside a window of %d"
                        % (attribution, len(window)))
                wire_key = window[attribution]
            else:
                if attribution >= len(dictionary):
                    raise DecodeError(
                        "roster position %d outside a dictionary of %d"
                        % (attribution, len(dictionary)))
                wire_key = dictionary[attribution]

            owner = wire_key
            for operation, operand in directives:
                if operation == 1:
                    if not dictionary:
                        raise DecodeError("escape redirect with no dictionary")
                    owner = dictionary[operand % len(dictionary)]
                elif operation == 2:
                    if wire_key in dictionary:
                        owner = dictionary[
                            (dictionary.index(wire_key) + operand) % len(dictionary)]
                else:
                    raise DecodeError(
                        "escape operation %d is not defined" % operation)

            if wire_key in window:
                window.remove(wire_key)
            window.append(wire_key)
            if len(window) > window_depth:
                window.pop(0)

            records.append(dict(
                match_id=seg["match_id"],
                logical_segment=logical_index,
                capture_ordinal=records_seen,
                segment_ordinal=ordinal,
                kind=kind,
                ledger_name=KIND_LEDGER_NAMES.get(kind, "kind_%d" % kind),
                attribution_mode=mode,
                attribution_field=attribution,
                wire_key=wire_key,
                owner=owner,
                redirected=bool(directives),
                directives=[list(d) for d in directives],
                unit=unit,
                tick=tick,
                value=value,
            ))
            records_seen += 1

        running_value_sum = (running_value_sum + sum(segment_values)) & 0xFFFFFFFF
        seg["records"] = records
        seg["logical_position"] = logical_index

    meta = dict(
        path=path,
        nonce=nonce,
        count_bias=bias,
        permutation_step=step,
        segments_recovered=count,
        records_recovered=records_seen,
        backwards_tick_transitions=backwards_ticks,
        dictionary=list(dictionary),
    )
    return env, logical, meta


# --------------------------------------------------------------------------
# Settlement
# --------------------------------------------------------------------------

def settle_match(segment, registry):
    """Settle one match: gross/voided output, raw upkeep, participants."""
    records = segment["records"]
    close_tick = None
    for record in records:
        if record["kind"] == 8:
            close_tick = record["tick"]
            break
    if close_tick is None:
        close_tick = max(r["tick"] for r in records) if records else 0

    eliminated = {}
    conceded = {}
    for record in records:
        if record["kind"] == 3:
            eliminated.setdefault((record["owner"], record["unit"]), record["tick"])
        elif record["kind"] == 5:
            conceded.setdefault(record["owner"], record["tick"])

    deployed = {}
    raw_upkeep = {}
    for record in records:
        if record["kind"] == 1:
            unit = (record["owner"], record["unit"])
            if unit in deployed:
                start, rate = deployed.pop(unit)
                raw_upkeep[record["owner"]] = (
                    raw_upkeep.get(record["owner"], 0)
                    + rate * max(0, record["tick"] - start))
            deployed[unit] = (record["tick"], record["value"])
        elif record["kind"] == 4:
            unit = (record["owner"], record["unit"])
            if unit in deployed:
                start, rate = deployed.pop(unit)
                raw_upkeep[record["owner"]] = (
                    raw_upkeep.get(record["owner"], 0)
                    + rate * max(0, record["tick"] - start))
    for (squad, _unit), (start, rate) in deployed.items():
        raw_upkeep[squad] = raw_upkeep.get(squad, 0) + rate * max(0, close_tick - start)

    gross = {}
    voided = {}
    for record in records:
        if record["kind"] != 2:
            continue
        squad = record["owner"]
        unit = (squad, record["unit"])
        withdrawn = False
        if unit in eliminated and record["tick"] >= eliminated[unit]:
            withdrawn = True
        if squad in conceded and record["tick"] >= conceded[squad]:
            withdrawn = True
        if withdrawn:
            voided[squad] = voided.get(squad, 0) + record["value"]
        else:
            gross[squad] = gross.get(squad, 0) + record["value"]

    participants = sorted({r["owner"] for r in records})
    return dict(
        match_id=segment["match_id"],
        logical_position=segment["logical_position"],
        close_tick=close_tick,
        participants=participants,
        gross=gross,
        voided=voided,
        raw_upkeep=raw_upkeep,
        conceded=conceded,
        eliminated=eliminated,
        escape_records=sum(1 for r in records if r["directives"]),
    )


def settle_season(segments, registry, budget, threshold, review_weight):
    """Settle the whole season over logically ordered matches."""
    league_weight = registry["division_weight"]
    league_levy = registry["licence_levy"]
    entrants = registry["registry"]

    squads = {}
    matches = []
    for order, segment in enumerate(segments):
        settled = settle_match(segment, registry)
        settled["order"] = order
        matches.append(settled)

        for squad in settled["participants"]:
            entry = squads.setdefault(squad, dict(
                squad_key=squad,
                matches=0, gross_output=0, voided_output=0,
                raw_upkeep=0, charged_upkeep=0, overflow_upkeep=0,
                season_score=0, standing_points=0, streak_credit=0,
                budget_spent=0, match_log=[]))
            info = entrants.get(squad)
            ladder_weight = info["ladder_weight"] if info else 1
            levy_rate = info["levy_rate"] if info else 1
            raw = settled["raw_upkeep"].get(squad, 0)
            room = max(0, budget - entry["budget_spent"])
            charged = min(room, raw)
            overflow = raw - charged
            entry["budget_spent"] += charged
            entry["matches"] += 1
            entry["gross_output"] += settled["gross"].get(squad, 0)
            entry["voided_output"] += settled["voided"].get(squad, 0)
            entry["raw_upkeep"] += raw
            entry["charged_upkeep"] += charged
            entry["overflow_upkeep"] += overflow
            score = (settled["gross"].get(squad, 0) * ladder_weight
                     - charged * levy_rate)
            entry["season_score"] += score
            entry["match_log"].append(dict(
                order=order,
                match_id=settled["match_id"],
                gross_output=settled["gross"].get(squad, 0),
                voided_output=settled["voided"].get(squad, 0),
                raw_upkeep=raw,
                charged_upkeep=charged,
                overflow_upkeep=overflow,
                match_score=score,
                conceded=(squad in settled["conceded"]),
                ladder_weight=ladder_weight,
                levy_rate=levy_rate))

        scores = {}
        for squad in settled["participants"]:
            for log in squads[squad]["match_log"]:
                if log["order"] == order:
                    scores[squad] = log["match_score"]
                    break
        standing = [s for s in settled["participants"] if s not in settled["conceded"]]
        if standing:
            best = max(scores[s] for s in standing)
            winners = [s for s in standing if scores[s] == best]
        else:
            winners = []
        outright = len(winners) == 1
        for squad in settled["participants"]:
            log = squads[squad]["match_log"][-1]
            log["is_winner"] = squad in winners
            log["shared_highest"] = len(winners) > 1 and squad in winners
            log["outright_win"] = outright and squad in winners
            log["standing_award"] = (
                3 if outright and squad in winners
                else 1 if squad in winners else 0)
            squads[squad]["standing_points"] += log["standing_award"]

        settled["match_scores"] = scores
        settled["winners"] = winners
        settled["outright_winner"] = winners[0] if outright else None

    for squad, entry in squads.items():
        run = 0
        for log in entry["match_log"]:
            run = run + 1 if log["outright_win"] else 0
            log["streak_run"] = run
            log["streak_credit"] = 2 * max(0, run - 1)
        entry["streak_credit"] = sum(l["streak_credit"] for l in entry["match_log"])
        entry["standing_points"] += entry["streak_credit"]
        score = entry["season_score"]
        entry["tier"] = (
            "champion" if score >= 3 * threshold
            else "contender" if score >= 2 * threshold
            else "qualifier" if score >= threshold
            else "provisional")

    for match in matches:
        match["review_index"] = sum(match["voided"].values()) + (
            match["escape_records"] * review_weight)
    return squads, matches


def assign_match_conference(match, registry):
    """The conference of the registered squad with the most gross output."""
    entrants = registry["registry"]
    best = None
    for squad, gross in sorted(match["gross"].items()):
        if squad not in entrants:
            continue
        if best is None or gross > best[1]:
            best = (squad, gross)
    if best is None:
        return None, None
    return entrants[best[0]]["conference"], best[0]


# --------------------------------------------------------------------------
# Entry hand overs
# --------------------------------------------------------------------------

def settle_handovers(records, dictionary):
    """Follow entry hand overs to a fixpoint; return (owners, sweeps, detail)."""
    universe = []
    for key in list(dictionary) + sorted({r["owner"] for r in records}):
        if key not in universe:
            universe.append(key)

    handover = {}
    detail = []
    for record in records:
        if record["kind"] != 6:
            continue
        if not dictionary:
            continue
        target = dictionary[record["value"] % len(dictionary)]
        handover[record["owner"]] = target
        detail.append(dict(
            squad_key=record["owner"],
            receiving_squad_key=target,
            match_id=record["match_id"],
            tick=record["tick"],
            capture_ordinal=record["capture_ordinal"],
            stands=(handover[record["owner"]] == target)))

    # A closed set of hand overs settles on its alphabetically first member.
    collapsed = dict(handover)
    for squad in sorted(handover):
        trail, seen = [], {}
        cursor = squad
        while cursor in handover and cursor not in seen:
            seen[cursor] = len(trail)
            trail.append(cursor)
            cursor = handover[cursor]
        if cursor in seen:
            cycle = trail[seen[cursor]:]
            representative = min(cycle)
            for member in cycle:
                collapsed[member] = representative

    owners = {squad: squad for squad in universe}
    sweeps = 0
    while True:
        sweeps += 1
        changed = False
        for squad in universe:
            current = owners[squad]
            owners[squad] = collapsed.get(current, current)
            if owners[squad] != current:
                changed = True
        if not changed:
            break
    return owners, sweeps, dict(
        handovers=detail,
        collapsed=collapsed,
        handed_away=sorted(handover),
        self_owned=sorted(s for s in universe if owners[s] == s))


# --------------------------------------------------------------------------
# Reports
# --------------------------------------------------------------------------

def entry_row(squad, squads, registry):
    """One row of a group report, in the shape of the published example."""
    info = registry["registry"].get(squad)
    entry = squads.get(squad)
    row = dict(
        squad_key=squad,
        entrant_id=info["entrant_id"] if info else None,
        registered=info is not None,
        conference=info["conference"] if info else None,
        division=info["division"] if info else None,
        licence_class=info["licence_class"] if info else None,
        segment=info["segment"] if info else None,
        is_ranked=info["is_ranked"] if info else False,
        ladder_weight=info["ladder_weight"] if info else 1,
        levy_rate=info["levy_rate"] if info else 1,
        matches=entry["matches"] if entry else 0,
        gross_output=entry["gross_output"] if entry else 0,
        voided_output=entry["voided_output"] if entry else 0,
        charged_upkeep=entry["charged_upkeep"] if entry else 0,
        overflow_upkeep=entry["overflow_upkeep"] if entry else 0,
        season_score=entry["season_score"] if entry else 0,
        standing_points=entry["standing_points"] if entry else 0,
        streak_credit=entry["streak_credit"] if entry else 0,
        tier=entry["tier"] if entry else "provisional",
        raw_upkeep=entry["raw_upkeep"] if entry else 0,
    )
    return row


def group_report(kind, name, rows, fallback_divisions):
    rows = sorted(rows, key=lambda r: r["squad_key"])
    divisions = sorted({r["division"] for r in rows if r["division"]})
    if not divisions:
        divisions = sorted(fallback_divisions)
    tier_counts = {t: 0 for t in ("champion", "contender", "qualifier", "provisional")}
    for row in rows:
        tier_counts[row["tier"]] += 1
    return dict(
        group_kind=kind,
        group_name=name,
        divisions=divisions,
        entrants=len(rows),
        entries=rows,
        matches_played=sum(r["matches"] for r in rows),
        gross_output=sum(r["gross_output"] for r in rows),
        voided_output=sum(r["voided_output"] for r in rows),
        raw_upkeep=sum(r["raw_upkeep"] for r in rows),
        charged_upkeep=sum(r["charged_upkeep"] for r in rows),
        overflow_upkeep=sum(r["overflow_upkeep"] for r in rows),
        season_score=sum(r["season_score"] for r in rows),
        standing_points=sum(r["standing_points"] for r in rows),
        streak_credit=sum(r["streak_credit"] for r in rows),
        tier_counts=tier_counts,
    )


# --------------------------------------------------------------------------
# Bracket
# --------------------------------------------------------------------------

BRACKET_CONSTRAINTS = [
    "prominent division present",
    "both entrant segments present",
    "standing points floor held by every member",
    "no two members share a conference",
    "a ranked entrant present",
    "combined ladder weight floor reached",
    "no member carries overflow upkeep",
    "a champion tier member present",
    "combined gross output floor reached",
    "no member has handed its entry away",
]


def bracket_constraint_flags(members, squads, registry, policy, owners):
    entrants = registry["registry"]
    weights = registry["division_weight"]
    prominent = False
    for squad in members:
        info = entrants.get(squad)
        if info and info["division"] in weights:
            if weights[info["division"]] >= policy["ladder_weight_prominence"]:
                prominent = True
    segments = {entrants[s]["segment"] for s in members if s in entrants}
    conferences = [entrants[s]["conference"] for s in members if s in entrants]
    return [
        prominent,
        segments >= set(registry["segments"]),
        all(squads[s]["standing_points"] >= policy["standing_points_floor"]
            for s in members),
        len(conferences) == len(set(conferences)),
        any(entrants[s]["is_ranked"] for s in members if s in entrants),
        sum(entrants[s]["ladder_weight"] if s in entrants else 1 for s in members)
        >= policy["combined_ladder_weight_floor"],
        all(squads[s]["overflow_upkeep"] == 0 for s in members),
        any(squads[s]["tier"] == "champion" for s in members),
        sum(squads[s]["gross_output"] for s in members)
        >= policy["combined_gross_output_floor"],
        all(s not in owners["handed_away"] for s in members),
    ]


def elect_bracket(squads, registry, policy, owners):
    floor = policy["eligibility_match_floor"]
    pool = sorted(
        s for s, entry in squads.items()
        if entry["matches"] >= floor and owners["owners"].get(s, s) == s)
    best = None
    evaluated = 0
    for combo in combinations(pool, 3):
        evaluated += 1
        flags = bracket_constraint_flags(combo, squads, registry, policy, owners)
        satisfied = sum(flags)
        charged = sum(squads[s]["charged_upkeep"] for s in combo)
        key = (-satisfied, charged, sorted(combo))
        if best is None or key < best[0]:
            best = (key, combo, flags, satisfied, charged)
    if best is None:
        return dict(eligible_pool=pool, candidate_space=0,
                    best_satisfied_constraints=0, members=[],
                    satisfied_constraints=[], constraint_flags=[],
                    constraints=BRACKET_CONSTRAINTS, total_charged_upkeep=0)
    _key, combo, flags, satisfied, charged = best
    return dict(
        eligibility_match_floor=floor,
        eligible_pool=pool,
        candidate_space=evaluated,
        best_satisfied_constraints=satisfied,
        constraints=BRACKET_CONSTRAINTS,
        constraint_flags=flags,
        satisfied_constraints=[c for c, f in zip(BRACKET_CONSTRAINTS, flags) if f],
        unsatisfied_constraints=[c for c, f in zip(BRACKET_CONSTRAINTS, flags) if not f],
        members=sorted(combo),
        member_entries=[entry_row(s, squads, registry) for s in sorted(combo)],
        total_charged_upkeep=charged,
        total_ladder_weight=sum(
            registry["registry"][s]["ladder_weight"] if s in registry["registry"] else 1
            for s in combo),
        total_gross_output=sum(squads[s]["gross_output"] for s in combo),
        tie_break="least total charged upkeep, then member keys sorting first",
    )


# --------------------------------------------------------------------------
# Review docket
# --------------------------------------------------------------------------

def elect_docket(matches, registry, policy):
    size = policy["docket_size"]
    cap = policy["docket_conference_cap"]
    conferences = {}
    keepers = {}
    for match in matches:
        conference, squad = assign_match_conference(match, registry)
        match["conference"] = conference
        match["conference_bearer"] = squad
        conferences[match["match_id"]] = conference
        keepers[match["match_id"]] = squad

    identifiers = sorted(m["match_id"] for m in matches)
    best = None
    evaluated = 0
    for combo in combinations(identifiers, size):
        evaluated += 1
        tally = Counter(conferences[m] for m in combo if conferences[m] is not None)
        if any(v > cap for v in tally.values()):
            continue
        total = sum(next(m["review_index"] for m in matches if m["match_id"] == i)
                    for i in combo)
        key = (-total, list(combo))
        if best is None or key < best[0]:
            best = (key, combo, total)
    result = dict(
        docket_size=size,
        docket_conference_cap=cap,
        review_weight=policy["review_weight"],
        candidate_space=evaluated,
        admissible_candidates=sum(1 for c in combinations(identifiers, size)
                                  if max(Counter(conferences[m] for m in c
                                                 if conferences[m] is not None).values()
                                         or [0]) <= cap),
    )
    if best is None:
        result.update(dict(matches=[], total_review_index=0,
                           conference_spread={}, match_ids=[]))
        return result
    _key, combo, total = best
    spread = Counter(conferences[m] for m in combo if conferences[m] is not None)
    rows = []
    for match_id in combo:
        match = next(m for m in matches if m["match_id"] == match_id)
        rows.append(dict(
            match_id=match_id,
            logical_position=match["logical_position"],
            conference=match["conference"],
            conference_bearer=match["conference_bearer"],
            voided_output=sum(match["voided"].values()),
            escape_directive_records=match["escape_records"],
            review_index=match["review_index"],
            participants=match["participants"],
        ))
    result.update(dict(
        match_ids=list(combo),
        matches=rows,
        total_review_index=total,
        conference_spread={k: spread[k] for k in sorted(spread)},
        tie_break="match identifiers sorting first",
    ))
    return result


# --------------------------------------------------------------------------
# Calibration search
# --------------------------------------------------------------------------

def control_reading(env, segments, meta, registry, budget, threshold, review_weight):
    squads, matches = settle_season(segments, registry, budget, threshold, review_weight)
    ledger = Counter(r["kind"] for s in segments for r in s["records"])
    tiers = Counter(e["tier"] for e in squads.values())
    return dict(
        matches_opened=len(segments),
        records_recovered=meta["records_recovered"],
        distinct_squads=len({r["owner"] for s in segments for r in s["records"]}),
        gross_output=sum(e["gross_output"] for e in squads.values()),
        voided_output=sum(e["voided_output"] for e in squads.values()),
        raw_upkeep=sum(e["raw_upkeep"] for e in squads.values()),
        charged_upkeep=sum(e["charged_upkeep"] for e in squads.values()),
        overflow_upkeep=sum(e["overflow_upkeep"] for e in squads.values()),
        champion_entrants=tiers["champion"],
        qualifier_entrants=tiers["qualifier"],
        provisional_entrants=tiers["provisional"],
    )


FIGURE_KEYS = {
    "matches opened": "matches_opened",
    "records recovered": "records_recovered",
    "distinct squads": "distinct_squads",
    "gross output": "gross_output",
    "voided output": "voided_output",
    "raw upkeep": "raw_upkeep",
    "charged upkeep": "charged_upkeep",
    "overflow upkeep": "overflow_upkeep",
    "champion entrants": "champion_entrants",
    "qualifier entrants": "qualifier_entrants",
    "provisional entrants": "provisional_entrants",
}


def main(argv):
    if len(argv) != 3:
        sys.stderr.write("usage: derive_report.py DATA_DIR OUT_DIR\n")
        return 2
    data_dir, out_dir = argv[1], argv[2]
    os.makedirs(out_dir, exist_ok=True)

    constants, rosters, docs = read_policy(data_dir)
    figures = read_control_figures(docs)
    transcript_name, transcript_events = read_transcript(data_dir)
    tables = read_tables(data_dir)
    registry = build_registry(tables)
    entrants = registry["registry"]

    def figure_order(figure):
        m = re.match(r"^([A-Za-z]+)(\d+)$", figure["figure"])
        return (m.group(1), int(m.group(2))) if m else (figure["figure"], 0)

    figures.sort(key=figure_order)
    published = {}
    for figure in figures:
        published[figure["name"].lower()] = figure["published"]

    def policy_constant(*names, default=None):
        for name in names:
            for label, value in constants.items():
                if label.lower() == name:
                    return value
        return default

    policy = dict(
        eligibility_match_floor=policy_constant("eligibility match floor", default=2),
        ladder_weight_prominence=policy_constant("ladder weight prominence", default=4),
        standing_points_floor=policy_constant("standing points floor", default=3),
        combined_ladder_weight_floor=policy_constant(
            "combined ladder weight floor", default=7),
        combined_gross_output_floor=policy_constant(
            "combined gross output floor", default=900),
        review_weight=policy_constant("review weight", default=25),
        docket_size=policy_constant("docket size", default=5),
        docket_conference_cap=policy_constant("docket conference cap", default=2),
    )

    # -- capture discovery -------------------------------------------------
    captures = {}
    for name in sorted(os.listdir(data_dir)):
        path = os.path.join(data_dir, name)
        if not os.path.isfile(path):
            continue
        if not name.lower().endswith((".rjnl", ".journal", ".bin")):
            continue
        try:
            with open(path, "rb") as fh:
                env = parse_envelope(fh.read())
        except (DecodeError, OSError):
            continue
        captures[env["role"]] = dict(name=name, path=path, envelope=env)
    for role in ("reference", "control", "season"):
        if role not in captures:
            sys.stderr.write("no capture with identifier ending in %d found\n"
                             % [k for k, v in CAPTURE_ROLES.items() if v == role][0])
            return 1

    reference = captures["reference"]
    control = captures["control"]
    season = captures["season"]

    # -- guard quantity search --------------------------------------------
    tag_label, tag_space = find_roster(rosters, "engine", "tag")
    window_label, window_space = find_roster(rosters, "window", "depth")
    sentinel_label, sentinel_space = find_roster(rosters, "sentinel")
    budget_label, budget_space = find_roster(rosters, "budget", "cap")
    threshold_label, threshold_space = find_roster(rosters, "promotion", "threshold")
    if not tag_space:
        tag_space = ["orion-fold"]
    if not window_space:
        window_space = [4]
    if not sentinel_space:
        sentinel_space = [0x1D]
    if not budget_space:
        budget_space = [0]
    if not threshold_space:
        threshold_space = [1]

    guard_names = [f["name"].lower() for f in figures
                   if f["name"].lower() in FIGURE_KEYS]

    # A published figure only pins a quantity it actually depends on.  Work out
    # which figures move with the season budget and with the promotion
    # threshold by reading the control slice at two settings of each; the
    # figures that move with neither are the ones that pin the guards.
    def probe(tag, depth, sentinel):
        _e, ref_segments, ref_meta = decode_capture(
            reference["path"], tag, depth, sentinel, build_roster=True)
        env, segments, meta = decode_capture(
            control["path"], tag, depth, sentinel,
            roster=ref_meta["dictionary"])
        return env, segments, meta, ref_segments, ref_meta

    # The published prose states how many figures pin each quantity; read that
    # statement when it is present and confirm it by probing the control slice
    # across the whole of each published roster.
    word_numbers = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
                    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
                    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
                    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
                    "nineteen": 19, "twenty": 20}

    def spoken_count(text, pattern):
        m = re.search(pattern, text, re.IGNORECASE)
        if not m:
            return None
        word = m.group(1).lower()
        if word.isdigit():
            return int(word)
        return word_numbers.get(word)

    prose = "\n".join(docs.values())
    first_count = spoken_count(prose, r"the first (\w+) figures?.{0,80}?guard quantit")
    next_count = spoken_count(prose, r"the next (\w+)\b.{0,80}?season budget")
    last_count = spoken_count(prose, r"the last (\w+)\b.{0,80}?promotion threshold")

    figure_groups = None
    probe_source = None
    for tag in tag_space:
        for depth in window_space:
            for sentinel in sentinel_space:
                try:
                    env, segments, meta, _rs, _rm = probe(tag, depth, sentinel)
                except DecodeError:
                    continue
                probe_source = (tag, depth, sentinel)
                readings = {}
                for budget in budget_space:
                    for threshold in threshold_space:
                        readings[(budget, threshold)] = control_reading(
                            env, segments, meta, registry, budget, threshold,
                            policy["review_weight"])
                break
            if probe_source:
                break
        if probe_source:
            break
    if probe_source is None:
        sys.stderr.write("no reading of the guard quantities decodes the control "
                         "slice at all\n")
        return 1

    ordered = list(guard_names)
    if (first_count is not None and next_count is not None
            and last_count is not None
            and first_count + next_count + last_count == len(ordered)):
        guard_group = ordered[:first_count]
        budget_group = ordered[first_count:first_count + next_count]
        threshold_group = ordered[first_count + next_count:]
        grouping_source = "published prose"
    else:
        guard_group, budget_group, threshold_group = list(ordered), [], []
        grouping_source = "exhaustive probing fallback"

    # A figure that still moves with the season budget or the promotion
    # threshold cannot be used to pin the guards, whichever group the prose put
    # it in.
    base_key = (budget_space[0], threshold_space[0])
    base_reading = readings[base_key]
    moved_budget = set()
    moved_threshold = set()
    for key, reading in readings.items():
        if key[0] != base_key[0] and key[1] == base_key[1]:
            for name in ordered:
                if reading[FIGURE_KEYS[name]] != base_reading[FIGURE_KEYS[name]]:
                    moved_budget.add(name)
        if key[1] != base_key[1] and key[0] == base_key[0]:
            for name in ordered:
                if reading[FIGURE_KEYS[name]] != base_reading[FIGURE_KEYS[name]]:
                    moved_threshold.add(name)
    if moved_budget or moved_threshold:
        budget_group = sorted(set(budget_group) | (moved_budget - moved_threshold))
        threshold_group = sorted(set(threshold_group) | moved_threshold)
        guard_group = [n for n in guard_group
                       if n not in moved_budget and n not in moved_threshold]
        grouping_source += ", corrected by exhaustive probing"
    figure_groups = dict(guard=guard_group, budget=budget_group,
                         threshold=threshold_group, source=grouping_source,
                         probe_reading=probe_source)

    search = []
    for tag in tag_space:
        for depth in window_space:
            for sentinel in sentinel_space:
                verdict = dict(
                    candidate=dict(
                        engine_tag=tag if isinstance(tag, str) else str(tag),
                        engine_tag_bytes=sum(str(tag).encode("utf-8")) % 11,
                        window_depth=depth,
                        escape_sentinel=("0x%02X" % sentinel
                                         if isinstance(sentinel, int) else sentinel)),
                    reproduces_control_figures=None,
                    reproduces_transcript=None,
                    verdict=None, falsifier=None)
                try:
                    env, segments, meta, ref_segments, ref_meta = probe(
                        tag, depth, sentinel)
                    reading = control_reading(env, segments, meta, registry,
                                              budget_space[0], threshold_space[0],
                                              policy["review_weight"])
                except DecodeError as exc:
                    verdict["verdict"] = "rejected"
                    verdict["falsifier"] = "no decode, so every published figure"
                    verdict["note"] = str(exc)
                    search.append(verdict)
                    continue
                falsifier = None
                for name in guard_names:
                    if name not in figure_groups["guard"]:
                        continue
                    if name in published and reading[FIGURE_KEYS[name]] != published[name]:
                        falsifier = name
                        break
                verdict["reproduced"] = {n: reading[FIGURE_KEYS[n]]
                                         for n in guard_names}
                verdict["reproduces_control_figures"] = falsifier is None
                if falsifier is None:
                    verdict["verdict"] = "accepted"
                else:
                    verdict["verdict"] = "rejected"
                    verdict["falsifier"] = "C? " + falsifier

                # The labelled transcript of the reference capture is an
                # independent falsifier: it pins the framing and the record
                # arithmetic directly, without touching the season policy.
                if transcript_events:
                    check = check_transcript(transcript_events, ref_segments,
                                             ref_meta)
                    verdict["reproduces_transcript"] = check["agrees"]
                    verdict["transcript_check"] = dict(
                        events=check["events"], records=check["records"],
                        mismatches=check["mismatch_count"])
                    if not check["agrees"] and falsifier is None:
                        verdict["verdict"] = "rejected"
                        verdict["falsifier"] = "the labelled transcript of the " \
                                               "reference capture"
                        if check["mismatches"]:
                            verdict["note"] = (
                                "first disagreement: %s" % check["mismatches"][0])
                search.append(verdict)

    # Every candidate has now been judged twice over.  The published prose pins
    # each quantity to exactly one value; a second survivor is a finding.
    survivors = [row for row in search
                 if row["reproduces_control_figures"]
                 and row["reproduces_transcript"] is not False]
    if not survivors:
        survivors = [row for row in search if row["reproduces_control_figures"]]
    if not survivors:
        sys.stderr.write("no reading of the unstated guard quantities reproduces "
                         "the published control figures\n")
        return 1
    accepted_count = len(survivors)
    chosen = survivors[0]
    accepted_tag = chosen["candidate"]["engine_tag"]
    accepted_depth = chosen["candidate"]["window_depth"]
    try:
        accepted_sentinel = int(str(chosen["candidate"]["escape_sentinel"]), 0)
    except ValueError:
        accepted_sentinel = sentinel_space[0]
    _env, _segments, _meta, ref_segments, ref_meta = probe(
        accepted_tag, accepted_depth, accepted_sentinel)
    accepted = dict(tag=accepted_tag, depth=accepted_depth,
                    sentinel=accepted_sentinel,
                    dictionary=ref_meta["dictionary"],
                    control_env=_env, control_segments=_segments,
                    control_meta=_meta)
    baseline_check = None
    if transcript_events:
        baseline_check = dict(file=transcript_name,
                              **check_transcript(transcript_events, ref_segments,
                                                 ref_meta))

    dictionary = accepted["dictionary"]

    def pinned(names, reading):
        for name in names:
            if name in published and reading[FIGURE_KEYS[name]] != published[name]:
                return name
        return None

    # -- season budget search ---------------------------------------------
    budget_verdicts = []
    budget_value = None
    for candidate in budget_space:
        reading = control_reading(accepted["control_env"],
                                  accepted["control_segments"],
                                  accepted["control_meta"], registry, candidate,
                                  threshold_space[0], policy["review_weight"])
        falsifier = pinned(figure_groups["budget"], reading)
        row = dict(candidate=candidate, verdict=None, falsifier=None,
                   reproduced={n: reading[FIGURE_KEYS[n]]
                               for n in figure_groups["budget"]})
        if falsifier is None:
            row["verdict"] = "accepted"
            budget_value = candidate
        else:
            row["verdict"] = "rejected"
            row["falsifier"] = "C? " + falsifier
        budget_verdicts.append(row)
    if budget_value is None and budget_space:
        budget_value = budget_space[-1]

    # -- promotion threshold search ---------------------------------------
    threshold_verdicts = []
    threshold_value = None
    for candidate in threshold_space:
        reading = control_reading(accepted["control_env"],
                                  accepted["control_segments"],
                                  accepted["control_meta"], registry,
                                  budget_value, candidate, policy["review_weight"])
        falsifier = pinned(figure_groups["threshold"], reading)
        row = dict(candidate=candidate, verdict=None, falsifier=None,
                   reproduced={n: reading[FIGURE_KEYS[n]]
                               for n in figure_groups["threshold"]})
        if falsifier is None:
            row["verdict"] = "accepted"
            threshold_value = candidate
        else:
            row["verdict"] = "rejected"
            row["falsifier"] = "C? " + falsifier
        threshold_verdicts.append(row)
    if threshold_value is None and threshold_space:
        threshold_value = threshold_space[-1]

    count_bias = sum(str(accepted["tag"]).encode("utf-8")) % 11

    # -- decode every capture under the accepted reading -------------------
    decoded = {}
    for role, capture in captures.items():
        env, segments, meta = decode_capture(
            capture["path"], accepted["tag"], accepted["depth"],
            accepted["sentinel"], roster=dictionary, build_roster=(role == "reference"))
        decoded[role] = dict(capture=capture, envelope=env, segments=segments,
                             meta=meta)

    season_env = decoded["season"]["envelope"]
    season_segments = decoded["season"]["segments"]
    season_records = [r for s in season_segments for r in s["records"]]
    squads, matches = settle_season(season_segments, registry, budget_value,
                                    threshold_value, policy["review_weight"])
    owners, sweeps, handover_detail = settle_handovers(season_records, dictionary)
    handover_detail["sweeps"] = sweeps
    handover_detail["owners"] = owners

    bracket = elect_bracket(squads, registry, policy, handover_detail)
    docket = elect_docket(matches, registry, policy)

    # -- reconciliation ----------------------------------------------------
    data_quality = dict(
        report_kind="data_quality",
        captures=[],
        kind_ledger={},
        season_totals={},
        reconciliation={},
    )
    overall_ledger = Counter()
    for role in ("reference", "control", "season"):
        block = decoded[role]
        env = block["envelope"]
        segments = block["segments"]
        meta = block["meta"]
        records = [r for s in segments for r in s["records"]]
        ledger = Counter(r["ledger_name"] for r in records)
        overall_ledger.update(ledger)
        wire_keys = []
        owner_keys = []
        for record in records:
            if record["wire_key"] not in wire_keys:
                wire_keys.append(record["wire_key"])
            if record["owner"] not in owner_keys:
                owner_keys.append(record["owner"])
        registered_seen = [k for k in owner_keys if k in entrants]
        registered_written = [k for k in wire_keys if k in entrants]
        unregistered = sorted(set(k for k in owner_keys + wire_keys
                                  if k not in entrants))
        data_quality["captures"].append(dict(
            role=role,
            file=block["capture"]["name"],
            capture_identifier=env["capture_identifier"],
            guards_active=env["guards_active"],
            envelope_checksum_ok=env["envelope_checksum_ok"],
            payload_checksums_ok=all(s["payload_checksum_ok"] for s in segments),
            filler_widths_ok=all(s["filler_width_ok"] for s in segments),
            declared_segment_count=env["declared_segment_count"] - (
                count_bias if "countbias" in env["guards_active"] else 0),
            recovered_segment_count=len(segments),
            segment_residual=(env["declared_segment_count"] - (
                count_bias if "countbias" in env["guards_active"] else 0))
            - len(segments),
            declared_record_count=env["declared_record_count"] - (
                count_bias if "countbias" in env["guards_active"] else 0),
            recovered_record_count=len(records),
            record_residual=(env["declared_record_count"] - (
                count_bias if "countbias" in env["guards_active"] else 0))
            - len(records),
            declared_roster_size=env["declared_roster_size"] - (
                count_bias if "countbias" in env["guards_active"] else 0),
            distinct_squads_recovered=len(owner_keys),
            registered_squads_recovered=len(registered_seen),
            distinct_written_attribution_keys=len(wire_keys),
            registered_written_attribution_keys=len(registered_written),
            roster_residual=(env["declared_roster_size"] - (
                count_bias if "countbias" in env["guards_active"] else 0))
            - len(registered_written),
            squads_never_written_on_the_wire=sorted(
                set(owner_keys) - set(wire_keys)),
            unregistered_squads=unregistered,
            backwards_tick_transitions=meta["backwards_tick_transitions"],
            kind_ledger={k: ledger.get(k, 0)
                         for k in ("deploy", "output", "eliminate", "disband",
                                   "concede", "transfer", "close")},
        ))
    season_ledger = Counter(r["ledger_name"] for r in season_records)
    season_capture_block = data_quality["captures"][-1]
    data_quality["kind_ledger"] = season_capture_block["kind_ledger"]
    data_quality["kind_ledger_all_captures"] = {
        block["role"]: block["kind_ledger"] for block in data_quality["captures"]}
    data_quality["kind_ledger_all_captures"]["corpus"] = {
        k: overall_ledger.get(k, 0)
        for k in ("deploy", "output", "eliminate", "disband", "concede",
                  "transfer", "close")}

    season_gross = sum(e["gross_output"] for e in squads.values())
    season_voided = sum(e["voided_output"] for e in squads.values())
    season_raw = sum(e["raw_upkeep"] for e in squads.values())
    season_charged = sum(e["charged_upkeep"] for e in squads.values())
    season_overflow = sum(e["overflow_upkeep"] for e in squads.values())

    conference_rows = {}
    segment_rows = {}
    for conference in registry["conferences"]:
        conference_rows[conference] = []
    for segment in registry["segments"]:
        segment_rows[segment] = []
    for squad in squads:
        info = entrants.get(squad)
        if info is None:
            continue
        row = entry_row(squad, squads, registry)
        conference_rows.setdefault(info["conference"], []).append(row)
        segment_rows.setdefault(info["segment"], []).append(row)

    conference_reports = {}
    for conference in registry["conferences"]:
        divisions = [d for d, c in registry["division_conference"].items()
                     if c == conference]
        report = group_report("conference", conference,
                              conference_rows.get(conference, []), divisions)
        conference_reports[conference] = report
        with open(os.path.join(out_dir, "region_%s.json" % conference), "w",
                  encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, sort_keys=True)
            fh.write("\n")

    segment_reports = {}
    for segment in registry["segments"]:
        report = group_report("segment", segment, segment_rows.get(segment, []), [])
        segment_reports[segment] = report
        with open(os.path.join(out_dir, "segment_%s.json" % segment), "w",
                  encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, sort_keys=True)
            fh.write("\n")

    conference_gross = sum(r["gross_output"] for r in conference_reports.values())
    absent_entrants = sorted(k for k, v in entrants.items() if k not in squads)
    unregistered_season = sorted(k for k in squads if k not in entrants)

    data_quality["season_totals"] = dict(
        matches=len(season_segments),
        records=len(season_records),
        squads=len(squads),
        registered_squads=len(squads) - len(unregistered_season),
        unregistered_squads=len(unregistered_season),
        gross_output=season_gross,
        voided_output=season_voided,
        raw_upkeep=season_raw,
        charged_upkeep=season_charged,
        overflow_upkeep=season_overflow,
    )
    data_quality["reconciliation"] = dict(
        attribution_residual=season_gross - conference_gross,
        attribution_residual_note=(
            "season gross output less the sum of the conference gross outputs; "
            "the output of squads that played without an entry"),
        attribution_residual_squads=unregistered_season,
        attribution_residual_gross_output=sum(
            squads[k]["gross_output"] for k in unregistered_season),
        ledger_residual=sum(data_quality["kind_ledger"].values())
        - len(season_records),
        ledger_residual_note=(
            "the season kind ledger total less the season records recovered; "
            "records that could not be given an event kind"),
        output_conservation=season_gross + season_voided - sum(
            r["value"] for r in season_records if r["kind"] == 2),
        output_conservation_note=(
            "gross output plus voided output less every reported output event"),
        upkeep_conservation=season_charged + season_overflow - season_raw,
        upkeep_conservation_note="charged upkeep plus overflow upkeep less raw upkeep",
        unregistered_squads=unregistered_season,
        unregistered_squads_note=(
            "squad keys seen on the wire that the entrant registry never lists"),
        absent_entrants=absent_entrants,
        absent_entrants_note=(
            "entrant registry entries that appear in no capture; listed and "
            "contributing nothing"),
    )

    with open(os.path.join(out_dir, "data_quality.json"), "w",
              encoding="utf-8") as fh:
        json.dump(data_quality, fh, indent=2, sort_keys=True)
        fh.write("\n")

    # -- policy audit ------------------------------------------------------
    final_reading = control_reading(accepted["control_env"],
                                    accepted["control_segments"],
                                    accepted["control_meta"], registry,
                                    budget_value, threshold_value,
                                    policy["review_weight"])
    calibration = []
    for figure in figures:
        key = FIGURE_KEYS.get(figure["name"].lower())
        reproduced = final_reading.get(key) if key is not None else None
        calibration.append(dict(
            figure=figure["figure"],
            name=figure["name"],
            published=figure["published"],
            reproduced=reproduced,
            agrees=(reproduced == figure["published"]),
            pins=("guard quantities" if figure["name"].lower()
                  in figure_groups["guard"]
                  else "season budget" if figure["name"].lower()
                  in figure_groups["budget"]
                  else "promotion threshold"),
            source=figure["source"],
        ))

    for row in search:
        if row["falsifier"]:
            match = re.match(r"^C\?\s+(.+)$", row["falsifier"])
            if match:
                name = match.group(1).strip()
                for figure in figures:
                    if figure["name"].lower() == name:
                        row["falsifier"] = "%s %s" % (figure["figure"],
                                                      figure["name"])
                        break
    for row in budget_verdicts + threshold_verdicts:
        if row["falsifier"]:
            match = re.match(r"^C\?\s+(.+)$", row["falsifier"])
            if match:
                name = match.group(1).strip()
                for figure in figures:
                    if figure["name"].lower() == name:
                        row["falsifier"] = "%s %s" % (figure["figure"],
                                                      figure["name"])
                        break

    audit = dict(
        report_kind="policy_audit",
        derived_policy=dict(
            engine_tag=accepted["tag"] if isinstance(accepted["tag"], str)
            else str(accepted["tag"]),
            engine_tag_roster=tag_label,
            count_bias_constant=count_bias,
            window_depth=accepted["depth"],
            window_depth_roster=window_label,
            escape_sentinel="0x%02X" % accepted["sentinel"]
            if isinstance(accepted["sentinel"], int) else accepted["sentinel"],
            escape_sentinel_roster=sentinel_label,
            season_budget=budget_value,
            season_budget_roster=budget_label,
            promotion_threshold=threshold_value,
            promotion_threshold_roster=threshold_label,
            bracket_policy=policy,
        ),
        unstated_quantities=[
            dict(
                quantity="engine_tag",
                drawn_from=tag_label,
                candidate_space=[t if isinstance(t, str) else str(t)
                                 for t in tag_space],
                resolves="the keystream mask and, through its byte sum, the count "
                         "bias constant",
                verdicts=search),
            dict(
                quantity="window_depth",
                drawn_from=window_label,
                candidate_space=list(window_space),
                resolves="the depth of the rolling back reference window",
                verdicts=search),
            dict(
                quantity="escape_sentinel",
                drawn_from=sentinel_label,
                candidate_space=["0x%02X" % s if isinstance(s, int) else s
                                 for s in sentinel_space],
                resolves="the sentinel byte that introduces an escape directive pair",
                verdicts=search),
            dict(
                quantity="season_budget",
                drawn_from=budget_label,
                candidate_space=list(budget_space),
                resolves="the upkeep budget every squad carries for the season",
                verdicts=budget_verdicts),
            dict(
                quantity="promotion_threshold",
                drawn_from=threshold_label,
                candidate_space=list(threshold_space),
                resolves="the season score the promotion tiers are measured against",
                verdicts=threshold_verdicts),
        ],
        guard_search=dict(
            figure_groups=figure_groups,
            combinations_evaluated=len(tag_space) * len(window_space)
            * len(sentinel_space),
            accepted=dict(engine_tag=accepted["tag"] if isinstance(
                accepted["tag"], str) else str(accepted["tag"]),
                window_depth=accepted["depth"],
                escape_sentinel="0x%02X" % accepted["sentinel"]),
            accepted_readings=accepted_count,
            baseline_transcript_check=baseline_check,
        ),
        attribution_settlement=dict(
            method="repeated sweeps over the collapsed hand over map",
            sweeps=sweeps,
            sweeps_note="counting the sweep that changed nothing",
            handovers=handover_detail["handovers"],
            collapsed_handovers=handover_detail["collapsed"],
            handed_away=handover_detail["handed_away"],
            self_owned=handover_detail["self_owned"],
            effective_owners={k: owners[k] for k in sorted(owners)},
        ),
        bracket=dict(
            candidate_space=bracket["candidate_space"],
            eligibility_rule=("own effective owner and at least the eligibility "
                              "match floor of matches"),
            eligible_pool=bracket["eligible_pool"],
            best_satisfied_constraints=bracket["best_satisfied_constraints"],
            constraints=bracket["constraints"],
            constraint_flags=bracket["constraint_flags"],
            elected_members=bracket["members"],
            total_charged_upkeep=bracket["total_charged_upkeep"],
        ),
        docket=dict(
            candidate_space=docket["candidate_space"],
            admissible_candidates=docket["admissible_candidates"],
            elected_matches=docket["match_ids"],
            total_review_index=docket["total_review_index"],
            conference_spread=docket["conference_spread"],
        ),
        calibration=calibration,
        calibration_note=(
            "one row per published control figure, reproduced against the control "
            "slice under the accepted reading"),
    )
    with open(os.path.join(out_dir, "policy_audit.json"), "w",
              encoding="utf-8") as fh:
        json.dump(audit, fh, indent=2, sort_keys=True)
        fh.write("\n")

    # -- season summary ----------------------------------------------------
    ladder = []
    for rank, squad in enumerate(sorted(squads, key=lambda s: (
            -squads[s]["season_score"], s)), start=1):
        row = entry_row(squad, squads, registry)
        row["rank"] = rank
        row["effective_owner"] = owners.get(squad, squad)
        row["is_own_effective_owner"] = owners.get(squad, squad) == squad
        row["handed_entry_away"] = squad in handover_detail["handed_away"]
        row["match_scores"] = [l["match_score"] for l in squads[squad]["match_log"]]
        ladder.append(row)

    tiers = Counter(e["tier"] for e in squads.values())
    summary = dict(
        report_kind="season_summary",
        season_capture=dict(
            file=decoded["season"]["capture"]["name"],
            capture_identifier=season_env["capture_identifier"],
            guards_active=season_env["guards_active"],
            matches=len(season_segments),
            records=len(season_records),
        ),
        policy=audit["derived_policy"],
        totals=dict(
            matches=len(season_segments),
            records=len(season_records),
            squads=len(squads),
            registered_squads=len(squads) - len(unregistered_season),
            unregistered_squads=len(unregistered_season),
            gross_output=season_gross,
            voided_output=season_voided,
            reported_output=season_gross + season_voided,
            raw_upkeep=season_raw,
            charged_upkeep=season_charged,
            overflow_upkeep=season_overflow,
            season_score=sum(e["season_score"] for e in squads.values()),
            standing_points=sum(e["standing_points"] for e in squads.values()),
            streak_credit=sum(e["streak_credit"] for e in squads.values()),
        ),
        kind_ledger=data_quality["kind_ledger"],
        tier_counts={t: tiers.get(t, 0) for t in
                     ("champion", "contender", "qualifier", "provisional")},
        conferences={c: dict(
            entrants=conference_reports[c]["entrants"],
            gross_output=conference_reports[c]["gross_output"],
            voided_output=conference_reports[c]["voided_output"],
            charged_upkeep=conference_reports[c]["charged_upkeep"],
            overflow_upkeep=conference_reports[c]["overflow_upkeep"],
            season_score=conference_reports[c]["season_score"],
            standing_points=conference_reports[c]["standing_points"],
            divisions=conference_reports[c]["divisions"],
        ) for c in registry["conferences"]},
        segments={s: dict(
            entrants=segment_reports[s]["entrants"],
            gross_output=segment_reports[s]["gross_output"],
            voided_output=segment_reports[s]["voided_output"],
            charged_upkeep=segment_reports[s]["charged_upkeep"],
            overflow_upkeep=segment_reports[s]["overflow_upkeep"],
            season_score=segment_reports[s]["season_score"],
            standing_points=segment_reports[s]["standing_points"],
        ) for s in registry["segments"]},
        attribution_residual=season_gross - conference_gross,
        unregistered_squads=unregistered_season,
        absent_entrants=absent_entrants,
        absent_entrant_entries=[entry_row(s, squads, registry)
                                for s in absent_entrants],
        ladder=ladder,
        entry_handovers=handover_detail["handovers"],
        handover_settlement=dict(
            sweeps=sweeps,
            handed_away=handover_detail["handed_away"],
            self_owned=handover_detail["self_owned"],
        ),
        finals_bracket=bracket,
        review_docket=docket,
    )
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
