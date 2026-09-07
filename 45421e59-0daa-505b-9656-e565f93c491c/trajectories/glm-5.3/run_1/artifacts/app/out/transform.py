#!/usr/bin/env python3
"""Crash reporter triage transform.

Usage:  python3 transform.py [DATA_DIR] [OUT_DIR]

Reads a corpus directory -- the crash reporter dump export, the runtime probe
and uplink side exports, the fleet reference tables, the control slice and the
control figures published for it -- and writes the whole triage set under
OUT_DIR:

    normalized.jsonl          the fault ledger, one object per accepted frame
    summary.json              the incident roll-up
    product_summary.json      the per-module roll-up
    region_<zone>.json        one report per deployment zone
    segment_<segment>.json    one report per tenant segment
    data_quality.json         reconciliation and derivation record

Nothing outside OUT_DIR is written.  Standard library only.
"""

import csv
import json
import os
import sys

# --------------------------------------------------------------------------
# Constants fixed by the written rules.
# ---------------------------------------------------------------------------

TICKS_PER_BUILD_INDEX = 900000000   # 1000 ticks per millisecond
CHECK_MODULUS = 65536
SIGNATURE_WIDTH = 16
SIGNATURE_MODULUS = 1000000007

# Dump flag word.
DUMP_SECOND_ORDER_SPANS = 0x001
DUMP_CHAINED_ENTRIES = 0x002
DUMP_DRIFTED_UNWINDS = 0x004
DUMP_ROTATED_LINES = 0x008
DUMP_BIASED_COUNT = 0x010
DUMP_ALTERNATE_BASE_RADIX = 0x020
DUMP_MODULE_ALIAS = 0x100
DUMP_SPLIT_BASE = 0x800

# Frame flag word.
FRAME_INDEX_IN_SYMBOL = 0x040
FRAME_ROTATED_SIGNATURE = 0x080
FRAME_COARSE_ALLOCATION = 0x200
FRAME_RING_SIGNATURE = 0x400

PARAMETER_ORDER = [
    "count multiplier",
    "rotation offset",
    "entry clock modulus",
    "drift period",
    "alternate radix",
    "base shift width",
    "coarse unit",
    "signature offset",
    "ring width",
]

PARAMETER_DOMAINS = {
    "entry clock modulus": [1 << exponent for exponent in range(22, 35)],
    "drift period": [48, 96, 192, 384, 768],
    "rotation offset": list(range(0, 8)),
    "count multiplier": list(range(0, 9)),
    "alternate radix": list(range(11, 36)),
    "signature offset": list(range(0, 8)),
    "coarse unit": [64, 128, 256, 512, 1024],
    "ring width": [4, 8, 12, 16],
    "base shift width": [8, 10, 12, 16],
}

# Which withheld wire parameters each cheap control figure reads.  This is a
# property of the reporter's rules, not of any particular corpus.
CONTROL_DEPENDENCIES = {
    "CTRL-A": ("count multiplier",),
    "CTRL-H": ("count multiplier",),
    "CTRL-C": ("count multiplier", "rotation offset"),
    "CTRL-D": ("count multiplier", "rotation offset", "entry clock modulus"),
    "CTRL-E": ("count multiplier", "rotation offset", "drift period"),
    "CTRL-F": ("count multiplier", "coarse unit"),
    "CTRL-B": ("count multiplier", "alternate radix", "base shift width"),
    "CTRL-G": ("count multiplier", "rotation offset", "signature offset",
               "ring width"),
}

CONTROL_ORDER = ["CTRL-A", "CTRL-B", "CTRL-C", "CTRL-D", "CTRL-E", "CTRL-F",
                 "CTRL-G", "CTRL-H", "CTRL-I", "CTRL-J", "CTRL-K"]

REJECTION_KINDS = [
    "unresolved_module",
    "retired_build",
    "outside_support",
    "unprobed_frame",
    "unreported_dump",
]

SIDE_EXPORT_PREFIX = "calibration_"


# --------------------------------------------------------------------------
# Small helpers.
# ---------------------------------------------------------------------------

def read_csv_rows(path):
    with open(path, "r", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def read_jsonl(path):
    rows = []
    with open(path, "r") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def to_int(text):
    """Read a numeral the fleet writes as a signed decimal integer.

    A negative written value is legitimate: the second-order differences a
    dump's resident span column carries run either way.
    """
    if isinstance(text, int):
        return text
    text = text.strip()
    body = text[1:] if text.startswith("-") else text
    if not body.isdigit():
        raise ValueError("not a decimal numeral: %r" % text)
    return int(text)


def rotate_left(text, count):
    count %= len(text)
    return text[count:] + text[:count]


def alternate_radix_value(numeral, radix):
    """Read a numeral written in the alternate radix, digits `0`-`9` then `a`-`z`."""
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    if radix < 2 or radix > len(digits):
        raise ValueError("unsupported radix %r" % radix)
    if numeral.startswith("-"):
        raise ValueError("negative alternate numeral: %r" % numeral)
    value = 0
    for ch in numeral:
        position = digits.index(ch)
        if position >= radix:
            raise ValueError("digit %r outside radix %d" % (ch, radix))
        value = value * radix + position
    return value


def dump_bases(dumps, counts, params):
    """The recovered dump base of every dump, in export order.

    A base the trial parameters cannot recover is left as None, so that a
    parameter the caller is not interested in cannot spoil a verdict.
    """
    bases = []
    for dump, count in zip(dumps, counts):
        flags = dump["flags"]
        numeral = dump["base_field"]
        try:
            if flags & DUMP_ALTERNATE_BASE_RADIX:
                base = alternate_radix_value(numeral, params["alternate radix"])
            else:
                base = to_int(numeral)
            if flags & DUMP_SPLIT_BASE:
                low = to_int(dump["lines"][count][0])
                base = (base << params["base shift width"]) | low
            if base < 0:
                raise ValueError("negative dump base")
        except (ValueError, IndexError):
            base = None
        bases.append(base)
    return bases


# --------------------------------------------------------------------------
# Reference tables.
# ---------------------------------------------------------------------------

class Registry(object):
    """The build registry, resolved as a chain of tags over a build index."""

    def __init__(self, rows):
        self.by_tag = {}
        for row in rows:
            self.by_tag.setdefault(row["module_tag"], []).append(row)

    def resolve(self, tag, build_index):
        """Return the terminal registry row covering `tag`, or None."""
        seen = set()
        current = tag
        while True:
            if current in seen:
                return None          # a chain that revisits a tag
            seen.add(current)
            row = self._cover(current, build_index)
            if row is None:
                return None          # no row covers this build index
            if row["canonical_tag"] == row["module_tag"]:
                return row
            current = row["canonical_tag"]

    def _cover(self, tag, build_index):
        covering = [row for row in self.by_tag.get(tag, [])
                    if int(row["effective_from_build"]) <= build_index
                    <= int(row["effective_to_build"])]
        if len(covering) != 1:
            return None
        return covering[0]


class FleetTables(object):
    """The five reference tables that govern every corpus alike."""

    def __init__(self, data_dir):
        self.registry = Registry(read_csv_rows(self._path(data_dir,
                                                          "build_registry.csv")))
        self.ladder = {}
        for row in read_csv_rows(self._path(data_dir, "severity_ladder.csv")):
            self.ladder.setdefault(row["component_key"],
                                   {})[int(row["tier_index"])] = int(row["budget_ns"])
        self.windows = {}
        for row in read_csv_rows(self._path(data_dir, "support_windows.csv")):
            self.windows[row["component_key"]] = (int(row["open_tick"]),
                                                  int(row["close_tick"]))
        self.host_zones = {}
        for row in read_csv_rows(self._path(data_dir, "host_zones.csv")):
            self.host_zones[row["host_id"]] = row["zone"]
        self.tenant_segments = {}
        for row in read_csv_rows(self._path(data_dir, "tenant_classes.csv")):
            self.tenant_segments[row["tenant_class"]] = row["tenant_segment"]
        self.zones = sorted(set(self.host_zones.values()))
        self.segments = sorted(set(self.tenant_segments.values()))

    @staticmethod
    def _path(data_dir, name):
        return os.path.join(data_dir, name)


# --------------------------------------------------------------------------
# Crash reporter dump export.
# --------------------------------------------------------------------------

def parse_export(path):
    """Split a pipe-delimited dump export into headers and their lines."""
    dumps = []
    current = None
    with open(path, "r") as handle:
        for raw in handle:
            line = raw.rstrip("\n")
            if not line.strip() or line.startswith("#"):
                continue
            fields = line.split("|")
            if fields[0] == "@":
                if len(fields) != 8:
                    raise ValueError("malformed dump header: %r" % line)
                current = {
                    "dump_id": fields[1],
                    "module": fields[2],
                    "declared_count": fields[3],
                    "base_field": fields[4],
                    "flags": int(fields[5], 16),
                    "key": fields[6],
                    "dump_check": int(fields[7], 16),
                    "lines": [],
                    "ordinal": len(dumps) + 1,
                }
                dumps.append(current)
            else:
                if current is None or len(fields) != 9:
                    raise ValueError("malformed fault frame line: %r" % line)
                current["lines"].append(fields)
    return dumps


def frame_check(columns):
    """The frame check total of a line, taken over its first eight columns."""
    return sum(ord(ch) for ch in "|".join(columns[:8])) % CHECK_MODULUS


def fault_frame_counts(dumps, count_multiplier):
    """Recover each dump's fault frame count.

    The count comes from the declared count less the count bias the dump flag
    word names, and is accepted only when the dump's check total covers exactly
    that many leading lines.  Returns None when the multiplier does not fit.
    """
    counts = []
    for dump in dumps:
        written = len(dump["lines"])
        declared = to_int(dump["declared_count"])
        if dump["flags"] & DUMP_BIASED_COUNT:
            count = declared - (dump["ordinal"] * count_multiplier) % 9
        else:
            count = declared
        if count < 1 or count > written:
            return None
        if (dump["flags"] & DUMP_SPLIT_BASE) and count >= written:
            return None              # a split base needs at least one trailer
        total = 0
        for columns in dump["lines"][:count]:
            total += int(columns[8], 16)
        if total % CHECK_MODULUS != dump["dump_check"]:
            return None
        counts.append(count)
    return counts


def verify_checks(dumps, counts):
    """Every frame and dump check total must hold over the columns it covers."""
    for dump, count in zip(dumps, counts):
        total = 0
        for columns in dump["lines"][:count]:
            if frame_check(columns) != int(columns[8], 16):
                raise ValueError("frame check total mismatch in dump %s line %s"
                                 % (dump["dump_id"], columns[0]))
            total += int(columns[8], 16)
        if total % CHECK_MODULUS != dump["dump_check"]:
            raise ValueError("dump check total mismatch in dump %s"
                             % dump["dump_id"])


def second_order_spans(written):
    """Invert the second-order difference encoding of a dump's resident spans.

    The first written value is the first span, the second is the first
    difference and every later one is the difference of differences.
    """
    spans = [written[0]]
    if len(written) > 1:
        delta = written[1]
        spans.append(spans[0] + delta)
        for value in written[2:]:
            delta += value
            spans.append(spans[-1] + delta)
    return spans


def decode_frames(dumps, counts, params):
    """Recover every fault frame of an export, in logical order.

    Each frame carries its source dump, module tag, authoritative frame index,
    symbol kind, entry and unwind stamps, resident span, allocated byte count,
    fault signature, capture tick and build index.  A field the trial
    parameters cannot recover is left as None, so that a parameter the caller
    is not testing cannot spoil a verdict.
    """
    bases = dump_bases(dumps, counts, params)

    frames = []
    running_span = 0
    ring = [None] * params["ring width"]
    ring_pos = 0
    for dump, count, base in zip(dumps, counts, bases):
        flags = dump["flags"]
        written = dump["lines"]
        shift = 0
        ordered = None
        spans = None
        entries = None
        try:
            if flags & DUMP_ROTATED_LINES:
                shift = (to_int(dump["key"]) + params["rotation offset"]) % count
            ordered = [written[(index - shift) % count] for index in range(count)]

            written_spans = [to_int(c[3]) for c in ordered]
            spans = second_order_spans(written_spans) \
                if (flags & DUMP_SECOND_ORDER_SPANS) else written_spans

            written_entries = [to_int(c[1]) for c in ordered]
            if flags & DUMP_CHAINED_ENTRIES:
                entries = [written_entries[0]]
                for value in written_entries[1:]:
                    entries.append(
                        (value - entries[-1]) % params["entry clock modulus"])
            else:
                entries = list(written_entries)
        except (ValueError, IndexError, ZeroDivisionError):
            ordered = None

        for position, columns in enumerate(ordered or []):
            frame_flags = int(columns[6], 16)
            symbol = columns[7]
            kind, _, numeral = symbol.rpartition(":")
            try:
                if frame_flags & FRAME_INDEX_IN_SYMBOL:
                    frame_index = to_int(numeral)
                else:
                    frame_index = to_int(columns[0])
            except ValueError:
                frame_index = None

            signature = recover_signature(columns[5], frame_flags, frame_index,
                                          ring, ring_pos, params)

            try:
                if flags & DUMP_DRIFTED_UNWINDS:
                    unwind = to_int(columns[2]) - (running_span
                                                   % params["drift period"])
                else:
                    unwind = to_int(columns[2])
                allocation_column = columns[4]
                if frame_flags & FRAME_COARSE_ALLOCATION:
                    allocated = to_int(allocation_column[1:]) \
                        * params["coarse unit"]
                else:
                    allocated = to_int(allocation_column)
                entry = entries[position]
                distance = (entry - entries[0]) % params["entry clock modulus"]
                capture_tick = None if base is None else base + distance
                build_index = None if capture_tick is None \
                    else capture_tick // TICKS_PER_BUILD_INDEX
            except (ValueError, ZeroDivisionError):
                entry = None
                unwind = None
                allocated = None
                capture_tick = None
                build_index = None

            frames.append({
                "source_dump": dump["dump_id"],
                "module_tag": dump["module"],
                "frame_index": frame_index,
                "symbol": kind,
                "entry": entry,
                "unwind": unwind,
                "span": spans[position],
                "allocated": allocated,
                "signature": signature,
                "capture_tick": capture_tick,
                "build_index": build_index,
            })
            running_span += spans[position]
            ring[ring_pos] = signature
            ring_pos = (ring_pos + 1) % params["ring width"]
    return frames


def recover_signature(column, frame_flags, frame_index, ring, ring_pos, params):
    """The fault signature a frame column carries, or None.

    Every fault signature recovered from an export is sixteen lowercase
    hexadecimal characters, whatever way the column wrote it.
    """
    try:
        if frame_flags & FRAME_RING_SIGNATURE:
            distance = to_int(column[1:])
            signature = ring[(ring_pos - distance) % params["ring width"]]
        elif frame_flags & FRAME_ROTATED_SIGNATURE:
            turn = (frame_index + params["signature offset"]) % SIGNATURE_WIDTH
            signature = rotate_left(column, SIGNATURE_WIDTH - turn)
        else:
            signature = column
    except (ValueError, TypeError, ZeroDivisionError):
        return None
    if not isinstance(signature, str) or len(signature) != SIGNATURE_WIDTH:
        return None
    if any(ch not in "0123456789abcdef" for ch in signature):
        return None
    return signature


def complete(frames, fields):
    """True when every frame recovered every named field."""
    return all(frame[name] is not None for frame in frames for name in fields)


# --------------------------------------------------------------------------
# Fault attribution.
# --------------------------------------------------------------------------

def load_probe(path):
    """(module_tag, frame_index) -> (samples, peak_ns)."""
    table = {}
    for row in read_csv_rows(path):
        table[(row["module_tag"], to_int(row["frame_index"]))] = (
            to_int(row["samples"]), to_int(row["peak_ns"]))
    return table


def load_uplink(path):
    """(module_tag, frame_index) -> the uplink report object."""
    table = {}
    for row in read_jsonl(path):
        table[(row["module_tag"], to_int(row["frame_index"]))] = row
    return table


def classify_frames(frames, tables, probe, uplink):
    """Test every recovered frame against the ledger's acceptance conditions.

    A frame whose module does not resolve onto a still-carried module is
    rejected at that gate and carries no further condition, because the side
    exports speak only for carried modules.  Past that gate a frame is counted
    once against each of the three remaining conditions it breaks.
    """
    records = []
    rejections = dict.fromkeys(REJECTION_KINDS, 0)
    for frame in frames:
        row = tables.registry.resolve(frame["module_tag"], frame["build_index"])
        reasons = []
        if row is None:
            reasons.append("unresolved_module")
        elif row["status"] != "supported":
            reasons.append("retired_build")
        else:
            window = tables.windows.get(row["component_key"])
            if window is not None and not (window[0] < frame["capture_tick"]
                                           < window[1]):
                reasons.append("outside_support")
            key = (row["canonical_tag"], frame["frame_index"])
            if key not in probe:
                reasons.append("unprobed_frame")
            if key not in uplink:
                reasons.append("unreported_dump")
        for reason in reasons:
            rejections[reason] += 1
        records.append({
            "frame": frame,
            "registry_row": row,
            "reasons": reasons,
            "accepted": not reasons,
        })
    return records, rejections


def settle_quarantine(modules, ladder):
    """Demote effective tiers until no tiered module runs over budget.

    A pass recomputes every containment envelope from the tiers standing at
    its start, demotes each offending module by one tier, and is counted even
    when it finds nothing left to demote.
    """
    tiers = dict((tag, info["declared_tier"]) for tag, info in modules.items())

    def standing():
        budgets = {}
        lowest = {}
        for tag, info in modules.items():
            component = info["component_key"]
            budgets[tag] = ladder[component][tiers[tag]]
            if component not in lowest or budgets[tag] < lowest[component]:
                lowest[component] = budgets[tag]
        allowance = {}
        for tag, info in modules.items():
            component = info["component_key"]
            envelope = lowest[component] * len(ladder[component])
            allowance[tag] = min(budgets[tag], envelope)
        return budgets, allowance

    passes = 0
    while True:
        passes += 1
        _, allowance = standing()
        demoted = False
        for tag in sorted(modules):
            if tiers[tag] <= 0:
                continue
            within = 0
            over = 0
            for peak in modules[tag]["peaks"]:
                if peak <= allowance[tag]:
                    within += 1
                else:
                    over += 1
            if over > within:
                tiers[tag] -= 1
                demoted = True
        if not demoted:
            break

    budgets, allowance = standing()
    quarantined = []
    for tag in sorted(modules):
        within = 0
        over = 0
        for peak in modules[tag]["peaks"]:
            if peak <= allowance[tag]:
                within += 1
            else:
                over += 1
        if tiers[tag] == 0 and over > within:
            quarantined.append(tag)
    return {
        "tiers": tiers,
        "budgets": budgets,
        "allowance": allowance,
        "quarantined": quarantined,
        "passes": passes,
    }


def build_ledger(records, probe, uplink, tables):
    """The fault ledger plus everything the roll-ups need."""
    accepted = [record for record in records if record["accepted"]]

    modules = {}
    for record in accepted:
        frame = record["frame"]
        row = record["registry_row"]
        tag = row["canonical_tag"]
        samples, peak = probe[(tag, frame["frame_index"])]
        info = modules.setdefault(tag, {
            "component_key": row["component_key"],
            "declared_tier": int(row["tier_index"]),
            "peaks": [],
            "samples": 0,
            "bytes_uploaded": 0,
            "accepted_count": 0,
        })
        if int(row["tier_index"]) > info["declared_tier"]:
            # A module renamed onto differently tiered rows keeps the
            # loosest bound its resolutions place on it.
            info["declared_tier"] = int(row["tier_index"])
        info["peaks"].append(peak)
        info["samples"] += samples
        info["accepted_count"] += 1
        info["bytes_uploaded"] += int(uplink[(tag, frame["frame_index"])]["bytes_uploaded"])

    settlement = settle_quarantine(modules, tables.ladder)

    rows = []
    for record in accepted:
        frame = record["frame"]
        row = record["registry_row"]
        tag = row["canonical_tag"]
        samples, peak = probe[(tag, frame["frame_index"])]
        report = uplink[(tag, frame["frame_index"])]
        rows.append({
            "bytes_allocated": frame["allocated"],
            "bytes_uploaded": int(report["bytes_uploaded"]),
            "capture_tick": frame["capture_tick"],
            "component_key": row["component_key"],
            "cost_ns": frame["span"],
            "effective_tier": settlement["tiers"][tag],
            "enter_ns": frame["entry"],
            "fault_signature": frame["signature"],
            "frame_index": frame["frame_index"],
            "host_zone": tables.host_zones[report["host_id"]],
            "module_tag": tag,
            "peak_ns": peak,
            "samples": samples,
            "source_dump": frame["source_dump"],
            "symbol": frame["symbol"],
            "tenant_segment": tables.tenant_segments[report["tenant_class"]],
            "unwind_ns": frame["unwind"],
            "within_budget": peak <= settlement["allowance"][tag],
        })
    rows.sort(key=lambda row: row["capture_tick"])
    return rows, modules, settlement


# --------------------------------------------------------------------------
# Roll-ups.
# --------------------------------------------------------------------------

def sum_by(rows, field, value):
    out = {}
    for row in rows:
        out[row[field]] = out.get(row[field], 0) + value(row)
    return out


def zone_segment_report(rows, label, value):
    """The report shape the shipped example zone report fixes.

    A zone report crosses its frames by tenant segment; a segment report
    crosses them by deployment zone.
    """
    cross = "by_tenant_segment" if label == "zone" else "by_zone"
    other = "tenant_segment" if label == "zone" else "host_zone"
    report = {
        "accepted_count": len(rows),
        "by_component": sum_by(rows, "component_key",
                               lambda row: row["bytes_uploaded"]),
        cross: sum_by(rows, other, lambda row: row["bytes_uploaded"]),
        "modules": sorted(set(row["module_tag"] for row in rows)),
        "total_bytes_uploaded": sum(row["bytes_uploaded"] for row in rows),
        "total_cost_ns": sum(row["cost_ns"] for row in rows),
        "within_budget_count": sum(1 for row in rows if row["within_budget"]),
    }
    report[label] = value
    return report


def roll_up(rows, tables, modules, settlement, rejected_count):
    """Every report of the triage set, keyed by its file name."""
    reports = {}

    reports["summary.json"] = {
        "accepted_count": len(rows),
        "by_component": sum_by(rows, "component_key",
                               lambda row: row["bytes_uploaded"]),
        "by_effective_tier": dict(
            ("%d" % tier, count) for tier, count in sorted(
                sum_by(rows, "effective_tier", lambda row: 1).items())),
        "by_tenant_segment": sum_by(rows, "tenant_segment",
                                    lambda row: row["bytes_uploaded"]),
        "by_zone": sum_by(rows, "host_zone", lambda row: row["bytes_uploaded"]),
        "rejected_count": rejected_count,
        "total_bytes_allocated": sum(row["bytes_allocated"] for row in rows),
        "total_bytes_uploaded": sum(row["bytes_uploaded"] for row in rows),
        "total_cost_ns": sum(row["cost_ns"] for row in rows),
        "within_budget_count": sum(1 for row in rows if row["within_budget"]),
    }

    ladder = tables.ladder
    product = []
    for tag in sorted(modules):
        info = modules[tag]
        tier = settlement["tiers"][tag]
        product.append({
            "accepted_count": info["accepted_count"],
            "allowance_ns": settlement["allowance"][tag],
            "budget_ns": settlement["budgets"][tag],
            "component_key": info["component_key"],
            "declared_tier": info["declared_tier"],
            "effective_tier": tier,
            "module_tag": tag,
            "net_samples": info["samples"],
            "quarantined": tag in settlement["quarantined"],
            "total_bytes_uploaded": info["bytes_uploaded"],
            "within_budget_count": sum(
                1 for row in rows
                if row["module_tag"] == tag and row["within_budget"]),
        })
    reports["product_summary.json"] = {"modules": product}

    for zone in tables.zones:
        reports["region_%s.json" % zone] = zone_segment_report(
            [row for row in rows if row["host_zone"] == zone], "zone", zone)
    for segment in tables.segments:
        reports["segment_%s.json" % segment] = zone_segment_report(
            [row for row in rows if row["tenant_segment"] == segment],
            "tenant_segment", segment)
    return reports


# --------------------------------------------------------------------------
# Control slice calibration.
# --------------------------------------------------------------------------

def derive_corpus(data_dir, prefix, params, tables):
    """Run the whole derivation over one corpus of the tier set."""
    params = complete_parameters(params)
    dumps = parse_export(os.path.join(data_dir, prefix + "fault_frames.psv"))
    counts = fault_frame_counts(dumps, params["count multiplier"])
    if counts is None:
        return None
    frames = decode_frames(dumps, counts, params)
    if not complete(frames, FRAME_FIELDS):
        return None
    probe = load_probe(os.path.join(data_dir, prefix + "runtime_probe.csv"))
    uplink = load_uplink(os.path.join(data_dir, prefix + "uplink_reports.jsonl"))
    records, _ = classify_frames(frames, tables, probe, uplink)
    rows, modules, settlement = build_ledger(records, probe, uplink, tables)
    return {
        "dumps": dumps,
        "counts": counts,
        "frames": frames,
        "rows": rows,
        "modules": modules,
        "settlement": settlement,
    }


FRAME_FIELDS = ("frame_index", "entry", "unwind", "span", "allocated",
                "signature", "capture_tick", "build_index")

EXPORT_CONTROL_FIELDS = {
    "CTRL-B": (),
    "CTRL-C": ("span",),
    "CTRL-D": ("entry",),
    "CTRL-E": ("unwind",),
    "CTRL-F": ("allocated",),
    "CTRL-G": ("signature",),
    "CTRL-H": ("frame_index",),
}


def cheap_control(control, dumps, params):
    """Recompute a control figure that reads the export alone.

    Returns None when the trial assignment cannot describe the export.  Only
    the parameters the figure actually reads need to be present in `params`;
    every other one is filled with a permissive default whose exact value
    cannot reach this figure.
    """
    params = complete_parameters(params)
    counts = fault_frame_counts(dumps, params["count multiplier"])
    if counts is None:
        return None
    if control == "CTRL-A":
        return sum(counts)
    bases = dump_bases(dumps, counts, params)
    if control == "CTRL-B":
        return None if any(base is None for base in bases) else sum(bases)
    frames = decode_frames(dumps, counts, params)
    fields = EXPORT_CONTROL_FIELDS[control]
    if not complete(frames, fields):
        return None
    if control == "CTRL-C":
        return sum(frame["span"] for frame in frames)
    if control == "CTRL-D":
        return sum(frame["entry"] for frame in frames)
    if control == "CTRL-E":
        return sum(frame["unwind"] for frame in frames)
    if control == "CTRL-F":
        return sum(frame["allocated"] for frame in frames)
    if control == "CTRL-G":
        try:
            return sum(int(frame["signature"], 16)
                       for frame in frames) % SIGNATURE_MODULUS
        except ValueError:
            return None
    if control == "CTRL-H":
        return sum(frame["frame_index"] for frame in frames)
    raise ValueError("not a cheap control: %s" % control)


def complete_parameters(params):
    """Fill any parameter the caller left unset with a permissive default."""
    filled = dict(params)
    for name, domain in PARAMETER_DOMAINS.items():
        filled.setdefault(name, domain[-1])
    return filled


def full_control_figures(data_dir, params, tables):
    """Every published control figure, read off a complete derivation."""
    derived = derive_corpus(data_dir, SIDE_EXPORT_PREFIX,
                            complete_parameters(params), tables)
    if derived is None:
        return None
    frames = derived["frames"]
    rows = derived["rows"]
    bases = dump_bases(derived["dumps"], derived["counts"],
                       complete_parameters(params))
    if any(base is None for base in bases):
        return None
    return {
        "CTRL-A": sum(derived["counts"]),
        "CTRL-B": sum(bases),
        "CTRL-C": sum(frame["span"] for frame in frames),
        "CTRL-D": sum(frame["entry"] for frame in frames),
        "CTRL-E": sum(frame["unwind"] for frame in frames),
        "CTRL-F": sum(frame["allocated"] for frame in frames),
        "CTRL-G": sum(int(frame["signature"], 16)
                      for frame in frames) % SIGNATURE_MODULUS,
        "CTRL-H": sum(frame["frame_index"] for frame in frames),
        "CTRL-I": len(rows),
        "CTRL-J": len(derived["settlement"]["quarantined"]),
        "CTRL-K": (sum(row["bytes_allocated"] for row in rows)
                   - sum(row["bytes_uploaded"] for row in rows)),
    }


def calibrate(data_dir, tables):
    """Settle the withheld wire parameters against the published controls.

    Every parameter is settled by the cheapest control figure that reads it
    and parameters already settled; each candidate in its published domain is
    evaluated before the value is selected.  Where a control reads two
    parameters at once, the first one swept accepts any completion of the
    second from its own domain, and the second is then swept against the
    selected value of the first.  Should that one figure not separate a
    parameter on its own, the sweep escalates to every control figure the
    settled parameters already make computable.
    """
    with open(os.path.join(data_dir, "control-slice-figures.json"), "r") as handle:
        published = json.load(handle)
    dumps = parse_export(os.path.join(data_dir,
                                      SIDE_EXPORT_PREFIX + "fault_frames.psv"))

    selected = {}
    parameters = []
    evaluations = 0

    for name in PARAMETER_ORDER:
        # The cheapest control that reads this parameter: the one that leaves
        # the fewest of its companions still unsettled.
        readable = [control for control in CONTROL_ORDER
                    if control in CONTROL_DEPENDENCIES
                    and name in CONTROL_DEPENDENCIES[control]
                    and control in published]
        if not readable:
            raise ValueError("no published control settles %s" % name)
        control = min(readable, key=lambda candidate: len(
            [other for other in CONTROL_DEPENDENCIES[candidate]
             if other != name and other not in selected]))
        companions = [other for other in CONTROL_DEPENDENCIES[control]
                      if other != name and other not in selected]
        reachable = set(selected) | set([name]) | set(companions)
        escalation = [other for other in CONTROL_ORDER
                      if other in CONTROL_DEPENDENCIES
                      and other in published
                      and set(CONTROL_DEPENDENCIES[other]) <= reachable]

        domain = PARAMETER_DOMAINS[name]
        survivors = []
        rejected = []
        for value in domain:
            evaluations += 1
            completions = [{}]
            for other in companions:
                completions = [dict(fixed, **{other: other_value})
                               for fixed in completions
                               for other_value in PARAMETER_DOMAINS[other]]
            reproduced = False
            falsified = None
            for completion in completions:
                trial = dict(selected)
                trial.update(completion)
                trial[name] = value
                broken = [figure for figure in escalation
                          if cheap_control(figure, dumps, trial)
                          != published[figure]]
                if not broken:
                    reproduced = True
                    break
                if falsified is None:
                    falsified = broken[0]
            if reproduced:
                survivors.append(value)
            else:
                rejected.append({"value": value,
                                 "control": falsified or control})
        if len(survivors) != 1:
            raise ValueError("the published controls leave %d candidates for %s"
                             % (len(survivors), name))
        selected[name] = survivors[0]
        parameters.append({
            "name": name,
            "candidates": len(domain),
            "control": control,
            "selected": survivors[0],
            "rejected": rejected,
        })

    derived = full_control_figures(data_dir, selected, tables)
    comparison = [{"control": control,
                   "published": published[control],
                   "reproduced": derived[control]}
                  for control in CONTROL_ORDER if control in published]
    for entry in comparison:
        if entry["published"] != entry["reproduced"]:
            raise ValueError("settled parameters fail to reproduce %s"
                             % entry["control"])
    return selected, {"evaluations": evaluations,
                      "parameters": parameters}, comparison


# --------------------------------------------------------------------------
# Output.
# ---------------------------------------------------------------------------

def write_reports(out_dir, rows, reports, quality):
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    with open(os.path.join(out_dir, "normalized.jsonl"), "w") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")))
            handle.write("\n")
    for name in sorted(reports):
        with open(os.path.join(out_dir, name), "w") as handle:
            handle.write(json.dumps(reports[name], indent=2, sort_keys=True))
            handle.write("\n")
    with open(os.path.join(out_dir, "data_quality.json"), "w") as handle:
        handle.write(json.dumps(quality, indent=2, sort_keys=True))
        handle.write("\n")


def run(data_dir, out_dir):
    tables = FleetTables(data_dir)
    params, search, comparison = calibrate(data_dir, tables)

    dumps = parse_export(os.path.join(data_dir, "fault_frames.psv"))
    counts = fault_frame_counts(dumps, params["count multiplier"])
    if counts is None:
        raise ValueError("the incident export does not admit its declared counts")
    verify_checks(dumps, counts)
    frames = decode_frames(dumps, counts, params)
    if not complete(frames, FRAME_FIELDS):
        raise ValueError("the incident export does not decode")
    probe = load_probe(os.path.join(data_dir, "runtime_probe.csv"))
    uplink = load_uplink(os.path.join(data_dir, "uplink_reports.jsonl"))

    records, rejections = classify_frames(frames, tables, probe, uplink)
    rows, modules, settlement = build_ledger(records, probe, uplink, tables)
    rejected_count = sum(1 for record in records if not record["accepted"])
    reports = roll_up(rows, tables, modules, settlement, rejected_count)

    allocated = sum(row["bytes_allocated"] for row in rows)
    uploaded = sum(row["bytes_uploaded"] for row in rows)
    quality = {
        "accepted_count": len(rows),
        "calibration_comparison": comparison,
        "candidate_search": search,
        "frame_count": len(frames),
        "reconciliation": {
            "allocated_bytes": allocated,
            "residual_bytes": allocated - uploaded,
            "uploaded_bytes": uploaded,
        },
        "rejections": rejections,
        "settlement": {
            "passes": settlement["passes"],
            "quarantined_modules": settlement["quarantined"],
        },
    }
    write_reports(out_dir, rows, reports, quality)
    return quality


def main(argv):
    if len(argv) == 3:
        data_dir, out_dir = argv[1], argv[2]
    elif len(argv) == 1:
        data_dir, out_dir = "/app/data", "/app/out"
    else:
        sys.stderr.write("usage: transform.py [DATA_DIR] [OUT_DIR]\n")
        return 2
    run(data_dir, out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
