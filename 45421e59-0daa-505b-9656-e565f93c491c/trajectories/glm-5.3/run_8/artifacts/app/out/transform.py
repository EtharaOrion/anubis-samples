#!/usr/bin/env python3
"""Reconstruct a crash-reporter incident window from a fleet data directory.

Usage:
    python3 transform.py [DATA_DIR] [OUT_DIR]

With no arguments the program reads /app/data and writes /app/out.  The data
directory is expected to carry the three tiers the fleet ships beside every
corpus -- the incident tier (``fault_frames.psv`` and its side exports), the
control slice (``calibration_*``) and the sample tier (``sample_*``) -- together
with the five reference tables and the control figures published for the
control slice.

The crash reporter withholds nine wire parameters.  They are settled first,
against the published control figures, by enumerating every candidate value in
each parameter's published domain and recording a verdict for it; only then is
any figure derived for the incident corpus.  All clock, byte and residency
arithmetic is carried in Python integers.

Nothing here depends on the identifiers, counts or totals of any particular
corpus: zones, segments, modules, registries and ladders are read from the
tables the data directory carries, and the wire parameters are re-derived from
the control slice every time the program runs.
"""

import csv
import json
import os
import sys
from itertools import product

DEFAULT_DATA_DIR = "/app/data"
DEFAULT_OUT_DIR = "/app/out"

TICKS_PER_BUILD_INDEX = 900000000     # 1000 ticks/ms over one build index
CHECK_MODULUS = 65536
SIGNATURE_MODULUS = 1000000007

# --- dump flag word -------------------------------------------------------
DUMP_SPAN_DIFFERENCES = 0x001
DUMP_ENTRY_CUMULATIVE = 0x002
DUMP_UNWIND_DRIFT = 0x004
DUMP_LINE_ROTATION = 0x008
DUMP_COUNT_BIAS = 0x010
DUMP_BASE_RADIX = 0x020
DUMP_MODULE_ALIAS = 0x100
DUMP_SPLIT_BASE = 0x800

# --- frame flag word ------------------------------------------------------
FRAME_INDEX_PLACEHOLDER = 0x040
FRAME_SIGNATURE_ROTATION = 0x080
FRAME_ALLOCATION_COARSE = 0x200
FRAME_SIGNATURE_RING = 0x400

REJECTION_ORDER = ("unresolved_module", "retired_build", "outside_support",
                   "unprobed_frame", "unreported_dump")

# --- the withheld wire parameters and the domains published for them -------
PARAMETER_DOMAINS = [
    ("entry clock modulus", [1 << e for e in range(22, 35)]),
    ("drift period", [48, 96, 192, 384, 768]),
    ("rotation offset", list(range(8))),
    ("count multiplier", list(range(9))),
    ("alternate radix", list(range(11, 36))),
    ("signature offset", list(range(8))),
    ("coarse unit", [64, 128, 256, 512, 1024]),
    ("ring width", [4, 8, 12, 16]),
    ("base shift width", [8, 10, 12, 16]),
]
PARAMETERS = [name for name, _ in PARAMETER_DOMAINS]
DOMAIN = dict(PARAMETER_DOMAINS)

# Every control figure the fleet publishes for a control slice, and the wire
# parameters its reproduction rests on.  The dependencies follow from the flag
# tables in wire-and-dumps.md, not from any corpus.
CONTROL_DEPS = {
    "CTRL-A": ("count multiplier",),
    "CTRL-B": ("count multiplier", "alternate radix", "base shift width"),
    "CTRL-C": ("count multiplier", "rotation offset"),
    "CTRL-D": ("count multiplier", "rotation offset", "entry clock modulus"),
    "CTRL-E": ("count multiplier", "rotation offset", "drift period"),
    "CTRL-F": ("count multiplier", "coarse unit"),
    "CTRL-G": ("count multiplier", "rotation offset", "signature offset",
               "ring width"),
    "CTRL-H": ("count multiplier",),
    "CTRL-I": tuple(PARAMETERS),
    "CTRL-J": tuple(PARAMETERS),
    "CTRL-K": tuple(PARAMETERS),
}
CONTROL_ORDER = sorted(CONTROL_DEPS)

# The parameters are settled in stages.  A stage is settled jointly whenever
# the controls that decide its members cannot separate them one at a time.
SETTLEMENT_STAGES = [
    ("count multiplier",),
    ("alternate radix", "base shift width"),
    ("rotation offset",),
    ("entry clock modulus",),
    ("drift period",),
    ("coarse unit",),
    ("signature offset", "ring width"),
]
# The control figure a parameter's own stage turns on.
STAGE_CONTROL = {
    "count multiplier": "CTRL-A",
    "alternate radix": "CTRL-B",
    "base shift width": "CTRL-B",
    "rotation offset": "CTRL-C",
    "entry clock modulus": "CTRL-D",
    "drift period": "CTRL-E",
    "coarse unit": "CTRL-F",
    "signature offset": "CTRL-G",
    "ring width": "CTRL-G",
}


class WireError(Exception):
    """A corpus broke a closure the reporter guarantees."""


# ---------------------------------------------------------------------------
# reading the materials
# ---------------------------------------------------------------------------

def read_text(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def read_csv_rows(path):
    with open(path, "r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def read_jsonl(path):
    return [json.loads(line) for line in read_text(path).splitlines()
            if line.strip()]


def parse_export(path):
    """Split a pipe-delimited dump export into dumps of raw column lists."""
    dumps = []
    current = None
    for raw in read_text(path).splitlines():
        if not raw.strip() or raw.startswith("#"):
            continue
        if raw.startswith("@"):
            columns = raw.split("|")
            if len(columns) != 8:
                raise WireError("%s: dump header carries %d columns"
                                % (path, len(columns)))
            current = {
                "dump_id": columns[1],
                "module_column": columns[2],
                "declared_count": int(columns[3], 10),
                "base_field": columns[4],
                "dump_flags": int(columns[5], 16),
                "dump_key": int(columns[6], 10),
                "dump_check": int(columns[7], 16),
                "lines": [],
            }
            dumps.append(current)
        else:
            if current is None:
                raise WireError("%s: frame line before any header" % path)
            columns = raw.split("|")
            if len(columns) != 9:
                raise WireError("%s: frame line carries %d columns"
                                % (path, len(columns)))
            current["lines"].append(columns)
    if not dumps:
        raise WireError("%s: export carries no dump" % path)
    return dumps


def frame_check_of(columns):
    """The check total the reporter rules define over a frame's columns."""
    return sum(ord(ch) for ch in "|".join(columns[:8])) % CHECK_MODULUS


def digit_value(ch):
    if "0" <= ch <= "9":
        return ord(ch) - ord("0")
    if "a" <= ch <= "z":
        return ord(ch) - ord("a") + 10
    raise WireError("digit %r is not a lowercase reporter digit" % ch)


def parse_in_radix(text, radix):
    value = 0
    for ch in text:
        digit = digit_value(ch)
        if digit >= radix:
            raise WireError("digit %r does not fit radix %d" % (ch, radix))
        value = value * radix + digit
    return value


# ---------------------------------------------------------------------------
# reference tables
# ---------------------------------------------------------------------------

class Tables(object):
    def __init__(self, data_dir):
        self.registry = {}
        for row in read_csv_rows(os.path.join(data_dir, "build_registry.csv")):
            self.registry.setdefault(row["module_tag"], []).append({
                "from_build": int(row["effective_from_build"], 10),
                "to_build": int(row["effective_to_build"], 10),
                "canonical_tag": row["canonical_tag"],
                "component_key": row["component_key"],
                "tier_index": int(row["tier_index"], 10),
                "status": row["status"],
            })
        self.ladder = {}
        for row in read_csv_rows(os.path.join(data_dir, "severity_ladder.csv")):
            self.ladder.setdefault(row["component_key"], {}
                                   )[int(row["tier_index"], 10)] = int(
                                       row["budget_ns"], 10)
        self.window = {}
        for row in read_csv_rows(os.path.join(data_dir, "support_windows.csv")):
            self.window[row["component_key"]] = (
                int(row["open_tick"], 10), int(row["close_tick"], 10))
        self.host_zone = {}
        self.zones = []
        for row in read_csv_rows(os.path.join(data_dir, "host_zones.csv")):
            self.host_zone[row["host_id"]] = row["zone"]
            if row["zone"] not in self.zones:
                self.zones.append(row["zone"])
        self.tenant_segment = {}
        self.segments = []
        for row in read_csv_rows(os.path.join(data_dir, "tenant_classes.csv")):
            self.tenant_segment[row["tenant_class"]] = row["tenant_segment"]
            if row["tenant_segment"] not in self.segments:
                self.segments.append(row["tenant_segment"])
        self.zones.sort()
        self.segments.sort()

    def resolve(self, module_tag, build_index):
        """Follow the registry chain for a tag on a build index."""
        seen = set()
        tag = module_tag
        while True:
            if tag in seen:
                return None                      # a chain that repeats a tag
            seen.add(tag)
            row = None
            for candidate in self.registry.get(tag, ()):
                if candidate["from_build"] <= build_index \
                        <= candidate["to_build"]:
                    row = candidate
                    break
            if row is None:
                return None                      # covered by no row
            if row["canonical_tag"] == tag:
                return {"module_tag": tag,
                        "component_key": row["component_key"],
                        "declared_tier": row["tier_index"],
                        "carried": row["status"] == "supported"}
            tag = row["canonical_tag"]


# ---------------------------------------------------------------------------
# decoding a dump export
# ---------------------------------------------------------------------------

def dump_frame_count(dump, ordinal, count_multiplier):
    if dump["dump_flags"] & DUMP_COUNT_BIAS:
        return dump["declared_count"] - (ordinal * count_multiplier) % 9
    return dump["declared_count"]


def recover_base(dump, ordinal, params):
    """The recovered dump base, from whichever fields the dump flag word names."""
    flags = dump["dump_flags"]
    count = dump_frame_count(dump, ordinal, params["count multiplier"])
    high_part = parse_in_radix(dump["base_field"],
                               params["alternate radix"]
                               if flags & DUMP_BASE_RADIX else 10)
    if flags & DUMP_SPLIT_BASE:
        low_part = int(dump["lines"][count][0], 10)
        return (high_part << params["base shift width"]) | low_part
    return high_part


def logical_order(dump, count, params):
    """A dump's fault frame columns, unrotated into logical order."""
    written = dump["lines"][:count]
    if not dump["dump_flags"] & DUMP_LINE_ROTATION:
        return written
    turn = (dump["dump_key"] + params["rotation offset"]) % count
    return [written[(index - turn) % count] for index in range(count)]


def recover_spans(columns, flags):
    """Resident spans, undoing second-order differences where they are used."""
    if not flags & DUMP_SPAN_DIFFERENCES:
        return [int(col[3], 10) for col in columns]
    written = [int(col[3], 10) for col in columns]
    spans = [written[0]]
    difference = 0
    for index in range(1, len(written)):
        difference += written[index]
        spans.append(spans[-1] + difference)
    return spans


def recover_entries(columns, flags, modulus):
    """Entry stamps, undoing the cumulative encoding where it is used."""
    if not flags & DUMP_ENTRY_CUMULATIVE:
        return [int(col[1], 10) for col in columns]
    entries = [int(columns[0][1], 10) % modulus]
    for index in range(1, len(columns)):
        entries.append((int(columns[index][1], 10)
                        - entries[index - 1]) % modulus)
    return entries


def decode_export(dumps, params):
    """Recover every fault frame of an export, in export order."""
    frames = []
    ring = [None] * params["ring width"]
    write_position = 0
    running_span_total = 0
    for ordinal, dump in enumerate(dumps, 1):
        flags = dump["dump_flags"]
        count = dump_frame_count(dump, ordinal, params["count multiplier"])
        if count < 1 or count > len(dump["lines"]):
            raise WireError("dump %s declares %d fault frames over %d lines"
                            % (dump["dump_id"], count, len(dump["lines"])))
        if flags & DUMP_SPLIT_BASE and len(dump["lines"]) - count < 1:
            raise WireError("dump %s splits its base but carries no trailer"
                            % dump["dump_id"])

        base = recover_base(dump, ordinal, params)
        if base < 0:
            raise WireError("dump %s recovers a negative base" % dump["dump_id"])

        columns = logical_order(dump, count, params)
        spans = recover_spans(columns, flags)
        entries = recover_entries(columns, flags, params["entry clock modulus"])

        recovered = []
        for index in range(count):
            col = columns[index]
            frame_flags = int(col[6], 16)

            if flags & DUMP_UNWIND_DRIFT:
                unwind = int(col[2], 10) - (running_span_total
                                            % params["drift period"])
            else:
                unwind = int(col[2], 10)

            if frame_flags & FRAME_ALLOCATION_COARSE:
                allocated = int(col[4][1:], 10) * params["coarse unit"]
            else:
                allocated = int(col[4], 10)

            if frame_flags & FRAME_INDEX_PLACEHOLDER:
                frame_index = int(col[7].rsplit(":", 1)[1], 10)
            else:
                frame_index = int(col[0], 10)

            written_signature = col[5]
            if frame_flags & FRAME_SIGNATURE_RING:
                distance = int(written_signature[1:], 10)
                signature = ring[(write_position - distance)
                                 % params["ring width"]]
                if signature is None:
                    raise WireError("dump %s frame %d reaches an empty ring slot"
                                    % (dump["dump_id"], frame_index))
            elif frame_flags & FRAME_SIGNATURE_ROTATION:
                turn = (frame_index + params["signature offset"]) % 16
                signature = (written_signature[-turn:] + written_signature[:-turn]
                             if turn else written_signature)
            else:
                signature = written_signature
            if len(signature) != 16 or any(
                    ch not in "0123456789abcdef" for ch in signature):
                raise WireError("dump %s frame %d recovers signature %r"
                                % (dump["dump_id"], frame_index, signature))

            recovered.append({
                "source_dump": dump["dump_id"],
                "module_column": dump["module_column"],
                "frame_index": frame_index,
                "entry": entries[index],
                "unwind": unwind,
                "span": spans[index],
                "allocated": allocated,
                "signature": signature,
                "symbol": col[7].rsplit(":", 1)[0],
            })
            ring[write_position] = signature
            write_position = (write_position + 1) % params["ring width"]
            running_span_total += spans[index]

        # The running span total and the ring both advance over the export as
        # a whole, whatever flags the frames' own dumps set.
        first_entry = recovered[0]["entry"]
        for frame in recovered:
            frame["capture_tick"] = base + (
                (frame["entry"] - first_entry) % params["entry clock modulus"])
        frames.extend(recovered)
    return frames


def verify_check_totals(dumps, params):
    """Check the shipped frame and dump totals against the columns they cover.

    A frame check total covers its own frame's eight preceding columns; a dump
    check total covers the sum of its fault frames' check totals.  Trailer
    lines are covered by neither.
    """
    for ordinal, dump in enumerate(dumps, 1):
        count = dump_frame_count(dump, ordinal, params["count multiplier"])
        if count < 1 or count > len(dump["lines"]):
            raise WireError("dump %s declares %d fault frames over %d lines"
                            % (dump["dump_id"], count, len(dump["lines"])))
        total = 0
        for col in dump["lines"][:count]:
            check = int(col[8], 16)
            if check != frame_check_of(col):
                raise WireError("dump %s: a frame check total does not cover "
                                "its columns" % dump["dump_id"])
            total = (total + check) % CHECK_MODULUS
        if total != dump["dump_check"]:
            raise WireError("dump %s: the dump check total does not cover its "
                            "fault frames" % dump["dump_id"])


# ---------------------------------------------------------------------------
# attributing and triaging a corpus
# ---------------------------------------------------------------------------

def load_side_exports(data_dir, prefix):
    probe = {}
    for row in read_csv_rows(os.path.join(
            data_dir, prefix + "runtime_probe.csv")):
        probe[(row["module_tag"], int(row["frame_index"], 10))] = (
            int(row["samples"], 10), int(row["peak_ns"], 10))
    uplink = {}
    for record in read_jsonl(os.path.join(
            data_dir, prefix + "uplink_reports.jsonl")):
        index = record["frame_index"]
        uplink[(record["module_tag"],
                index if isinstance(index, int) else int(index, 10))] = record
    return probe, uplink


def triage(data_dir, prefix, tables, params):
    """Decode a corpus and read each fault frame's acceptance verdict off it."""
    dumps = parse_export(os.path.join(data_dir, prefix + "fault_frames.psv"))
    verify_check_totals(dumps, params)
    frames = decode_export(dumps, params)
    probe, uplink = load_side_exports(data_dir, prefix)

    for frame in frames:
        build_index = frame["capture_tick"] // TICKS_PER_BUILD_INDEX
        resolution = tables.resolve(frame["module_column"], build_index)
        frame["build_index"] = build_index
        frame["resolution"] = resolution
        verdict = dict.fromkeys(REJECTION_ORDER, False)
        if resolution is None:
            # No canonical module to attribute the frame under, so no window,
            # probe or report can be read for it.
            verdict["unresolved_module"] = True
        elif not resolution["carried"]:
            # The module is retired on this build index; the fleet holds no
            # probe or report for a module no longer carried.
            verdict["retired_build"] = True
        else:
            component = resolution["component_key"]
            if component in tables.window:
                opening, closing = tables.window[component]
                if not opening < frame["capture_tick"] < closing:
                    verdict["outside_support"] = True
            else:
                verdict["outside_support"] = True
            key = (resolution["module_tag"], frame["frame_index"])
            if key not in probe:
                verdict["unprobed_frame"] = True
            if key not in uplink:
                verdict["unreported_dump"] = True
            frame["probe"] = probe.get(key)
            frame["uplink"] = uplink.get(key)
        frame["verdict"] = verdict
        frame["rejected"] = any(verdict.values())
    return frames


def settle_quarantine(accepted, tables):
    """Settle effective tiers by passes over the module set."""
    modules = {}
    for frame in accepted:
        resolution = frame["resolution"]
        tag = resolution["module_tag"]
        module = modules.get(tag)
        if module is None:
            module = modules[tag] = {
                "module_tag": tag,
                "component_key": resolution["component_key"],
                "declared_tier": resolution["declared_tier"],
                "frames": [],
            }
        module["frames"].append(frame)
    effective = {tag: module["declared_tier"] for tag, module in modules.items()}
    tier_count = {component: len(rungs)
                  for component, rungs in tables.ladder.items()}

    allowance = {}
    passes = 0
    while True:
        envelope = {}
        for component in sorted(set(m["component_key"]
                                    for m in modules.values())):
            in_service = [tables.ladder[component][effective[tag]]
                          for tag, module in modules.items()
                          if module["component_key"] == component]
            envelope[component] = min(in_service) * tier_count[component]
        allowance = {tag: min(tables.ladder[module["component_key"]]
                              [effective[tag]],
                              envelope[module["component_key"]])
                     for tag, module in modules.items()}
        demote = []
        for tag in sorted(modules):
            module = modules[tag]
            over = sum(1 for frame in module["frames"]
                       if frame["probe"][1] > allowance[tag])
            within = len(module["frames"]) - over
            if effective[tag] > 0 and over > within:
                demote.append(tag)
        passes += 1                       # the pass that finds nothing counts
        if not demote:
            break
        for tag in demote:
            effective[tag] -= 1

    quarantined = []
    for tag in sorted(modules):
        module = modules[tag]
        over = sum(1 for frame in module["frames"]
                   if frame["probe"][1] > allowance[tag])
        if effective[tag] == 0 and over > len(module["frames"]) - over:
            quarantined.append(tag)
    return modules, effective, allowance, passes, quarantined


def ledger_rows(frames, tables, effective, allowance):
    """The fault ledger of a corpus, strictly increasing in capture tick."""
    accepted = sorted((frame for frame in frames if not frame["rejected"]),
                      key=lambda frame: frame["capture_tick"])
    rows = []
    for frame in accepted:
        resolution = frame["resolution"]
        tag = resolution["module_tag"]
        peak = frame["probe"][1]
        rows.append({
            "bytes_allocated": frame["allocated"],
            "bytes_uploaded": frame["uplink"]["bytes_uploaded"],
            "capture_tick": frame["capture_tick"],
            "component_key": resolution["component_key"],
            "cost_ns": frame["span"],
            "effective_tier": effective[tag],
            "enter_ns": frame["entry"],
            "fault_signature": frame["signature"],
            "frame_index": frame["frame_index"],
            "host_zone": tables.host_zone[frame["uplink"]["host_id"]],
            "module_tag": tag,
            "peak_ns": peak,
            "samples": frame["probe"][0],
            "source_dump": frame["source_dump"],
            "symbol": frame["symbol"],
            "tenant_segment": tables.tenant_segment[
                frame["uplink"]["tenant_class"]],
            "unwind_ns": frame["unwind"],
            "within_budget": peak <= allowance[tag],
        })
    return rows


# ---------------------------------------------------------------------------
# reports
# ---------------------------------------------------------------------------

def _byte_breakdown(rows, key):
    """Uploaded bytes by a row field, over the values that actually occur."""
    totals = {}
    for row in rows:
        totals[row[key]] = totals.get(row[key], 0) + row["bytes_uploaded"]
    return {name: totals[name] for name in sorted(totals)}


def _counts_by_tier(rows):
    tiers = {}
    for row in rows:
        tiers[row["effective_tier"]] = tiers.get(row["effective_tier"], 0) + 1
    return {str(tier): tiers[tier] for tier in sorted(tiers)}


def _slice_report(rows, identity, identity_field, identity_key, cross_field,
                  cross_key):
    chosen = [row for row in rows if row[identity_field] == identity]
    report = {identity_key: identity,
              "accepted_count": len(chosen),
              "modules": sorted(set(row["module_tag"] for row in chosen)),
              "total_bytes_uploaded": sum(r["bytes_uploaded"] for r in chosen),
              "total_cost_ns": sum(r["cost_ns"] for r in chosen),
              "within_budget_count": sum(1 for r in chosen if r["within_budget"])}
    report["by_component"] = _byte_breakdown(chosen, "component_key")
    report[cross_key] = _byte_breakdown(chosen, cross_field)
    return report


def build_reports(frames, rows, modules, effective, allowance, passes,
                  quarantined, tables):
    files = {"summary.json": {
        "accepted_count": len(rows),
        "rejected_count": len(frames) - len(rows),
        "total_bytes_allocated": sum(r["bytes_allocated"] for r in rows),
        "total_bytes_uploaded": sum(r["bytes_uploaded"] for r in rows),
        "total_cost_ns": sum(r["cost_ns"] for r in rows),
        "within_budget_count": sum(1 for r in rows if r["within_budget"]),
        "by_component": _byte_breakdown(rows, "component_key"),
        "by_effective_tier": _counts_by_tier(rows),
        "by_tenant_segment": _byte_breakdown(rows, "tenant_segment"),
        "by_zone": _byte_breakdown(rows, "host_zone"),
    }}
    for zone in tables.zones:
        files["region_%s.json" % zone] = _slice_report(
            rows, zone, "host_zone", "zone", "tenant_segment",
            "by_tenant_segment")
    for segment in tables.segments:
        files["segment_%s.json" % segment] = _slice_report(
            rows, segment, "tenant_segment", "tenant_segment", "host_zone",
            "by_zone")
    files["product_summary.json"] = {"modules": [
        {"module_tag": tag,
         "component_key": modules[tag]["component_key"],
         "declared_tier": modules[tag]["declared_tier"],
         "effective_tier": effective[tag],
         "budget_ns": tables.ladder[modules[tag]["component_key"]]
                      [effective[tag]],
         "allowance_ns": allowance[tag],
         "accepted_count": sum(1 for r in rows if r["module_tag"] == tag),
         "within_budget_count": sum(1 for r in rows if r["module_tag"] == tag
                                    and r["within_budget"]),
         "net_samples": sum(r["samples"] for r in rows
                            if r["module_tag"] == tag),
         "total_bytes_uploaded": sum(r["bytes_uploaded"] for r in rows
                                     if r["module_tag"] == tag),
         "quarantined": tag in quarantined}
        for tag in sorted(modules)]}
    return files


def data_quality(frames, rows, passes, quarantined, calibration):
    rejections = dict.fromkeys(REJECTION_ORDER, 0)
    for frame in frames:
        for name in REJECTION_ORDER:
            if frame["verdict"][name]:
                rejections[name] += 1
    allocated = sum(row["bytes_allocated"] for row in rows)
    uploaded = sum(row["bytes_uploaded"] for row in rows)
    return {
        "frame_count": len(frames),
        "accepted_count": len(rows),
        "rejections": {name: rejections[name] for name in sorted(rejections)},
        "reconciliation": {"allocated_bytes": allocated,
                           "uploaded_bytes": uploaded,
                           "residual_bytes": allocated - uploaded},
        "settlement": {"passes": passes, "quarantined_modules": quarantined},
        "calibration_comparison": calibration["comparison"],
        "candidate_search": calibration["search"],
    }


# ---------------------------------------------------------------------------
# settling the withheld wire parameters against the control figures
# ---------------------------------------------------------------------------

class Calibration(object):
    """Reproduces the published control figures for a candidate assignment.

    Every figure the fleet publishes for the control slice is recomputed here
    from the calibration export alone, so a candidate assignment of the
    withheld wire parameters can be accepted or falsified figure by figure.
    Each figure is read off the wire with only the parameters it rests on, so
    that a candidate can be judged before the parameters it does not touch
    have been settled.
    """

    #: figures that follow from the recovered frames alone
    WIRE_CONTROLS = ("CTRL-A", "CTRL-B", "CTRL-C", "CTRL-D", "CTRL-E",
                     "CTRL-F", "CTRL-G", "CTRL-H")
    #: figures that need the corpus triaged end to end
    TRIAGE_CONTROLS = ("CTRL-I", "CTRL-J", "CTRL-K")

    def __init__(self, data_dir, tables):
        self.data_dir = data_dir
        self.tables = tables
        self.dumps = parse_export(
            os.path.join(data_dir, "calibration_fault_frames.psv"))
        figures = json.loads(read_text(
            os.path.join(data_dir, "control-slice-figures.json")))
        self.published = {name: int(figures[name]) for name in CONTROL_ORDER}
        self._indices = {}
        self._bases = {}
        self._walks = {}
        self._triaged = {}
        self.evaluations = 0

    # -- shared readings of the wire ------------------------------------
    def indices(self, count_multiplier):
        """The authoritative frame index of every fault frame, in export order.

        Follows from the declared counts and each line's own index column or
        symbol column alone.
        """
        if count_multiplier not in self._indices:
            self.evaluations += 1
            try:
                indices = []
                for ordinal, dump in enumerate(self.dumps, 1):
                    count = dump_frame_count(dump, ordinal, count_multiplier)
                    if count < 1 or count > len(dump["lines"]):
                        raise WireError("dump %s declares %d fault frames over "
                                        "%d lines" % (dump["dump_id"], count,
                                                      len(dump["lines"])))
                    for col in dump["lines"][:count]:
                        if int(col[6], 16) & FRAME_INDEX_PLACEHOLDER:
                            indices.append(int(col[7].rsplit(":", 1)[1], 10))
                        else:
                            indices.append(int(col[0], 10))
                self._indices[count_multiplier] = indices
            except (WireError, ValueError, IndexError):
                self._indices[count_multiplier] = None
        return self._indices[count_multiplier]

    def bases(self, params):
        """The recovered dump base of every dump, summed."""
        key = (params["count multiplier"], params["alternate radix"],
               params["base shift width"])
        if key not in self._bases:
            self.evaluations += 1
            try:
                self._bases[key] = sum(
                    recover_base(dump, ordinal, params)
                    for ordinal, dump in enumerate(self.dumps, 1))
            except (WireError, ValueError, IndexError):
                self._bases[key] = None
        return self._bases[key]

    def walk(self, params):
        """Walk the export's dumps in order, each with its logical columns.

        Carries, per dump, its flag word, its fault frame columns in logical
        order and their recovered resident spans -- everything the wire
        figures read off the frames, needing no parameter beyond the count
        multiplier and the rotation offset.
        """
        key = (params["count multiplier"], params["rotation offset"])
        if key not in self._walks:
            self.evaluations += 1
            try:
                walked = []
                for ordinal, dump in enumerate(self.dumps, 1):
                    flags = dump["dump_flags"]
                    count = dump_frame_count(dump, ordinal, key[0])
                    if count < 1 or count > len(dump["lines"]):
                        raise WireError("dump %s declares %d fault frames over "
                                        "%d lines" % (dump["dump_id"], count,
                                                      len(dump["lines"])))
                    columns = logical_order(dump, count, params)
                    walked.append({"dump_flags": flags,
                                   "columns": columns,
                                   "spans": recover_spans(columns, flags)})
                self._walks[key] = walked
            except (WireError, ValueError, IndexError):
                self._walks[key] = None
        return self._walks[key]

    def triaged(self, params):
        """The control slice triaged and settled, or None if it cannot be."""
        key = tuple(params[name] for name in PARAMETERS)
        if key not in self._triaged:
            try:
                attributed = triage(self.data_dir, "calibration_",
                                    self.tables, params)
                accepted = [f for f in attributed if not f["rejected"]]
                self._triaged[key] = (attributed,) + \
                    settle_quarantine(accepted, self.tables)
            except (WireError, OSError, ValueError, IndexError, KeyError):
                self._triaged[key] = None
        return self._triaged[key]

    # -- the eleven figures ---------------------------------------------
    def reproduce(self, params, controls):
        """Reproduce whichever of `controls` this assignment can decide.

        Returns the figures that could be computed, or None as soon as one of
        them cannot be computed at all.
        """
        values = {}
        for control in controls:
            if control in ("CTRL-A", "CTRL-H"):
                indices = self.indices(params["count multiplier"])
                if indices is None:
                    return None
                values[control] = (len(indices) if control == "CTRL-A"
                                   else sum(indices))
            elif control == "CTRL-B":
                bases = self.bases(params)
                if bases is None:
                    return None
                values[control] = bases
            elif control in ("CTRL-C", "CTRL-D", "CTRL-E", "CTRL-F", "CTRL-G"):
                walked = self.walk(params)
                if walked is None:
                    return None
                if control == "CTRL-C":
                    values[control] = sum(sum(group["spans"])
                                          for group in walked)
                elif control == "CTRL-D":
                    total = 0
                    for group in walked:
                        total += sum(recover_entries(
                            group["columns"], group["dump_flags"],
                            params["entry clock modulus"]))
                    values[control] = total
                elif control == "CTRL-E":
                    values[control] = self._unwind_total(params, walked)
                elif control == "CTRL-F":
                    total = 0
                    for group in walked:
                        for col in group["columns"]:
                            if int(col[6], 16) & FRAME_ALLOCATION_COARSE:
                                total += int(col[4][1:], 10) * \
                                    params["coarse unit"]
                            else:
                                total += int(col[4], 10)
                    values[control] = total
                else:
                    values[control] = self._signature_total(params, walked)
            else:
                outcome = self.triaged(params)
                if outcome is None:
                    return None
                attributed = outcome[0]
                effective, allowance, quarantined = outcome[2], outcome[3], \
                    outcome[5]
                rows = ledger_rows(attributed, self.tables, effective, allowance)
                if control == "CTRL-I":
                    values[control] = len(rows)
                elif control == "CTRL-J":
                    values[control] = len(quarantined)
                else:
                    values[control] = (
                        sum(r["bytes_allocated"] for r in rows)
                        - sum(r["bytes_uploaded"] for r in rows))
        if None in values:
            return None
        return values

    def _unwind_total(self, params, walked):
        """Sum of the recovered unwind stamps, under the running span total."""
        running_span_total = 0
        total = 0
        for group in walked:
            drift = group["dump_flags"] & DUMP_UNWIND_DRIFT
            for index, col in enumerate(group["columns"]):
                if drift:
                    total += int(col[2], 10) - (running_span_total
                                                % params["drift period"])
                else:
                    total += int(col[2], 10)
                running_span_total += group["spans"][index]
        return total

    def _signature_total(self, params, walked):
        """Sum of the recovered fault signatures, over the export-wide ring."""
        ring = [None] * params["ring width"]
        write_position = 0
        total = 0
        for group in walked:
            for col in group["columns"]:
                frame_flags = int(col[6], 16)
                written = col[5]
                if frame_flags & FRAME_SIGNATURE_RING:
                    distance = int(written[1:], 10)
                    signature = ring[(write_position - distance)
                                     % params["ring width"]]
                elif frame_flags & FRAME_SIGNATURE_ROTATION:
                    index = (int(col[7].rsplit(":", 1)[1], 10)
                             if frame_flags & FRAME_INDEX_PLACEHOLDER
                             else int(col[0], 10))
                    turn = (index + params["signature offset"]) % 16
                    signature = written[-turn:] + written[:-turn] if turn \
                        else written
                else:
                    signature = written
                total += int(signature, 16)
                ring[write_position] = signature
                write_position = (write_position + 1) % params["ring width"]
        return total % SIGNATURE_MODULUS

    def matches(self, params, controls):
        """Whether a candidate reproduces every one of `controls`."""
        try:
            values = self.reproduce(params, controls)
        except (WireError, ValueError, IndexError, KeyError, TypeError):
            return False
        return values is not None and all(
            values[control] == self.published[control] for control in controls)


def settle_parameters(calibration):
    """Enumerate the candidate space and keep the assignment that survives."""
    partials = [{}]
    for stage in SETTLEMENT_STAGES:
        already = set(partials[0])
        # The controls this stage brings within reach for the first time.
        controls = [control for control in CONTROL_ORDER
                    if set(CONTROL_DEPS[control]) <= already | set(stage)
                    and not set(CONTROL_DEPS[control]) <= already]
        survivors = []
        for partial in partials:
            for values in product(*(DOMAIN[name] for name in stage)):
                candidate = dict(partial)
                candidate.update(zip(stage, values))
                if calibration.matches(candidate, controls):
                    survivors.append(candidate)
        if not survivors:
            raise WireError("no assignment of %s reproduces the published "
                            "figures" % ", ".join(stage))
        partials = survivors

    full = [partial for partial in partials if len(partial) == len(PARAMETERS)]
    if len(full) != 1:
        raise WireError("the control slice leaves %d assignments standing"
                        % len(full))
    selected = full[0]

    if not calibration.matches(selected, CONTROL_ORDER):
        raise WireError("the settled assignment does not reproduce every "
                        "published figure")

    # A verdict for every candidate value in every parameter's domain: hold the
    # rest of the assignment at its settled value and name the control figure
    # the candidate cannot reproduce.
    parameters = []
    for name in PARAMETERS:
        rejected = []
        for value in DOMAIN[name]:
            if value == selected[name]:
                continue
            candidate = dict(selected)
            candidate[name] = value
            falsified = None
            for control in CONTROL_ORDER:
                if not calibration.matches(candidate, [control]):
                    falsified = control
                    break
            if falsified is None:
                raise WireError("candidate %s=%r reproduces every published "
                                "figure" % (name, value))
            rejected.append({"value": value, "control": falsified})
        parameters.append({"name": name,
                           "candidates": len(DOMAIN[name]),
                           "control": STAGE_CONTROL[name],
                           "selected": selected[name],
                           "rejected": rejected})

    comparison = []
    for control in CONTROL_ORDER:
        reproduced = calibration.reproduce(selected, [control])
        comparison.append({"control": control,
                           "published": calibration.published[control],
                           "reproduced": reproduced[control]})

    return selected, {"comparison": comparison,
                      "search": {"evaluations": calibration.evaluations,
                                 "parameters": parameters}}


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------

def write_report(path, report):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(report, indent=2, sort_keys=True))
        handle.write("\n")


def write_ledger(path, rows):
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True,
                                    separators=(",", ":")))
            handle.write("\n")


def main(argv):
    data_dir = argv[1] if len(argv) > 1 else DEFAULT_DATA_DIR
    out_dir = argv[2] if len(argv) > 2 else DEFAULT_OUT_DIR
    os.makedirs(out_dir, exist_ok=True)

    tables = Tables(data_dir)

    # Settle the withheld wire parameters against the published control
    # figures before any figure for the incident corpus is computed.
    calibration_files = ("calibration_fault_frames.psv",
                         "calibration_runtime_probe.csv",
                         "calibration_uplink_reports.jsonl",
                         "control-slice-figures.json")
    if all(os.path.exists(os.path.join(data_dir, name))
           for name in calibration_files):
        params, calibration_record = settle_parameters(
            Calibration(data_dir, tables))
    else:
        # A corpus whose reporter wrote every flag word clear exercises none
        # of the withheld parameters, so there is nothing to settle; the
        # widest clock modulus keeps the capture tick arithmetic exact and
        # the rest are never read.
        params = {name: DOMAIN[name][0] for name in PARAMETERS}
        params["entry clock modulus"] = max(DOMAIN["entry clock modulus"])
        calibration_record = {"comparison": [],
                              "search": {"evaluations": 0,
                                         "parameters": []}}

    # With the parameter set settled, derive the whole triage set afresh from
    # the incident corpus.
    frames = triage(data_dir, "", tables, params)
    accepted = [frame for frame in frames if not frame["rejected"]]
    modules, effective, allowance, passes, quarantined = \
        settle_quarantine(accepted, tables)
    rows = ledger_rows(frames, tables, effective, allowance)

    seen = set()
    for row in rows:
        if row["capture_tick"] in seen:
            raise WireError("two ledger rows share capture tick %d"
                            % row["capture_tick"])
        seen.add(row["capture_tick"])

    files = build_reports(frames, rows, modules, effective, allowance, passes,
                          quarantined, tables)
    files["data_quality.json"] = data_quality(frames, rows, passes, quarantined,
                                              calibration_record)

    write_ledger(os.path.join(out_dir, "normalized.jsonl"), rows)
    for name, report in files.items():
        write_report(os.path.join(out_dir, name), report)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
