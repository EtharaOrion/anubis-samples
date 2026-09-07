#!/usr/bin/env python3
"""Reconstruct the fault ledger and its roll-ups from a crash-reporter corpus.

Usage:
    python3 transform.py [DATA_DIR] [OUT_DIR]

Defaults to reading /app/data and writing /app/out.

The crash reporter withholds nine wire parameters.  They are settled against
the published control figures of the shipped control slice before any figure
of the incident corpus is computed, then the whole derivation is re-run from
an empty output directory with the settled parameter set.

Standard library only.  All clock, byte and residency arithmetic is carried in
Python integers.
"""

import csv
import json
import os
import shutil
import sys

# ---------------------------------------------------------------------------
# Fixed fleet constants (stated by the shipped rules, not by the corpus).
# ---------------------------------------------------------------------------

TICKS_PER_BUILD = 900000000
CHECK_MOD = 65536
COUNT_BIAS_MOD = 9
SIG_ROT_MOD = 16
SIG_LEN = 16
SIG_SUM_MOD = 1000000007
HEX_DIGITS = set("0123456789abcdef")

# Dump flag word.
F_SPAN_DIFF = 0x001
F_ENTRY_CHAIN = 0x002
F_UNWIND_DRIFT = 0x004
F_ROTATE = 0x008
F_COUNT_BIAS = 0x010
F_ALT_RADIX = 0x020
F_ALIAS = 0x100
F_SPLIT_BASE = 0x800

# Frame flag word.
G_INDEX_IN_SYMBOL = 0x040
G_SIG_ROTATED = 0x080
G_ALLOC_COARSE = 0x200
G_SIG_RING = 0x400

REJECTION_KINDS = (
    "outside_support",
    "retired_build",
    "unprobed_frame",
    "unreported_dump",
    "unresolved_module",
)

# Candidate domain of every withheld wire parameter, as published with the
# control slice.
DOMAINS = {
    "count_multiplier": list(range(0, 9)),
    "rotation_offset": list(range(0, 8)),
    "entry_clock_modulus": [1 << e for e in range(22, 35)],
    "drift_period": [48, 96, 192, 384, 768],
    "coarse_unit": [64, 128, 256, 512, 1024],
    "signature_offset": list(range(0, 8)),
    "ring_width": [4, 8, 12, 16],
    "alternate_radix": list(range(11, 36)),
    "base_shift_width": [8, 10, 12, 16],
}

# The order in which the withheld parameters bite.  Each stage settles the
# parameters it names against one published control figure, using only the
# parameters settled by the stages before it.
STAGES = [
    ("CTRL-A", ["count_multiplier"]),
    ("CTRL-C", ["rotation_offset"]),
    ("CTRL-D", ["entry_clock_modulus"]),
    ("CTRL-E", ["drift_period"]),
    ("CTRL-F", ["coarse_unit"]),
    ("CTRL-G", ["signature_offset", "ring_width"]),
    ("CTRL-B", ["alternate_radix", "base_shift_width"]),
]

ALL_CONTROLS = [
    "CTRL-A", "CTRL-B", "CTRL-C", "CTRL-D", "CTRL-E", "CTRL-F",
    "CTRL-G", "CTRL-H", "CTRL-I", "CTRL-J", "CTRL-K",
]


class WireError(Exception):
    """A candidate parameter assignment contradicts the wire rules."""


# ---------------------------------------------------------------------------
# Reporter dump export
# ---------------------------------------------------------------------------

def parse_export(path):
    """Read a pipe-delimited reporter export into a list of dump records."""
    dumps = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.rstrip("\n").rstrip("\r")
            if not line or line.startswith("#"):
                continue
            cols = line.split("|")
            if cols[0] == "@":
                dumps.append({
                    "dump_id": cols[1],
                    "module_tag": cols[2],
                    "declared_count": int(cols[3]),
                    "base_field": cols[4],
                    "flags": int(cols[5], 16),
                    "key": int(cols[6]),
                    "check": int(cols[7], 16),
                    "lines": [],
                })
            else:
                if not dumps:
                    raise WireError("frame line before any dump header")
                dumps[-1]["lines"].append(cols)
    return dumps


def frame_check(cols):
    """The check total the reporter rules define over a frame's columns."""
    return sum(ord(ch) for ch in "|".join(cols[:8])) % CHECK_MOD


def rotate_right(text, count):
    count %= len(text)
    if not count:
        return text
    return text[-count:] + text[:-count]


def dump_frame_count(dump, ordinal, count_multiplier):
    """How many of a dump's lines are fault frames."""
    declared = dump["declared_count"]
    if dump["flags"] & F_COUNT_BIAS:
        if count_multiplier is None:
            raise WireError("count multiplier not settled")
        declared -= (ordinal * count_multiplier) % COUNT_BIAS_MOD
    return declared


def decode_export(dumps, params):
    """Recover every fault frame of an export under a parameter assignment.

    Parameters still standing at ``None`` simply leave the fields that depend
    on them out of the recovered records, so that a stage can compute the one
    control figure it settles against without the parameters it has not
    reached yet.
    """
    multiplier = params.get("count_multiplier")
    rotation = params.get("rotation_offset")
    modulus = params.get("entry_clock_modulus")
    period = params.get("drift_period")
    radix = params.get("alternate_radix")
    shift = params.get("base_shift_width")
    sig_offset = params.get("signature_offset")
    coarse = params.get("coarse_unit")
    ring_width = params.get("ring_width")

    frames = []
    bases = []
    for ordinal, dump in enumerate(dumps, start=1):
        flags = dump["flags"]
        count = dump_frame_count(dump, ordinal, multiplier)
        if count < 1 or count > len(dump["lines"]):
            raise WireError("declared fault frame count out of range")
        frame_lines = dump["lines"][:count]
        trailer_lines = dump["lines"][count:]

        # Check totals cover exactly the columns of the dump's fault frames.
        for cols in frame_lines:
            if len(cols) != 9 or frame_check(cols) != int(cols[8], 16):
                raise WireError("frame check total mismatch")
        if sum(int(c[8], 16) for c in frame_lines) % CHECK_MOD != dump["check"]:
            raise WireError("dump check total mismatch")
        if (flags & F_SPLIT_BASE) and not trailer_lines:
            raise WireError("split base dump carries no trailer line")

        # Dump base.
        base = None
        if (flags & F_ALT_RADIX) and radix is None:
            base = None
        elif (flags & F_SPLIT_BASE) and shift is None:
            base = None
        else:
            try:
                high = int(dump["base_field"], radix) if (flags & F_ALT_RADIX) \
                    else int(dump["base_field"])
            except ValueError:
                raise WireError("base field is not a numeral in the radix")
            if flags & F_SPLIT_BASE:
                low = int(trailer_lines[0][0])
                if low < 0 or low >= (1 << shift):
                    raise WireError("split base low part does not fit")
                base = (high << shift) | low
            else:
                base = high
            if base < 0:
                raise WireError("negative dump base")
        dump["base"] = base
        bases.append(base)

        # Logical order of the dump's fault frames.
        if flags & F_ROTATE:
            if rotation is None:
                order = list(range(count))
            else:
                turn = (dump["key"] + rotation) % count
                order = [(j - turn) % count for j in range(count)]
        else:
            order = list(range(count))
        dump["_ordered"] = bool(not (flags & F_ROTATE) or rotation is not None)

        records = []
        for logical, position in enumerate(order):
            cols = frame_lines[position]
            fflags = int(cols[6], 16)
            symbol_kind, _, tail = cols[7].rpartition(":")
            index = int(tail) if (fflags & G_INDEX_IN_SYMBOL) else int(cols[0])
            if fflags & G_ALLOC_COARSE:
                if coarse is None:
                    alloc = None
                else:
                    alloc = int(cols[4][1:]) * coarse
            else:
                alloc = int(cols[4])
            records.append({
                "source_dump": dump["dump_id"],
                "raw_module_tag": dump["module_tag"],
                "dump_flags": flags,
                "frame_flags": fflags,
                "dump_base": base,
                "frame_index": index,
                "symbol": symbol_kind,
                "bytes_allocated": alloc,
                "raw_span": int(cols[3]),
                "raw_entry": int(cols[1]),
                "raw_unwind": int(cols[2]),
                "raw_signature": cols[5],
                "logical": logical,
                "dump_ordinal": ordinal,
            })

        ordered = dump["_ordered"]

        # Resident spans.
        if flags & F_SPAN_DIFF:
            if ordered:
                spans = []
                for j, rec in enumerate(records):
                    value = rec["raw_span"]
                    if j == 0:
                        spans.append(value)
                    elif j == 1:
                        spans.append(spans[0] + value)
                    else:
                        spans.append(value + 2 * spans[j - 1] - spans[j - 2])
                for j, rec in enumerate(records):
                    rec["cost_ns"] = spans[j]
            else:
                for rec in records:
                    rec["cost_ns"] = None
        else:
            for rec in records:
                rec["cost_ns"] = rec["raw_span"]
        if any(r["cost_ns"] is not None and r["cost_ns"] < 0 for r in records):
            raise WireError("negative resident span")

        # Entry stamps.
        if flags & F_ENTRY_CHAIN:
            if ordered and modulus is not None:
                previous = None
                for j, rec in enumerate(records):
                    if j == 0:
                        rec["enter_ns"] = rec["raw_entry"]
                    else:
                        rec["enter_ns"] = (rec["raw_entry"] - previous) % modulus
                    previous = rec["enter_ns"]
            else:
                for rec in records:
                    rec["enter_ns"] = None
        else:
            for rec in records:
                rec["enter_ns"] = rec["raw_entry"]

        # Capture ticks.
        if records[0]["enter_ns"] is not None and modulus is not None \
                and base is not None:
            first = records[0]["enter_ns"]
            for rec in records:
                rec["capture_tick"] = base + ((rec["enter_ns"] - first) % modulus)
        else:
            for rec in records:
                rec["capture_tick"] = None

        frames.extend(records)

    # The running resident span total and the signature ring both advance over
    # the export as a whole, over its fault frames in logical order.
    sequence = sorted(frames, key=lambda r: (r["dump_ordinal"], r["logical"]))
    ring = [None] * ring_width if ring_width else None
    write_at = 0
    running = 0
    running_known = all(r["cost_ns"] is not None for r in frames)
    for rec in sequence:
        fflags = rec["frame_flags"]
        signature = None
        if fflags & G_SIG_RING:
            if ring is not None:
                distance = int(rec["raw_signature"][1:])
                if distance < 1:
                    raise WireError("ring distance is not positive")
                signature = ring[(write_at - distance) % ring_width]
                if signature is None:
                    raise WireError("ring slot never written")
        elif fflags & G_SIG_ROTATED:
            if sig_offset is not None:
                signature = rotate_right(
                    rec["raw_signature"],
                    (rec["frame_index"] + sig_offset) % SIG_ROT_MOD)
        else:
            signature = rec["raw_signature"]
        if signature is not None:
            if len(signature) != SIG_LEN or set(signature) - HEX_DIGITS:
                raise WireError("recovered signature is not sixteen hex digits")
            if ring is not None:
                ring[write_at % ring_width] = signature
                write_at += 1
        elif ring is not None:
            raise WireError("signature unrecoverable while a ring is running")
        rec["fault_signature"] = signature

        if rec["dump_flags"] & F_UNWIND_DRIFT:
            if running_known and period is not None:
                rec["unwind_ns"] = rec["raw_unwind"] - (running % period)
            else:
                rec["unwind_ns"] = None
        else:
            rec["unwind_ns"] = rec["raw_unwind"]
        if running_known:
            running += rec["cost_ns"]

    for rec in frames:
        if rec["enter_ns"] is not None and rec["unwind_ns"] is not None:
            if rec["unwind_ns"] > rec["enter_ns"]:
                raise WireError("unwind stamp later than entry stamp")

    return frames, bases


# ---------------------------------------------------------------------------
# Reference tables
# ---------------------------------------------------------------------------

def read_csv(path):
    with open(path, "r", encoding="utf-8", newline="") as handle:
        return [row for row in csv.DictReader(handle) if any(row.values())]


def read_jsonl(path):
    rows = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


class References(object):
    """The five tables that govern every corpus alike."""

    def __init__(self, data_dir):
        self.registry = {}
        for row in read_csv(os.path.join(data_dir, "build_registry.csv")):
            self.registry.setdefault(row["module_tag"], []).append({
                "canonical_tag": row["canonical_tag"],
                "component_key": row["component_key"],
                "tier_index": int(row["tier_index"]),
                "from_build": int(row["effective_from_build"]),
                "to_build": int(row["effective_to_build"]),
                "status": row["status"],
            })

        self.ladder = {}
        for row in read_csv(os.path.join(data_dir, "severity_ladder.csv")):
            self.ladder.setdefault(row["component_key"], {})[
                int(row["tier_index"])] = int(row["budget_ns"])

        self.windows = {}
        for row in read_csv(os.path.join(data_dir, "support_windows.csv")):
            self.windows[row["component_key"]] = (
                int(row["open_tick"]), int(row["close_tick"]))

        self.host_zones = {}
        for row in read_csv(os.path.join(data_dir, "host_zones.csv")):
            self.host_zones[row["host_id"]] = row["zone"]

        self.tenant_segments = {}
        for row in read_csv(os.path.join(data_dir, "tenant_classes.csv")):
            self.tenant_segments[row["tenant_class"]] = row["tenant_segment"]

        self.zones = sorted(set(self.host_zones.values()))
        self.segments = sorted(set(self.tenant_segments.values()))

    def row_for(self, tag, build):
        for row in self.registry.get(tag, ()):
            if row["from_build"] <= build <= row["to_build"]:
                return row
        return None

    def resolve(self, tag, build):
        """Follow the registry chain to a terminal row, or return None."""
        seen = set()
        current = tag
        while True:
            if current in seen:
                return None
            seen.add(current)
            row = self.row_for(current, build)
            if row is None:
                return None
            if row["canonical_tag"] == current:
                return row
            current = row["canonical_tag"]

    def tier_count(self, component):
        return len(self.ladder.get(component, {}))

    def budget(self, component, tier):
        return self.ladder.get(component, {}).get(tier)


# ---------------------------------------------------------------------------
# Ledger derivation
# ---------------------------------------------------------------------------

def build_ledger(frames, probe, uplink, refs):
    """Classify every recovered fault frame and settle the quarantine."""
    probe_by_key = {(r["module_tag"], int(r["frame_index"])): r for r in probe}
    uplink_by_key = {(r["module_tag"], int(r["frame_index"])): r for r in uplink}

    rejections = dict((kind, 0) for kind in REJECTION_KINDS)
    rejected_frames = 0
    accepted = []

    for rec in frames:
        build = rec["capture_tick"] // TICKS_PER_BUILD
        row = refs.resolve(rec["raw_module_tag"], build)
        if row is None:
            rejections["unresolved_module"] += 1
            rejected_frames += 1
            continue
        if row["status"] != "supported":
            rejections["retired_build"] += 1
            rejected_frames += 1
            continue

        module = row["canonical_tag"]
        component = row["component_key"]
        key = (module, rec["frame_index"])
        broken = 0

        open_tick, close_tick = refs.windows[component]
        if not (open_tick < rec["capture_tick"] < close_tick):
            rejections["outside_support"] += 1
            broken += 1
        probe_row = probe_by_key.get(key)
        if probe_row is None:
            rejections["unprobed_frame"] += 1
            broken += 1
        uplink_row = uplink_by_key.get(key)
        if uplink_row is None:
            rejections["unreported_dump"] += 1
            broken += 1

        if broken:
            rejected_frames += 1
            continue

        host = uplink_row["host_id"]
        tenant = uplink_row["tenant_class"]
        accepted.append({
            "bytes_allocated": rec["bytes_allocated"],
            "bytes_uploaded": int(uplink_row["bytes_uploaded"]),
            "capture_tick": rec["capture_tick"],
            "component_key": component,
            "cost_ns": rec["cost_ns"],
            "declared_tier": row["tier_index"],
            "enter_ns": rec["enter_ns"],
            "fault_signature": rec["fault_signature"],
            "frame_index": rec["frame_index"],
            "host_zone": refs.host_zones[host],
            "module_tag": module,
            "peak_ns": int(probe_row["peak_ns"]),
            "samples": int(probe_row["samples"]),
            "source_dump": rec["source_dump"],
            "symbol": rec["symbol"],
            "tenant_segment": refs.tenant_segments[tenant],
            "unwind_ns": rec["unwind_ns"],
        })

    accepted.sort(key=lambda r: r["capture_tick"])
    return accepted, rejections, rejected_frames


def settle_quarantine(accepted, refs):
    """Demote effective tiers in passes until no module is left to demote."""
    modules = {}
    for row in accepted:
        entry = modules.setdefault(row["module_tag"], {
            "component_key": row["component_key"],
            "declared_tier": row["declared_tier"],
            "frames": [],
        })
        entry["declared_tier"] = max(entry["declared_tier"], row["declared_tier"])
        entry["frames"].append(row)

    tiers = dict((tag, entry["declared_tier"]) for tag, entry in modules.items())

    def allowances(state):
        floors = {}
        for tag, entry in modules.items():
            component = entry["component_key"]
            budget = refs.budget(component, state[tag])
            current = floors.get(component)
            if current is None or budget < current:
                floors[component] = budget
        result = {}
        for tag, entry in modules.items():
            component = entry["component_key"]
            envelope = floors[component] * refs.tier_count(component)
            budget = refs.budget(component, state[tag])
            result[tag] = min(budget, envelope)
        return result

    passes = 0
    while True:
        passes += 1
        allowance = allowances(tiers)
        demote = []
        for tag, entry in modules.items():
            within = sum(1 for f in entry["frames"]
                         if f["peak_ns"] <= allowance[tag])
            over = len(entry["frames"]) - within
            if tiers[tag] > 0 and over > within:
                demote.append(tag)
        if not demote:
            break
        for tag in demote:
            tiers[tag] -= 1

    allowance = allowances(tiers)
    quarantined = []
    module_rows = []
    for tag in sorted(modules):
        entry = modules[tag]
        within = sum(1 for f in entry["frames"]
                     if f["peak_ns"] <= allowance[tag])
        over = len(entry["frames"]) - within
        is_quarantined = tiers[tag] == 0 and over > within
        if is_quarantined:
            quarantined.append(tag)
        module_rows.append({
            "accepted_count": len(entry["frames"]),
            "allowance_ns": allowance[tag],
            "budget_ns": refs.budget(entry["component_key"], tiers[tag]),
            "component_key": entry["component_key"],
            "declared_tier": entry["declared_tier"],
            "effective_tier": tiers[tag],
            "module_tag": tag,
            "net_samples": sum(f["samples"] for f in entry["frames"]),
            "quarantined": is_quarantined,
            "total_bytes_uploaded": sum(f["bytes_uploaded"] for f in entry["frames"]),
            "within_budget_count": within,
        })

    for row in accepted:
        tag = row["module_tag"]
        row["effective_tier"] = tiers[tag]
        row["within_budget"] = row["peak_ns"] <= allowance[tag]
        del row["declared_tier"]

    return {
        "passes": passes,
        "quarantined_modules": quarantined,
        "module_rows": module_rows,
    }


def derive(corpus_prefix, data_dir, refs, params):
    """Run the whole derivation for one corpus tier."""
    dumps = parse_export(
        os.path.join(data_dir, corpus_prefix + "fault_frames.psv"))
    frames, bases = decode_export(dumps, params)
    probe = read_csv(os.path.join(data_dir, corpus_prefix + "runtime_probe.csv"))
    uplink = read_jsonl(
        os.path.join(data_dir, corpus_prefix + "uplink_reports.jsonl"))
    accepted, rejections, rejected_frames = build_ledger(
        frames, probe, uplink, refs)
    settlement = settle_quarantine(accepted, refs)
    allocated = sum(r["bytes_allocated"] for r in accepted)
    uploaded = sum(r["bytes_uploaded"] for r in accepted)
    return {
        "frames": frames,
        "bases": bases,
        "accepted": accepted,
        "rejections": rejections,
        "rejected_frames": rejected_frames,
        "settlement": settlement,
        "allocated_bytes": allocated,
        "uploaded_bytes": uploaded,
        "residual_bytes": allocated - uploaded,
    }


# ---------------------------------------------------------------------------
# Control figures
# ---------------------------------------------------------------------------

def wire_control(control, frames, bases):
    """Reproduce one wire-level control figure, or None if unrecoverable."""
    if control == "CTRL-A":
        return len(frames)
    if control == "CTRL-B":
        if any(b is None for b in bases):
            return None
        return sum(bases)
    if control == "CTRL-C":
        values = [r["cost_ns"] for r in frames]
    elif control == "CTRL-D":
        values = [r["enter_ns"] for r in frames]
    elif control == "CTRL-E":
        values = [r["unwind_ns"] for r in frames]
    elif control == "CTRL-F":
        values = [r["bytes_allocated"] for r in frames]
    elif control == "CTRL-H":
        values = [r["frame_index"] for r in frames]
    elif control == "CTRL-G":
        sigs = [r["fault_signature"] for r in frames]
        if any(s is None for s in sigs):
            return None
        return sum(int(s, 16) for s in sigs) % SIG_SUM_MOD
    else:
        return None
    if any(v is None for v in values):
        return None
    return sum(values)


def reproduce_controls(data_dir, refs, params):
    """Every control figure the control slice publishes, reproduced."""
    result = derive("calibration_", data_dir, refs, params)
    figures = {}
    for control in ("CTRL-A", "CTRL-B", "CTRL-C", "CTRL-D", "CTRL-E",
                    "CTRL-F", "CTRL-G", "CTRL-H"):
        figures[control] = wire_control(control, result["frames"],
                                        result["bases"])
    figures["CTRL-I"] = len(result["accepted"])
    figures["CTRL-J"] = len(result["settlement"]["quarantined_modules"])
    figures["CTRL-K"] = result["residual_bytes"]
    return figures


# ---------------------------------------------------------------------------
# Settling the withheld wire parameters
# ---------------------------------------------------------------------------

def product(domains):
    combos = [()]
    for values in domains:
        combos = [combo + (value,) for combo in combos for value in values]
    return combos


def settle_parameters(data_dir, published):
    """Settle all nine withheld parameters against the published controls."""
    dumps = parse_export(
        os.path.join(data_dir, "calibration_fault_frames.psv"))
    settled = dict((name, None) for name in DOMAINS)
    report = []
    evaluations = 0

    for control, names in STAGES:
        target = published[control]
        combos = product([DOMAINS[name] for name in names])
        survivors = []
        for combo in combos:
            evaluations += 1
            trial = dict(settled)
            for name, value in zip(names, combo):
                trial[name] = value
            try:
                frames, bases = decode_export(dumps, trial)
            except WireError:
                continue
            except (ValueError, IndexError, ZeroDivisionError):
                continue
            if wire_control(control, frames, bases) == target:
                survivors.append(combo)
        if not survivors:
            raise SystemExit(
                "no candidate assignment reproduces %s" % control)
        for position, name in enumerate(names):
            kept = sorted(set(combo[position] for combo in survivors))
            if len(kept) != 1:
                raise SystemExit(
                    "%s does not settle %s uniquely" % (control, name))
            selected = kept[0]
            settled[name] = selected
            report.append({
                "name": name,
                "candidates": len(DOMAINS[name]),
                "control": control,
                "selected": selected,
                "rejected": [{"value": value, "control": control}
                             for value in DOMAINS[name] if value != selected],
            })

    return settled, report, evaluations


# ---------------------------------------------------------------------------
# Roll-ups
# ---------------------------------------------------------------------------

def bucket_totals(rows, key):
    totals = {}
    for row in rows:
        totals[row[key]] = totals.get(row[key], 0) + row["bytes_uploaded"]
    return totals


def slice_report(rows, axis_key, axis_value, cross_key, cross_name):
    report = {
        "accepted_count": len(rows),
        "by_component": bucket_totals(rows, "component_key"),
        cross_name: bucket_totals(rows, cross_key),
        "modules": sorted(set(r["module_tag"] for r in rows)),
        "total_bytes_uploaded": sum(r["bytes_uploaded"] for r in rows),
        "total_cost_ns": sum(r["cost_ns"] for r in rows),
        "within_budget_count": sum(1 for r in rows if r["within_budget"]),
    }
    report[axis_key] = axis_value
    return report


def build_reports(result, refs):
    accepted = result["accepted"]
    reports = {}

    for zone in refs.zones:
        rows = [r for r in accepted if r["host_zone"] == zone]
        reports["region_%s.json" % zone] = slice_report(
            rows, "zone", zone, "tenant_segment", "by_tenant_segment")

    for segment in refs.segments:
        rows = [r for r in accepted if r["tenant_segment"] == segment]
        reports["segment_%s.json" % segment] = slice_report(
            rows, "tenant_segment", segment, "host_zone", "by_zone")

    by_tier = {}
    for row in accepted:
        key = str(row["effective_tier"])
        by_tier[key] = by_tier.get(key, 0) + 1

    reports["summary.json"] = {
        "accepted_count": len(accepted),
        "by_component": bucket_totals(accepted, "component_key"),
        "by_effective_tier": by_tier,
        "by_tenant_segment": bucket_totals(accepted, "tenant_segment"),
        "by_zone": bucket_totals(accepted, "host_zone"),
        "rejected_count": result["rejected_frames"],
        "total_bytes_allocated": result["allocated_bytes"],
        "total_bytes_uploaded": result["uploaded_bytes"],
        "total_cost_ns": sum(r["cost_ns"] for r in accepted),
        "within_budget_count": sum(1 for r in accepted if r["within_budget"]),
    }

    reports["product_summary.json"] = {
        "modules": result["settlement"]["module_rows"],
    }
    return reports


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

OUTPUT_NAMES = ["normalized.jsonl", "summary.json", "product_summary.json",
                "data_quality.json"]


def write_json(path, payload):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def write_ledger(path, rows):
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True,
                                    separators=(",", ":")))
            handle.write("\n")


def clear_output(out_dir, refs):
    """Start the final derivation from an empty output directory."""
    names = list(OUTPUT_NAMES)
    names += ["region_%s.json" % z for z in refs.zones]
    names += ["segment_%s.json" % s for s in refs.segments]
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)
        return
    for name in names:
        path = os.path.join(out_dir, name)
        if os.path.isfile(path):
            os.remove(path)
        elif os.path.isdir(path):
            shutil.rmtree(path)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv):
    data_dir = argv[1] if len(argv) > 1 else "/app/data"
    out_dir = argv[2] if len(argv) > 2 else "/app/out"

    refs = References(data_dir)
    published = json.load(
        open(os.path.join(data_dir, "control-slice-figures.json"),
             "r", encoding="utf-8"))

    # Rule one: settle the withheld wire parameters against the published
    # control figures before computing any figure for the incident corpus.
    params, search_report, evaluations = settle_parameters(data_dir, published)
    reproduced = reproduce_controls(data_dir, refs, params)
    mismatched = [c for c in ALL_CONTROLS if reproduced.get(c) != published[c]]
    if mismatched:
        raise SystemExit("settled parameters miss control figures: %s"
                         % ", ".join(mismatched))

    # Rule four: with the parameter set settled, re-run the whole derivation
    # from an empty output directory.
    clear_output(out_dir, refs)
    result = derive("", data_dir, refs, params)
    reports = build_reports(result, refs)

    reports["data_quality.json"] = {
        "accepted_count": len(result["accepted"]),
        "calibration_comparison": [
            {"control": control,
             "published": published[control],
             "reproduced": reproduced[control]}
            for control in ALL_CONTROLS
        ],
        "candidate_search": {
            "evaluations": evaluations,
            "parameters": search_report,
        },
        "frame_count": len(result["frames"]),
        "reconciliation": {
            "allocated_bytes": result["allocated_bytes"],
            "residual_bytes": result["residual_bytes"],
            "uploaded_bytes": result["uploaded_bytes"],
        },
        "rejections": result["rejections"],
        "settlement": {
            "passes": result["settlement"]["passes"],
            "quarantined_modules": result["settlement"]["quarantined_modules"],
        },
    }

    write_ledger(os.path.join(out_dir, "normalized.jsonl"), result["accepted"])
    for name, payload in sorted(reports.items()):
        write_json(os.path.join(out_dir, name), payload)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
