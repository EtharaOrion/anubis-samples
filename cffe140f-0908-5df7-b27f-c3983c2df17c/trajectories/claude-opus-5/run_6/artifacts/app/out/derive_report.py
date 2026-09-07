#!/usr/bin/env python3
"""Settle a sealed tactics-league season from its replay journals.

Usage:  python3 derive_report.py DATA_DIR OUT_DIR

Reads only DATA_DIR, writes only OUT_DIR, standard library only.

The program implements the written contract: it discovers the captures by the
capture identifier in their envelopes, decodes them against the guard register,
pins the unstated policy quantities against the published control figures and
regenerates the nine season reports.
"""

import collections
import csv
import hashlib
import itertools
import json
import math
import os
import re
import struct
import sys

M32 = 0xFFFFFFFF
POW32 = 1 << 32

TIERS = ("champion", "contender", "qualifier", "provisional")
KIND_NAMES = {1: "deploy", 2: "output", 3: "eliminate", 4: "disband",
              5: "concede", 6: "transfer", 8: "close"}


# --------------------------------------------------------------------------
# document loading
# --------------------------------------------------------------------------

def load_corpus(data_dir):
    """Read every document under the data directory."""
    corpus = {"prose": "", "csv": {}, "captures": [], "jsonl": {}, "json": {}}
    prose = []
    for name in sorted(os.listdir(data_dir)):
        path = os.path.join(data_dir, name)
        if not os.path.isfile(path):
            continue
        low = name.lower()
        if low.endswith(".md") or low.endswith(".txt"):
            with open(path, "r", encoding="utf-8") as fh:
                prose.append(fh.read())
        elif low.endswith(".csv"):
            with open(path, "r", encoding="utf-8", newline="") as fh:
                corpus["csv"][name] = list(csv.DictReader(fh))
        elif low.endswith(".rjnl"):
            with open(path, "rb") as fh:
                corpus["captures"].append((name, fh.read()))
        elif low.endswith(".jsonl"):
            with open(path, "r", encoding="utf-8") as fh:
                corpus["jsonl"][name] = [json.loads(l) for l in fh if l.strip()]
        elif low.endswith(".json"):
            with open(path, "r", encoding="utf-8") as fh:
                corpus["json"][name] = json.load(fh)
    corpus["prose"] = "\n".join(prose)
    return corpus


def find_table(rows_by_file, *required):
    """Return the first parsed csv whose header carries every required column."""
    for name in sorted(rows_by_file):
        rows = rows_by_file[name]
        if rows and all(c in rows[0] for c in required):
            return rows
    raise SystemExit("no table with columns %s under the data directory" % (required,))


def roster(prose, label):
    """`<label> roster: \\`a\\`, \\`b\\`, ...` -> ['a', 'b', ...]."""
    m = re.search(re.escape(label) + r"\s*roster\s*:\s*(.+)", prose, re.I)
    if not m:
        return []
    return re.findall(r"`([^`]+)`", m.group(1))


def figure(prose, label, default=None):
    """`<label>: \\`7\\`` -> 7."""
    m = re.search(re.escape(label) + r"\s*:\s*`?(-?\d+)`?", prose, re.I)
    return int(m.group(1)) if m else default


def guard_bits(prose):
    """Parse the `| bit | guard |` table into {name: bit}."""
    bits = {}
    for line in prose.splitlines():
        m = re.match(r"\s*\|\s*(\d+)\s*\|\s*([a-z][a-z0-9 _-]*?)\s*\|\s*$", line, re.I)
        if m:
            bits[m.group(2).strip().lower()] = int(m.group(1))
    default = {"keystream": 0, "chain": 1, "delta2": 2, "driftkey": 3, "segperm": 4,
               "backref": 5, "endian": 6, "countbias": 7, "shadow": 8, "rotate": 9,
               "alias": 10, "escape": 11}
    for k, v in default.items():
        bits.setdefault(k, v)
    return bits


def control_figures(prose):
    """`- C4 gross output: 6228` -> [{'id','label','published'}, ...]."""
    out = []
    for m in re.finditer(r"^[-*]\s*(C\d+)\s+([A-Za-z][A-Za-z ]*?)\s*:\s*(-?\d+)\s*$",
                         prose, re.M):
        out.append({"id": m.group(1), "label": m.group(2).strip().lower(),
                    "published": int(m.group(3))})
    return out


def as_int(text):
    text = text.strip()
    return int(text, 16) if text.lower().startswith("0x") else int(text)


# --------------------------------------------------------------------------
# journal decoding
# --------------------------------------------------------------------------

def rot_left(b, n):
    n %= len(b)
    return b[n:] + b[:n]


_KS_CACHE = {}


def keystream(tag, mid, salt, ordinal, width):
    key = (tag, mid, salt, ordinal)
    dig = _KS_CACHE.get(key)
    if dig is None:
        dig = hashlib.sha256(("%s:%d:%d:%d" % (tag, mid, salt, ordinal)).encode())
        dig = dig.digest()
        _KS_CACHE[key] = dig
    while len(dig) < width:
        dig = dig + hashlib.sha256(dig).digest()
    return dig[:width]


def read_envelope(blob, bits):
    if len(blob) < 36 or blob[0:4] != b"RJNL":
        raise ValueError("not a journal")
    guards = struct.unpack("<H", blob[6:8])[0]
    env = {"version": blob[4], "reserved": blob[5], "guard_register": guards}
    env["active"] = sorted(n for n, b in bits.items() if guards >> b & 1)
    endian = bool(guards >> bits["endian"] & 1)
    env["nonce"] = struct.unpack(">I" if endian else "<I", blob[8:12])[0]
    (env["decl_segments"], env["decl_records"], env["base_tick"],
     env["capture_id"], env["decl_roster"], env["checksum"]) = struct.unpack("<6I", blob[12:36])
    env["checksum_ok"] = (sum(blob[0:32]) % POW32) == env["checksum"]
    return env


def segment_order(nonce, nseg):
    """Logical position L -> storage slot, for the segment permutation guard."""
    step = ((nonce >> 8) % nseg) + 2
    while math.gcd(step, nseg) != 1:
        step += 1
    slot_of = {}
    for slot in range(nseg):
        slot_of[(nonce + slot * step) % nseg] = slot
    if len(slot_of) != nseg:
        raise ValueError("segment permutation is not a bijection")
    return [slot_of[L] for L in range(nseg)], step


def decode_capture(blob, bits, tag, sentinel):
    """Recover the record stream of one capture in logical order."""
    env = read_envelope(blob, bits)
    guards = env["guard_register"]
    on = lambda name: bool(guards >> bits[name] & 1)

    bias = (sum(tag.encode()) % 11) if on("countbias") else 0
    nseg = env["decl_segments"] - bias
    nrec = env["decl_records"] - bias
    nrost = env["decl_roster"] - bias
    if nseg <= 0 or nrec < 0:
        raise ValueError("count bias exceeds declared counts")

    off, segs = 36, []
    for _ in range(nseg):
        if blob[off:off + 4] != b"SEG0":
            raise ValueError("bad segment marker")
        slot, mid, rc, salt, pw, pck = struct.unpack("<6I", blob[off + 4:off + 28])
        rc -= bias
        if rc < 0:
            raise ValueError("negative segment record count")
        payload = blob[off + 28:off + 28 + pw]
        if len(payload) != pw:
            raise ValueError("segment payload truncated")
        if sum(payload) % POW32 != pck:
            raise ValueError("payload checksum mismatch")
        segs.append({"slot": slot, "match_id": mid, "records": rc, "salt": salt,
                     "width": pw, "payload": payload})
        off += 28 + pw
    if off != len(blob):
        raise ValueError("trailing bytes after last segment")

    if on("segperm") and nseg > 1:
        order, step = segment_order(env["nonce"], nseg)
    else:
        order, step = list(range(nseg)), None

    recs = []
    gord = 0
    drift = 0
    prev_tick = None
    backwards = []
    for logical, slot in enumerate(order):
        seg = segs[slot]
        salt = (seg["salt"] ^ drift) & M32 if on("driftkey") else seg["salt"]
        seg["effective_salt"] = salt
        seg["logical"] = logical
        payload = seg["payload"]
        pos = 0
        prev_d1 = 0
        prev_seg_tick = None
        seg_sum = 0
        for r in range(seg["records"]):
            if pos + 2 > len(payload):
                raise ValueError("payload exhausted")
            visible, shadow = payload[pos], payload[pos + 1]
            width = (shadow ^ (salt & 0xFF)) if on("shadow") else visible
            if width < 13 or pos + 2 + width > len(payload):
                raise ValueError("implausible frame width %d" % width)
            body = payload[pos + 2:pos + 2 + width]
            pos += 2 + width
            if on("keystream"):
                ks = keystream(tag, seg["match_id"], salt, r, width)
                body = bytes(a ^ b for a, b in zip(body, ks))
            if on("rotate"):
                body = rot_left(body, (-(salt + gord)) % width)

            i, dirs = 0, []
            if on("escape"):
                while len(dirs) < 2 and i + 1 < len(body) and body[i] == sentinel:
                    dirs.append((body[i + 1] >> 4, body[i + 1] & 0x0F))
                    i += 2
            body = body[i:]
            kind, mode = body[0], body[1]
            if mode == 0:
                raw = body[2:5].decode("ascii")
                tail = body[5:]
            elif mode in (1, 2):
                raw = body[2]
                tail = body[3:]
            else:
                raise ValueError("unknown attribution mode %d" % mode)
            if len(tail) != 10:
                raise ValueError("record body is %d bytes, expected 10" % len(tail))
            unit, stored_tick, stored_val = struct.unpack("<HII", tail)

            if on("delta2"):
                d1 = (prev_d1 + stored_tick) & M32
                tick = ((prev_seg_tick or 0) + d1) & M32
                prev_d1 = d1
            else:
                tick = stored_tick
            if prev_seg_tick is not None and tick < prev_seg_tick:
                backwards.append({"match_id": seg["match_id"], "record_ordinal": gord,
                                  "tick": tick, "previous_tick": prev_seg_tick})
            prev_seg_tick = tick

            if on("chain"):
                value = stored_val ^ (env["nonce"] if prev_tick is None else prev_tick)
            else:
                value = stored_val
            value &= M32
            prev_tick = tick
            seg_sum = (seg_sum + value) & M32

            recs.append({"kind": kind, "mode": mode, "raw": raw, "unit": unit,
                         "tick": tick, "value": value, "dirs": dirs,
                         "match_id": seg["match_id"], "logical_segment": logical,
                         "segment_ordinal": r, "record_ordinal": gord})
            gord += 1
        drift = (drift + seg_sum) & M32

    meta = {"segments": nseg, "declared_segments": env["decl_segments"],
            "records": nrec, "declared_records": env["decl_records"],
            "roster": nrost, "declared_roster": env["decl_roster"],
            "count_bias": bias, "permutation_step": step,
            "logical_slots": order, "backwards_ticks": backwards}
    return env, segs, recs, meta


def attribute(recs, dictionary, depth, bits, guards):
    """Resolve every record's attribution key; returns unresolved advance count."""
    n = len(dictionary)
    index = {k: i for i, k in enumerate(dictionary)}
    window = []
    unresolved = 0
    for r in recs:
        if r["mode"] == 0:
            wire = r["raw"]
        elif r["mode"] == 1:
            if r["raw"] >= len(window):
                raise ValueError("back reference %d beyond window of %d"
                                 % (r["raw"], len(window)))
            wire = window[r["raw"]]
        else:
            if n == 0:
                raise ValueError("empty roster dictionary")
            wire = dictionary[r["raw"] % n]
        key = wire
        # Directive pairs sit ahead of the body; the pair nearest the body binds
        # first, so the outermost directive is the one that finally stands.
        for op, operand in reversed(r["dirs"]):
            if op == 1 and n:
                key = dictionary[operand % n]
            elif op == 2 and n:
                if key in index:
                    key = dictionary[(index[key] + operand) % n]
                else:
                    unresolved += 1
        r["wire_key"] = wire
        r["squad"] = key
        # the window is refreshed with the key as written on the wire,
        # before any escape directive has been weighed
        if wire in window:
            window.remove(wire)
        window.append(wire)
        if len(window) > depth:
            del window[0]
    return unresolved


# --------------------------------------------------------------------------
# match settlement
# --------------------------------------------------------------------------

def settle_matches(recs):
    """Per-match gross/voided output, raw upkeep, participants, escapes."""
    by_seg = collections.OrderedDict()
    for r in recs:
        by_seg.setdefault(r["logical_segment"], []).append(r)
    matches = []
    for logical in sorted(by_seg):
        rs = by_seg[logical]
        mid = rs[0]["match_id"]
        close = None
        concede, elim, disband = {}, {}, {}
        for r in rs:
            if r["kind"] == 8:
                close = r["tick"] if close is None else max(close, r["tick"])
            elif r["kind"] == 5:
                s = r["squad"]
                concede[s] = r["tick"] if s not in concede else min(concede[s], r["tick"])
            elif r["kind"] == 3:
                k = (r["squad"], r["unit"])
                elim[k] = r["tick"] if k not in elim else min(elim[k], r["tick"])
            elif r["kind"] == 4:
                k = (r["squad"], r["unit"])
                disband[k] = r["tick"] if k not in disband else min(disband[k], r["tick"])
        if close is None:
            close = max((r["tick"] for r in rs), default=0)

        gross = collections.Counter()
        void = collections.Counter()
        raw = collections.Counter()
        parts = set()
        escapes = 0
        for r in rs:
            parts.add(r["squad"])
            if r["dirs"]:
                escapes += 1
            if r["kind"] == 2:
                s, k = r["squad"], (r["squad"], r["unit"])
                withdrawn = (k in elim and r["tick"] >= elim[k]) or \
                            (s in concede and r["tick"] >= concede[s])
                (void if withdrawn else gross)[s] += r["value"]
            elif r["kind"] == 1:
                k = (r["squad"], r["unit"])
                end = disband.get(k, close)
                if end < r["tick"]:
                    end = r["tick"]
                raw[r["squad"]] += r["value"] * (end - r["tick"])
        matches.append({"match_id": mid, "logical": logical, "close_tick": close,
                        "gross": gross, "void": void, "raw": raw,
                        "participants": parts, "conceded": set(concede),
                        "escape_records": escapes, "records": len(rs)})
    return matches


def effective_owners(recs, dictionary):
    """Follow hand overs to their end; returns (owner map, sweep count, links)."""
    n = len(dictionary)
    last = {}
    for r in recs:
        if r["kind"] == 6 and n:
            last[r["squad"]] = (r["record_ordinal"], dictionary[r["value"] % n])
    links = {s: t for s, (_, t) in last.items()}

    squads = set(r["squad"] for r in recs) | set(links) | set(links.values())
    nxt = {s: links.get(s, s) for s in squads}

    # A closed set of hand overs settles on the member whose key sorts first.
    colour, state = {}, {}
    for s in squads:
        if s in state:
            continue
        path, cur = [], s
        while cur not in state and cur not in colour:
            colour[cur] = len(path)
            path.append(cur)
            cur = nxt[cur]
        if cur in colour:                       # closed on itself
            cycle = path[colour[cur]:]
            winner = min(cycle)
            for m in cycle:
                nxt[m] = winner
            nxt[winner] = winner
        for m in path:
            state[m] = True
            colour.pop(m, None)

    owner = dict(nxt)
    passes = 0
    while True:
        passes += 1
        step = {s: owner[owner[s]] for s in owner}
        if step == owner:
            break
        owner = step
    return owner, passes, links


# --------------------------------------------------------------------------
# season settlement
# --------------------------------------------------------------------------

class Policy(object):
    def __init__(self, divisions, licences, registry):
        self.division = divisions          # code -> (conference, ladder weight)
        self.licence = licences            # code -> (segment, levy rate)
        self.registry = registry           # squad key -> row
        self.conferences = sorted(set(v[0] for v in divisions.values()))
        self.segments = sorted(set(v[0] for v in licences.values()))

    def weight(self, squad):
        row = self.registry.get(squad)
        return self.division.get(row["division"], (None, 1))[1] if row else 1

    def levy(self, squad):
        row = self.registry.get(squad)
        return self.licence.get(row["licence_class"], (None, 1))[1] if row else 1

    def conference(self, squad):
        row = self.registry.get(squad)
        return self.division.get(row["division"], (None, 1))[0] if row else None

    def segment(self, squad):
        row = self.registry.get(squad)
        return self.licence.get(row["licence_class"], (None, 1))[0] if row else None


def settle_season(matches, policy, budget_cap, threshold):
    """Score, tier and rank every squad seen in the capture."""
    squads = sorted(set().union(*[m["participants"] for m in matches]) if matches else set())
    budget = {s: budget_cap for s in squads}
    acc = {s: {"squad_key": s, "matches": 0, "gross_output": 0, "voided_output": 0,
               "raw_upkeep": 0, "charged_upkeep": 0, "overflow_upkeep": 0,
               "season_score": 0, "match_awards": 0, "streak_credit": 0,
               "outcomes": []} for s in squads}

    for m in matches:
        m["charged"] = {}
        m["score"] = {}
        for s in sorted(m["participants"]):
            raw = m["raw"].get(s, 0)
            fits = min(raw, budget[s])
            budget[s] -= fits
            over = raw - fits
            m["charged"][s] = fits
            a = acc[s]
            a["matches"] += 1
            a["gross_output"] += m["gross"].get(s, 0)
            a["voided_output"] += m["void"].get(s, 0)
            a["raw_upkeep"] += raw
            a["charged_upkeep"] += fits
            a["overflow_upkeep"] += over
            sc = m["gross"].get(s, 0) * policy.weight(s) - fits * policy.levy(s)
            m["score"][s] = sc
            a["season_score"] += sc

        contenders = [s for s in m["participants"] if s not in m["conceded"]]
        best = max((m["score"][s] for s in contenders), default=None)
        top = sorted(s for s in contenders if m["score"][s] == best) if best is not None else []
        m["best_score"] = best
        m["winners"] = top
        for s in sorted(m["participants"]):
            if s in top and len(top) == 1:
                acc[s]["match_awards"] += 3
                acc[s]["outcomes"].append("win")
            elif s in top:
                acc[s]["match_awards"] += 1
                acc[s]["outcomes"].append("shared")
            else:
                acc[s]["outcomes"].append("none")

    for a in acc.values():
        run = 0
        for o in a["outcomes"]:
            if o == "win":
                run += 1
                if run > 1:
                    a["streak_credit"] += 2
            else:
                run = 0
        a["standing_points"] = a["match_awards"] + a["streak_credit"]
        v = a["season_score"]
        if v >= 3 * threshold:
            a["tier"] = "champion"
        elif v >= 2 * threshold:
            a["tier"] = "contender"
        elif v >= threshold:
            a["tier"] = "qualifier"
        else:
            a["tier"] = "provisional"
        del a["outcomes"]
    return acc


# --------------------------------------------------------------------------
# calibration
# --------------------------------------------------------------------------

def capture_metrics(blob, bits, dictionary, tag, sentinel, depth, policy,
                    budget_cap=None, threshold=None):
    env, segs, recs, meta = decode_capture(blob, bits, tag, sentinel)
    attribute(recs, dictionary, depth, bits, env["guard_register"])
    matches = settle_matches(recs)
    met = {
        "matches opened": meta["segments"],
        "records recovered": len(recs),
        "distinct squads": len(set(r["squad"] for r in recs)),
        "gross output": sum(sum(m["gross"].values()) for m in matches),
        "voided output": sum(sum(m["void"].values()) for m in matches),
        "raw upkeep": sum(sum(m["raw"].values()) for m in matches),
    }
    if budget_cap is not None:
        acc = settle_season(matches, policy, budget_cap,
                            threshold if threshold is not None else 1)
        met["charged upkeep"] = sum(a["charged_upkeep"] for a in acc.values())
        met["overflow upkeep"] = sum(a["overflow_upkeep"] for a in acc.values())
        if threshold is not None:
            counts = collections.Counter(a["tier"] for a in acc.values())
            for t in TIERS:
                met["%s entrants" % t] = counts.get(t, 0)
    return met


GUARD_STAGE = ["matches opened", "records recovered", "distinct squads",
               "gross output", "voided output", "raw upkeep"]
BUDGET_STAGE = ["charged upkeep", "overflow upkeep"]
TIER_STAGE = ["%s entrants" % t for t in TIERS]


def first_failure(metrics, published, stage):
    for fig in published:
        if fig["label"] in stage:
            got = metrics.get(fig["label"])
            if got is None or got != fig["published"]:
                return {"control": fig["id"], "figure": fig["label"],
                        "published": fig["published"], "reproduced": got}
    return None


def calibrate(control_blob, bits, ref_recs, tags, depths, sentinels,
              budgets, thresholds, published, policy, audit):
    """Pin every unstated quantity against the published control figures."""
    def build_dict(tag, sentinel):
        dic = []
        for r in ref_recs[(tag, sentinel)]:
            if r["mode"] == 0 and r["raw"] not in dic:
                dic.append(r["raw"])
            elif r["mode"] != 0 and r["raw"] not in dic:
                pass
        return dic

    combos = []
    for tag, depth, sentinel in itertools.product(tags, depths, sentinels):
        rec = {"engine_tag": tag, "window_depth": depth,
               "sentinel": "0x%02X" % sentinel}
        try:
            dic = build_dict(tag, sentinel)
            met = capture_metrics(control_blob, bits, dic, tag, sentinel, depth, policy)
            bad = first_failure(met, published, GUARD_STAGE)
            rec["verdict"] = "accepted" if bad is None else "rejected"
            rec["falsifier"] = bad
            rec["metrics"] = {k: met.get(k) for k in GUARD_STAGE}
            rec["reached"] = sum(1 for f in published if f["label"] in GUARD_STAGE and
                                 met.get(f["label"]) == f["published"])
        except Exception as exc:
            rec["verdict"] = "rejected"
            rec["reached"] = 0
            first = next((f for f in published if f["label"] in GUARD_STAGE), None)
            rec["falsifier"] = {"control": first["id"] if first else None,
                                "figure": first["label"] if first else None,
                                "published": first["published"] if first else None,
                                "reproduced": None,
                                "detail": "capture does not decode: %s" % exc}
        combos.append(rec)

    winners = [c for c in combos if c["verdict"] == "accepted"]
    if not winners:
        raise SystemExit("no reading of the guards reproduces the control figures")
    win = winners[0]
    tag = win["engine_tag"]
    depth = win["window_depth"]
    sentinel = as_int(win["sentinel"])
    dictionary = build_dict(tag, sentinel)

    def verdicts(name, values, fmt=str):
        rows = []
        for v in values:
            mine = [c for c in combos if c[name] == v]
            good = [c for c in mine if c["verdict"] == "accepted"]
            best = max(mine, key=lambda c: c["reached"]) if mine else None
            rows.append({"candidate": fmt(v),
                         "verdict": "accepted" if good else "rejected",
                         "falsifier": None if good else (best or {}).get("falsifier")})
        return rows

    audit["engine_tag"] = {
        "roster": list(tags), "candidates": verdicts("engine_tag", tags),
        "resolved": tag}
    audit["window_depth"] = {
        "roster": list(depths), "candidates": verdicts("window_depth", depths),
        "resolved": depth}
    audit["sentinel"] = {
        "roster": ["0x%02X" % s for s in sentinels],
        "candidates": verdicts("sentinel", ["0x%02X" % s for s in sentinels]),
        "resolved": "0x%02X" % sentinel}
    audit["joint_guard_candidate_space"] = len(combos)

    # stage two: the season budget
    budget_rows, budget = [], None
    for b in budgets:
        met = capture_metrics(control_blob, bits, dictionary, tag, sentinel, depth,
                              policy, budget_cap=b)
        bad = first_failure(met, published, BUDGET_STAGE)
        budget_rows.append({"candidate": b,
                            "verdict": "accepted" if bad is None else "rejected",
                            "falsifier": bad})
        if bad is None and budget is None:
            budget = b
    audit["season_budget"] = {"roster": list(budgets), "candidates": budget_rows,
                              "resolved": budget}
    if budget is None:
        raise SystemExit("no budget on the roster reproduces the control figures")

    # stage three: the promotion threshold
    thr_rows, threshold = [], None
    for t in thresholds:
        met = capture_metrics(control_blob, bits, dictionary, tag, sentinel, depth,
                              policy, budget_cap=budget, threshold=t)
        bad = first_failure(met, published, TIER_STAGE)
        thr_rows.append({"candidate": t,
                         "verdict": "accepted" if bad is None else "rejected",
                         "falsifier": bad})
        if bad is None and threshold is None:
            threshold = t
    audit["promotion_threshold"] = {"roster": list(thresholds),
                                    "candidates": thr_rows, "resolved": threshold}
    if threshold is None:
        raise SystemExit("no threshold on the roster reproduces the control figures")

    met = capture_metrics(control_blob, bits, dictionary, tag, sentinel, depth,
                          policy, budget_cap=budget, threshold=threshold)
    table = [{"control": f["id"], "figure": f["label"], "published": f["published"],
              "reproduced": met.get(f["label"]),
              "agrees": met.get(f["label"]) == f["published"]} for f in published]
    return tag, depth, sentinel, budget, threshold, dictionary, table


# --------------------------------------------------------------------------
# bracket and docket
# --------------------------------------------------------------------------

def elect_bracket(pool, acc, policy, owner, handed, limits):
    """Return the bracket meeting the largest number of the league's wishes."""
    size = 3
    checks = [
        "prominent_division", "both_segments", "standing_points_floor",
        "distinct_conferences", "ranked_entrant", "combined_ladder_weight",
        "no_overflow_upkeep", "champion_member", "combined_gross_output",
        "no_handed_entry",
    ]

    def evaluate(members):
        weights = [policy.weight(s) for s in members]
        confs = [policy.conference(s) for s in members]
        segs = set(policy.segment(s) for s in members) - {None}
        named = [c for c in confs if c is not None]
        return {
            "prominent_division": any(w >= limits["prominence"] for w in weights),
            "both_segments": set(policy.segments) <= segs,
            "standing_points_floor": all(acc[s]["standing_points"] >= limits["points_floor"]
                                         for s in members),
            "distinct_conferences": len(named) == len(set(named)),
            "ranked_entrant": any(
                (policy.registry.get(s) or {}).get("is_ranked", "").strip().lower()
                in ("yes", "true", "1", "y") for s in members),
            "combined_ladder_weight": sum(weights) >= limits["weight_floor"],
            "no_overflow_upkeep": all(acc[s]["overflow_upkeep"] == 0 for s in members),
            "champion_member": any(acc[s]["tier"] == "champion" for s in members),
            "combined_gross_output": sum(acc[s]["gross_output"] for s in members)
                                     >= limits["gross_floor"],
            # a squad that recorded a hand over has handed its entry away, even
            # where a closed set of hand overs settles it back on itself
            "no_handed_entry": all(s not in handed for s in members),
        }

    best = None
    space = 0
    for members in itertools.combinations(pool, size):
        space += 1
        res = evaluate(members)
        met = sum(1 for c in checks if res[c])
        upkeep = sum(acc[s]["charged_upkeep"] for s in members)
        key = (-met, upkeep, list(members))
        if best is None or key < best[0]:
            best = (key, members, res, met, upkeep)
    if best is None:
        return {"members": [], "satisfied": 0, "candidate_space": 0,
                "constraints": {c: False for c in checks},
                "charged_upkeep": 0, "pool": list(pool)}
    _, members, res, met, upkeep = best
    return {"members": list(members), "satisfied": met, "candidate_space": space,
            "constraint_count": len(checks), "constraints": res,
            "charged_upkeep": upkeep, "pool": list(pool)}


def elect_docket(match_rows, size, cap):
    """Largest total review index, never more than `cap` matches per conference."""
    n = len(match_rows)
    size = min(size, n)
    if size == 0:
        return [], 0, 0

    by_conf = collections.defaultdict(list)
    for m in match_rows:
        by_conf[m["conference"]].append(m)

    def feasible(subset):
        c = collections.Counter(m["conference"] for m in subset)
        return all(k is None or v <= cap for k, v in c.items())

    space = math.comb(n, size)
    best = None
    if space <= 400000:
        pool = match_rows
    else:
        # Only the strongest `cap` matches of a conference can ever be needed,
        # plus a margin so identical review indices are still weighed.
        pool = []
        for conf, rows in by_conf.items():
            rows = sorted(rows, key=lambda m: (-m["review_index"], m["match_id"]))
            keep = len(rows) if conf is None else min(len(rows), cap + size)
            pool.extend(rows[:keep])
        pool.sort(key=lambda m: m["match_id"])
    for subset in itertools.combinations(pool, size):
        if not feasible(subset):
            continue
        total = sum(m["review_index"] for m in subset)
        ids = sorted(m["match_id"] for m in subset)
        key = (-total, ids)
        if best is None or key < best[0]:
            best = (key, subset, total)
    if best is None:
        return [], 0, space
    return list(best[1]), best[2], space


# --------------------------------------------------------------------------
# reports
# --------------------------------------------------------------------------

def entry_row(s, acc, policy):
    row = policy.registry.get(s) or {}
    return {
        "charged_upkeep": acc["charged_upkeep"],
        "conference": policy.conference(s),
        "division": row.get("division"),
        "entrant_id": row.get("entrant_id"),
        "gross_output": acc["gross_output"],
        "licence_class": row.get("licence_class"),
        "matches": acc["matches"],
        "overflow_upkeep": acc["overflow_upkeep"],
        "season_score": acc["season_score"],
        "segment": policy.segment(s),
        "squad_key": s,
        "standing_points": acc["standing_points"],
        "streak_credit": acc["streak_credit"],
        "tier": acc["tier"],
        "voided_output": acc["voided_output"],
    }


def group_report(kind, name, keys, acc, policy):
    entries = [entry_row(s, acc[s], policy) for s in sorted(keys)]
    rep = {
        "charged_upkeep": sum(e["charged_upkeep"] for e in entries),
        "divisions": sorted(set(e["division"] for e in entries if e["division"])),
        "entrants": len(entries),
        "entries": entries,
        "gross_output": sum(e["gross_output"] for e in entries),
        "group_kind": kind,
        "group_name": name,
        "overflow_upkeep": sum(e["overflow_upkeep"] for e in entries),
        "season_score": sum(e["season_score"] for e in entries),
        "standing_points": sum(e["standing_points"] for e in entries),
        "tier_counts": {t: sum(1 for e in entries if e["tier"] == t) for t in TIERS},
        "voided_output": sum(e["voided_output"] for e in entries),
    }
    if kind == "segment":
        rep["licence_classes"] = sorted(set(e["licence_class"] for e in entries
                                            if e["licence_class"]))
    return rep


def write_json(out_dir, name, payload):
    path = os.path.join(out_dir, name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main(argv):
    if len(argv) != 3:
        sys.stderr.write("usage: derive_report.py DATA_DIR OUT_DIR\n")
        return 2
    data_dir, out_dir = argv[1], argv[2]
    os.makedirs(out_dir, exist_ok=True)

    corpus = load_corpus(data_dir)
    prose = corpus["prose"]
    bits = guard_bits(prose)

    divisions = {r["division"]: (r["conference"], int(r["ladder_weight"]))
                 for r in find_table(corpus["csv"], "division", "conference", "ladder_weight")}
    licences = {r["licence_class"]: (r["segment"], int(r["levy_rate"]))
                for r in find_table(corpus["csv"], "licence_class", "segment", "levy_rate")}
    registry_rows = find_table(corpus["csv"], "entrant_id", "squad_key", "division",
                               "licence_class")
    registry = {r["squad_key"]: r for r in registry_rows}
    policy = Policy(divisions, licences, registry)

    tags = roster(prose, "Engine tag") or ["orion-fold"]
    depths = [int(x) for x in roster(prose, "Window depth")] or [8]
    sentinels = [as_int(x) for x in roster(prose, "Sentinel")] or [0x1A]
    budgets = [int(x) for x in roster(prose, "Budget cap")] or [860]
    thresholds = [int(x) for x in roster(prose, "Promotion threshold")] or [700]
    published = control_figures(prose)

    limits = {
        "floor": figure(prose, "Eligibility match floor", 2),
        "prominence": figure(prose, "Ladder weight prominence", 4),
        "points_floor": figure(prose, "Standing points floor", 3),
        "weight_floor": figure(prose, "Combined ladder weight floor", 7),
        "gross_floor": figure(prose, "Combined gross output floor", 900),
        "review_weight": figure(prose, "Review weight", 25),
        "docket_size": figure(prose, "Docket size", 5),
        "docket_cap": figure(prose, "Docket conference cap", 2),
    }

    # --- tell the captures apart by the identifier in their envelopes ---
    roles = {}
    for name, blob in corpus["captures"]:
        env = read_envelope(blob, bits)
        role = {1: "reference", 2: "control", 3: "season"}.get(env["capture_id"] % 10)
        if role:
            roles[role] = (name, blob, env)
    for need in ("reference", "control", "season"):
        if need not in roles:
            raise SystemExit("no %s capture under %s" % (need, data_dir))

    # --- the reference capture carries the roster dictionary ---
    ref_recs = {}
    for tag, sentinel in itertools.product(tags, sentinels):
        try:
            _, _, recs, _ = decode_capture(roles["reference"][1], bits, tag, sentinel)
        except Exception:
            recs = []
        ref_recs[(tag, sentinel)] = recs

    audit = {}
    tag, depth, sentinel, budget, threshold, dictionary, calib = calibrate(
        roles["control"][1], bits, ref_recs, tags, depths, sentinels,
        budgets, thresholds, published, policy, audit)

    # --- baseline cross-check against the labelled transcript, if shipped ---
    baseline_check = {"available": False}
    transcript = None
    for rows in corpus["jsonl"].values():
        if rows and "squad_key" in rows[0] and "record_ordinal" in rows[0]:
            transcript = rows
            break
    if transcript is not None:
        _, _, recs, _ = decode_capture(roles["reference"][1], bits, tag, sentinel)
        mine = [{"kind": r["kind"], "logical_segment": r["logical_segment"],
                 "match_id": r["match_id"], "record_ordinal": r["record_ordinal"],
                 "squad_key": r["raw"], "tick": r["tick"], "unit": r["unit"],
                 "value": r["value"]} for r in recs]
        baseline_check = {"available": True, "transcript_records": len(transcript),
                          "recovered_records": len(mine),
                          "byte_exact": mine == transcript}

    # --- settle the season ---
    senv, ssegs, srecs, smeta = decode_capture(roles["season"][1], bits, tag, sentinel)
    unresolved = attribute(srecs, dictionary, depth, bits, senv["guard_register"])
    matches = settle_matches(srecs)
    owner, passes, links = effective_owners(srecs, dictionary)
    acc = settle_season(matches, policy, budget, threshold)

    squads = sorted(acc)
    registered = [s for s in squads if s in registry]
    unregistered = [s for s in squads if s not in registry]
    absent = sorted(s for s in registry if s not in acc)

    # --- conference and segment reports ---
    for conf in policy.conferences:
        keys = [s for s in registered if policy.conference(s) == conf]
        write_json(out_dir, "region_%s.json" % conf,
                   group_report("conference", conf, keys, acc, policy))
    for seg in policy.segments:
        keys = [s for s in registered if policy.segment(s) == seg]
        write_json(out_dir, "segment_%s.json" % seg,
                   group_report("segment", seg, keys, acc, policy))

    # --- match rows, review index, conference of each match ---
    match_rows = []
    for m in matches:
        best_key, best_gross = None, None
        for s in sorted(m["participants"]):
            if s not in registry:
                continue
            g = m["gross"].get(s, 0)
            if best_gross is None or g > best_gross:
                best_gross, best_key = g, s
        match_rows.append({
            "match_id": m["match_id"], "logical_index": m["logical"],
            "conference": policy.conference(best_key) if best_key else None,
            "lead_squad": best_key,
            "voided_output": sum(m["void"].values()),
            "gross_output": sum(m["gross"].values()),
            "escape_records": m["escape_records"],
            "review_index": sum(m["void"].values())
                            + limits["review_weight"] * m["escape_records"],
            "records": m["records"],
            "participants": sorted(m["participants"]),
            "conceded": sorted(m["conceded"]),
            "winners": m["winners"],
        })

    docket, docket_total, docket_space = elect_docket(
        match_rows, limits["docket_size"], limits["docket_cap"])
    docket_ids = sorted(m["match_id"] for m in docket)
    docket_spread = collections.Counter(m["conference"] for m in docket)

    # --- finals bracket ---
    pool = sorted(s for s in squads
                  if owner.get(s, s) == s and acc[s]["matches"] >= limits["floor"])
    handed = set(k for k, v in links.items() if v != k)
    bracket = elect_bracket(pool, acc, policy, owner, handed, limits)

    # --- standings ---
    order = sorted(squads, key=lambda s: (-acc[s]["season_score"],
                                          -acc[s]["standing_points"], s))
    standings = []
    for i, s in enumerate(order):
        row = entry_row(s, acc[s], policy)
        row.update({"rank": i + 1, "registered": s in registry,
                    "raw_upkeep": acc[s]["raw_upkeep"],
                    "match_awards": acc[s]["match_awards"],
                    "effective_owner": owner.get(s, s),
                    "is_own_effective_owner": owner.get(s, s) == s})
        standings.append(row)

    totals = {k: sum(a[k] for a in acc.values()) for k in
              ("gross_output", "voided_output", "raw_upkeep", "charged_upkeep",
               "overflow_upkeep", "season_score", "standing_points", "streak_credit")}

    conf_gross = sum(sum(acc[s]["gross_output"] for s in registered
                         if policy.conference(s) == c) for c in policy.conferences)
    seg_gross = sum(sum(acc[s]["gross_output"] for s in registered
                        if policy.segment(s) == g) for g in policy.segments)

    summary = {
        "season": {
            "capture_file": roles["season"][0],
            "capture_id": senv["capture_id"],
            "guard_register": senv["guard_register"],
            "guards_active": senv["active"],
            "base_tick": senv["base_tick"],
            "matches": smeta["segments"],
            "records_recovered": len(srecs),
            "squads_recovered": len(squads),
            "match_order": [m["match_id"] for m in match_rows],
        },
        "policy": {
            "engine_tag": tag,
            "window_depth": depth,
            "sentinel": "0x%02X" % sentinel,
            "count_bias": smeta["count_bias"],
            "season_budget": budget,
            "promotion_threshold": threshold,
            "eligibility_match_floor": limits["floor"],
            "review_weight": limits["review_weight"],
            "docket_size": limits["docket_size"],
            "docket_conference_cap": limits["docket_cap"],
        },
        "totals": totals,
        "tier_counts": {t: sum(1 for a in acc.values() if a["tier"] == t) for t in TIERS},
        "standings": standings,
        "conference_totals": {
            c: {"entrants": sum(1 for s in registered if policy.conference(s) == c),
                "gross_output": sum(acc[s]["gross_output"] for s in registered
                                    if policy.conference(s) == c),
                "season_score": sum(acc[s]["season_score"] for s in registered
                                    if policy.conference(s) == c),
                "standing_points": sum(acc[s]["standing_points"] for s in registered
                                       if policy.conference(s) == c)}
            for c in policy.conferences},
        "segment_totals": {
            g: {"entrants": sum(1 for s in registered if policy.segment(s) == g),
                "gross_output": sum(acc[s]["gross_output"] for s in registered
                                    if policy.segment(s) == g),
                "season_score": sum(acc[s]["season_score"] for s in registered
                                    if policy.segment(s) == g),
                "standing_points": sum(acc[s]["standing_points"] for s in registered
                                       if policy.segment(s) == g)}
            for g in policy.segments},
        "matches": match_rows,
        "effective_owners": {s: owner.get(s, s) for s in squads},
        "hand_overs": [{"from": k, "to": v} for k, v in sorted(links.items())],
        "attribution_settlement_passes": passes,
        "finals_bracket": {
            "members": bracket["members"],
            "satisfied_constraints": bracket["satisfied"],
            "constraint_count": bracket.get("constraint_count", 0),
            "constraints": bracket["constraints"],
            "combined_charged_upkeep": bracket["charged_upkeep"],
            "combined_ladder_weight": sum(policy.weight(s) for s in bracket["members"]),
            "combined_gross_output": sum(acc[s]["gross_output"] for s in bracket["members"]),
            "eligible_pool": bracket["pool"],
        },
        "review_docket": {
            "match_ids": docket_ids,
            "total_review_index": docket_total,
            "conference_spread": {str(k): v for k, v in sorted(
                docket_spread.items(), key=lambda kv: (kv[0] is None, kv[0]))},
            "matches": [{"match_id": m["match_id"], "conference": m["conference"],
                         "review_index": m["review_index"],
                         "voided_output": m["voided_output"],
                         "escape_records": m["escape_records"]}
                        for m in sorted(docket, key=lambda m: m["match_id"])],
        },
    }
    write_json(out_dir, "summary.json", summary)

    # --- data quality ---
    cap_rows = []
    for role in ("reference", "control", "season"):
        name, blob, env = roles[role]
        try:
            e2, segs2, recs2, meta2 = decode_capture(blob, bits, tag, sentinel)
            attribute(recs2, dictionary, depth, bits, e2["guard_register"])
            distinct = sorted(set(r["squad"] for r in recs2))
        except Exception as exc:
            cap_rows.append({"role": role, "file": name, "error": str(exc)})
            continue
        reg_seen = [s for s in distinct if s in registry]
        cap_rows.append({
            "role": role, "file": name, "capture_id": e2["capture_id"],
            "guard_register": e2["guard_register"], "guards_active": e2["active"],
            "envelope_checksum_ok": e2["checksum_ok"],
            "count_bias": meta2["count_bias"],
            "declared_segments": meta2["segments"],
            "recovered_segments": len(segs2),
            "segment_residual": meta2["segments"] - len(segs2),
            "declared_records": meta2["records"],
            "recovered_records": len(recs2),
            "record_residual": meta2["records"] - len(recs2),
            "declared_roster": meta2["roster"],
            "registered_squads_recovered": len(reg_seen),
            "distinct_squads_recovered": len(distinct),
            "roster_residual": meta2["roster"] - len(reg_seen),
            "backwards_tick_events": len(meta2["backwards_ticks"]),
        })

    kinds = collections.Counter(r["kind"] for r in srecs)
    ledger = {name: kinds.get(k, 0) for k, name in sorted(KIND_NAMES.items())}
    ledger_other = {str(k): v for k, v in kinds.items() if k not in KIND_NAMES}
    total_output = sum(r["value"] for r in srecs if r["kind"] == 2)

    unreg_totals = {k: sum(acc[s][k] for s in unregistered) for k in
                    ("gross_output", "voided_output", "raw_upkeep",
                     "charged_upkeep", "overflow_upkeep", "season_score")}

    quality = {
        "baseline_transcript_check": baseline_check,
        "captures": cap_rows,
        "season_declaration": {
            "declared_segments": smeta["segments"],
            "recovered_segments": len(ssegs),
            "segment_residual": smeta["segments"] - len(ssegs),
            "declared_records": smeta["records"],
            "recovered_records": len(srecs),
            "record_residual": smeta["records"] - len(srecs),
            "declared_roster": smeta["roster"],
            "registered_squads_recovered": len(registered),
            "distinct_squads_recovered": len(squads),
            "roster_residual": smeta["roster"] - len(registered),
        },
        "kind_ledger": ledger,
        "kind_ledger_unnamed": ledger_other,
        "kind_ledger_residual": len(srecs) - sum(ledger.values()) - sum(ledger_other.values()),
        "output_ledger": {
            "gross_output": totals["gross_output"],
            "voided_output": totals["voided_output"],
            "output_reported": total_output,
            "residual": total_output - totals["gross_output"] - totals["voided_output"],
        },
        "upkeep_ledger": {
            "charged_upkeep": totals["charged_upkeep"],
            "overflow_upkeep": totals["overflow_upkeep"],
            "raw_upkeep": totals["raw_upkeep"],
            "residual": totals["raw_upkeep"] - totals["charged_upkeep"]
                        - totals["overflow_upkeep"],
        },
        "attribution_residual": {
            "season_gross_output": totals["gross_output"],
            "conference_gross_output": conf_gross,
            "residual": totals["gross_output"] - conf_gross,
            "segment_gross_output": seg_gross,
            "segment_residual": totals["gross_output"] - seg_gross,
        },
        "unregistered_squads": unregistered,
        "unregistered_totals": unreg_totals,
        "absent_entrants": absent,
        "backwards_tick_events": len(smeta["backwards_ticks"]),
        "backwards_tick_detail": smeta["backwards_ticks"],
        "unresolved_escape_advances": unresolved,
        "findings": [],
    }
    f = quality["findings"]
    q = quality["season_declaration"]
    if q["record_residual"]:
        f.append("season declares %d records, %d recovered (residual %d)"
                 % (q["declared_records"], q["recovered_records"], q["record_residual"]))
    if q["segment_residual"]:
        f.append("season declares %d segments, %d recovered (residual %d)"
                 % (q["declared_segments"], q["recovered_segments"], q["segment_residual"]))
    if q["roster_residual"]:
        f.append("season declares a roster of %d registered squads, %d recovered "
                 "(residual %d)" % (q["declared_roster"],
                                    q["registered_squads_recovered"], q["roster_residual"]))
    if unregistered:
        f.append("squads on the wire with no entry in the registry: %s"
                 % ", ".join(unregistered))
    if quality["attribution_residual"]["residual"]:
        f.append("attribution residual of %d gross output sits outside every conference"
                 % quality["attribution_residual"]["residual"])
    if absent:
        f.append("entrants registered but never seen: %s" % ", ".join(absent))
    if quality["backwards_tick_events"]:
        f.append("%d records step a tick backwards inside their segment"
                 % quality["backwards_tick_events"])
    write_json(out_dir, "data_quality.json", quality)

    # --- policy audit ---
    audit["attribution_settlement_passes"] = passes
    audit["attribution_settlement"] = {
        "hand_overs": [{"from": k, "to": v} for k, v in sorted(links.items())],
        "passes": passes,
        "owners": {s: owner.get(s, s) for s in squads},
    }
    audit["bracket"] = {
        "eligibility_match_floor": limits["floor"],
        "eligible_pool": bracket["pool"],
        "candidate_space": bracket["candidate_space"],
        "bracket_size": 3,
        "constraint_count": bracket.get("constraint_count", 0),
        "best_satisfied_constraints": bracket["satisfied"],
        "elected": bracket["members"],
        "constraints": bracket["constraints"],
        "tie_break_charged_upkeep": bracket["charged_upkeep"],
    }
    audit["docket"] = {
        "docket_size": limits["docket_size"],
        "conference_cap": limits["docket_cap"],
        "candidate_space": docket_space,
        "elected": docket_ids,
        "total_review_index": docket_total,
    }
    audit["calibration"] = calib
    audit["calibration_agrees"] = all(r["agrees"] for r in calib)
    audit["baseline_transcript_check"] = baseline_check
    write_json(out_dir, "policy_audit.json", audit)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
