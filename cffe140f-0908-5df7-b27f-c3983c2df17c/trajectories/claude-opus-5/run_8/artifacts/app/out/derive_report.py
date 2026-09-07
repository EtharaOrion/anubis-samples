#!/usr/bin/env python3
"""Settle a sealed tactics-league season from a directory of replay journals.

Usage:  python3 derive_report.py DATA_DIR OUT_DIR

Standard library only.  Reads nothing outside DATA_DIR, writes nothing outside
OUT_DIR.  Both directories come from argv; neither is hard coded.

The program

  * decodes the reference (baseline) capture and, when a labelled transcript
    ships beside it, confirms a byte-exact agreement,
  * searches the published candidate rosters for the three guard quantities
    (engine tag, back-reference window depth, escape sentinel), then for the
    season budget and the promotion threshold, pinning each against the
    published control figures and recording a falsifier for every rejection,
  * settles attribution (escape directives, then entry hand-overs to a fixpoint),
  * scores, tiers and ranks every squad, elects the finals bracket and the
    review docket, and
  * writes summary / data-quality / policy-audit reports plus one report per
    conference and per entrant segment.
"""

import csv
import glob
import hashlib
import itertools
import json
import math
import os
import re
import struct
import sys

# --------------------------------------------------------------------------
# document parsing
# --------------------------------------------------------------------------

KIND_NAMES = {1: "deploy", 2: "output", 3: "eliminate", 4: "disband",
              5: "concede", 6: "transfer", 8: "close"}

GUARD_BITS = {"keystream": 0, "chain": 1, "delta2": 2, "driftkey": 3,
              "segperm": 4, "backref": 5, "endian": 6, "countbias": 7,
              "shadow": 8, "rotate": 9, "alias": 10, "escape": 11}


def read_text(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def load_documents(data_dir):
    docs = {}
    for path in sorted(glob.glob(os.path.join(data_dir, "*.md"))):
        docs[os.path.basename(path)] = read_text(path)
    return docs


def parse_rosters(docs):
    """`Engine tag roster: `a`, `b`, ...` -> {'engine tag': ['a', 'b', ...]}"""
    rosters = {}
    pat = re.compile(r"^(.*?)\s+roster:\s*(.+?)\s*$", re.M | re.I)
    for text in docs.values():
        for label, body in pat.findall(text):
            items = re.findall(r"`([^`]+)`", body)
            if not items:
                continue
            key = re.sub(r"[^a-z0-9]+", "_", label.strip().lower()).strip("_")
            rosters.setdefault(key, items)
    return rosters


def parse_scalars(docs):
    """`Docket size: `5`` -> {'docket_size': 5}"""
    out = {}
    pat = re.compile(r"^([A-Za-z][A-Za-z ]*?):\s*`?(-?\d+)`?\s*$", re.M)
    for text in docs.values():
        for label, value in pat.findall(text):
            key = re.sub(r"[^a-z0-9]+", "_", label.strip().lower()).strip("_")
            out.setdefault(key, int(value))
    return out


def parse_controls(docs):
    """`- C1 matches opened: 5` -> ordered list of (id, label, value)."""
    controls = []
    pat = re.compile(r"^\s*[-*]?\s*(C\d+)\s+(.+?):\s*(-?\d+)\s*$", re.M)
    for text in docs.values():
        for cid, label, value in pat.findall(text):
            controls.append({"id": cid, "label": label.strip(),
                             "published": int(value)})
    controls.sort(key=lambda c: int(c["id"][1:]))
    seen, uniq = set(), []
    for c in controls:
        if c["id"] not in seen:
            seen.add(c["id"])
            uniq.append(c)
    return uniq


def parse_int(text):
    text = text.strip()
    return int(text, 16) if text.lower().startswith("0x") else int(text)


def load_tables(data_dir):
    registry, topology, licences = {}, {}, {}
    for path in sorted(glob.glob(os.path.join(data_dir, "*.csv"))):
        with open(path, newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        if not rows:
            continue
        cols = set(rows[0])
        if {"squad_key", "division", "licence_class"} <= cols:
            for r in rows:
                registry[r["squad_key"]] = {
                    "entrant_id": r.get("entrant_id", ""),
                    "division": r["division"],
                    "licence_class": r["licence_class"],
                    "is_ranked": str(r.get("is_ranked", "")).strip().lower()
                                 in ("yes", "true", "1", "y"),
                }
        elif {"division", "conference", "ladder_weight"} <= cols:
            for r in rows:
                topology[r["division"]] = {
                    "conference": r["conference"],
                    "ladder_weight": int(r["ladder_weight"]),
                }
        elif {"licence_class", "segment", "levy_rate"} <= cols:
            for r in rows:
                licences[r["licence_class"]] = {
                    "segment": r["segment"],
                    "levy_rate": int(r["levy_rate"]),
                }
    return registry, topology, licences


# --------------------------------------------------------------------------
# journal decoding
# --------------------------------------------------------------------------

class DecodeError(Exception):
    pass


def read_envelope(blob):
    e = blob[:36]
    if len(e) < 36 or e[:4] != b"RJNL":
        raise DecodeError("not a journal")
    return {
        "version": e[4],
        "reserved": e[5],
        "guard_register": struct.unpack("<H", e[6:8])[0],
        "nonce_le": struct.unpack("<I", e[8:12])[0],
        "nonce_be": struct.unpack(">I", e[8:12])[0],
        "declared_segment_count_raw": struct.unpack("<I", e[12:16])[0],
        "declared_record_count_raw": struct.unpack("<I", e[16:20])[0],
        "season_base_tick": struct.unpack("<I", e[20:24])[0],
        "capture_id": struct.unpack("<I", e[24:28])[0],
        "declared_roster_size_raw": struct.unpack("<I", e[28:32])[0],
        "checksum": struct.unpack("<I", e[32:36])[0],
        "checksum_ok": sum(e[:32]) % 2 ** 32 == struct.unpack("<I", e[32:36])[0],
    }


def read_segments(blob):
    off, out = 36, []
    while off < len(blob):
        head = blob[off:off + 28]
        if len(head) < 28 or head[:4] != b"SEG0":
            raise DecodeError("bad segment marker at %d" % off)
        slot, mid, rc, salt, width, cks = struct.unpack("<IIIIII", head[4:28])
        payload = blob[off + 28:off + 28 + width]
        if len(payload) != width:
            raise DecodeError("truncated segment payload")
        out.append({"slot": slot, "match_id": mid, "declared_records_raw": rc,
                    "salt": salt, "payload_width": width, "checksum": cks,
                    "checksum_ok": sum(payload) % 2 ** 32 == cks,
                    "payload": payload})
        off += 28 + width
    return out


def _keystream(tag, match_id, salt, ordinal, width):
    seed = ("%s:%d:%d:%d" % (tag, match_id, salt, ordinal)).encode()
    out = hashlib.sha256(seed).digest()
    while len(out) < width:                      # spec truncates; extend safely
        out += hashlib.sha256(out).digest()
    return out[:width]


def _rotl(data, n):
    n %= len(data)
    return data[n:] + data[:n]


def logical_order(nonce, nsegments):
    """Storage slot -> logical position, inverted into a read order."""
    step = ((nonce >> 8) % nsegments) + 2
    while math.gcd(step, nsegments) != 1:
        step += 1
    logical = {i: (nonce + i * step) % nsegments for i in range(nsegments)}
    return [slot for slot, _ in sorted(logical.items(), key=lambda kv: kv[1])], step


def decode_capture(blob, tag, depth, sentinel, dictionary, grow_dictionary=False):
    """Decode one capture into a list of records in logical order."""
    env = read_envelope(blob)
    reg = env["guard_register"]

    def on(name):
        return bool((reg >> GUARD_BITS[name]) & 1)

    bias = (sum(tag.encode()) % 11) if on("countbias") else 0
    nonce = env["nonce_be"] if on("endian") else env["nonce_le"]
    segments = read_segments(blob)
    nseg = len(segments)
    if nseg == 0:
        raise DecodeError("no segments")

    if on("segperm"):
        order, step = logical_order(nonce, nseg)
    else:
        order, step = list(range(nseg)), None

    dictionary = list(dictionary)
    records = []
    window = []
    running_value_sum = 0
    prev_tick = None
    global_ordinal = 0
    backwards_ticks = 0

    for logical_pos, slot in enumerate(order):
        seg = segments[slot]
        count = seg["declared_records_raw"] - bias
        if count < 0:
            raise DecodeError("negative segment record count")
        if on("driftkey"):
            eff_salt = (seg["salt"] ^ running_value_sum) & 0xFFFFFFFF
        else:
            eff_salt = seg["salt"]
        payload = seg["payload"]
        pos = 0
        first_diff = 0
        tick_acc = 0
        seg_value_sum = 0
        last_tick = None
        for seg_ordinal in range(count):
            if pos + 2 > len(payload):
                raise DecodeError("record frame overruns payload")
            visible, shadow = payload[pos], payload[pos + 1]
            width = (shadow ^ (eff_salt & 0xFF)) if on("shadow") else visible
            if width <= 0 or pos + 2 + width > len(payload):
                raise DecodeError("implausible record width %d" % width)
            body = payload[pos + 2:pos + 2 + width]
            pos += 2 + width

            if on("keystream"):
                ks = _keystream(tag, seg["match_id"], eff_salt, seg_ordinal, width)
                body = bytes(a ^ b for a, b in zip(body, ks))
            if on("rotate"):
                turn = (eff_salt + global_ordinal) % width
                body = _rotl(body, width - turn)

            directives = []
            cut = 0
            if on("escape"):
                while cut < len(body) and body[cut] == sentinel and len(directives) < 2:
                    if cut + 1 >= len(body):
                        raise DecodeError("truncated escape directive")
                    d = body[cut + 1]
                    directives.append((d >> 4, d & 0x0F))
                    cut += 2
            blob_body = body[cut:]
            if len(blob_body) < 2:
                raise DecodeError("record body too short")
            kind, mode = blob_body[0], blob_body[1]
            if mode == 0:
                if len(blob_body) != 15:
                    raise DecodeError("bad mode-0 body width %d" % len(blob_body))
                raw_key = blob_body[2:5]
                if not all(0x41 <= c <= 0x5A for c in raw_key):
                    raise DecodeError("non-alphabetic squad key")
                key = raw_key.decode("ascii")
                tail = blob_body[5:]
            elif mode in (1, 2):
                if len(blob_body) != 13:
                    raise DecodeError("bad mode-%d body width %d" % (mode, len(blob_body)))
                slot_byte = blob_body[2]
                tail = blob_body[3:]
                if mode == 1:
                    if slot_byte >= len(window):
                        raise DecodeError("back reference out of window")
                    key = window[slot_byte]
                else:
                    if not dictionary:
                        raise DecodeError("alias with empty roster dictionary")
                    key = dictionary[slot_byte % len(dictionary)]
            else:
                raise DecodeError("unknown attribution mode %d" % mode)
            if kind not in KIND_NAMES:
                raise DecodeError("unknown event kind %d" % kind)
            unit, tick_field, value_field = struct.unpack("<HII", tail[:10])

            if grow_dictionary and key not in dictionary:
                dictionary.append(key)

            # the rolling window is refreshed with the key as written on the
            # wire, before any escape directive is weighed
            if on("backref"):
                if key in window:
                    window.remove(key)
                window.append(key)
                while len(window) > depth:
                    window.pop(0)

            effective = key
            for op, operand in directives:
                if op == 1:
                    if dictionary:
                        effective = dictionary[operand % len(dictionary)]
                elif op == 2:
                    if effective in dictionary:
                        idx = dictionary.index(effective)
                        effective = dictionary[(idx + operand) % len(dictionary)]
                    # a key outside the dictionary holds no position to advance
                    # from, so operation 2 leaves it where it is
                else:
                    raise DecodeError("unknown escape operation %d" % op)

            if on("delta2"):
                first_diff = (first_diff + tick_field) & 0xFFFFFFFF
                tick_acc = (tick_acc + first_diff) & 0xFFFFFFFF
                tick = tick_acc
            else:
                tick = tick_field

            if on("chain"):
                mask = nonce if global_ordinal == 0 else prev_tick
                value = value_field ^ mask
            else:
                value = value_field
            prev_tick = tick
            seg_value_sum = (seg_value_sum + value) & 0xFFFFFFFF

            if last_tick is not None and tick < last_tick:
                backwards_ticks += 1
            last_tick = tick

            records.append({
                "kind": kind, "logical_segment": logical_pos,
                "match_id": seg["match_id"], "record_ordinal": global_ordinal,
                "segment_ordinal": seg_ordinal, "squad_key": effective,
                "wire_key": key, "tick": tick, "unit": unit, "value": value,
                "mode": mode, "escaped": bool(directives),
                "directives": directives,
            })
            global_ordinal += 1

        filler = (seg["salt"] % 5) + 3
        if len(payload) - pos != filler:
            raise DecodeError("filler width mismatch (%d, expected %d)"
                              % (len(payload) - pos, filler))
        running_value_sum = (running_value_sum + seg_value_sum) & 0xFFFFFFFF

    meta = {
        "guard_register": reg,
        "guards_active": sorted(n for n in GUARD_BITS if on(n)),
        "count_bias": bias,
        "nonce": nonce,
        "segment_step": step,
        "declared_segment_count": env["declared_segment_count_raw"] - bias,
        "declared_record_count": env["declared_record_count_raw"] - bias,
        "declared_roster_size": env["declared_roster_size_raw"] - bias,
        "recovered_segment_count": nseg,
        "recovered_record_count": len(records),
        "backwards_tick_events": backwards_ticks,
        "envelope_checksum_ok": env["checksum_ok"],
        "segment_checksums_ok": all(s["checksum_ok"] for s in segments),
        "segment_order": [segments[s]["match_id"] for s in order],
        "season_base_tick": env["season_base_tick"],
        "capture_id": env["capture_id"],
    }
    return env, records, meta, dictionary


# --------------------------------------------------------------------------
# settlement
# --------------------------------------------------------------------------

def match_tables(records):
    """Per match: per-squad gross / voided output and raw upkeep."""
    by_match = {}
    for r in records:
        by_match.setdefault(r["match_id"], []).append(r)
    ordered = sorted(by_match, key=lambda m: min(x["logical_segment"]
                                                 for x in by_match[m]))
    out = []
    for mid in ordered:
        rs = by_match[mid]
        closes = [r["tick"] for r in rs if r["kind"] == 8]
        close_tick = max(closes) if closes else max(r["tick"] for r in rs)
        elim, disband, concede = {}, {}, {}
        for r in rs:
            key = (r["squad_key"], r["unit"])
            if r["kind"] == 3:
                elim[key] = min(elim.get(key, r["tick"]), r["tick"])
            elif r["kind"] == 4:
                disband[key] = min(disband.get(key, r["tick"]), r["tick"])
            elif r["kind"] == 5:
                s = r["squad_key"]
                concede[s] = min(concede.get(s, r["tick"]), r["tick"])
        stats = {}

        def slot(s):
            return stats.setdefault(s, {"gross": 0, "voided": 0, "raw": 0,
                                        "records": 0})
        for r in rs:
            st = slot(r["squad_key"])
            st["records"] += 1
            if r["kind"] == 2:
                key = (r["squad_key"], r["unit"])
                dead = key in elim and r["tick"] >= elim[key]
                gone = (r["squad_key"] in concede
                        and r["tick"] >= concede[r["squad_key"]])
                if dead or gone:
                    st["voided"] += r["value"]
                else:
                    st["gross"] += r["value"]
            elif r["kind"] == 1:
                key = (r["squad_key"], r["unit"])
                end = disband.get(key, close_tick)
                if end > close_tick:
                    end = close_tick
                if end > r["tick"]:
                    st["raw"] += r["value"] * (end - r["tick"])
        out.append({
            "match_id": mid,
            "logical_position": min(x["logical_segment"] for x in rs),
            "close_tick": close_tick,
            "squads": stats,
            "conceded": set(concede),
            "records": len(rs),
            "escape_records": sum(1 for r in rs if r["escaped"]),
            "voided_output": sum(v["voided"] for v in stats.values()),
            "gross_output": sum(v["gross"] for v in stats.values()),
        })
    return out


def settle_owners(records, dictionary):
    """Follow entry hand-overs to a fixpoint.  Returns owners and pass count."""
    hands = {}
    for r in records:                                  # last one read stands
        if r["kind"] == 6 and dictionary:
            hands[r["squad_key"]] = dictionary[r["value"] % len(dictionary)]
    squads = sorted({r["squad_key"] for r in records} | set(hands.values()))
    step = {s: hands.get(s, s) for s in squads}

    # collapse every closed set (cycle) onto its first-sorting member
    colour, cycles = {}, []
    for s in squads:
        if s in colour:
            continue
        path, node = [], s
        while node not in colour and node not in path:
            path.append(node)
            node = step.get(node, node)
        if node in path:                               # found a cycle
            cyc = path[path.index(node):]
            if len(cyc) > 1 or step.get(node, node) == node:
                cycles.append(sorted(cyc))
                winner = min(cyc)
                for m in cyc:
                    step[m] = winner
        for n in path:
            colour[n] = True

    owners = {s: s for s in squads}
    passes = 0
    while True:
        passes += 1
        nxt = {s: step.get(owners[s], owners[s]) for s in squads}
        if nxt == owners:
            break
        owners = nxt
        if passes > 4 * len(squads) + 8:               # cannot happen
            raise DecodeError("hand-over settlement did not converge")
    return owners, passes, hands, [c for c in cycles if len(c) > 1]


def build_season(records, registry, topology, licences, budget, threshold):
    """Score, tier and rank every squad seen on the wire."""
    matches = match_tables(records)

    def weight_levy(squad):
        info = registry.get(squad)
        if not info:
            return 1, 1
        w = topology.get(info["division"], {}).get("ladder_weight", 1)
        lv = licences.get(info["licence_class"], {}).get("levy_rate", 1)
        return w, lv

    squads = sorted({r["squad_key"] for r in records})
    remaining = {s: budget for s in squads}
    acc = {s: {"gross": 0, "voided": 0, "raw": 0, "charged": 0, "overflow": 0,
               "matches": 0, "match_awards": 0, "streak": 0, "score": 0,
               "wins": []} for s in squads}

    for m in matches:
        scores = {}
        for s, st in sorted(m["squads"].items()):
            w, lv = weight_levy(s)
            charged = min(st["raw"], remaining[s])
            remaining[s] -= charged
            overflow = st["raw"] - charged
            st["charged"] = charged
            st["overflow"] = overflow
            st["score"] = st["gross"] * w - charged * lv
            scores[s] = st["score"]
            a = acc[s]
            a["gross"] += st["gross"]
            a["voided"] += st["voided"]
            a["raw"] += st["raw"]
            a["charged"] += charged
            a["overflow"] += overflow
            a["matches"] += 1
            a["score"] += st["score"]
        contenders = {s: v for s, v in scores.items() if s not in m["conceded"]}
        m["match_scores"] = scores
        m["awards"] = {}
        if contenders:
            top = max(contenders.values())
            winners = sorted(s for s, v in contenders.items() if v == top)
            points = 3 if len(winners) == 1 else 1
            for s in winners:
                acc[s]["match_awards"] += points
                m["awards"][s] = points
            for s in m["squads"]:
                outright = (len(winners) == 1 and s == winners[0])
                acc[s]["wins"].append(outright)
        else:
            for s in m["squads"]:
                acc[s]["wins"].append(False)

    for s, a in acc.items():
        run = 0
        credit = 0
        for won in a["wins"]:
            if won:
                run += 1
                if run > 1:
                    credit += 2
            else:
                run = 0
        a["streak"] = credit
        a["standing_points"] = a["match_awards"] + credit
        del a["wins"]

    def tier_of(score):
        if score >= 3 * threshold:
            return "champion"
        if score >= 2 * threshold:
            return "contender"
        if score >= threshold:
            return "qualifier"
        return "provisional"

    for s, a in acc.items():
        a["tier"] = tier_of(a["score"])
    return matches, acc


def elect_bracket(pool, stats, meta, scalars, segments_all):
    """Return the bracket that satisfies the most published wishes."""
    prominence = scalars.get("ladder_weight_prominence", 0)
    sp_floor = scalars.get("standing_points_floor", 0)
    weight_floor = scalars.get("combined_ladder_weight_floor", 0)
    gross_floor = scalars.get("combined_gross_output_floor", 0)
    size = 3

    def checks(members):
        w = [meta[s]["ladder_weight"] for s in members]
        confs = [meta[s]["conference"] for s in members]
        segs = {meta[s]["segment"] for s in members if meta[s]["segment"]}
        named = [c for c in confs if c]
        return [
            ("prominent_division", any(x >= prominence for x in w)),
            ("both_segments_present", segs >= set(segments_all)),
            ("standing_points_floor",
             all(stats[s]["standing_points"] >= sp_floor for s in members)),
            ("distinct_conferences",
             len(set(named)) == len(named) and len(named) == len(members)),
            ("ranked_entrant_present", any(meta[s]["is_ranked"] for s in members)),
            ("combined_ladder_weight", sum(w) >= weight_floor),
            ("no_overflow_upkeep",
             all(stats[s]["overflow"] == 0 for s in members)),
            ("champion_present",
             any(stats[s]["tier"] == "champion" for s in members)),
            ("combined_gross_output",
             sum(stats[s]["gross"] for s in members) >= gross_floor),
            ("no_handed_entry", all(not meta[s]["handed_away"] for s in members)),
        ]

    best = None
    considered = 0
    best_count = 0
    for combo in itertools.combinations(sorted(pool), size):
        considered += 1
        res = checks(combo)
        n = sum(1 for _, ok in res if ok)
        best_count = max(best_count, n)
        upkeep = sum(stats[s]["charged"] for s in combo)
        key = (-n, upkeep, list(combo))
        if best is None or key < best[0]:
            best = (key, combo, res, n, upkeep)
    if best is None:
        return {"members": [], "satisfied_count": 0, "constraints": [],
                "candidate_space": 0, "best_satisfied_count": 0,
                "combined_charged_upkeep": 0, "pool": sorted(pool)}
    _, combo, res, n, upkeep = best
    return {
        "members": list(combo),
        "satisfied_count": n,
        "constraint_count": len(res),
        "constraints": [{"constraint": c, "satisfied": ok} for c, ok in res],
        "candidate_space": considered,
        "best_satisfied_count": best_count,
        "combined_charged_upkeep": upkeep,
        "pool": sorted(pool),
        "pool_size": len(pool),
    }


def elect_docket(matches, registry, topology, scalars):
    weight = scalars.get("review_weight", 0)
    size = scalars.get("docket_size", 0)
    cap = scalars.get("docket_conference_cap", size)

    rows = []
    for m in matches:
        best_key, best_gross = None, None
        for s in sorted(m["squads"]):
            if s not in registry:
                continue
            g = m["squads"][s]["gross"]
            if best_gross is None or g > best_gross:
                best_gross, best_key = g, s
        conf = None
        if best_key is not None:
            conf = topology.get(registry[best_key]["division"], {}).get("conference")
        idx = m["voided_output"] + weight * m["escape_records"]
        rows.append({"match_id": m["match_id"], "conference": conf,
                     "voided_output": m["voided_output"],
                     "escape_records": m["escape_records"],
                     "review_index": idx,
                     "leading_squad": best_key})

    groups = {}
    for r in rows:
        groups.setdefault(r["conference"], []).append(r)
    keys = sorted(groups, key=lambda c: (c is None, c))
    for k in keys:
        groups[k].sort(key=lambda r: (-r["review_index"], r["match_id"]))

    size = min(size, len(rows))
    best = None
    space = 0
    for counts in itertools.product(*[range(0, min(cap, len(groups[k])) + 1)
                                      for k in keys]):
        if sum(counts) != size:
            continue
        space += 1
        picked = []
        for k, n in zip(keys, counts):
            picked.extend(groups[k][:n])
        total = sum(r["review_index"] for r in picked)
        ids = sorted(r["match_id"] for r in picked)
        key = (-total, ids)
        if best is None or key < best[0]:
            best = (key, picked, total)
    if best is None:
        # the cap cannot be honoured for a docket of this size; fall back to
        # the plain best-index selection so the docket is still elected
        picked = sorted(rows, key=lambda r: (-r["review_index"],
                                             r["match_id"]))[:size]
        best = ((0, []), picked, sum(r["review_index"] for r in picked))
    _, picked, total = best
    picked.sort(key=lambda r: r["match_id"])
    spread = {}
    for r in picked:
        name = r["conference"] if r["conference"] else "unassigned"
        spread[name] = spread.get(name, 0) + 1
    return {"matches": picked, "total_review_index": total,
            "conference_spread": dict(sorted(spread.items())),
            "candidate_space": space,
            "all_matches": sorted(rows, key=lambda r: r["match_id"])}


# --------------------------------------------------------------------------
# calibration
# --------------------------------------------------------------------------

def control_figures(records, meta, registry, topology, licences,
                    budget=None, threshold=None):
    """Reproduce the published control figures from a decoded capture."""
    fig = {}
    fig["matches opened"] = meta["recovered_segment_count"]
    fig["records recovered"] = len(records)
    fig["distinct squads"] = len({r["squad_key"] for r in records})
    matches = match_tables(records)
    fig["gross output"] = sum(m["gross_output"] for m in matches)
    fig["voided output"] = sum(m["voided_output"] for m in matches)
    fig["raw upkeep"] = sum(st["raw"] for m in matches
                            for st in m["squads"].values())
    if budget is not None:
        _, acc = build_season(records, registry, topology, licences,
                              budget, threshold if threshold else 1)
        fig["charged upkeep"] = sum(a["charged"] for a in acc.values())
        fig["overflow upkeep"] = sum(a["overflow"] for a in acc.values())
        if threshold is not None:
            counts = {"champion": 0, "contender": 0, "qualifier": 0,
                      "provisional": 0}
            for a in acc.values():
                counts[a["tier"]] += 1
            fig["champion entrants"] = counts["champion"]
            fig["contender entrants"] = counts["contender"]
            fig["qualifier entrants"] = counts["qualifier"]
            fig["provisional entrants"] = counts["provisional"]
    return fig


def figure_lookup(controls, keywords):
    for c in controls:
        if all(k in c["label"].lower() for k in keywords):
            return c
    return None


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def find_captures(data_dir):
    caps = {}
    for path in sorted(glob.glob(os.path.join(data_dir, "*"))):
        if not os.path.isfile(path):
            continue
        with open(path, "rb") as fh:
            head = fh.read(4)
        if head != b"RJNL":
            continue
        with open(path, "rb") as fh:
            blob = fh.read()
        env = read_envelope(blob)
        role = {1: "reference", 2: "control", 3: "season"}.get(
            env["capture_id"] % 10)
        if role and role not in caps:
            caps[role] = {"path": path, "name": os.path.basename(path),
                          "blob": blob, "envelope": env}
    return caps


def json_dump(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, sort_keys=True)
        fh.write("\n")


def main(argv):
    if len(argv) != 3:
        sys.stderr.write("usage: %s DATA_DIR OUT_DIR\n" % argv[0])
        return 2
    data_dir, out_dir = os.path.abspath(argv[1]), os.path.abspath(argv[2])
    os.makedirs(out_dir, exist_ok=True)

    docs = load_documents(data_dir)
    rosters = parse_rosters(docs)
    scalars = parse_scalars(docs)
    controls = parse_controls(docs)
    registry, topology, licences = load_tables(data_dir)

    tag_space = rosters.get("engine_tag", [""])
    depth_space = [parse_int(x) for x in rosters.get("window_depth", ["0"])]
    sentinel_space = [parse_int(x) for x in rosters.get("sentinel", ["0"])]
    budget_space = [parse_int(x) for x in rosters.get("budget_cap", ["0"])]
    threshold_space = [parse_int(x) for x in rosters.get("promotion_threshold", ["1"])]

    caps = find_captures(data_dir)
    for role in ("reference", "control", "season"):
        if role not in caps:
            raise SystemExit("missing %s capture in %s" % (role, data_dir))

    audit = {"unstated_quantities": {}, "notes": []}

    # ---- baseline decode and transcript check --------------------------
    ref = caps["reference"]
    base_env, base_recs, base_meta, base_dict = decode_capture(
        ref["blob"], tag_space[0], depth_space[0], sentinel_space[0],
        [], grow_dictionary=True)
    transcript_check = {"transcript_found": False}
    for path in sorted(glob.glob(os.path.join(data_dir, "*.jsonl"))):
        try:
            expected = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
        except Exception:
            continue
        if not expected or "squad_key" not in expected[0]:
            continue
        fields = sorted(expected[0])
        actual = [{k: r[k] for k in fields if k in r} for r in base_recs]
        transcript_check = {
            "transcript_found": True,
            "transcript": os.path.basename(path),
            "declared_records": len(expected),
            "decoded_records": len(base_recs),
            "byte_exact": actual == expected,
        }
        break

    # ---- pin the guard quantities --------------------------------------
    c_matches = figure_lookup(controls, ["matches"])
    c_records = figure_lookup(controls, ["records"])
    c_squads = figure_lookup(controls, ["distinct", "squads"])
    c_gross = figure_lookup(controls, ["gross", "output"])
    c_voided = figure_lookup(controls, ["voided"])
    c_raw = figure_lookup(controls, ["raw", "upkeep"])
    guard_controls = [c for c in (c_matches, c_records, c_squads, c_gross,
                                  c_voided, c_raw) if c]

    ctrl_blob = caps["control"]["blob"]
    guard_candidates = []
    winner = None
    for tag, depth, sentinel in itertools.product(tag_space, depth_space,
                                                  sentinel_space):
        entry = {"engine_tag": tag, "window_depth": depth,
                 "sentinel": "0x%02X" % sentinel, "verdict": "rejected",
                 "falsifier": None, "reproduced": {}}
        try:
            _, recs, meta, dictionary = decode_capture(
                ref["blob"], tag, depth, sentinel, [], grow_dictionary=True)
            _, crecs, cmeta, _ = decode_capture(
                ctrl_blob, tag, depth, sentinel, dictionary)
            fig = control_figures(crecs, cmeta, registry, topology, licences)
            entry["reproduced"] = fig
            bad = None
            for c in guard_controls:
                key = c["label"].lower()
                got = fig.get(key)
                if got != c["published"]:
                    bad = (c, got)
                    break
            if bad is None:
                entry["verdict"] = "accepted"
            else:
                entry["falsifier"] = {
                    "control": bad[0]["id"], "figure": bad[0]["label"],
                    "published": bad[0]["published"], "reproduced": bad[1],
                }
        except DecodeError as exc:
            entry["decode_error"] = str(exc)
            ref_c = c_records or (controls[0] if controls else None)
            entry["falsifier"] = {
                "control": ref_c["id"] if ref_c else None,
                "figure": ref_c["label"] if ref_c else None,
                "published": ref_c["published"] if ref_c else None,
                "reproduced": None,
                "detail": "capture would not decode: %s" % exc,
            }
        guard_candidates.append(entry)
        if entry["verdict"] == "accepted" and winner is None:
            winner = (tag, depth, sentinel, dictionary)
    if winner is None:
        raise SystemExit("no guard reading reproduces the control figures")
    tag, depth, sentinel, dictionary = winner

    audit["unstated_quantities"]["engine_tag"] = {
        "candidate_space": list(tag_space), "pinned": tag}
    audit["unstated_quantities"]["window_depth"] = {
        "candidate_space": list(depth_space), "pinned": depth}
    audit["unstated_quantities"]["escape_sentinel"] = {
        "candidate_space": ["0x%02X" % s for s in sentinel_space],
        "pinned": "0x%02X" % sentinel}
    audit["guard_candidate_verdicts"] = guard_candidates

    _, ctrl_recs, ctrl_meta, _ = decode_capture(ctrl_blob, tag, depth,
                                                sentinel, dictionary)

    # ---- pin the season budget -----------------------------------------
    c_charged = figure_lookup(controls, ["charged", "upkeep"])
    c_overflow = figure_lookup(controls, ["overflow", "upkeep"])
    budget_controls = [c for c in (c_charged, c_overflow) if c]
    budget_candidates = []
    budget = None
    for cand in budget_space:
        fig = control_figures(ctrl_recs, ctrl_meta, registry, topology,
                              licences, budget=cand)
        entry = {"candidate": cand, "verdict": "rejected", "falsifier": None,
                 "reproduced": {c["label"]: fig.get(c["label"].lower())
                                for c in budget_controls}}
        bad = None
        for c in budget_controls:
            got = fig.get(c["label"].lower())
            if got != c["published"]:
                bad = (c, got)
                break
        if bad is None:
            entry["verdict"] = "accepted"
            if budget is None:
                budget = cand
        else:
            entry["falsifier"] = {"control": bad[0]["id"],
                                  "figure": bad[0]["label"],
                                  "published": bad[0]["published"],
                                  "reproduced": bad[1]}
        budget_candidates.append(entry)
    if budget is None:
        raise SystemExit("no budget reproduces the control upkeep figures")
    audit["unstated_quantities"]["season_budget"] = {
        "candidate_space": list(budget_space), "pinned": budget}
    audit["budget_candidate_verdicts"] = budget_candidates

    # ---- pin the promotion threshold -----------------------------------
    tier_controls = [c for c in controls
                     if c["label"].lower().endswith("entrants")]
    threshold_candidates = []
    threshold = None
    for cand in threshold_space:
        fig = control_figures(ctrl_recs, ctrl_meta, registry, topology,
                              licences, budget=budget, threshold=cand)
        entry = {"candidate": cand, "verdict": "rejected", "falsifier": None,
                 "reproduced": {c["label"]: fig.get(c["label"].lower())
                                for c in tier_controls}}
        bad = None
        for c in tier_controls:
            got = fig.get(c["label"].lower())
            if got != c["published"]:
                bad = (c, got)
                break
        if bad is None:
            entry["verdict"] = "accepted"
            if threshold is None:
                threshold = cand
        else:
            entry["falsifier"] = {"control": bad[0]["id"],
                                  "figure": bad[0]["label"],
                                  "published": bad[0]["published"],
                                  "reproduced": bad[1]}
        threshold_candidates.append(entry)
    if threshold is None:
        raise SystemExit("no threshold reproduces the control tier figures")
    audit["unstated_quantities"]["promotion_threshold"] = {
        "candidate_space": list(threshold_space), "pinned": threshold}
    audit["threshold_candidate_verdicts"] = threshold_candidates

    # ---- calibration table ---------------------------------------------
    fig = control_figures(ctrl_recs, ctrl_meta, registry, topology, licences,
                          budget=budget, threshold=threshold)
    calibration = []
    for c in controls:
        got = fig.get(c["label"].lower())
        calibration.append({"control": c["id"], "figure": c["label"],
                            "published": c["published"], "reproduced": got,
                            "agrees": got == c["published"]})
    audit["calibration"] = calibration

    # ---- settle the season ---------------------------------------------
    season = caps["season"]
    s_env, records, s_meta, _ = decode_capture(season["blob"], tag, depth,
                                               sentinel, dictionary)
    owners, passes, hands, cycles = settle_owners(records, dictionary)
    audit["attribution_settlement_passes"] = passes
    audit["roster_dictionary"] = dictionary
    audit["hand_overs"] = {k: v for k, v in sorted(hands.items())}
    audit["closed_hand_over_sets"] = cycles
    audit["notes"].append(
        "Escape operation 2 advances within the roster dictionary; a record "
        "whose current attribution is not a dictionary entry holds no position "
        "to advance from, so operation 2 leaves it unchanged.")
    audit["notes"].append(
        "Record blobs are unmasked keystream-first and then turned right; the "
        "filler width follows the salt written in the segment envelope, not "
        "the drift-adjusted effective salt.")

    matches, stats = build_season(records, registry, topology, licences,
                                  budget, threshold)

    conferences = sorted({v["conference"] for v in topology.values()})
    segments_all = sorted({v["segment"] for v in licences.values()})

    meta_by_squad = {}
    all_keys = sorted(set(stats) | set(registry))
    for s in all_keys:
        info = registry.get(s)
        div = info["division"] if info else None
        conf = topology.get(div, {}).get("conference") if div else None
        lic = info["licence_class"] if info else None
        seg = licences.get(lic, {}).get("segment") if lic else None
        weight = topology.get(div, {}).get("ladder_weight", 1) if div else 1
        levy = licences.get(lic, {}).get("levy_rate", 1) if lic else 1
        meta_by_squad[s] = {
            "entrant_id": info["entrant_id"] if info else None,
            "division": div, "conference": conf, "licence_class": lic,
            "segment": seg, "ladder_weight": weight, "levy_rate": levy,
            "is_ranked": bool(info["is_ranked"]) if info else False,
            "registered": info is not None,
            "seen": s in stats,
            "effective_owner": owners.get(s, s),
            "handed_away": s in hands,
        }

    zero = {"gross": 0, "voided": 0, "raw": 0, "charged": 0, "overflow": 0,
            "matches": 0, "match_awards": 0, "streak": 0, "score": 0,
            "standing_points": 0,
            "tier": ("champion" if 0 >= 3 * threshold else
                     "contender" if 0 >= 2 * threshold else
                     "qualifier" if 0 >= threshold else "provisional")}

    def stat_of(s):
        return stats.get(s, zero)

    def entry_of(s):
        a = stat_of(s)
        m = meta_by_squad[s]
        return {
            "squad_key": s,
            "entrant_id": m["entrant_id"],
            "division": m["division"],
            "conference": m["conference"],
            "licence_class": m["licence_class"],
            "segment": m["segment"],
            "matches": a["matches"],
            "gross_output": a["gross"],
            "voided_output": a["voided"],
            "charged_upkeep": a["charged"],
            "overflow_upkeep": a["overflow"],
            "season_score": a["score"],
            "standing_points": a["standing_points"],
            "streak_credit": a["streak"],
            "tier": a["tier"],
        }

    def group_report(kind, name, members):
        entries = [entry_of(s) for s in sorted(members)]
        counts = {"champion": 0, "contender": 0, "qualifier": 0,
                  "provisional": 0}
        for e in entries:
            counts[e["tier"]] += 1
        return {
            "group_kind": kind,
            "group_name": name,
            "entrants": len(entries),
            "divisions": sorted({e["division"] for e in entries
                                 if e["division"]}),
            "entries": entries,
            "gross_output": sum(e["gross_output"] for e in entries),
            "voided_output": sum(e["voided_output"] for e in entries),
            "charged_upkeep": sum(e["charged_upkeep"] for e in entries),
            "overflow_upkeep": sum(e["overflow_upkeep"] for e in entries),
            "season_score": sum(e["season_score"] for e in entries),
            "standing_points": sum(e["standing_points"] for e in entries),
            "tier_counts": counts,
        }

    conf_reports = {}
    for conf in conferences:
        members = [s for s in all_keys if meta_by_squad[s]["conference"] == conf]
        conf_reports[conf] = group_report("conference", conf, members)
    seg_reports = {}
    for seg in segments_all:
        members = [s for s in all_keys if meta_by_squad[s]["segment"] == seg]
        seg_reports[seg] = group_report("segment", seg, members)

    # ---- bracket and docket --------------------------------------------
    floor = scalars.get("eligibility_match_floor", 0)
    pool = [s for s in sorted(stats)
            if owners.get(s, s) == s and not meta_by_squad[s]["handed_away"]
            and stats[s]["matches"] >= floor]
    bracket = elect_bracket(pool, stats, meta_by_squad, scalars, segments_all)
    bracket["eligibility_match_floor"] = floor
    docket = elect_docket(matches, registry, topology, scalars)

    audit["bracket"] = {
        "eligible_pool": bracket["pool"],
        "eligible_pool_size": bracket["pool_size"],
        "candidate_space": bracket["candidate_space"],
        "best_satisfied_constraint_count": bracket["best_satisfied_count"],
        "constraint_count": bracket.get("constraint_count", 0),
        "elected": bracket["members"],
        "tie_break": "most satisfied wishes, then least combined charged "
                     "upkeep, then first-sorting member keys",
    }
    audit["docket"] = {
        "candidate_space": docket["candidate_space"],
        "docket_size": scalars.get("docket_size"),
        "conference_cap": scalars.get("docket_conference_cap"),
        "review_weight": scalars.get("review_weight"),
        "elected": [r["match_id"] for r in docket["matches"]],
        "total_review_index": docket["total_review_index"],
    }

    # ---- standings ------------------------------------------------------
    standings = []
    for s in sorted(stats, key=lambda k: (-stats[k]["score"],
                                          -stats[k]["standing_points"], k)):
        a = stats[s]
        m = meta_by_squad[s]
        standings.append({
            "rank": 0, "squad_key": s, "entrant_id": m["entrant_id"],
            "registered": m["registered"], "division": m["division"],
            "conference": m["conference"], "licence_class": m["licence_class"],
            "segment": m["segment"], "ladder_weight": m["ladder_weight"],
            "levy_rate": m["levy_rate"], "is_ranked": m["is_ranked"],
            "effective_owner": m["effective_owner"],
            "handed_away": m["handed_away"],
            "matches": a["matches"], "gross_output": a["gross"],
            "voided_output": a["voided"], "raw_upkeep": a["raw"],
            "charged_upkeep": a["charged"], "overflow_upkeep": a["overflow"],
            "season_score": a["score"], "match_awards": a["match_awards"],
            "streak_credit": a["streak"],
            "standing_points": a["standing_points"], "tier": a["tier"],
        })
    for i, row in enumerate(standings, 1):
        row["rank"] = i

    kind_ledger = {n: 0 for n in KIND_NAMES.values()}
    for r in records:
        kind_ledger[KIND_NAMES[r["kind"]]] += 1

    tier_counts = {"champion": 0, "contender": 0, "qualifier": 0,
                   "provisional": 0}
    for a in stats.values():
        tier_counts[a["tier"]] += 1

    total_gross = sum(a["gross"] for a in stats.values())
    total_voided = sum(a["voided"] for a in stats.values())
    total_raw = sum(a["raw"] for a in stats.values())
    total_charged = sum(a["charged"] for a in stats.values())
    total_overflow = sum(a["overflow"] for a in stats.values())

    unregistered = sorted(s for s in stats if not meta_by_squad[s]["registered"])
    absent = sorted(s for s in registry if s not in stats)

    summary = {
        "capture": {
            "name": season["name"],
            "capture_id": s_meta["capture_id"],
            "guard_register": s_meta["guard_register"],
            "guards_active": s_meta["guards_active"],
            "season_base_tick": s_meta["season_base_tick"],
            "matches": len(matches),
            "records": len(records),
            "match_order": [m["match_id"] for m in matches],
        },
        "pinned_quantities": {
            "engine_tag": tag,
            "window_depth": depth,
            "escape_sentinel": "0x%02X" % sentinel,
            "count_bias": s_meta["count_bias"],
            "season_budget": budget,
            "promotion_threshold": threshold,
        },
        "totals": {
            "gross_output": total_gross,
            "voided_output": total_voided,
            "reported_output": total_gross + total_voided,
            "raw_upkeep": total_raw,
            "charged_upkeep": total_charged,
            "overflow_upkeep": total_overflow,
            "season_score": sum(a["score"] for a in stats.values()),
            "standing_points": sum(a["standing_points"] for a in stats.values()),
            "streak_credit": sum(a["streak"] for a in stats.values()),
            "squads_seen": len(stats),
            "registered_squads_seen": len(stats) - len(unregistered),
            "unregistered_squads_seen": len(unregistered),
            "absent_entrants": len(absent),
        },
        "kind_ledger": kind_ledger,
        "tier_counts": tier_counts,
        "standings": standings,
        "effective_owners": {s: owners[s] for s in sorted(owners)},
        "attribution_settlement_passes": passes,
        "conference_totals": {c: {k: conf_reports[c][k] for k in
                                  ("entrants", "gross_output", "voided_output",
                                   "charged_upkeep", "overflow_upkeep",
                                   "season_score", "standing_points")}
                              for c in conferences},
        "segment_totals": {g: {k: seg_reports[g][k] for k in
                               ("entrants", "gross_output", "voided_output",
                                "charged_upkeep", "overflow_upkeep",
                                "season_score", "standing_points")}
                           for g in segments_all},
        "finals_bracket": {
            "members": bracket["members"],
            "satisfied_constraint_count": bracket["satisfied_count"],
            "constraint_count": bracket.get("constraint_count", 0),
            "constraints": bracket["constraints"],
            "combined_charged_upkeep": bracket["combined_charged_upkeep"],
            "eligible_pool": bracket["pool"],
        },
        "review_docket": {
            "matches": docket["matches"],
            "total_review_index": docket["total_review_index"],
            "conference_spread": docket["conference_spread"],
        },
        "matches": [{
            "match_id": m["match_id"],
            "logical_position": m["logical_position"],
            "close_tick": m["close_tick"],
            "records": m["records"],
            "escape_records": m["escape_records"],
            "gross_output": m["gross_output"],
            "voided_output": m["voided_output"],
            "participants": sorted(m["squads"]),
            "conceded": sorted(m["conceded"]),
            "match_scores": {s: m["match_scores"][s]
                             for s in sorted(m["match_scores"])},
            "awards": {s: m["awards"][s] for s in sorted(m["awards"])},
        } for m in matches],
    }

    # ---- data quality ---------------------------------------------------
    def capture_block(meta, records):
        return {
            "capture_id": meta["capture_id"],
            "guard_register": meta["guard_register"],
            "guards_active": meta["guards_active"],
            "count_bias": meta["count_bias"],
            "declared_record_count": meta["declared_record_count"],
            "recovered_record_count": meta["recovered_record_count"],
            "record_residual": (meta["recovered_record_count"]
                                - meta["declared_record_count"]),
            "declared_segment_count": meta["declared_segment_count"],
            "recovered_segment_count": meta["recovered_segment_count"],
            "segment_residual": (meta["recovered_segment_count"]
                                 - meta["declared_segment_count"]),
            "declared_roster_size": meta["declared_roster_size"],
            "distinct_squads_recovered": len({r["squad_key"] for r in records}),
            "registered_squads_recovered": len({r["squad_key"] for r in records
                                                if r["squad_key"] in registry}),
            "roster_residual": (len({r["squad_key"] for r in records
                                     if r["squad_key"] in registry})
                                - meta["declared_roster_size"]),
            "squads_without_an_entry": (len({r["squad_key"] for r in records})
                                        - meta["declared_roster_size"]),
            "unregistered_squads": sorted({r["squad_key"] for r in records
                                           if r["squad_key"] not in registry}),
            "backwards_tick_events": meta["backwards_tick_events"],
            "envelope_checksum_ok": meta["envelope_checksum_ok"],
            "segment_checksums_ok": meta["segment_checksums_ok"],
        }

    conf_gross = sum(conf_reports[c]["gross_output"] for c in conferences)
    seg_gross = sum(seg_reports[g]["gross_output"] for g in segments_all)
    conf_score = sum(conf_reports[c]["season_score"] for c in conferences)
    seg_score = sum(seg_reports[g]["season_score"] for g in segments_all)

    findings = []
    dq_season = capture_block(s_meta, records)
    if dq_season["record_residual"]:
        findings.append("record count residual %+d against the declared count"
                        % dq_season["record_residual"])
    if dq_season["segment_residual"]:
        findings.append("segment count residual %+d against the declared count"
                        % dq_season["segment_residual"])
    if dq_season["roster_residual"]:
        findings.append("roster residual %+d registered squads against the "
                        "declared roster size" % dq_season["roster_residual"])
    if unregistered:
        findings.append("%d squad(s) played without an entry in the registry: %s"
                        % (len(unregistered), ", ".join(unregistered)))
    if absent:
        findings.append("%d registered entrant(s) appear in no capture: %s"
                        % (len(absent), ", ".join(absent)))
    if total_gross - conf_gross:
        findings.append("attribution residual %d gross output falls outside "
                        "the conference reports" % (total_gross - conf_gross))
    if dq_season["backwards_tick_events"]:
        findings.append("%d record(s) move the tick backwards inside a segment"
                        % dq_season["backwards_tick_events"])

    cap_blocks = {
        "reference": dict(capture_block(base_meta, base_recs), name=ref["name"]),
        "control": dict(capture_block(ctrl_meta, ctrl_recs),
                        name=caps["control"]["name"]),
        "season": dict(dq_season, name=season["name"]),
    }
    for role in ("reference", "control"):
        blk = cap_blocks[role]
        for field, text in (("record_residual", "record count"),
                            ("segment_residual", "segment count"),
                            ("roster_residual", "registered roster size")):
            if blk[field]:
                findings.append("%s capture: %s residual %+d against the "
                                "declared count" % (role, text, blk[field]))

    data_quality = {
        "captures": cap_blocks,
        "baseline_transcript_check": transcript_check,
        "kind_ledger": kind_ledger,
        "kind_ledger_residual": len(records) - sum(kind_ledger.values()),
        "output_ledger": {
            "gross_output": total_gross,
            "voided_output": total_voided,
            "reported_output": total_gross + total_voided,
            "residual": (total_gross + total_voided)
                        - sum(r["value"] for r in records if r["kind"] == 2),
        },
        "upkeep_ledger": {
            "raw_upkeep": total_raw,
            "charged_upkeep": total_charged,
            "overflow_upkeep": total_overflow,
            "residual": total_raw - (total_charged + total_overflow),
        },
        "attribution_residual": {
            "season_gross_output": total_gross,
            "conference_gross_output": conf_gross,
            "gross_output_residual": total_gross - conf_gross,
            "season_score": sum(a["score"] for a in stats.values()),
            "conference_season_score": conf_score,
            "season_score_residual": sum(a["score"] for a in stats.values())
                                     - conf_score,
            "segment_gross_output": seg_gross,
            "segment_gross_output_residual": total_gross - seg_gross,
            "segment_season_score_residual":
                sum(a["score"] for a in stats.values()) - seg_score,
        },
        "unregistered_squads": [
            {"squad_key": s, "matches": stats[s]["matches"],
             "gross_output": stats[s]["gross"],
             "voided_output": stats[s]["voided"],
             "raw_upkeep": stats[s]["raw"],
             "charged_upkeep": stats[s]["charged"],
             "overflow_upkeep": stats[s]["overflow"],
             "season_score": stats[s]["score"]} for s in unregistered],
        "absent_entrants": [
            {"squad_key": s, "entrant_id": registry[s]["entrant_id"],
             "division": registry[s]["division"],
             "conference": meta_by_squad[s]["conference"],
             "segment": meta_by_squad[s]["segment"]} for s in absent],
        "backwards_tick_events": dq_season["backwards_tick_events"],
        "findings": findings,
    }

    audit["review_index_table"] = docket["all_matches"]
    audit["control_capture"] = {"name": caps["control"]["name"],
                                "records": len(ctrl_recs),
                                "guards_active": ctrl_meta["guards_active"]}
    audit["baseline_transcript_check"] = transcript_check

    json_dump(os.path.join(out_dir, "summary.json"), summary)
    json_dump(os.path.join(out_dir, "data_quality.json"), data_quality)
    json_dump(os.path.join(out_dir, "policy_audit.json"), audit)
    for conf in conferences:
        json_dump(os.path.join(out_dir, "region_%s.json" % conf),
                  conf_reports[conf])
    for seg in segments_all:
        json_dump(os.path.join(out_dir, "segment_%s.json" % seg),
                  seg_reports[seg])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
