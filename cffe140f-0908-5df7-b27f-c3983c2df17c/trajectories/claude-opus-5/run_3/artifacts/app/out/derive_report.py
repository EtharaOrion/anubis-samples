#!/usr/bin/env python3
"""Settle a sealed tactics-league season from its replay journals.

Usage:  python3 derive_report.py DATA_DIR OUT_DIR

Reads only DATA_DIR, writes only OUT_DIR, uses only the standard library.
Every policy number, roster and control figure is parsed out of the shipped
documents; nothing about the shipped captures is memorised.
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
from collections import defaultdict, Counter

M32 = 0xFFFFFFFF

KIND_NAMES = {1: "deploy", 2: "output", 3: "eliminate", 4: "disband",
              5: "concede", 6: "transfer", 8: "close"}

TIERS = ("champion", "contender", "qualifier", "provisional")

# guard bit positions, overridden by the published guard table when present
DEFAULT_GUARD_BITS = {
    "keystream": 0, "chain": 1, "delta2": 2, "driftkey": 3, "segperm": 4,
    "backref": 5, "endian": 6, "countbias": 7, "shadow": 8, "rotate": 9,
    "alias": 10, "escape": 11,
}


# --------------------------------------------------------------------------
# document harvesting
# --------------------------------------------------------------------------

class Docs(object):
    """Everything the league wrote down, harvested by content not by filename."""

    def __init__(self, data_dir):
        self.data_dir = data_dir
        names = sorted(os.listdir(data_dir))
        self.md_text = ""
        self.captures = []
        self.transcripts = []
        csv_files = []
        for n in names:
            p = os.path.join(data_dir, n)
            if not os.path.isfile(p):
                continue
            low = n.lower()
            if low.endswith(".md") or low.endswith(".txt"):
                with open(p, "r", encoding="utf-8", errors="replace") as fh:
                    self.md_text += fh.read() + "\n"
            elif low.endswith(".csv"):
                csv_files.append(p)
            elif low.endswith(".jsonl"):
                self.transcripts.append(p)
            else:
                with open(p, "rb") as fh:
                    head = fh.read(4)
                if head == b"RJNL":
                    self.captures.append(p)

        self.guard_bits = self._guard_bits()
        self.engine_tags = self._roster_str("engine tag")
        self.window_depths = [int(x) for x in self._roster_str("window depth")]
        self.sentinels = [int(x, 16) if x.lower().startswith("0x") else int(x)
                          for x in self._roster_str("sentinel")]
        self.budget_caps = [int(x) for x in self._roster_str("budget cap")]
        self.thresholds = [int(x) for x in self._roster_str("promotion threshold")]

        self.eligibility_floor = self._const("eligibility match floor", 2)
        self.prominence = self._const("ladder weight prominence", 4)
        self.sp_floor = self._const("standing points floor", 3)
        self.weight_floor = self._const("combined ladder weight floor", 7)
        self.gross_floor = self._const("combined gross output floor", 900)
        self.review_weight = self._const("review weight", 25)
        self.docket_size = self._const("docket size", 5)
        self.docket_cap = self._const("docket conference cap", 2)
        self.bracket_size = self._bracket_size()
        self.constraint_labels = self._constraint_labels()
        self.controls = self._controls()

        # reference tables
        self.divisions = {}
        self.registry = {}
        self.licences = {}
        for p in csv_files:
            with open(p, newline="", encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
            if not rows:
                continue
            cols = set(rows[0].keys())
            if {"division", "conference", "ladder_weight"} <= cols:
                for r in rows:
                    self.divisions[r["division"]] = (r["conference"], int(r["ladder_weight"]))
            elif {"licence_class", "segment", "levy_rate"} <= cols:
                for r in rows:
                    self.licences[r["licence_class"]] = (r["segment"], int(r["levy_rate"]))
            elif {"entrant_id", "squad_key"} <= cols:
                for r in rows:
                    self.registry[r["squad_key"]] = r

        self.conferences = sorted({c for c, _ in self.divisions.values()})
        self.segments = sorted({s for s, _ in self.licences.values()})

    # -- markdown scraping -------------------------------------------------
    def _roster_str(self, label):
        m = re.search(re.escape(label) + r"\s+roster\s*:(.*)", self.md_text, re.I)
        if not m:
            return []
        return re.findall(r"`([^`]+)`", m.group(1))

    def _const(self, label, default):
        m = re.search(re.escape(label) + r"\s*:\s*`?(-?\d+)`?", self.md_text, re.I)
        return int(m.group(1)) if m else default

    def _bracket_size(self):
        words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
                 "six": 6, "seven": 7, "eight": 8}
        m = re.search(r"finals bracket holds exactly (\w+)", self.md_text, re.I)
        if m:
            w = m.group(1).lower()
            if w in words:
                return words[w]
            if w.isdigit():
                return int(w)
        return 3

    def _guard_bits(self):
        bits = {}
        for m in re.finditer(r"^\|\s*(\d+)\s*\|\s*([a-z0-9 ]+?)\s*\|\s*$",
                             self.md_text, re.M | re.I):
            name = m.group(2).strip().lower().replace(" ", "")
            if name in ("guard", "bit"):
                continue
            bits[name] = int(m.group(1))
        for k, v in DEFAULT_GUARD_BITS.items():
            bits.setdefault(k, v)
        return bits

    def _constraint_labels(self):
        m = re.search(r"largest number of them\.?\s*\n(.*?)\n\s*\n",
                      self.md_text, re.S | re.I)
        if not m:
            return []
        return [ln.strip()[2:].strip() for ln in m.group(1).splitlines()
                if ln.strip().startswith("- ")]

    def _controls(self):
        out = []
        for m in re.finditer(r"^-\s*(C\d+)\s+(.+?)\s*:\s*(-?\d+)\s*$",
                             self.md_text, re.M):
            out.append({"id": m.group(1), "name": m.group(2).strip().lower(),
                        "published": int(m.group(3))})
        out.sort(key=lambda r: int(r["id"][1:]))
        return out

    # -- lookups -----------------------------------------------------------
    def squad_facts(self, key):
        r = self.registry.get(key)
        if r is None:
            return {"registered": False, "entrant_id": None, "division": None,
                    "conference": None, "licence_class": None, "segment": None,
                    "ladder_weight": 1, "levy_rate": 1, "is_ranked": False}
        conf, weight = self.divisions.get(r.get("division"), (None, 1))
        seg, levy = self.licences.get(r.get("licence_class"), (None, 1))
        ranked = str(r.get("is_ranked", "")).strip().lower() in ("yes", "true", "y", "1")
        return {"registered": True, "entrant_id": r.get("entrant_id"),
                "division": r.get("division"), "conference": conf,
                "licence_class": r.get("licence_class"), "segment": seg,
                "ladder_weight": weight, "levy_rate": levy, "is_ranked": ranked}


# --------------------------------------------------------------------------
# journal decoding
# --------------------------------------------------------------------------

def read_capture(path):
    with open(path, "rb") as fh:
        d = fh.read()
    env = {
        "marker": d[0:4].decode("latin1"),
        "version": d[4],
        "reserved": d[5],
        "guard_register": struct.unpack("<H", d[6:8])[0],
        "nonce_le": struct.unpack("<I", d[8:12])[0],
        "nonce_be": struct.unpack(">I", d[8:12])[0],
        "declared_segments_raw": struct.unpack("<I", d[12:16])[0],
        "declared_records_raw": struct.unpack("<I", d[16:20])[0],
        "base_tick": struct.unpack("<I", d[20:24])[0],
        "capture_id": struct.unpack("<I", d[24:28])[0],
        "declared_roster_raw": struct.unpack("<I", d[28:32])[0],
        "checksum": struct.unpack("<I", d[32:36])[0],
    }
    env["checksum_computed"] = sum(d[0:32]) & M32
    env["checksum_ok"] = env["checksum"] == env["checksum_computed"]
    env["path"] = path
    env["role_digit"] = env["capture_id"] % 10
    segs = []
    off = 36
    while off + 28 <= len(d):
        if d[off:off + 4] != b"SEG0":
            break
        slot, mid, rc, salt, pw, pc = struct.unpack("<6I", d[off + 4:off + 28])
        off += 28
        payload = d[off:off + pw]
        off += pw
        segs.append({"slot": slot, "match_id": mid, "declared_records_raw": rc,
                     "salt": salt, "payload_width": pw, "payload_checksum": pc,
                     "payload": payload,
                     "checksum_ok": (sum(payload) & M32) == pc})
    env["trailing_bytes"] = len(d) - off
    return env, segs


def perm_step(nonce, nsegs):
    """Smallest integer at least two more than (nonce >> 8) mod nsegs that is
    coprime with nsegs."""
    s = ((nonce >> 8) % nsegs) + 2
    while math.gcd(s, nsegs) != 1:
        s += 1
    return s


def _ror(b, n):
    n %= len(b)
    return b if n == 0 else b[-n:] + b[:-n]


def _keystream(tag, mid, esalt, ordinal, width):
    return hashlib.sha256(("%s:%d:%d:%d" % (tag, mid, esalt, ordinal)).encode())\
        .digest()[:width]


class DecodeError(Exception):
    pass


def decode_capture(env, segs, docs, tag, sentinel):
    """Undo the stream guards; returns wire-level records in logical order.

    Attribution is left unresolved here (mode/field only) because resolving it
    needs the roster dictionary and the window depth.
    """
    g = env["guard_register"]
    bits = docs.guard_bits
    on = lambda name: bool((g >> bits[name]) & 1)

    nonce = env["nonce_be"] if on("endian") else env["nonce_le"]
    bias = (sum(tag.encode()) % 11) if on("countbias") else 0
    nsegs = len(segs)
    env_segments = env["declared_segments_raw"] - bias
    env_records = env["declared_records_raw"] - bias
    env_roster = env["declared_roster_raw"] - bias

    if nsegs == 0:
        raise DecodeError("no segments")

    if on("segperm"):
        step = perm_step(nonce, nsegs)
        logical_of_slot = [(nonce + i * step) % nsegs for i in range(nsegs)]
    else:
        step = None
        logical_of_slot = list(range(nsegs))
    if sorted(logical_of_slot) != list(range(nsegs)):
        raise DecodeError("segment permutation is not a permutation")
    slot_of_logical = [0] * nsegs
    for i, lp in enumerate(logical_of_slot):
        slot_of_logical[lp] = i

    records = []
    running = 0
    prev_tick = None
    backwards = 0
    for lp in range(nsegs):
        s = segs[slot_of_logical[lp]]
        rc = s["declared_records_raw"] - bias
        if rc < 0:
            raise DecodeError("negative segment record count")
        esalt = (s["salt"] ^ running) & M32 if on("driftkey") else s["salt"]
        s["effective_salt"] = esalt
        s["logical_position"] = lp
        s["records"] = rc
        p = s["payload"]
        q = 0
        d1 = 0
        t = 0
        prev_seg_tick = None
        for j in range(rc):
            if q + 2 > len(p):
                raise DecodeError("frame runs off the payload")
            visible = p[q]
            shadow = p[q + 1]
            width = (shadow ^ (esalt & 0xFF)) if on("shadow") else visible
            blob = p[q + 2:q + 2 + width]
            q += 2 + width
            if len(blob) != width or width == 0:
                raise DecodeError("short record blob")
            b = blob
            if on("keystream"):
                ks = _keystream(tag, s["match_id"], esalt, j, width)
                b = bytes(x ^ y for x, y in zip(b, ks))
            if on("rotate"):
                b = _ror(b, (esalt + len(records)) % width)
            dirs = []
            z = 0
            if on("escape"):
                while z + 1 < len(b) and b[z] == sentinel and len(dirs) < 2:
                    dirs.append(b[z + 1])
                    z += 2
            body = b[z:]
            if len(body) < 13:
                raise DecodeError("record body too short")
            kind = body[0]
            mode = body[1]
            if kind not in KIND_NAMES:
                raise DecodeError("unknown event kind %d" % kind)
            if mode == 0:
                field = body[2:5]
                rest = body[5:]
            elif mode in (1, 2):
                field = body[2]
                rest = body[3:]
            else:
                raise DecodeError("unknown attribution mode %d" % mode)
            if len(rest) != 10:
                raise DecodeError("record body length %d" % len(body))
            unit = struct.unpack("<H", rest[0:2])[0]
            tick_field = struct.unpack("<I", rest[2:6])[0]
            value_field = struct.unpack("<I", rest[6:10])[0]
            if on("delta2"):
                d1 = (d1 + tick_field) & M32
                t = (t + d1) & M32
                tick = t
            else:
                tick = tick_field
            if on("chain"):
                value = value_field ^ (nonce if prev_tick is None else prev_tick)
            else:
                value = value_field
            prev_tick = tick
            if prev_seg_tick is not None and tick < prev_seg_tick:
                backwards += 1
            prev_seg_tick = tick
            if mode == 0:
                try:
                    field = field.decode("ascii")
                except UnicodeDecodeError:
                    raise DecodeError("non ascii squad key")
                if not field.isprintable():
                    raise DecodeError("unprintable squad key")
            records.append({
                "kind": kind, "mode": mode, "field": field, "unit": unit,
                "tick": tick, "value": value, "directives": dirs,
                "match_id": s["match_id"], "logical_segment": lp,
                "segment_ordinal": j, "record_ordinal": len(records),
            })
            running = (running + value) & M32
        s["filler_width"] = len(p) - q
    info = {
        "bias": bias, "nonce": nonce, "step": step,
        "declared_segments": env_segments, "declared_records": env_records,
        "declared_roster": env_roster,
        "recovered_segments": nsegs, "recovered_records": len(records),
        "ticks_backwards": backwards,
        "logical_of_slot": logical_of_slot,
    }
    return records, info


def resolve_attribution(records, roster_dict, depth, docs, guard_register,
                        build_dictionary=False, strict=True):
    """Turn wire attribution fields into squad keys, then apply escape
    directives.  Optionally grows the roster dictionary as keys are first met
    (that is how the reference capture defines it)."""
    bits = docs.guard_bits
    on = lambda name: bool((guard_register >> bits[name]) & 1)
    window = []
    anomalies = {"backref_out_of_range": 0, "alias_out_of_range": 0,
                 "unknown_directive_op": 0, "advance_off_dictionary": 0}
    rd = roster_dict
    for r in records:
        mode = r["mode"]
        if mode == 0:
            key = r["field"]
        elif mode == 1:
            pos = r["field"]
            if pos >= len(window):
                if strict:
                    raise DecodeError("back reference %d beyond window" % pos)
                anomalies["backref_out_of_range"] += 1
                key = window[-1] if window else None
                if key is None:
                    raise DecodeError("back reference with empty window")
            else:
                key = window[pos]
        else:
            if not rd:
                raise DecodeError("alias with an empty roster dictionary")
            pos = r["field"] % len(rd)
            key = rd[pos]
        if build_dictionary and key not in rd:
            rd.append(key)
        if key in window:
            window.remove(key)
        window.append(key)
        if depth and len(window) > depth:
            window.pop(0)
        r["wire_key"] = key
        eff = key
        for db in r["directives"]:
            op = db >> 4
            operand = db & 0x0F
            if not rd:
                break
            if op == 1:
                eff = rd[operand % len(rd)]
            elif op == 2:
                if eff in rd:
                    eff = rd[(rd.index(eff) + operand) % len(rd)]
                else:
                    anomalies["advance_off_dictionary"] += 1
                    r["advance_off_dictionary"] = True
            else:
                anomalies["unknown_directive_op"] += 1
        r["squad_key"] = eff
    return anomalies


def build_roster_dictionary(env, segs, docs, tag, depth, sentinel):
    records, info = decode_capture(env, segs, docs, tag, sentinel)
    rd = []
    resolve_attribution(records, rd, depth, docs, env["guard_register"],
                        build_dictionary=True, strict=True)
    return rd, records, info


# --------------------------------------------------------------------------
# settlement
# --------------------------------------------------------------------------

def match_breakdown(records):
    """Per match: per squad gross/voided output and raw upkeep, plus review
    inputs.  Matches come back in logical order."""
    by_match = defaultdict(list)
    for r in records:
        by_match[(r["logical_segment"], r["match_id"])].append(r)
    out = []
    for (lp, mid) in sorted(by_match):
        rs = by_match[(lp, mid)]
        closes = [r["tick"] for r in rs if r["kind"] == 8]
        close = max(closes) if closes else max(r["tick"] for r in rs)
        elim, disband, concede, deploy = {}, {}, {}, {}
        for r in rs:
            uk = (r["squad_key"], r["unit"])
            if r["kind"] == 3:
                elim.setdefault(uk, r["tick"])
            elif r["kind"] == 4:
                disband.setdefault(uk, r["tick"])
            elif r["kind"] == 5:
                concede.setdefault(r["squad_key"], r["tick"])
            elif r["kind"] == 1:
                deploy.setdefault(uk, (r["tick"], r["value"]))
        stats = {}
        for r in rs:
            stats.setdefault(r["squad_key"], {"gross": 0, "voided": 0, "raw_upkeep": 0})
        for r in rs:
            if r["kind"] != 2:
                continue
            uk = (r["squad_key"], r["unit"])
            withdrawn = (uk in elim and r["tick"] >= elim[uk]) or \
                        (r["squad_key"] in concede and r["tick"] >= concede[r["squad_key"]])
            stats[r["squad_key"]]["voided" if withdrawn else "gross"] += r["value"]
        for uk, (t0, rate) in deploy.items():
            t1 = disband.get(uk, close)
            stats[uk[0]]["raw_upkeep"] += rate * max(0, t1 - t0)
        escaped = sum(1 for r in rs if r["directives"])
        out.append({
            "match_id": mid, "logical_position": lp, "close_tick": close,
            "stats": stats, "conceded": set(concede),
            "records": len(rs), "escape_records": escaped,
            "voided_output": sum(v["voided"] for v in stats.values()),
            "gross_output": sum(v["gross"] for v in stats.values()),
        })
    return out


def settle_transfers(records, roster_dict):
    """Follow entry hand overs to a fixpoint by repeated sweeps."""
    hand_to = {}
    transfers = []
    for r in records:
        if r["kind"] == 6 and roster_dict:
            target = roster_dict[r["value"] % len(roster_dict)]
            hand_to[r["squad_key"]] = target
            transfers.append({"from": r["squad_key"], "to": target,
                              "match_id": r["match_id"], "tick": r["tick"]})
    squads = set(r["squad_key"] for r in records) | set(hand_to) | set(hand_to.values())

    # A closed set of hand overs never reaches a fixpoint under sweeping, so
    # collapse every cycle first: the whole closed set settles on the member
    # whose key sorts first.
    effective_hand = dict(hand_to)
    cycles = []
    colour = {}
    for start in sorted(hand_to):
        if start in colour:
            continue
        path = []
        cur = start
        while cur in hand_to and cur not in colour:
            colour[cur] = 1
            path.append(cur)
            cur = hand_to[cur]
        if cur in colour and colour[cur] == 1 and cur in path:
            cyc = path[path.index(cur):]
            cycles.append(sorted(cyc))
            rep = min(cyc)
            for m in cyc:
                if m == rep:
                    effective_hand.pop(m, None)
                else:
                    effective_hand[m] = rep
        for n in path:
            colour[n] = 2

    owner = {k: k for k in squads}
    passes = 0
    while True:
        passes += 1
        nxt = {k: effective_hand.get(v, v) for k, v in owner.items()}
        if nxt == owner:
            break
        owner = nxt
    return owner, hand_to, transfers, passes, cycles


def settle(records, docs, budget, threshold, roster_dict):
    matches = match_breakdown(records)
    owner, hand_to, transfers, passes, cycles = settle_transfers(records, roster_dict)

    remaining = {}
    squads = {}

    def blank(key):
        f = docs.squad_facts(key)
        e = {"squad_key": key, "gross_output": 0, "voided_output": 0,
             "raw_upkeep": 0, "charged_upkeep": 0, "overflow_upkeep": 0,
             "season_score": 0, "matches": 0, "match_awards": 0,
             "streak_credit": 0, "standing_points": 0, "results": []}
        e.update(f)
        return e

    for m in matches:
        scores = {}
        for key in sorted(m["stats"]):
            v = m["stats"][key]
            e = squads.setdefault(key, blank(key))
            if key not in remaining:
                remaining[key] = budget
            charged = min(v["raw_upkeep"], remaining[key])
            remaining[key] -= charged
            overflow = v["raw_upkeep"] - charged
            score = v["gross"] * e["ladder_weight"] - charged * e["levy_rate"]
            scores[key] = score
            e["gross_output"] += v["gross"]
            e["voided_output"] += v["voided"]
            e["raw_upkeep"] += v["raw_upkeep"]
            e["charged_upkeep"] += charged
            e["overflow_upkeep"] += overflow
            e["season_score"] += score
            e["matches"] += 1
            v["charged"] = charged
            v["overflow"] = overflow
            v["score"] = score
        eligible = {k: s for k, s in scores.items() if k not in m["conceded"]}
        if eligible:
            top = max(eligible.values())
            winners = sorted(k for k, s in eligible.items() if s == top)
        else:
            top, winners = None, []
        m["scores"] = scores
        m["top_score"] = top
        m["winners"] = winners
        m["outright"] = winners[0] if len(winners) == 1 else None
        for key in m["stats"]:
            e = squads[key]
            if key in m["conceded"]:
                e["results"].append("conceded")
            elif len(winners) == 1 and key in winners:
                e["results"].append("win")
                e["match_awards"] += 3
            elif len(winners) > 1 and key in winners:
                e["results"].append("shared")
                e["match_awards"] += 1
            else:
                e["results"].append("loss")

    for e in squads.values():
        run = 0
        credit = 0
        for res in e["results"]:
            if res == "win":
                run += 1
                if run > 1:
                    credit += 2
            else:
                run = 0
        e["streak_credit"] = credit
        e["standing_points"] = e["match_awards"] + credit
        s = e["season_score"]
        if s >= 3 * threshold:
            e["tier"] = "champion"
        elif s >= 2 * threshold:
            e["tier"] = "contender"
        elif s >= threshold:
            e["tier"] = "qualifier"
        else:
            e["tier"] = "provisional"
        e["effective_owner"] = owner.get(e["squad_key"], e["squad_key"])
        e["handed_over"] = e["squad_key"] in hand_to

    return {"matches": matches, "squads": squads, "owner": owner,
            "hand_to": hand_to, "transfers": transfers, "passes": passes,
            "cycles": cycles}


def totals(state):
    t = Counter()
    for e in state["squads"].values():
        for f in ("gross_output", "voided_output", "raw_upkeep",
                  "charged_upkeep", "overflow_upkeep", "season_score",
                  "standing_points"):
            t[f] += e[f]
    return t


def tier_counts(entries):
    c = {k: 0 for k in TIERS}
    for e in entries:
        c[e["tier"]] += 1
    return c


# --------------------------------------------------------------------------
# calibration
# --------------------------------------------------------------------------

GUARD_METRICS = ("matches opened", "records recovered", "distinct squads",
                 "gross output", "voided output", "raw upkeep")
BUDGET_METRICS = ("charged upkeep", "overflow upkeep")
THRESHOLD_METRICS = tuple(t + " entrants" for t in TIERS)


def control_metric(name, info, state):
    t = totals(state)
    if name == "matches opened":
        return info["recovered_segments"]
    if name == "records recovered":
        return info["recovered_records"]
    if name == "distinct squads":
        return len(state["squads"])
    if name == "gross output":
        return t["gross_output"]
    if name == "voided output":
        return t["voided_output"]
    if name == "raw upkeep":
        return t["raw_upkeep"]
    if name == "charged upkeep":
        return t["charged_upkeep"]
    if name == "overflow upkeep":
        return t["overflow_upkeep"]
    for tier in TIERS:
        if name == tier + " entrants":
            return sum(1 for e in state["squads"].values() if e["tier"] == tier)
    return None


def stage_of(name):
    if name in GUARD_METRICS:
        return "guard"
    if name in BUDGET_METRICS:
        return "budget"
    if name in THRESHOLD_METRICS:
        return "threshold"
    return None


WORD_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
                "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
                "twelve": 12}


def stage_sizes(docs):
    """How the calibration note splits its figures across the three stages."""
    def grab(pattern, default):
        m = re.search(pattern, docs.md_text, re.I)
        if not m:
            return default
        w = m.group(1).lower()
        if w.isdigit():
            return int(w)
        return WORD_NUMBERS.get(w, default)
    return (grab(r"reproduces the first (\w+)", 6),
            grab(r"reproduces the next (\w+)", 2),
            grab(r"reproduces the last (\w+)", 3))


def stage_figures(docs):
    """Split the published control figures into guard, budget and threshold
    stages.  The name of a figure decides its stage when we recognise it; a
    figure we do not recognise falls back to the position the calibration note
    gives it."""
    n1, n2, n3 = stage_sizes(docs)
    positional = {}
    for i, c in enumerate(docs.controls):
        positional[c["id"]] = ("guard" if i < n1 else
                               "budget" if i < n1 + n2 else
                               "threshold" if i < n1 + n2 + n3 else None)
    out = {"guard": [], "budget": [], "threshold": []}
    for c in docs.controls:
        st = stage_of(c["name"]) or positional.get(c["id"])
        if st in out and control_metric_known(c["name"]):
            out[st].append(c)
    return out["guard"], out["budget"], out["threshold"]


def control_metric_known(name):
    return stage_of(name) is not None


def pin_quantities(docs, ref_paths, control_path, log):
    """Search the published rosters; keep only the reading that reproduces
    every control figure, and record what killed each rejection."""
    ctrl_env, ctrl_segs = read_capture(control_path)
    ref_env, ref_segs = read_capture(ref_paths)

    guard_figs, budget_figs, thr_figs = stage_figures(docs)

    combos = []
    for tag in docs.engine_tags:
        for depth in docs.window_depths:
            for sent in docs.sentinels:
                combos.append((tag, depth, sent))

    results = {}
    survivors = []
    for tag, depth, sent in combos:
        entry = {"engine_tag": tag, "window_depth": depth,
                 "sentinel": "0x%02X" % sent, "reached": 0}
        try:
            rd, _ref_records, _ref_info = build_roster_dictionary(
                ref_env, [dict(s) for s in ref_segs], docs, tag, depth, sent)
            recs, info = decode_capture(ctrl_env, [dict(s) for s in ctrl_segs],
                                        docs, tag, sent)
            resolve_attribution(recs, rd, depth, docs, ctrl_env["guard_register"])
            st = settle(recs, docs, 10 ** 15, 10 ** 15, rd)
        except DecodeError as exc:
            entry["verdict"] = "rejected"
            entry["falsifier"] = guard_figs[min(1, len(guard_figs) - 1)]["id"] \
                if guard_figs else None
            entry["falsifier_detail"] = "the capture will not decode under this " \
                                        "reading (%s)" % str(exc)
            results[(tag, depth, sent)] = entry
            continue
        except Exception as exc:  # pragma: no cover - defensive
            entry["verdict"] = "rejected"
            entry["falsifier"] = guard_figs[0]["id"] if guard_figs else None
            entry["falsifier_detail"] = str(exc)
            results[(tag, depth, sent)] = entry
            continue
        bad = None
        for i, fig in enumerate(guard_figs):
            got = control_metric(fig["name"], info, st)
            if got != fig["published"]:
                bad = (fig, got, i)
                break
            entry["reached"] = i + 1
        if bad is None:
            entry["verdict"] = "accepted"
            entry["falsifier"] = None
            survivors.append((tag, depth, sent, rd))
        else:
            fig, got, _ = bad
            entry["verdict"] = "rejected"
            entry["falsifier"] = fig["id"]
            entry["falsifier_detail"] = "%s %s: published %d, reproduced %d" % (
                fig["id"], fig["name"], fig["published"], got)
        results[(tag, depth, sent)] = entry

    if not survivors:
        raise SystemExit("no reading of the guard rosters reproduces the control figures")

    tag, depth, sent, rd = survivors[0]
    pinned = (tag, depth, sent)

    def per_value(index, values, label, fmt=str):
        rows = []
        for v in values:
            hits = [(k, e) for k, e in results.items() if k[index] == v]
            ok = [e for _, e in hits if e["verdict"] == "accepted"]
            if ok:
                rows.append({"candidate": fmt(v), "verdict": "accepted",
                             "falsifier": None,
                             "note": "reproduces every guard stage control figure"})
                continue
            # prefer the reading that holds the other two quantities at the
            # values the control slice pinned, so the falsifier is about this
            # candidate alone
            held = [e for k, e in hits
                    if all(k[i] == pinned[i] for i in range(3) if i != index)]
            best = max(held or [e for _, e in hits], key=lambda e: e["reached"])
            rows.append({"candidate": fmt(v), "verdict": "rejected",
                         "falsifier": best["falsifier"],
                         "note": best.get("falsifier_detail", ""),
                         "held_at": {"engine_tag": tag, "window_depth": depth,
                                     "sentinel": "0x%02X" % sent} if held else None})
        return {"quantity": label, "roster": [fmt(v) for v in values],
                "candidates": rows}

    audit_guard = [
        per_value(0, docs.engine_tags, "engine tag"),
        per_value(1, docs.window_depths, "window depth", str),
        per_value(2, docs.sentinels, "sentinel", lambda v: "0x%02X" % v),
    ]

    # --- season budget --------------------------------------------------
    recs, info = decode_capture(ctrl_env, [dict(s) for s in ctrl_segs], docs, tag, sent)
    resolve_attribution(recs, rd, depth, docs, ctrl_env["guard_register"])
    budget_rows = []
    budget = None
    for cap in docs.budget_caps:
        st = settle(recs, docs, cap, 10 ** 15, rd)
        bad = None
        for fig in budget_figs:
            got = control_metric(fig["name"], info, st)
            if got != fig["published"]:
                bad = (fig, got)
                break
        if bad is None:
            budget_rows.append({"candidate": str(cap), "verdict": "accepted",
                                "falsifier": None,
                                "note": "reproduces every budget stage control figure"})
            if budget is None:
                budget = cap
        else:
            fig, got = bad
            budget_rows.append({"candidate": str(cap), "verdict": "rejected",
                                "falsifier": fig["id"],
                                "note": "%s %s: published %d, reproduced %d" % (
                                    fig["id"], fig["name"], fig["published"], got)})
    if budget is None:
        raise SystemExit("no budget cap on the roster reproduces the control figures")

    # --- promotion threshold ---------------------------------------------
    thr_rows = []
    threshold = None
    for thr in docs.thresholds:
        st = settle(recs, docs, budget, thr, rd)
        bad = None
        for fig in thr_figs:
            got = control_metric(fig["name"], info, st)
            if got != fig["published"]:
                bad = (fig, got)
                break
        if bad is None:
            thr_rows.append({"candidate": str(thr), "verdict": "accepted",
                             "falsifier": None,
                             "note": "reproduces every threshold stage control figure"})
            if threshold is None:
                threshold = thr
        else:
            fig, got = bad
            thr_rows.append({"candidate": str(thr), "verdict": "rejected",
                             "falsifier": fig["id"],
                             "note": "%s %s: published %d, reproduced %d" % (
                                 fig["id"], fig["name"], fig["published"], got)})
    if threshold is None:
        raise SystemExit("no promotion threshold on the roster reproduces the control figures")

    final = settle(recs, docs, budget, threshold, rd)
    wire_seen = {r["wire_key"] for r in recs}
    escape_only = sorted(k for k in final["squads"] if k not in wire_seen)
    calibration = []
    for fig in docs.controls:
        got = control_metric(fig["name"], info, final)
        calibration.append({"control": fig["id"], "figure": fig["name"],
                            "published": fig["published"], "reproduced": got,
                            "agrees": got == fig["published"]})

    audit = {
        "candidate_spaces": audit_guard + [
            {"quantity": "season budget", "roster": [str(c) for c in docs.budget_caps],
             "candidates": budget_rows},
            {"quantity": "promotion threshold", "roster": [str(c) for c in docs.thresholds],
             "candidates": thr_rows},
        ],
        "guard_combinations_searched": len(combos),
        "calibration_table": calibration,
        "control_settlement_passes": final["passes"],
        "control_squads_reached_only_by_escape": escape_only,
    }
    resolved = {"engine_tag": tag, "window_depth": depth,
                "sentinel": "0x%02X" % sent, "season_budget": budget,
                "promotion_threshold": threshold}
    return resolved, rd, audit, (ctrl_env, info, final)


# --------------------------------------------------------------------------
# bracket and docket
# --------------------------------------------------------------------------

def elect_bracket(docs, state, threshold):
    squads = state["squads"]
    pool = sorted(k for k, e in squads.items()
                  if e["effective_owner"] == k and e["matches"] >= docs.eligibility_floor)
    size = docs.bracket_size

    def constraints(members):
        es = [squads[k] for k in members]
        confs = [e["conference"] for e in es]
        segs = set(e["segment"] for e in es if e["segment"] is not None)
        return [
            ("prominent division", any(e["ladder_weight"] >= docs.prominence for e in es)),
            ("both segments", set(docs.segments) <= segs),
            ("standing points floor", all(e["standing_points"] >= docs.sp_floor for e in es)),
            ("distinct conferences", len(set(confs)) == len(confs)),
            ("a ranked entrant", any(e["is_ranked"] for e in es)),
            ("combined ladder weight", sum(e["ladder_weight"] for e in es) >= docs.weight_floor),
            ("no overflow upkeep", all(e["overflow_upkeep"] == 0 for e in es)),
            ("a champion member", any(e["tier"] == "champion" for e in es)),
            ("combined gross output", sum(e["gross_output"] for e in es) >= docs.gross_floor),
            ("no member handed its entry away", not any(e["handed_over"] for e in es)),
        ]

    best = None
    best_key = None
    considered = 0
    best_count = -1
    for members in itertools.combinations(pool, size):
        considered += 1
        res = constraints(members)
        met = sum(1 for _, ok in res if ok)
        upkeep = sum(squads[k]["charged_upkeep"] for k in members)
        key = (-met, upkeep, list(members))
        if best_key is None or key < best_key:
            best_key, best = key, (members, res, met, upkeep)
        best_count = max(best_count, met)

    labels = docs.constraint_labels
    if best is None:
        return {"members": [], "eligible_pool": pool, "pool_size": len(pool),
                "candidate_brackets": considered, "bracket_size": size,
                "satisfied_constraints": 0,
                "best_satisfied_constraint_count": 0,
                "constraints": []}, considered, 0
    members, res, met, upkeep = best
    detail = []
    for i, (name, ok) in enumerate(res):
        detail.append({"constraint": labels[i] if i < len(labels) else name,
                       "short_name": name, "satisfied": bool(ok)})
    out = {
        "members": list(members),
        "bracket_size": size,
        "eligible_pool": pool,
        "pool_size": len(pool),
        "candidate_brackets": considered,
        "satisfied_constraints": met,
        "best_satisfied_constraint_count": best_count,
        "combined_charged_upkeep": upkeep,
        "combined_ladder_weight": sum(squads[k]["ladder_weight"] for k in members),
        "combined_gross_output": sum(squads[k]["gross_output"] for k in members),
        "constraints": detail,
        "member_detail": [
            {"squad_key": k, "conference": squads[k]["conference"],
             "segment": squads[k]["segment"], "division": squads[k]["division"],
             "ladder_weight": squads[k]["ladder_weight"],
             "tier": squads[k]["tier"],
             "standing_points": squads[k]["standing_points"],
             "gross_output": squads[k]["gross_output"],
             "charged_upkeep": squads[k]["charged_upkeep"],
             "overflow_upkeep": squads[k]["overflow_upkeep"],
             "is_ranked": squads[k]["is_ranked"]} for k in members],
    }
    return out, considered, best_count


def match_conference(docs, m):
    best = None
    for key, v in sorted(m["stats"].items()):
        f = docs.squad_facts(key)
        if not f["registered"]:
            continue
        cand = (-v["gross"], key)
        if best is None or cand < best[0]:
            best = (cand, f["conference"], key)
    if best is None:
        return None, None
    return best[1], best[2]


def elect_docket(docs, state):
    rows = []
    for m in state["matches"]:
        conf, deciding = match_conference(docs, m)
        idx = m["voided_output"] + docs.review_weight * m["escape_records"]
        rows.append({"match_id": m["match_id"],
                     "logical_position": m["logical_position"],
                     "conference": conf, "deciding_squad": deciding,
                     "voided_output": m["voided_output"],
                     "escape_records": m["escape_records"],
                     "review_index": idx})
    k = min(docs.docket_size, len(rows))
    cap = docs.docket_cap
    by_conf = defaultdict(list)
    for r in rows:
        by_conf[r["conference"]].append(r)
    pool = []
    for conf, rs in by_conf.items():
        rs = sorted(rs, key=lambda r: (-r["review_index"], r["match_id"]))
        pool.extend(rs if conf is None else rs[:cap])
    pool = sorted(pool, key=lambda r: r["match_id"])
    best = None
    best_key = None
    for combo in itertools.combinations(pool, k):
        counts = Counter(r["conference"] for r in combo if r["conference"] is not None)
        if counts and max(counts.values()) > cap:
            continue
        total = sum(r["review_index"] for r in combo)
        ids = sorted(r["match_id"] for r in combo)
        key = (-total, ids)
        if best_key is None or key < best_key:
            best_key, best = key, (combo, total)
    if best is None:
        return {"matches": [], "total_review_index": 0,
                "conference_spread": {}, "all_matches": rows}
    combo, total = best
    spread = Counter(r["conference"] for r in combo)
    return {
        "docket_size": k,
        "conference_cap": cap,
        "review_weight": docs.review_weight,
        "matches": sorted(({"match_id": r["match_id"],
                            "conference": r["conference"],
                            "deciding_squad": r["deciding_squad"],
                            "voided_output": r["voided_output"],
                            "escape_records": r["escape_records"],
                            "review_index": r["review_index"]} for r in combo),
                          key=lambda r: r["match_id"]),
        "total_review_index": total,
        "conference_spread": {(c if c is not None else "unassigned"): n
                              for c, n in sorted(spread.items(),
                                                 key=lambda kv: (kv[0] is None, kv[0]))},
        "candidate_pool": [r["match_id"] for r in pool],
        "all_matches": rows,
    }


# --------------------------------------------------------------------------
# reports
# --------------------------------------------------------------------------

def entry_record(e):
    return {
        "charged_upkeep": e["charged_upkeep"],
        "conference": e["conference"],
        "division": e["division"],
        "entrant_id": e["entrant_id"],
        "gross_output": e["gross_output"],
        "licence_class": e["licence_class"],
        "matches": e["matches"],
        "overflow_upkeep": e["overflow_upkeep"],
        "season_score": e["season_score"],
        "segment": e["segment"],
        "squad_key": e["squad_key"],
        "standing_points": e["standing_points"],
        "streak_credit": e["streak_credit"],
        "tier": e["tier"],
        "voided_output": e["voided_output"],
    }


def group_report(kind, name, entries):
    entries = sorted(entries, key=lambda e: e["squad_key"])
    rows = [entry_record(e) for e in entries]
    return {
        "charged_upkeep": sum(e["charged_upkeep"] for e in entries),
        "divisions": sorted({e["division"] for e in entries if e["division"]}),
        "entrants": len(rows),
        "entries": rows,
        "gross_output": sum(e["gross_output"] for e in entries),
        "group_kind": kind,
        "group_name": name,
        "overflow_upkeep": sum(e["overflow_upkeep"] for e in entries),
        "season_score": sum(e["season_score"] for e in entries),
        "standing_points": sum(e["standing_points"] for e in entries),
        "tier_counts": tier_counts(entries),
        "voided_output": sum(e["voided_output"] for e in entries),
    }


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, sort_keys=True)
        fh.write("\n")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main(argv):
    if len(argv) != 3:
        sys.stderr.write("usage: %s DATA_DIR OUT_DIR\n" % argv[0])
        return 2
    data_dir, out_dir = argv[1], argv[2]
    os.makedirs(out_dir, exist_ok=True)
    docs = Docs(data_dir)

    roles = {}
    for p in docs.captures:
        env, segs = read_capture(p)
        roles.setdefault(env["role_digit"], []).append((p, env, segs))
    if 1 not in roles or 2 not in roles or 3 not in roles:
        raise SystemExit("the corpus must carry a reference, a control and a season capture")
    ref_path = roles[1][0][0]
    ctrl_path = roles[2][0][0]
    season_path, season_env, season_segs = roles[3][0]

    resolved, roster_dict, audit, (ctrl_env, ctrl_info, ctrl_state) = \
        pin_quantities(docs, ref_path, ctrl_path, None)
    tag = resolved["engine_tag"]
    depth = resolved["window_depth"]
    sentinel = int(resolved["sentinel"], 16)
    budget = resolved["season_budget"]
    threshold = resolved["promotion_threshold"]

    # -- reference capture against its labelled transcript -----------------
    ref_env, ref_segs = read_capture(ref_path)
    ref_records, ref_info = decode_capture(ref_env, ref_segs, docs, tag, sentinel)
    ref_rd = []
    resolve_attribution(ref_records, ref_rd, depth, docs, ref_env["guard_register"],
                        build_dictionary=True, strict=False)
    transcript = {"available": False}
    if docs.transcripts:
        with open(docs.transcripts[0], "r", encoding="utf-8") as fh:
            labelled = [json.loads(ln) for ln in fh if ln.strip()]
        mismatches = []
        for i, want in enumerate(labelled):
            if i >= len(ref_records):
                mismatches.append({"record_ordinal": i, "reason": "missing"})
                continue
            got = ref_records[i]
            mine = {"kind": got["kind"], "logical_segment": got["logical_segment"],
                    "match_id": got["match_id"], "record_ordinal": got["record_ordinal"],
                    "squad_key": got["squad_key"], "tick": got["tick"],
                    "unit": got["unit"], "value": got["value"]}
            diff = {k: [want[k], mine.get(k)] for k in want if mine.get(k) != want[k]}
            if diff:
                mismatches.append({"record_ordinal": i, "fields": diff})
        transcript = {
            "available": True,
            "transcript_records": len(labelled),
            "decoded_records": len(ref_records),
            "mismatched_records": len(mismatches),
            "byte_exact_agreement": len(mismatches) == 0 and len(labelled) == len(ref_records),
            "first_mismatches": mismatches[:5],
        }

    # -- season ------------------------------------------------------------
    season_records, season_info = decode_capture(season_env, season_segs, docs,
                                                 tag, sentinel)
    anomalies = resolve_attribution(season_records, roster_dict, depth, docs,
                                    season_env["guard_register"], strict=False)
    state = settle(season_records, docs, budget, threshold, roster_dict)
    squads = state["squads"]
    t = totals(state)

    registered = [e for e in squads.values() if e["registered"]]
    unregistered = sorted(e["squad_key"] for e in squads.values() if not e["registered"])
    absent = sorted(k for k in docs.registry if k not in squads)

    # -- group reports -----------------------------------------------------
    conf_reports = {}
    for conf in docs.conferences:
        entries = [e for e in registered if e["conference"] == conf]
        conf_reports[conf] = group_report("conference", conf, entries)
    seg_reports = {}
    for seg in docs.segments:
        entries = [e for e in registered if e["segment"] == seg]
        seg_reports[seg] = group_report("segment", seg, entries)

    attribution_residual = t["gross_output"] - sum(r["gross_output"] for r in conf_reports.values())
    segment_residual = t["gross_output"] - sum(r["gross_output"] for r in seg_reports.values())

    bracket, bracket_considered, best_count = elect_bracket(docs, state, threshold)
    docket = elect_docket(docs, state)

    # -- standings ---------------------------------------------------------
    standings = sorted(squads.values(),
                       key=lambda e: (-e["season_score"], -e["standing_points"],
                                      e["squad_key"]))
    ranked = []
    for i, e in enumerate(standings):
        row = entry_record(e)
        row.update({"rank": i + 1, "raw_upkeep": e["raw_upkeep"],
                    "match_awards": e["match_awards"],
                    "registered": e["registered"],
                    "is_ranked": e["is_ranked"],
                    "ladder_weight": e["ladder_weight"],
                    "levy_rate": e["levy_rate"],
                    "effective_owner": e["effective_owner"],
                    "handed_over": e["handed_over"],
                    "results": e["results"]})
        ranked.append(row)

    kinds = Counter(r["kind"] for r in season_records)
    ledger = {KIND_NAMES[k]: kinds.get(k, 0) for k in sorted(KIND_NAMES)}
    reported_output = sum(r["value"] for r in season_records if r["kind"] == 2)

    summary = {
        "capture": {
            "source": os.path.basename(season_path),
            "capture_id": season_env["capture_id"],
            "role_digit": season_env["role_digit"],
            "envelope_version": season_env["version"],
            "guard_register": season_env["guard_register"],
            "guards_active": sorted(n for n, b in docs.guard_bits.items()
                                    if (season_env["guard_register"] >> b) & 1),
            "capture_nonce": season_info["nonce"],
            "season_base_tick": season_env["base_tick"],
            "segment_step": season_info["step"],
            "matches": season_info["recovered_segments"],
            "records": season_info["recovered_records"],
        },
        "policy": {
            "engine_tag": tag,
            "window_depth": depth,
            "sentinel": resolved["sentinel"],
            "count_bias": season_info["bias"],
            "season_budget": budget,
            "promotion_threshold": threshold,
            "eligibility_match_floor": docs.eligibility_floor,
            "ladder_weight_prominence": docs.prominence,
            "standing_points_floor": docs.sp_floor,
            "combined_ladder_weight_floor": docs.weight_floor,
            "combined_gross_output_floor": docs.gross_floor,
            "review_weight": docs.review_weight,
            "docket_size": docs.docket_size,
            "docket_conference_cap": docs.docket_cap,
            "roster_dictionary": roster_dict,
        },
        "totals": {
            "matches": len(state["matches"]),
            "squads": len(squads),
            "registered_squads": len(registered),
            "unregistered_squads": len(unregistered),
            "gross_output": t["gross_output"],
            "voided_output": t["voided_output"],
            "reported_output": t["gross_output"] + t["voided_output"],
            "raw_upkeep": t["raw_upkeep"],
            "charged_upkeep": t["charged_upkeep"],
            "overflow_upkeep": t["overflow_upkeep"],
            "season_score": t["season_score"],
            "standing_points": t["standing_points"],
        },
        "tier_counts": tier_counts(list(squads.values())),
        "standings": ranked,
        "matches": [
            {"match_id": m["match_id"], "logical_position": m["logical_position"],
             "close_tick": m["close_tick"], "records": m["records"],
             "participants": sorted(m["stats"]),
             "conceded": sorted(m["conceded"]),
             "winners": m["winners"], "outright_winner": m["outright"],
             "top_score": m["top_score"],
             "gross_output": m["gross_output"], "voided_output": m["voided_output"],
             "escape_records": m["escape_records"],
             "conference": match_conference(docs, m)[0],
             "review_index": m["voided_output"] + docs.review_weight * m["escape_records"],
             "scores": {k: m["scores"][k] for k in sorted(m["scores"])}}
            for m in state["matches"]],
        "entry_transfers": state["transfers"],
        "effective_owners": {k: v for k, v in sorted(state["owner"].items()) if k != v},
        "attribution_settlement_passes": state["passes"],
        "finals_bracket": bracket,
        "review_docket": {k: v for k, v in docket.items() if k != "all_matches"},
        "conference_totals": {c: {"gross_output": r["gross_output"],
                                  "season_score": r["season_score"],
                                  "entrants": r["entrants"]}
                              for c, r in sorted(conf_reports.items())},
        "segment_totals": {s: {"gross_output": r["gross_output"],
                               "season_score": r["season_score"],
                               "entrants": r["entrants"]}
                           for s, r in sorted(seg_reports.items())},
        "unregistered_squads": unregistered,
        "absent_entrants": absent,
    }

    data_quality = {
        "reference_capture": {
            "source": os.path.basename(ref_path),
            "capture_id": ref_env["capture_id"],
            "envelope_checksum_ok": ref_env["checksum_ok"],
            "declared_segments": ref_info["declared_segments"],
            "recovered_segments": ref_info["recovered_segments"],
            "declared_records": ref_info["declared_records"],
            "recovered_records": ref_info["recovered_records"],
            "roster_dictionary_size": len(roster_dict),
            "transcript_agreement": transcript,
        },
        "control_capture": {
            "source": os.path.basename(ctrl_path),
            "capture_id": ctrl_env["capture_id"],
            "envelope_checksum_ok": ctrl_env["checksum_ok"],
            "declared_segments": ctrl_info["declared_segments"],
            "recovered_segments": ctrl_info["recovered_segments"],
            "segment_residual": ctrl_info["declared_segments"] - ctrl_info["recovered_segments"],
            "declared_records": ctrl_info["declared_records"],
            "recovered_records": ctrl_info["recovered_records"],
            "record_residual": ctrl_info["declared_records"] - ctrl_info["recovered_records"],
            "declared_roster_size": ctrl_info["declared_roster"],
            "registered_squads_recovered": sum(1 for e in ctrl_state["squads"].values()
                                               if e["registered"]),
            "distinct_squads_recovered": len(ctrl_state["squads"]),
            "roster_residual": ctrl_info["declared_roster"] -
                               sum(1 for e in ctrl_state["squads"].values() if e["registered"]),
            "unregistered_squads": sorted(k for k, e in ctrl_state["squads"].items()
                                          if not e["registered"]),
            "squads_reached_only_by_escape":
                audit["control_squads_reached_only_by_escape"],
        },
        "season_capture": {
            "source": os.path.basename(season_path),
            "capture_id": season_env["capture_id"],
            "envelope_checksum_ok": season_env["checksum_ok"],
            "segment_checksums_ok": all(s["checksum_ok"] for s in season_segs),
            "declared_segments": season_info["declared_segments"],
            "recovered_segments": season_info["recovered_segments"],
            "segment_residual": season_info["declared_segments"] - season_info["recovered_segments"],
            "declared_records": season_info["declared_records"],
            "recovered_records": season_info["recovered_records"],
            "record_residual": season_info["declared_records"] - season_info["recovered_records"],
            "declared_roster_size": season_info["declared_roster"],
            "registered_squads_recovered": len(registered),
            "distinct_squads_recovered": len(squads),
            "roster_residual": season_info["declared_roster"] - len(registered),
            "trailing_bytes_after_last_segment": season_env["trailing_bytes"],
        },
        "kind_ledger": ledger,
        "kind_ledger_total": sum(ledger.values()),
        "kind_ledger_residual": season_info["recovered_records"] - sum(ledger.values()),
        "output_ledger": {
            "gross_output": t["gross_output"],
            "voided_output": t["voided_output"],
            "settled_output": t["gross_output"] + t["voided_output"],
            "reported_output": reported_output,
            "residual": reported_output - (t["gross_output"] + t["voided_output"]),
        },
        "upkeep_ledger": {
            "raw_upkeep": t["raw_upkeep"],
            "charged_upkeep": t["charged_upkeep"],
            "overflow_upkeep": t["overflow_upkeep"],
            "residual": t["raw_upkeep"] - (t["charged_upkeep"] + t["overflow_upkeep"]),
        },
        "attribution_residual": {
            "season_gross_output": t["gross_output"],
            "conference_gross_output": sum(r["gross_output"] for r in conf_reports.values()),
            "residual": attribution_residual,
            "segment_gross_output": sum(r["gross_output"] for r in seg_reports.values()),
            "segment_residual": segment_residual,
            "explained_by": sorted(
                [{"squad_key": k, "gross_output": squads[k]["gross_output"],
                  "voided_output": squads[k]["voided_output"],
                  "charged_upkeep": squads[k]["charged_upkeep"],
                  "season_score": squads[k]["season_score"]}
                 for k in unregistered], key=lambda r: r["squad_key"]),
        },
        "unregistered_squads": unregistered,
        "absent_entrants": [
            {"squad_key": k, "entrant_id": docs.registry[k].get("entrant_id"),
             "division": docs.registry[k].get("division"),
             "conference": docs.squad_facts(k)["conference"],
             "segment": docs.squad_facts(k)["segment"]}
            for k in absent],
        "ticks_moving_backwards": season_info["ticks_backwards"],
        "attribution_anomalies": anomalies,
        "findings": [],
    }

    f = data_quality["findings"]
    for label, key in (("segment", "segment_residual"), ("record", "record_residual"),
                       ("roster", "roster_residual")):
        v = data_quality["season_capture"][key]
        if v:
            f.append("season capture %s residual is %d" % (label, v))
    if attribution_residual:
        f.append("attribution residual of %d gross output belongs to unregistered squads %s"
                 % (attribution_residual, ", ".join(unregistered) or "(none)"))
    if data_quality["output_ledger"]["residual"]:
        f.append("output ledger residual is %d" % data_quality["output_ledger"]["residual"])
    if data_quality["upkeep_ledger"]["residual"]:
        f.append("upkeep ledger residual is %d" % data_quality["upkeep_ledger"]["residual"])
    if season_info["ticks_backwards"]:
        f.append("%d ticks move backwards inside a segment" % season_info["ticks_backwards"])
    if unregistered:
        f.append("squads seen on the wire with no entry in the registry: %s"
                 % ", ".join(unregistered))
    if absent:
        f.append("entrants registered but absent from the capture: %s" % ", ".join(absent))
    for label, key in (("segment", "segment_residual"), ("record", "record_residual"),
                       ("roster", "roster_residual")):
        v = data_quality["control_capture"][key]
        if v:
            extra = ""
            if key == "roster_residual" and \
                    audit["control_squads_reached_only_by_escape"]:
                extra = " (squads reached only by an escape directive, never on " \
                        "the wire: %s)" % ", ".join(
                            audit["control_squads_reached_only_by_escape"])
            f.append("control slice %s residual is %d%s" % (label, v, extra))
    escape_only = sorted(k for k, e in squads.items()
                         if all(r["wire_key"] != k for r in season_records))
    if escape_only:
        data_quality["season_capture"]["squads_reached_only_by_escape"] = escape_only
        f.append("squads that never appear on the wire and are reached only by an "
                 "escape directive: %s" % ", ".join(escape_only))
    if anomalies["advance_off_dictionary"]:
        off = sorted({r["wire_key"] for r in season_records
                      if r.get("advance_off_dictionary")})
        stood = [r for r in season_records if r.get("advance_off_dictionary")
                 and r["squad_key"] == r["wire_key"]]
        f.append("%d escape advance directives were applied to squads that hold no "
                 "roster dictionary position (%s); the advance is a no-op, and %d of "
                 "those records finish on their wire attribution"
                 % (anomalies["advance_off_dictionary"], ", ".join(off), len(stood)))
    if not f:
        f.append("every residual is zero")

    policy_audit = {
        "resolved": {
            "engine_tag": tag, "window_depth": depth, "sentinel": resolved["sentinel"],
            "count_bias": season_info["bias"],
            "season_budget": budget, "promotion_threshold": threshold,
        },
        "candidate_spaces": audit["candidate_spaces"],
        "guard_combinations_searched": audit["guard_combinations_searched"],
        "calibration_table": audit["calibration_table"],
        "calibration_all_agree": all(r["agrees"] for r in audit["calibration_table"]),
        "attribution_settlement_passes": state["passes"],
        "control_attribution_settlement_passes": audit["control_settlement_passes"],
        "bracket_candidate_space": {
            "bracket_size": docs.bracket_size,
            "eligibility_match_floor": docs.eligibility_floor,
            "eligible_pool": bracket.get("eligible_pool", []),
            "eligible_pool_size": bracket.get("pool_size", 0),
            "candidate_brackets": bracket_considered,
            "constraints_published": len(docs.constraint_labels) or 10,
            "best_satisfied_constraint_count": best_count,
            "elected_bracket": bracket.get("members", []),
            "elected_satisfied_constraints": bracket.get("satisfied_constraints", 0),
            "tie_break_combined_charged_upkeep": bracket.get("combined_charged_upkeep"),
            "constraint_verdicts": bracket.get("constraints", []),
        },
        "docket_candidate_space": {
            "docket_size": docs.docket_size,
            "conference_cap": docs.docket_cap,
            "matches_considered": len(state["matches"]),
            "candidate_pool": docket.get("candidate_pool", []),
            "elected": [r["match_id"] for r in docket.get("matches", [])],
            "total_review_index": docket.get("total_review_index", 0),
            "match_review_index": docket.get("all_matches", []),
        },
    }

    write_json(os.path.join(out_dir, "summary.json"), summary)
    write_json(os.path.join(out_dir, "data_quality.json"), data_quality)
    write_json(os.path.join(out_dir, "policy_audit.json"), policy_audit)
    for conf, rep in conf_reports.items():
        write_json(os.path.join(out_dir, "region_%s.json" % conf), rep)
    for seg, rep in seg_reports.items():
        write_json(os.path.join(out_dir, "segment_%s.json" % seg), rep)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
