#!/usr/bin/env python3
"""Reconstruct a crash-reporter incident window's fault ledger and roll-ups.

Usage:  python3 transform.py [DATA_DIR] [OUT_DIR]

The nine withheld wire parameters of the crash reporter are settled against the
published control-slice figures before any incident figure is computed.  Only
the Python standard library is used and all clock / byte / residency arithmetic
is carried in Python integers.
"""

import json
import os
import sys
from itertools import product

# ---------------------------------------------------------------------------
# constants fixed by the fleet's written rules
# ---------------------------------------------------------------------------

TICKS_PER_MS = 1000
TICKS_PER_BUILD = 900000000
CHECK_MOD = 65536
SIGNATURE_MOD = 1000000007
SIGNATURE_LEN = 16
COUNT_BIAS_MOD = 9
HEX_DIGITS = "0123456789abcdef"

# dump flag word
F_SECOND_ORDER = 0x001
F_ENTRY_CHAIN = 0x002
F_UNWIND_DRIFT = 0x004
F_ROTATED = 0x008
F_COUNT_BIAS = 0x010
F_ALT_RADIX = 0x020
F_ALIAS = 0x100
F_SPLIT_BASE = 0x800

# frame flag word
FF_INDEX_IN_SYMBOL = 0x040
FF_SIG_ROTATED = 0x080
FF_COARSE_ALLOC = 0x200
FF_SIG_RING = 0x400

PARAM_DOMAINS = {
    "entry_clock_modulus": [1 << k for k in range(22, 35)],
    "drift_period": [48, 96, 192, 384, 768],
    "rotation_offset": list(range(0, 8)),
    "count_multiplier": list(range(0, 9)),
    "alternate_radix": list(range(11, 36)),
    "signature_offset": list(range(0, 8)),
    "coarse_unit": [64, 128, 256, 512, 1024],
    "ring_width": [4, 8, 12, 16],
    "base_shift_width": [8, 10, 12, 16],
}

# the stage plan: each group of parameters is settled against one control
SEARCH_PLAN = [
    (["count_multiplier"], "CTRL-A"),
    (["rotation_offset"], "CTRL-C"),
    (["entry_clock_modulus"], "CTRL-D"),
    (["drift_period"], "CTRL-E"),
    (["coarse_unit"], "CTRL-F"),
    (["alternate_radix", "base_shift_width"], "CTRL-B"),
    (["signature_offset", "ring_width"], "CTRL-G"),
]

CONTROL_ORDER = ["CTRL-A", "CTRL-B", "CTRL-C", "CTRL-D", "CTRL-E", "CTRL-F",
                 "CTRL-G", "CTRL-H", "CTRL-I", "CTRL-J", "CTRL-K"]

REJECTION_KINDS = ["outside_support", "retired_build", "unprobed_frame",
                   "unreported_dump", "unresolved_module"]


class DecodeError(Exception):
    """A wire parameter assignment cannot decode the export."""


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def read_text(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def read_csv_rows(path):
    """Minimal comma-separated reader for the fleet's plain reference tables."""
    rows = []
    lines = [ln for ln in read_text(path).splitlines() if ln.strip()]
    if not lines:
        return rows
    header = [c.strip() for c in lines[0].split(",")]
    for line in lines[1:]:
        cells = [c.strip() for c in line.split(",")]
        rows.append(dict(zip(header, cells)))
    return rows


def read_jsonl(path):
    return [json.loads(ln) for ln in read_text(path).splitlines() if ln.strip()]


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(obj, indent=2, sort_keys=True))
        handle.write("\n")


def frame_check(cols):
    """Check total over a frame's eight leading columns joined by the pipe."""
    total = 0
    for ch in "|".join(cols[:8]):
        total += ord(ch)
    return total % CHECK_MOD


def rotate_left(text, count):
    count %= len(text)
    return text[count:] + text[:count]


def parse_radix(text, radix):
    value = 0
    if not text:
        raise DecodeError("empty numeral")
    for ch in text:
        digit = "0123456789abcdefghijklmnopqrstuvwxyz".find(ch)
        if digit < 0 or digit >= radix:
            raise DecodeError("digit outside radix")
        value = value * radix + digit
    return value


# ---------------------------------------------------------------------------
# reporter export parsing
# ---------------------------------------------------------------------------

def parse_export(path):
    """Split the pipe-delimited export into dumps with their raw line columns."""
    dumps = []
    current = None
    for raw in read_text(path).splitlines():
        if not raw.strip():
            continue
        if raw.startswith("#"):
            continue
        cols = raw.split("|")
        if cols[0] == "@":
            if len(cols) != 8:
                raise DecodeError("malformed dump header")
            current = {
                "dump_id": cols[1],
                "module_column": cols[2],
                "count_raw": int(cols[3]),
                "base_field": cols[4],
                "dump_flags": int(cols[5], 16),
                "dump_key": int(cols[6]),
                "dump_check": cols[7],
                "lines": [],
            }
            dumps.append(current)
        else:
            if current is None:
                raise DecodeError("fault frame line before any dump header")
            if len(cols) != 9:
                raise DecodeError("malformed fault frame line")
            current["lines"].append(cols)
    return dumps


# ---------------------------------------------------------------------------
# staged decoding of one export under a candidate parameter assignment
# ---------------------------------------------------------------------------

STAGES = ["counts", "order", "spans", "entries", "unwinds", "allocs",
          "bases", "sigs", "ticks"]


def decode(raw_dumps, params, upto):
    """Decode the export as far as the named stage under ``params``.

    Each stage consumes only the wire parameters its rule names, so a stage
    plan can settle one parameter at a time without touching unsettled ones.
    """
    limit = STAGES.index(upto)
    dumps = []
    for source in raw_dumps:
        dumps.append(dict(source))

    # ---- stage: counts -----------------------------------------------------
    for ordinal, dump in enumerate(dumps, start=1):
        count = dump["count_raw"]
        if dump["dump_flags"] & F_COUNT_BIAS:
            bias = (ordinal * params["count_multiplier"]) % COUNT_BIAS_MOD
            count -= bias
        lines = dump["lines"]
        if count < 1 or count > len(lines):
            raise DecodeError("declared count outside the dump's lines")
        frame_lines = lines[:count]
        trailers = lines[count:]
        if dump["dump_flags"] & F_SPLIT_BASE and not trailers:
            raise DecodeError("split-base dump without a trailer line")
        # the shipped check totals cover exactly these columns
        total = 0
        for cols in frame_lines:
            own = frame_check(cols)
            if "%04x" % own != cols[8]:
                raise DecodeError("frame check total mismatch")
            total += own
        if "%04x" % (total % CHECK_MOD) != dump["dump_check"]:
            raise DecodeError("dump check total mismatch")
        dump["count"] = count
        dump["frame_lines"] = frame_lines
        dump["trailers"] = trailers
    if limit == STAGES.index("counts"):
        return dumps

    # ---- stage: order (rotation) and the per-frame skeleton -----------------
    for dump in dumps:
        count = dump["count"]
        shift = 0
        if dump["dump_flags"] & F_ROTATED:
            shift = (dump["dump_key"] + params["rotation_offset"]) % count
        ordered = [dump["frame_lines"][(j - shift) % count] for j in range(count)]
        frames = []
        for cols in ordered:
            flags = int(cols[6], 16)
            kind, _, tail = cols[7].rpartition(":")
            if not tail:
                raise DecodeError("symbol column without an integer")
            if flags & FF_INDEX_IN_SYMBOL:
                index = int(tail)
            else:
                index = int(cols[0])
            frames.append({
                "cols": cols,
                "frame_flags": flags,
                "symbol": kind,
                "frame_index": index,
                "source_dump": dump["dump_id"],
            })
        dump["frames"] = frames
    if limit == STAGES.index("order"):
        return dumps

    # ---- stage: spans ------------------------------------------------------
    for dump in dumps:
        raw = [int(f["cols"][3]) for f in dump["frames"]]
        if dump["dump_flags"] & F_SECOND_ORDER:
            spans = []
            for i, value in enumerate(raw):
                if i == 0:
                    spans.append(value)
                elif i == 1:
                    spans.append(spans[0] + value)
                else:
                    spans.append(2 * spans[i - 1] - spans[i - 2] + value)
        else:
            spans = raw
        for frame, span in zip(dump["frames"], spans):
            frame["cost_ns"] = span
    if limit == STAGES.index("spans"):
        return dumps

    # ---- stage: entries ----------------------------------------------------
    modulus = params["entry_clock_modulus"]
    for dump in dumps:
        raw = [int(f["cols"][1]) for f in dump["frames"]]
        if dump["dump_flags"] & F_ENTRY_CHAIN:
            entries = [raw[0] % modulus]
            for i in range(1, len(raw)):
                entries.append((raw[i] - entries[i - 1]) % modulus)
        else:
            entries = raw
        for frame, entry in zip(dump["frames"], entries):
            frame["enter_ns"] = entry
    if limit == STAGES.index("entries"):
        return dumps

    # ---- stage: unwinds ----------------------------------------------------
    drift = params["drift_period"]
    running = 0
    for dump in dumps:
        drifted = bool(dump["dump_flags"] & F_UNWIND_DRIFT)
        for frame in dump["frames"]:
            column = int(frame["cols"][2])
            frame["unwind_ns"] = column - (running % drift) if drifted else column
            running += frame["cost_ns"]
    if limit == STAGES.index("unwinds"):
        return dumps

    # ---- stage: allocations ------------------------------------------------
    coarse = params["coarse_unit"]
    for dump in dumps:
        for frame in dump["frames"]:
            column = frame["cols"][4]
            if frame["frame_flags"] & FF_COARSE_ALLOC:
                if not column.startswith("~"):
                    raise DecodeError("coarse allocation without its tilde")
                frame["bytes_allocated"] = int(column[1:]) * coarse
            else:
                frame["bytes_allocated"] = int(column)
    if limit == STAGES.index("allocs"):
        return dumps

    # ---- stage: dump bases -------------------------------------------------
    width = params["base_shift_width"]
    for dump in dumps:
        field = dump["base_field"]
        if dump["dump_flags"] & F_ALT_RADIX:
            high = parse_radix(field, params["alternate_radix"])
        else:
            high = parse_radix(field, 10)
        if dump["dump_flags"] & F_SPLIT_BASE:
            low = int(dump["trailers"][0][0])
            if low < 0 or low >= (1 << width):
                raise DecodeError("low part does not fit the base shift width")
            base = (high << width) | low
        else:
            base = high
        if base < 0:
            raise DecodeError("negative dump base")
        dump["base"] = base
    if limit == STAGES.index("bases"):
        return dumps

    # ---- stage: fault signatures ------------------------------------------
    width = params["ring_width"]
    offset = params["signature_offset"]
    ring = [None] * width
    write_pos = 0
    for dump in dumps:
        for frame in dump["frames"]:
            column = frame["cols"][5]
            if frame["frame_flags"] & FF_SIG_RING:
                if not column.startswith("^"):
                    raise DecodeError("ring reference without its caret")
                distance = int(column[1:])
                if distance < 1:
                    raise DecodeError("ring distance is not a step back")
                slot = (write_pos - distance) % width
                if ring[slot] is None:
                    raise DecodeError("ring slot never written")
                signature = ring[slot]
            elif frame["frame_flags"] & FF_SIG_ROTATED:
                turn = (frame["frame_index"] + offset) % SIGNATURE_LEN
                if len(column) != SIGNATURE_LEN:
                    raise DecodeError("rotated signature of the wrong length")
                signature = rotate_left(column, SIGNATURE_LEN - turn)
            else:
                signature = column
            if len(signature) != SIGNATURE_LEN or any(
                    ch not in HEX_DIGITS for ch in signature):
                raise DecodeError("recovered signature is not 16 lowercase hex")
            frame["fault_signature"] = signature
            ring[write_pos % width] = signature
            write_pos += 1
    if limit == STAGES.index("sigs"):
        return dumps

    # ---- stage: capture ticks ---------------------------------------------
    modulus = params["entry_clock_modulus"]
    for dump in dumps:
        first = dump["frames"][0]["enter_ns"]
        for frame in dump["frames"]:
            distance = (frame["enter_ns"] - first) % modulus
            tick = dump["base"] + distance
            frame["capture_tick"] = tick
            frame["build_index"] = tick // TICKS_PER_BUILD
    return dumps


def all_frames(dumps):
    for dump in dumps:
        for frame in dump.get("frames", []):
            yield dump, frame


# ---------------------------------------------------------------------------
# reference tables
# ---------------------------------------------------------------------------

class Tables(object):
    def __init__(self, data_dir):
        registry = read_csv_rows(os.path.join(data_dir, "build_registry.csv"))
        self.registry = {}
        for row in registry:
            self.registry.setdefault(row["module_tag"], []).append({
                "canonical_tag": row["canonical_tag"],
                "component_key": row["component_key"],
                "tier_index": int(row["tier_index"]),
                "from": int(row["effective_from_build"]),
                "to": int(row["effective_to_build"]),
                "status": row["status"],
            })

        self.ladder = {}
        for row in read_csv_rows(os.path.join(data_dir, "severity_ladder.csv")):
            self.ladder.setdefault(row["component_key"], {})[
                int(row["tier_index"])] = int(row["budget_ns"])

        self.windows = {}
        for row in read_csv_rows(os.path.join(data_dir, "support_windows.csv")):
            self.windows[row["component_key"]] = (int(row["open_tick"]),
                                                  int(row["close_tick"]))

        self.zones = {}
        for row in read_csv_rows(os.path.join(data_dir, "host_zones.csv")):
            self.zones[row["host_id"]] = row["zone"]

        self.segments = {}
        for row in read_csv_rows(os.path.join(data_dir, "tenant_classes.csv")):
            self.segments[row["tenant_class"]] = row["tenant_segment"]

        self.zone_names = sorted(set(self.zones.values()))
        self.segment_names = sorted(set(self.segments.values()))

    def row_for(self, tag, build_index):
        covering = [r for r in self.registry.get(tag, [])
                    if r["from"] <= build_index <= r["to"]]
        if len(covering) != 1:
            return None
        return covering[0]

    def resolve(self, tag, build_index):
        """Follow the canonical chain on one build index.

        Returns the terminal registry row, or None when the module is
        unresolved.
        """
        seen = set()
        current = tag
        while True:
            if current in seen:
                return None
            seen.add(current)
            row = self.row_for(current, build_index)
            if row is None:
                return None
            if row["canonical_tag"] == current:
                return dict(row, canonical_tag=current)
            current = row["canonical_tag"]


# ---------------------------------------------------------------------------
# corpus assembly: acceptance, settlement, conservation
# ---------------------------------------------------------------------------

class Corpus(object):
    """One decoded corpus with its side exports resolved into a fault ledger."""

    def __init__(self, dumps, probe, uplink, tables):
        self.tables = tables
        self.dumps = dumps
        self.probe = {(r["module_tag"], int(r["frame_index"])):
                      (int(r["samples"]), int(r["peak_ns"])) for r in probe}
        self.uplink = {(r["module_tag"], int(r["frame_index"])): r
                       for r in uplink}
        self.frame_count = sum(len(d["frames"]) for d in dumps)
        self._triage()
        self._settle()

    # -- acceptance ---------------------------------------------------------
    def _triage(self):
        rejections = dict((kind, 0) for kind in REJECTION_KINDS)
        ledger = []
        for dump, frame in all_frames(self.dumps):
            faults = []
            row = self.tables.resolve(dump["module_column"], frame["build_index"])
            canonical = None
            if row is None:
                # nothing downstream of identity can be judged for this frame
                faults.append("unresolved_module")
            elif row["status"] != "supported":
                faults.append("retired_build")
            else:
                canonical = row["canonical_tag"]
                open_tick, close_tick = self.tables.windows[row["component_key"]]
                if not (open_tick < frame["capture_tick"] < close_tick):
                    faults.append("outside_support")
                key = (canonical, frame["frame_index"])
                if key not in self.probe:
                    faults.append("unprobed_frame")
                if key not in self.uplink:
                    faults.append("unreported_dump")
            if faults:
                for kind in faults:
                    rejections[kind] += 1
                continue

            samples, peak_ns = self.probe[(canonical, frame["frame_index"])]
            report = self.uplink[(canonical, frame["frame_index"])]
            zone = self.tables.zones.get(report["host_id"])
            segment = self.tables.segments.get(report["tenant_class"])
            ledger.append({
                "bytes_allocated": frame["bytes_allocated"],
                "bytes_uploaded": int(report["bytes_uploaded"]),
                "capture_tick": frame["capture_tick"],
                "component_key": row["component_key"],
                "cost_ns": frame["cost_ns"],
                "declared_tier": row["tier_index"],
                "enter_ns": frame["enter_ns"],
                "fault_signature": frame["fault_signature"],
                "frame_index": frame["frame_index"],
                "host_zone": zone,
                "module_tag": canonical,
                "peak_ns": peak_ns,
                "samples": samples,
                "source_dump": frame["source_dump"],
                "symbol": frame["symbol"],
                "tenant_segment": segment,
                "unwind_ns": frame["unwind_ns"],
            })
        ledger.sort(key=lambda row: row["capture_tick"])
        self.ledger = ledger
        self.rejections = rejections
        self.rejected_count = self.frame_count - len(ledger)

    # -- quarantine settlement ---------------------------------------------
    def _settle(self):
        modules = {}
        for row in self.ledger:
            entry = modules.setdefault(row["module_tag"], {
                "module_tag": row["module_tag"],
                "component_key": row["component_key"],
                "declared_tier": row["declared_tier"],
                "rows": [],
            })
            entry["declared_tier"] = min(entry["declared_tier"],
                                         row["declared_tier"])
            entry["rows"].append(row)

        effective = dict((tag, m["declared_tier"]) for tag, m in modules.items())
        by_component = {}
        for tag, module in modules.items():
            by_component.setdefault(module["component_key"], []).append(tag)

        passes = 0
        allowances = {}
        while True:
            passes += 1
            envelopes = {}
            for component, tags in by_component.items():
                ladder = self.tables.ladder[component]
                lowest = min(ladder[effective[t]] for t in tags)
                envelopes[component] = lowest * len(ladder)
            allowances = {}
            for tag, module in modules.items():
                ladder = self.tables.ladder[module["component_key"]]
                allowances[tag] = min(ladder[effective[tag]],
                                      envelopes[module["component_key"]])
            tally = {}
            for tag, module in modules.items():
                within = sum(1 for r in module["rows"]
                             if r["peak_ns"] <= allowances[tag])
                tally[tag] = (within, len(module["rows"]) - within)
            demote = [tag for tag in modules
                      if effective[tag] > 0 and tally[tag][1] > tally[tag][0]]
            if not demote:
                break
            for tag in demote:
                effective[tag] -= 1

        for row in self.ledger:
            tag = row["module_tag"]
            row["effective_tier"] = effective[tag]
            row["within_budget"] = row["peak_ns"] <= allowances[tag]
            del row["declared_tier"]

        self.modules = modules
        self.effective = effective
        self.allowances = allowances
        self.passes = passes
        self.quarantined = sorted(
            tag for tag, module in modules.items()
            if effective[tag] == 0 and tally[tag][1] > tally[tag][0])

    # -- conservation -------------------------------------------------------
    @property
    def allocated_bytes(self):
        return sum(r["bytes_allocated"] for r in self.ledger)

    @property
    def uploaded_bytes(self):
        return sum(r["bytes_uploaded"] for r in self.ledger)

    @property
    def residual_bytes(self):
        return self.allocated_bytes - self.uploaded_bytes


# ---------------------------------------------------------------------------
# control figures
# ---------------------------------------------------------------------------

# the wire parameters each decoding stage consumes, accumulated over the
# stage sequence: a control can only be reproduced once its stage's parameters
# are settled
STAGE_PARAMS = {}
_introduced = {
    "counts": ["count_multiplier"],
    "order": ["rotation_offset"],
    "spans": [],
    "entries": ["entry_clock_modulus"],
    "unwinds": ["drift_period"],
    "allocs": ["coarse_unit"],
    "bases": ["alternate_radix", "base_shift_width"],
    "sigs": ["signature_offset", "ring_width"],
    "ticks": [],
}
_seen = set()
for _stage in STAGES:
    _seen = _seen | set(_introduced[_stage])
    STAGE_PARAMS[_stage] = frozenset(_seen)

CONTROL_STAGE = {
    "CTRL-A": "counts",
    "CTRL-B": "bases",
    "CTRL-C": "spans",
    "CTRL-D": "entries",
    "CTRL-E": "unwinds",
    "CTRL-F": "allocs",
    "CTRL-G": "sigs",
    "CTRL-H": "order",
    "CTRL-I": "ticks",
    "CTRL-J": "ticks",
    "CTRL-K": "ticks",
}


def control_figure(name, raw_dumps, params, side):
    """Reproduce one published control figure from the control slice."""
    dumps = decode(raw_dumps, params, CONTROL_STAGE[name])
    if name == "CTRL-A":
        return sum(d["count"] for d in dumps)
    if name == "CTRL-B":
        return sum(d["base"] for d in dumps)
    if name == "CTRL-C":
        return sum(f["cost_ns"] for _, f in all_frames(dumps))
    if name == "CTRL-D":
        return sum(f["enter_ns"] for _, f in all_frames(dumps))
    if name == "CTRL-E":
        return sum(f["unwind_ns"] for _, f in all_frames(dumps))
    if name == "CTRL-F":
        return sum(f["bytes_allocated"] for _, f in all_frames(dumps))
    if name == "CTRL-G":
        total = 0
        for _, frame in all_frames(dumps):
            total += int(frame["fault_signature"], 16)
        return total % SIGNATURE_MOD
    if name == "CTRL-H":
        return sum(f["frame_index"] for _, f in all_frames(dumps))
    corpus = Corpus(dumps, side["probe"], side["uplink"], side["tables"])
    if name == "CTRL-I":
        return len(corpus.ledger)
    if name == "CTRL-J":
        return len(corpus.quarantined)
    return corpus.residual_bytes


# ---------------------------------------------------------------------------
# the candidate search
# ---------------------------------------------------------------------------

def settle_parameters(raw_dumps, published, side):
    """Settle the nine withheld wire parameters against the control slice."""
    evaluations = [0]

    def figure(name, params):
        evaluations[0] += 1
        try:
            return control_figure(name, raw_dumps, params, side)
        except DecodeError:
            return None
        except (ValueError, KeyError, IndexError, ZeroDivisionError):
            return None

    survivors = [{}]
    known = set()
    for names, control in SEARCH_PLAN:
        known.update(names)
        # every control whose stage needs no unsettled parameter now bites
        due = [c for c in CONTROL_ORDER if STAGE_PARAMS[CONTROL_STAGE[c]] <= known]
        nxt = []
        for partial in survivors:
            for combo in product(*(PARAM_DOMAINS[n] for n in names)):
                candidate = dict(partial)
                candidate.update(dict(zip(names, combo)))
                if all(figure(c, candidate) == published[c] for c in due):
                    nxt.append(candidate)
        if not nxt:
            raise SystemExit("no candidate reproduces %s" % control)
        survivors = nxt

    settled = [s for s in survivors
               if all(figure(name, s) == published[name]
                      for name in CONTROL_ORDER)]
    if len(settled) != 1:
        raise SystemExit(
            "the control slice does not single out one parameter assignment")
    params = settled[0]

    # A verdict for every candidate value of every parameter: substitute the
    # value into the settled assignment and name the control that falsifies it.
    stage_control = {}
    for names, control in SEARCH_PLAN:
        for name in names:
            stage_control[name] = control
    report = []
    for name in sorted(PARAM_DOMAINS):
        rejected = []
        for value in PARAM_DOMAINS[name]:
            if value == params[name]:
                continue
            trial = dict(params)
            trial[name] = value
            falsified_by = None
            for control in CONTROL_ORDER:
                if figure(control, trial) != published[control]:
                    falsified_by = control
                    break
            if falsified_by is None:
                raise SystemExit("candidate %s=%r is not falsified" % (name, value))
            rejected.append({"value": value, "control": falsified_by})
        report.append({
            "name": name,
            "candidates": len(PARAM_DOMAINS[name]),
            "control": stage_control[name],
            "selected": params[name],
            "rejected": rejected,
        })
    return params, report, evaluations[0]


# ---------------------------------------------------------------------------
# reports
# ---------------------------------------------------------------------------

def bucket_sums(rows, key, value_key="bytes_uploaded"):
    out = {}
    for row in rows:
        out[row[key]] = out.get(row[key], 0) + row[value_key]
    return out


def build_summary(corpus):
    ledger = corpus.ledger
    tiers = {}
    for row in ledger:
        name = str(row["effective_tier"])
        tiers[name] = tiers.get(name, 0) + 1
    return {
        "accepted_count": len(ledger),
        "by_component": bucket_sums(ledger, "component_key"),
        "by_effective_tier": tiers,
        "by_tenant_segment": bucket_sums(ledger, "tenant_segment"),
        "by_zone": bucket_sums(ledger, "host_zone"),
        "rejected_count": corpus.rejected_count,
        "total_bytes_allocated": corpus.allocated_bytes,
        "total_bytes_uploaded": corpus.uploaded_bytes,
        "total_cost_ns": sum(r["cost_ns"] for r in ledger),
        "within_budget_count": sum(1 for r in ledger if r["within_budget"]),
    }


def build_product_summary(corpus):
    modules = []
    for tag in sorted(corpus.modules):
        module = corpus.modules[tag]
        rows = module["rows"]
        ladder = corpus.tables.ladder[module["component_key"]]
        modules.append({
            "accepted_count": len(rows),
            "allowance_ns": corpus.allowances[tag],
            "budget_ns": ladder[corpus.effective[tag]],
            "component_key": module["component_key"],
            "declared_tier": module["declared_tier"],
            "effective_tier": corpus.effective[tag],
            "module_tag": tag,
            "net_samples": sum(r["samples"] for r in rows),
            "quarantined": tag in corpus.quarantined,
            "total_bytes_uploaded": sum(r["bytes_uploaded"] for r in rows),
            "within_budget_count": sum(1 for r in rows if r["within_budget"]),
        })
    return {"modules": modules}


def build_slice_report(rows, kind, name):
    report = {
        "accepted_count": len(rows),
        "by_component": bucket_sums(rows, "component_key"),
        "modules": sorted(set(r["module_tag"] for r in rows)),
        "total_bytes_uploaded": sum(r["bytes_uploaded"] for r in rows),
        "total_cost_ns": sum(r["cost_ns"] for r in rows),
        "within_budget_count": sum(1 for r in rows if r["within_budget"]),
    }
    if kind == "zone":
        report["by_tenant_segment"] = bucket_sums(rows, "tenant_segment")
        report["zone"] = name
    else:
        report["by_zone"] = bucket_sums(rows, "host_zone")
        report["tenant_segment"] = name
    return report


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------

def load_side(data_dir, prefix):
    return {
        "probe": read_csv_rows(os.path.join(data_dir, prefix + "runtime_probe.csv")),
        "uplink": read_jsonl(os.path.join(data_dir, prefix + "uplink_reports.jsonl")),
    }


def main(argv):
    data_dir = argv[1] if len(argv) > 1 else "/app/data"
    out_dir = argv[2] if len(argv) > 2 else "/app/out"
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)

    tables = Tables(data_dir)

    # 1. settle the withheld wire parameters against the published controls
    published = json.loads(read_text(os.path.join(data_dir,
                                                  "control-slice-figures.json")))
    control_dumps = parse_export(os.path.join(data_dir,
                                              "calibration_fault_frames.psv"))
    control_side = load_side(data_dir, "calibration_")
    control_side["tables"] = tables
    params, param_report, evaluations = settle_parameters(
        control_dumps, published, control_side)

    comparison = []
    for name in CONTROL_ORDER:
        comparison.append({
            "control": name,
            "published": published[name],
            "reproduced": control_figure(name, control_dumps, params,
                                         control_side),
        })

    # 2. run the settled parameters over the incident corpus
    dumps = decode(parse_export(os.path.join(data_dir, "fault_frames.psv")),
                   params, "ticks")
    side = load_side(data_dir, "")
    corpus = Corpus(dumps, side["probe"], side["uplink"], tables)

    # 3. write the triage set
    ledger_path = os.path.join(out_dir, "normalized.jsonl")
    with open(ledger_path, "w", encoding="utf-8") as handle:
        for row in corpus.ledger:
            handle.write(json.dumps(row, sort_keys=True,
                                    separators=(",", ":")))
            handle.write("\n")

    write_json(os.path.join(out_dir, "summary.json"), build_summary(corpus))
    write_json(os.path.join(out_dir, "product_summary.json"),
               build_product_summary(corpus))

    for zone in tables.zone_names:
        rows = [r for r in corpus.ledger if r["host_zone"] == zone]
        write_json(os.path.join(out_dir, "region_%s.json" % zone),
                   build_slice_report(rows, "zone", zone))
    for segment in tables.segment_names:
        rows = [r for r in corpus.ledger if r["tenant_segment"] == segment]
        write_json(os.path.join(out_dir, "segment_%s.json" % segment),
                   build_slice_report(rows, "segment", segment))

    write_json(os.path.join(out_dir, "data_quality.json"), {
        "accepted_count": len(corpus.ledger),
        "calibration_comparison": comparison,
        "candidate_search": {
            "evaluations": evaluations,
            "parameters": param_report,
        },
        "frame_count": corpus.frame_count,
        "reconciliation": {
            "allocated_bytes": corpus.allocated_bytes,
            "residual_bytes": corpus.residual_bytes,
            "uploaded_bytes": corpus.uploaded_bytes,
        },
        "rejections": corpus.rejections,
        "settlement": {
            "passes": corpus.passes,
            "quarantined_modules": corpus.quarantined,
        },
    })
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
