#!/usr/bin/env python3
"""Fleet crash-reporter triage transform.

Usage:
    python3 transform.py [DATA_DIR] [OUT_DIR]

Reads a corpus directory (the reporter dump export, the runtime probe export,
the uplink export, the five reference tables and, when present, the control
slice and its published control figures), settles the crash reporter's
withheld wire parameters against the control figures, and writes the whole
triage set under the output directory.

Called with no arguments it reads /app/data and writes /app/out.
Only the Python standard library is used, and nothing outside OUT_DIR is
written.
"""

import json
import os
import sys

# ---------------------------------------------------------------------------
# Wire constants that the reporter's notes fix outright.
# ---------------------------------------------------------------------------

TICKS_PER_BUILD_INDEX = 900000000          # 1000 ticks/ms over a build index
CHECK_MODULUS = 65536
SIGNATURE_WIDTH = 16
COUNT_BIAS_MODULUS = 9
SIGNATURE_SUM_MODULUS = 1000000007

# Dump flag word.
F_SPAN_SECOND_DIFFERENCE = 0x001
F_ENTRY_CHAINED = 0x002
F_UNWIND_DRIFTED = 0x004
F_LINES_ROTATED = 0x008
F_COUNT_BIASED = 0x010
F_BASE_ALT_RADIX = 0x020
F_MODULE_ALIASED = 0x100
F_BASE_SPLIT = 0x800

# Frame flag word.
F_INDEX_FROM_SYMBOL = 0x040
F_SIGNATURE_ROTATED = 0x080
F_ALLOCATION_COARSE = 0x200
F_SIGNATURE_FROM_RING = 0x400

ALT_DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"

# Rejection reasons, in the order the ledger closure lists the acceptance
# conditions.
REASONS = (
    "unresolved_module",
    "retired_build",
    "outside_support",
    "unprobed_frame",
    "unreported_dump",
)

# The candidate domain the control slice publishes for each withheld wire
# parameter.  Each is settled against its own published control figure; the
# radix/shift and offset/ring-width pairs are settled jointly, a single
# control figure covering both, and are searched together in one stage.
PARAMETER_DOMAINS = {
    "entry_clock_modulus": [1 << p for p in range(22, 35)],
    "drift_period": [48, 96, 192, 384, 768],
    "rotation_offset": list(range(0, 8)),
    "count_multiplier": list(range(0, 9)),
    "alternate_radix": list(range(11, 36)),
    "signature_offset": list(range(0, 8)),
    "coarse_unit": [64, 128, 256, 512, 1024],
    "ring_width": [4, 8, 12, 16],
    "base_shift_width": [8, 10, 12, 16],
}

# Used only when a corpus ships without a control slice; the sample tier is
# written with every flag word clear, so no wire rule beyond the plain column
# layout is in force there and any assignment decodes it identically.
UNCONSTRAINED_PARAMETERS = {
    "entry_clock_modulus": 1 << 32,
    "drift_period": 768,
    "rotation_offset": 0,
    "count_multiplier": 0,
    "alternate_radix": 36,
    "signature_offset": 0,
    "coarse_unit": 1024,
    "ring_width": 16,
    "base_shift_width": 16,
}


# ---------------------------------------------------------------------------
# Small helpers.
# ---------------------------------------------------------------------------

def parse_alt_radix(text, radix):
    """Read a numeral written in the alternate radix (0-9 then a-z)."""
    value = 0
    for ch in text:
        digit = ALT_DIGITS.index(ch)
        if digit < 0 or digit >= radix:
            raise ValueError("digit %r out of radix %d" % (ch, radix))
        value = value * radix + digit
    return value


def frame_check_total(cols):
    """The check total a frame line carries over its first eight columns."""
    text = "|".join(cols[:8])
    return sum(ord(ch) for ch in text) % CHECK_MODULUS


def read_csv_rows(path):
    """Read a headed CSV table into a list of dicts."""
    with open(path, encoding="utf-8") as handle:
        lines = [ln.rstrip("\n") for ln in handle if ln.strip()]
    header = lines[0].split(",")
    return [dict(zip(header, ln.split(","))) for ln in lines[1:]]


# ---------------------------------------------------------------------------
# Reporter dump export.
# ---------------------------------------------------------------------------

class Dump(object):
    """One captured dump: a header, its fault frames and its trailer lines."""

    def __init__(self, cols, ordinal):
        self.dump_id = cols[1]
        self.module_column = cols[2]
        self.declared_count = int(cols[3])
        self.base_field = cols[4]
        self.flags = int(cols[5], 16)
        self.key = int(cols[6])
        self.check_total = int(cols[7], 16)
        self.ordinal = ordinal
        self.lines = []


def read_dump_export(path):
    """Split a pipe-delimited reporter export into dumps of raw lines."""
    dumps = []
    with open(path, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            cols = line.split("|")
            if cols[0] == "@":
                dumps.append(Dump(cols, len(dumps) + 1))
            else:
                if not dumps:
                    raise ValueError("frame line before any dump header")
                dumps[-1].lines.append(cols)
    return dumps


def declared_count(dump, params):
    """The fault frame count a dump's header declares, unbiased."""
    if dump.flags & F_COUNT_BIASED:
        bias = (dump.ordinal * params["count_multiplier"]) % COUNT_BIAS_MODULUS
        return dump.declared_count - bias
    return dump.declared_count


def split_and_order(dump, params):
    """Split a dump's lines into fault frames and trailers, in logical order."""
    count = declared_count(dump, params)
    if count < 1 or count > len(dump.lines):
        raise ValueError("dump %s declares an unusable frame count" % dump.dump_id)

    written = dump.lines[:count]
    dump.trailers = dump.lines[count:]
    dump.frame_count = count

    # The dump's fault frame lines are written rotated to the left by the dump
    # key added to the rotation offset; the logical order is what the rotation
    # was applied to.
    if dump.flags & F_LINES_ROTATED:
        k = (dump.key + params["rotation_offset"]) % count
        dump.logical = [written[(j - k) % count] for j in range(count)]
    else:
        dump.logical = list(written)
    dump.written = written


def dump_base(dump, params):
    """The capture base a dump's header and first trailer line carry."""
    if dump.flags & F_BASE_ALT_RADIX:
        high = parse_alt_radix(dump.base_field, params["alternate_radix"])
    else:
        high = int(dump.base_field, 10)
    if dump.flags & F_BASE_SPLIT:
        if not dump.trailers:
            raise ValueError("dump %s splits its base but holds no trailer" % dump.dump_id)
        high = (high << params["base_shift_width"]) | int(dump.trailers[0][0])
    return high


def recover_dump_export(path, params):
    """Read a dump export and recover every dump's structure and base."""
    dumps = read_dump_export(path)
    for dump in dumps:
        split_and_order(dump, params)
        dump.base = dump_base(dump, params)
    return dumps


def recover_resident_spans(dump):
    """Invert the second-order differences a dump writes over its spans.

    The differencing starts at the dump's first fault frame, so the first
    column carries the span itself, the second carries a first difference and
    every later one a full second difference.
    """
    spans = []
    for i, cols in enumerate(dump.logical):
        written = int(cols[3])
        if not dump.flags & F_SPAN_SECOND_DIFFERENCE:
            spans.append(written)
        elif i == 0:
            spans.append(written)
        elif i == 1:
            spans.append(written + spans[0])
        else:
            spans.append(written + 2 * spans[i - 1] - spans[i - 2])
    return spans


def recover_entry_stamps(dump, modulus):
    """Invert the entry chaining a dump writes over its fault frames."""
    entries = []
    previous = None
    for i, cols in enumerate(dump.logical):
        written = int(cols[1])
        if (dump.flags & F_ENTRY_CHAINED) and i > 0:
            stamp = (written - previous) % modulus
        else:
            stamp = written
        entries.append(stamp)
        previous = stamp
    return entries


def recover_frames(path, params):
    """Decode a reporter dump export into fault frames, in logical order.

    The running resident span total and the fault signature ring advance over
    the export as a whole and over every fault frame it carries, whatever
    flags a frame's own dump sets.
    """
    dumps = recover_dump_export(path, params)

    modulus = params["entry_clock_modulus"]
    drift = params["drift_period"]
    ring_width = params["ring_width"]

    ring = [None] * ring_width
    ring_position = 0
    running_span_total = 0

    frames = []
    for dump in dumps:
        spans = recover_resident_spans(dump)
        entries = recover_entry_stamps(dump, modulus)
        first_entry = entries[0] if entries else 0

        for i, cols in enumerate(dump.logical):
            frame_flags = int(cols[6], 16)
            if frame_flags & F_INDEX_FROM_SYMBOL:
                frame_index = int(cols[7].rsplit(":", 1)[1])
            else:
                frame_index = int(cols[0])

            unwind_written = int(cols[2])
            if dump.flags & F_UNWIND_DRIFTED:
                unwind = unwind_written - (running_span_total % drift)
            else:
                unwind = unwind_written

            allocation = cols[4]
            if frame_flags & F_ALLOCATION_COARSE:
                allocated = int(allocation[1:]) * params["coarse_unit"]
            else:
                allocated = int(allocation)

            signature_column = cols[5]
            if frame_flags & F_SIGNATURE_FROM_RING:
                distance = int(signature_column[1:])
                signature = ring[(ring_position - distance) % ring_width]
                if signature is None:
                    raise ValueError(
                        "dump %s frame %d reaches an unwritten ring slot"
                        % (dump.dump_id, frame_index)
                    )
            elif frame_flags & F_SIGNATURE_ROTATED:
                turn = (frame_index + params["signature_offset"]) % SIGNATURE_WIDTH
                signature = (
                    signature_column[SIGNATURE_WIDTH - turn:]
                    + signature_column[:SIGNATURE_WIDTH - turn]
                )
            else:
                signature = signature_column

            frames.append({
                "source_dump": dump.dump_id,
                "module_column": dump.module_column,
                "ordinal": dump.ordinal,
                "frame_index": frame_index,
                "symbol": cols[7].rsplit(":", 1)[0],
                "entry": entries[i],
                "unwind": unwind,
                "span": spans[i],
                "allocated": allocated,
                "signature": signature,
                "capture_tick": dump.base + ((entries[i] - first_entry) % modulus),
            })

            ring[ring_position] = signature
            ring_position = (ring_position + 1) % ring_width
            running_span_total += spans[i]

    return dumps, frames


# ---------------------------------------------------------------------------
# Calibration against the published control figures.
# ---------------------------------------------------------------------------

WIRE_FIGURES = ("frames", "bases", "spans", "entry", "unwind",
                 "allocated", "signatures", "indices")

WIRE_CONTROL_OF = {
    "CTRL-A": "frames",
    "CTRL-B": "bases",
    "CTRL-C": "spans",
    "CTRL-D": "entry",
    "CTRL-E": "unwind",
    "CTRL-F": "allocated",
    "CTRL-G": "signatures",
    "CTRL-H": "indices",
}


def decode_wire(path, params):
    """Recompute the eight wire-level control figures under one assignment.

    Each figure is settled on its own, so a candidate that cannot decode one
    column fails to reproduce only the figure that covers it and leaves the
    others standing.  A figure that cannot be decoded at all comes back None
    and never matches a published total.
    """
    dumps = read_dump_export(path)
    totals = dict((figure, 0) for figure in WIRE_FIGURES)

    # Dump structure first: the frame count decides which lines are frames.
    for dump in dumps:
        try:
            split_and_order(dump, params)
        except (ValueError, ZeroDivisionError):
            return dict((figure, None) for figure in WIRE_FIGURES)
    totals["frames"] = sum(dump.frame_count for dump in dumps)

    bases = 0
    for dump in dumps:
        try:
            bases += dump_base(dump, params)
        except (ValueError, IndexError):
            totals["bases"] = None
    if totals["bases"] is not None:
        totals["bases"] = bases

    ring = [None] * params["ring_width"]
    position = 0
    running = 0
    modulus = params["entry_clock_modulus"]
    drift = params["drift_period"]

    for dump in dumps:
        try:
            spans = recover_resident_spans(dump)
            entries = recover_entry_stamps(dump, modulus)
        except (ValueError, ZeroDivisionError):
            spans = entries = None
            totals["spans"] = None
        if spans is not None:
            totals["spans"] += sum(spans)
        else:
            totals["unwind"] = None
        if entries is not None:
            totals["entry"] += sum(entries)
        else:
            totals["entry"] = None

        for i, cols in enumerate(dump.logical):
            frame_flags = int(cols[6], 16)

            if frame_flags & F_INDEX_FROM_SYMBOL:
                try:
                    frame_index = int(cols[7].rsplit(":", 1)[1])
                except (ValueError, IndexError):
                    frame_index = None
                    totals["indices"] = None
            else:
                try:
                    frame_index = int(cols[0])
                except ValueError:
                    frame_index = None
                    totals["indices"] = None
            if frame_index is not None and totals["indices"] is not None:
                totals["indices"] += frame_index

            if dump.flags & F_UNWIND_DRIFTED and totals["unwind"] is not None:
                try:
                    totals["unwind"] += int(cols[2]) - (running % drift)
                except ValueError:
                    totals["unwind"] = None
            elif totals["unwind"] is not None:
                try:
                    totals["unwind"] += int(cols[2])
                except ValueError:
                    totals["unwind"] = None

            if totals["allocated"] is not None:
                try:
                    allocation = cols[4]
                    totals["allocated"] += (
                        int(allocation[1:]) * params["coarse_unit"]
                        if frame_flags & F_ALLOCATION_COARSE
                        else int(allocation)
                    )
                except ValueError:
                    totals["allocated"] = None

            signature = None
            if totals["signatures"] is not None:
                try:
                    column = cols[5]
                    if frame_flags & F_SIGNATURE_FROM_RING:
                        signature = ring[
                            (position - int(column[1:])) % params["ring_width"]
                        ]
                        if signature is None:
                            raise ValueError
                    elif frame_flags & F_SIGNATURE_ROTATED:
                        if frame_index is None:
                            raise ValueError
                        turn = (
                            frame_index + params["signature_offset"]
                        ) % SIGNATURE_WIDTH
                        signature = (
                            column[SIGNATURE_WIDTH - turn:]
                            + column[:SIGNATURE_WIDTH - turn]
                        )
                    else:
                        signature = column
                    totals["signatures"] = (
                        totals["signatures"] + int(signature, 16)
                    ) % SIGNATURE_SUM_MODULUS
                except (ValueError, TypeError, IndexError):
                    totals["signatures"] = None

            # The signature ring and the running resident span total advance
            # over every frame the export carries, decoded or not.
            ring[position] = signature
            position = (position + 1) % params["ring_width"]
            if spans is not None:
                running += spans[i]

    return totals


def calibrate(data_dir, controls):
    """Settle the withheld wire parameters against the published figures.

    Each control figure is checked against every candidate value of the
    parameters its column depends on, with every parameter the earlier figures
    already settled held fixed.  A control that cannot separate two candidates
    of a parameter leaves both standing for the figures after it to settle; a
    parameter no figure settles is an error.  Every candidate value gets its
    verdict recorded against the control that eliminated it.
    """
    export_path = os.path.join(data_dir, "calibration_fault_frames.psv")
    live = dict((name, list(domain)) for name, domain in PARAMETER_DOMAINS.items())
    eliminated_by = {}
    settled_order = []
    settled = []

    for control, covered in CALIBRATION_STAGES:
        if control not in controls:
            continue
        figure = WIRE_CONTROL_OF[control]
        swept = []
        for name in covered + FIGURE_DEPENDENCIES[figure]:
            if name not in swept:
                swept.append(name)

        winners = []
        for combo in _cartesian(live, swept):
            params = _params_from(live, dict(zip(swept, combo)))
            try:
                totals = decode_wire(export_path, params)
            except (ValueError, KeyError, IndexError, ZeroDivisionError):
                continue
            if totals[figure] == controls[control]:
                winners.append(combo)
        if not winners:
            raise ValueError("no candidate for %s reproduces %s" % (swept, control))

        for i, name in enumerate(swept):
            survivors = [value for value in live[name]
                         if any(combo[i] == value for combo in winners)]
            if not survivors:
                raise ValueError("control %s leaves no candidate for %s" % (control, name))
            for value in live[name]:
                if value not in survivors:
                    eliminated_by[(name, value)] = control
            live[name] = survivors
            if len(survivors) == 1 and name not in settled_order:
                settled_order.append(name)

    for name in settled_order:
        selected = live[name][0]
        settled.append({
            "name": name,
            "candidates": len(PARAMETER_DOMAINS[name]),
            "control": eliminated_by.get((name, selected), CALIBRATION_CONTROL_OF[name]),
            "selected": selected,
            "rejected": [
                {"value": value,
                 "control": eliminated_by.get((name, value),
                                              CALIBRATION_CONTROL_OF[name])}
                for value in PARAMETER_DOMAINS[name] if value != selected
            ],
        })

    evaluations = sum(len(domain) for domain in PARAMETER_DOMAINS.values())
    return _params_from(live, {}), settled, evaluations


# The order the control figures are worked in, and the parameters each one
# covers in the record.  The figures are worked from the frame structure
# outward: counts first, then bases, spans, entry stamps, unwinds,
# allocations and signatures.
CALIBRATION_STAGES = (
    ("CTRL-A", ("count_multiplier",)),
    ("CTRL-B", ("alternate_radix", "base_shift_width")),
    ("CTRL-C", ("rotation_offset",)),
    ("CTRL-D", ("entry_clock_modulus",)),
    ("CTRL-E", ("drift_period",)),
    ("CTRL-F", ("coarse_unit",)),
    ("CTRL-G", ("signature_offset", "ring_width")),
)

# The control figure that covers each parameter, for the record's verdicts.
CALIBRATION_CONTROL_OF = {
    "count_multiplier": "CTRL-A",
    "alternate_radix": "CTRL-B",
    "base_shift_width": "CTRL-B",
    "rotation_offset": "CTRL-C",
    "entry_clock_modulus": "CTRL-D",
    "drift_period": "CTRL-E",
    "coarse_unit": "CTRL-F",
    "signature_offset": "CTRL-G",
    "ring_width": "CTRL-G",
}

# Which wire parameters each control figure's columns depend on.  Every figure
# depends on the frame count, since the count decides which lines are frames.
FIGURE_DEPENDENCIES = {
    "frames": ("count_multiplier",),
    "bases": ("count_multiplier", "alternate_radix", "base_shift_width"),
    "spans": ("count_multiplier", "rotation_offset"),
    "entry": ("count_multiplier", "rotation_offset", "entry_clock_modulus"),
    "unwind": ("count_multiplier", "rotation_offset", "drift_period"),
    "allocated": ("count_multiplier", "coarse_unit"),
    "signatures": ("count_multiplier", "rotation_offset",
                   "signature_offset", "ring_width"),
    "indices": ("count_multiplier",),
}


def _cartesian(live, names):
    """Every combination of the live candidates of the named parameters."""
    pools = [live[name] for name in names]
    if not pools:
        return [()]
    out = [()]
    for pool in pools:
        out = [prefix + (value,) for prefix in out for value in pool]
    return out


def _params_from(live, overrides):
    params = {}
    for name, values in live.items():
        params[name] = overrides.get(name, values[0])
    return params


# ---------------------------------------------------------------------------
# Reference tables.
# ---------------------------------------------------------------------------

class Registry(object):
    """The build registry: tag resolution by build index."""

    def __init__(self, rows):
        self.rows = {}
        for row in rows:
            self.rows.setdefault(row["module_tag"], []).append({
                "canonical_tag": row["canonical_tag"],
                "component_key": row["component_key"],
                "tier_index": int(row["tier_index"]),
                "from_build": int(row["effective_from_build"]),
                "to_build": int(row["effective_to_build"]),
                "status": row["status"],
            })
        for tag in self.rows:
            self.rows[tag].sort(key=lambda r: (r["from_build"], r["to_build"]))

    def resolve(self, tag, build_index):
        """Follow the canonical chain from a tag on one build index.

        The terminal row supplies the canonical tag, the component key and the
        declared tier.  A tag the registry covers on that index at no point of
        the chain, or a chain that arrives twice at the same tag, resolves to
        nothing.
        """
        seen = set()
        while True:
            if tag in seen:
                return None
            seen.add(tag)
            row = self._cover(tag, build_index)
            if row is None:
                return None
            if row["canonical_tag"] == tag:
                return {
                    "canonical_tag": tag,
                    "component_key": row["component_key"],
                    "tier_index": row["tier_index"],
                    "status": row["status"],
                }
            tag = row["canonical_tag"]

    def _cover(self, tag, build_index):
        best = None
        for row in self.rows.get(tag, ()):
            if row["from_build"] <= build_index <= row["to_build"]:
                if best is None or row["to_build"] < best["to_build"]:
                    best = row
        return best


def load_tables(data_dir):
    registry = Registry(read_csv_rows(os.path.join(data_dir, "build_registry.csv")))
    ladder = {}
    for row in read_csv_rows(os.path.join(data_dir, "severity_ladder.csv")):
        ladder.setdefault(row["component_key"], {})[int(row["tier_index"])] = int(row["budget_ns"])
    windows = dict(
        (row["component_key"], (int(row["open_tick"]), int(row["close_tick"])))
        for row in read_csv_rows(os.path.join(data_dir, "support_windows.csv"))
    )
    zones = dict(
        (row["host_id"], row["zone"])
        for row in read_csv_rows(os.path.join(data_dir, "host_zones.csv"))
    )
    segments = dict(
        (row["tenant_class"], row["tenant_segment"])
        for row in read_csv_rows(os.path.join(data_dir, "tenant_classes.csv"))
    )
    return registry, ladder, windows, zones, segments


def read_probe(path):
    probe = {}
    for row in read_csv_rows(path):
        probe[(row["module_tag"], int(row["frame_index"]))] = (
            int(row["samples"]), int(row["peak_ns"])
        )
    return probe


def read_uplink(path):
    uplink = {}
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            uplink[(row["module_tag"], int(row["frame_index"]))] = row
    return uplink


# ---------------------------------------------------------------------------
# Derivation.
# ---------------------------------------------------------------------------

def build_ledger(frames, registry, ladder, windows, zones, segments, probe, uplink):
    """Accept or reject every fault frame and build the fault ledger."""
    ledger = []
    rejections = dict((reason, 0) for reason in REASONS)

    for frame in frames:
        build_index = frame["capture_tick"] // TICKS_PER_BUILD_INDEX
        resolution = registry.resolve(frame["module_column"], build_index)
        broken = set()

        # The probe and the uplink reports are keyed by canonical module tag,
        # so a frame whose module never resolves to a supported build has no
        # row to look up and is not counted against them.
        if resolution is None:
            broken.add("unresolved_module")
        elif resolution["status"] != "supported":
            broken.add("retired_build")
        else:
            component = resolution["component_key"]
            window = windows.get(component)
            if window is None or not (window[0] < frame["capture_tick"] < window[1]):
                broken.add("outside_support")

            key = (resolution["canonical_tag"], frame["frame_index"])
            facts = probe.get(key)
            if facts is None:
                broken.add("unprobed_frame")
            report = uplink.get(key)
            if report is None:
                broken.add("unreported_dump")

        if broken:
            for reason in broken:
                rejections[reason] += 1
            continue

        canonical = resolution["canonical_tag"]
        component = resolution["component_key"]
        samples, peak = facts
        ledger.append({
            "frame": frame,
            "module_tag": canonical,
            "component_key": component,
            "declared_tier": resolution["tier_index"],
            "capture_tick": frame["capture_tick"],
            "samples": samples,
            "peak_ns": peak,
            "bytes_allocated": frame["allocated"],
            "bytes_uploaded": int(report["bytes_uploaded"]),
            "host_id": report["host_id"],
            "tenant_class": report["tenant_class"],
        })

    ledger.sort(key=lambda row: row["capture_tick"])
    return ledger, rejections


def settle_modules(ledger, ladder):
    """Demote effective tiers until no module above zero is over budget.

    Returns the module table, the number of passes taken (counting the pass
    that found nothing left to demote) and the quarantined module tags.
    """
    modules = {}
    for row in ledger:
        entry = modules.get(row["module_tag"])
        if entry is None:
            entry = {
                "module_tag": row["module_tag"],
                "component_key": row["component_key"],
                "declared_tier": row["declared_tier"],
                "effective_tier": row["declared_tier"],
                "frames": [],
            }
            modules[row["module_tag"]] = entry
        entry["frames"].append(row)

    passes = 0
    if not modules:
        return modules, passes

    while True:
        demoted = False
        passes += 1
        allowances = _allowances(modules, ladder)
        for tag in sorted(modules):
            entry = modules[tag]
            if entry["effective_tier"] == 0:
                continue
            over = 0
            within = 0
            for row in entry["frames"]:
                if row["peak_ns"] <= allowances[tag]:
                    within += 1
                else:
                    over += 1
            if over > within:
                entry["effective_tier"] -= 1
                demoted = True
                allowances = _allowances(modules, ladder)
        if not demoted:
            break

    allowances = _allowances(modules, ladder)
    for tag in modules:
        entry = modules[tag]
        over = sum(1 for row in entry["frames"] if row["peak_ns"] > allowances[tag])
        within = len(entry["frames"]) - over
        entry["allowance_ns"] = allowances[tag]
        entry["budget_ns"] = ladder[entry["component_key"]][entry["effective_tier"]]
        entry["within_budget_count"] = within
        entry["quarantined"] = entry["effective_tier"] == 0 and over > within

    return modules, passes


def _allowances(modules, ladder):
    """Every module's allowance under the current effective-tier assignment."""
    by_component = {}
    for entry in modules.values():
        by_component.setdefault(entry["component_key"], []).append(entry)
    envelopes = {}
    for component, entries in by_component.items():
        tiers = len(ladder[component])
        lowest = min(
            ladder[component][entry["effective_tier"]]
            for entry in entries
            if entry["frames"]
        )
        envelopes[component] = lowest * tiers
    return dict(
        (entry["module_tag"],
         min(ladder[entry["component_key"]][entry["effective_tier"]],
             envelopes[entry["component_key"]]))
        for entry in modules.values()
    )


# ---------------------------------------------------------------------------
# Reports.
# ---------------------------------------------------------------------------

def ledger_row(entry, row, zones, segments):
    frame = row["frame"]
    return {
        "bytes_allocated": row["bytes_allocated"],
        "bytes_uploaded": row["bytes_uploaded"],
        "capture_tick": row["capture_tick"],
        "component_key": row["component_key"],
        "cost_ns": frame["span"],
        "effective_tier": entry["effective_tier"],
        "enter_ns": frame["entry"],
        "fault_signature": frame["signature"],
        "frame_index": frame["frame_index"],
        "host_zone": zones[row["host_id"]],
        "module_tag": row["module_tag"],
        "peak_ns": row["peak_ns"],
        "samples": row["samples"],
        "source_dump": frame["source_dump"],
        "symbol": frame["symbol"],
        "tenant_segment": segments[row["tenant_class"]],
        "unwind_ns": frame["unwind"],
        "within_budget": row["peak_ns"] <= entry["allowance_ns"],
    }


def group_report(field, value, rows, secondary):
    """One deployment-zone or tenant-segment report."""
    selected = [row for row in rows if row[field] == value]
    by_component = {}
    by_secondary = {}
    tags = set()
    for row in selected:
        by_component[row["component_key"]] = (
            by_component.get(row["component_key"], 0) + row["bytes_uploaded"]
        )
        by_secondary[row[secondary]] = (
            by_secondary.get(row[secondary], 0) + row["bytes_uploaded"]
        )
        tags.add(row["module_tag"])
    report = {
        "accepted_count": len(selected),
        "by_component": by_component,
        "modules": sorted(tags),
        "total_bytes_uploaded": sum(row["bytes_uploaded"] for row in selected),
        "total_cost_ns": sum(row["frame"]["span"] for row in selected),
        "within_budget_count": sum(1 for row in selected if row["within_budget"]),
        field: value,
        "by_" + secondary: by_secondary,
    }
    return report


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(obj, handle, indent=2, sort_keys=True)
        handle.write("\n")


# ---------------------------------------------------------------------------
# Driver.
# ---------------------------------------------------------------------------

def run(data_dir, out_dir):
    tables = load_tables(data_dir)
    registry, ladder, windows, zones, segments = tables

    controls_path = os.path.join(data_dir, "control-slice-figures.json")
    calibration_export = os.path.join(data_dir, "calibration_fault_frames.psv")
    if os.path.exists(controls_path) and os.path.exists(calibration_export):
        with open(controls_path, encoding="utf-8") as handle:
            controls = json.load(handle)
        params, search_stages, evaluations = calibrate(data_dir, controls)
        verify_check_totals(recover_dump_export(calibration_export, params))
    else:
        controls = {}
        params = dict(UNCONSTRAINED_PARAMETERS)
        search_stages = []
        evaluations = 0

    probe = read_probe(os.path.join(data_dir, "runtime_probe.csv"))
    uplink = read_uplink(os.path.join(data_dir, "uplink_reports.jsonl"))

    dumps, frames = recover_frames(os.path.join(data_dir, "fault_frames.psv"), params)
    verify_check_totals(dumps)

    ledger, rejections = build_ledger(
        frames, registry, ladder, windows, zones, segments, probe, uplink
    )
    modules, passes = settle_modules(ledger, ladder)

    for row in ledger:
        row["within_budget"] = row["peak_ns"] <= modules[row["module_tag"]]["allowance_ns"]
        row["zone"] = zones[row["host_id"]]
        row["tenant_segment"] = segments[row["tenant_class"]]

    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)

    # --- the fault ledger -------------------------------------------------
    with open(os.path.join(out_dir, "normalized.jsonl"), "w", encoding="utf-8") as handle:
        for row in ledger:
            handle.write(json.dumps(
                ledger_row(modules[row["module_tag"]], row, zones, segments),
                sort_keys=True, separators=(",", ":")
            ))
            handle.write("\n")

    # --- the per-module roll-up -------------------------------------------
    product_modules = []
    for tag in sorted(modules):
        entry = modules[tag]
        rows = entry["frames"]
        product_modules.append({
            "accepted_count": len(rows),
            "allowance_ns": entry["allowance_ns"],
            "budget_ns": entry["budget_ns"],
            "component_key": entry["component_key"],
            "declared_tier": entry["declared_tier"],
            "effective_tier": entry["effective_tier"],
            "module_tag": tag,
            "net_samples": sum(row["samples"] for row in rows),
            "quarantined": entry["quarantined"],
            "total_bytes_uploaded": sum(row["bytes_uploaded"] for row in rows),
            "within_budget_count": entry["within_budget_count"],
        })
    write_json(os.path.join(out_dir, "product_summary.json"), {"modules": product_modules})

    # --- the incident roll-up ---------------------------------------------
    by_effective_tier = {}
    for row in ledger:
        tier = str(modules[row["module_tag"]]["effective_tier"])
        by_effective_tier[tier] = by_effective_tier.get(tier, 0) + 1
    write_json(os.path.join(out_dir, "summary.json"), {
        "accepted_count": len(ledger),
        "by_component": _uploaded_by(ledger, "component_key"),
        "by_effective_tier": by_effective_tier,
        "by_tenant_segment": _uploaded_by(ledger, "tenant_segment"),
        "by_zone": _uploaded_by(ledger, "zone"),
        "rejected_count": len(frames) - len(ledger),
        "total_bytes_allocated": sum(row["bytes_allocated"] for row in ledger),
        "total_bytes_uploaded": sum(row["bytes_uploaded"] for row in ledger),
        "total_cost_ns": sum(row["frame"]["span"] for row in ledger),
        "within_budget_count": sum(1 for row in ledger if row["within_budget"]),
    })

    # --- one report per deployment zone and per tenant segment ------------
    for zone in sorted(set(zones.values())):
        write_json(
            os.path.join(out_dir, "region_%s.json" % zone),
            group_report("zone", zone, ledger, "tenant_segment"),
        )
    for segment in sorted(set(segments.values())):
        write_json(
            os.path.join(out_dir, "segment_%s.json" % segment),
            group_report("tenant_segment", segment, ledger, "zone"),
        )

    # --- the reconciliation and derivation record -------------------------
    comparison = calibration_rollup(data_dir, params, tables, controls) if controls else []

    write_json(os.path.join(out_dir, "data_quality.json"), {
        "accepted_count": len(ledger),
        "calibration_comparison": comparison,
        "candidate_search": {
            "evaluations": evaluations,
            "parameters": search_stages,
        },
        "frame_count": len(frames),
        "reconciliation": {
            "allocated_bytes": sum(row["bytes_allocated"] for row in ledger),
            "residual_bytes": sum(row["bytes_allocated"] - row["bytes_uploaded"]
                                  for row in ledger),
            "uploaded_bytes": sum(row["bytes_uploaded"] for row in ledger),
        },
        "rejections": rejections,
        "settlement": {
            "passes": passes,
            "quarantined_modules": sorted(
                tag for tag, entry in modules.items() if entry["quarantined"]
            ),
        },
    })


def _uploaded_by(rows, field):
    totals = {}
    for row in rows:
        totals[row[field]] = totals.get(row[field], 0) + row["bytes_uploaded"]
    return totals


def calibration_rollup(data_dir, params, tables, controls):
    """Recompute every published control figure for the control slice."""
    registry, ladder, windows, zones, segments = tables
    probe = read_probe(os.path.join(data_dir, "calibration_runtime_probe.csv"))
    uplink = read_uplink(os.path.join(data_dir, "calibration_uplink_reports.jsonl"))
    dumps, frames = recover_frames(
        os.path.join(data_dir, "calibration_fault_frames.psv"), params
    )
    reproduced = {
        "CTRL-A": len(frames),
        "CTRL-B": sum(dump.base for dump in dumps),
        "CTRL-C": sum(frame["span"] for frame in frames),
        "CTRL-D": sum(frame["entry"] for frame in frames),
        "CTRL-E": sum(frame["unwind"] for frame in frames),
        "CTRL-F": sum(frame["allocated"] for frame in frames),
        "CTRL-G": sum(
            int(frame["signature"], 16) for frame in frames
        ) % SIGNATURE_SUM_MODULUS,
        "CTRL-H": sum(frame["frame_index"] for frame in frames),
    }

    ledger, _ = build_ledger(
        frames, registry, ladder, windows, zones, segments, probe, uplink
    )
    modules, _ = settle_modules(ledger, ladder)
    reproduced["CTRL-I"] = len(ledger)
    reproduced["CTRL-J"] = sum(1 for entry in modules.values() if entry["quarantined"])
    reproduced["CTRL-K"] = sum(
        row["bytes_allocated"] - row["bytes_uploaded"] for row in ledger
    )

    return [
        {"control": name, "published": controls[name], "reproduced": reproduced[name]}
        for name in sorted(controls)
    ]


def verify_check_totals(dumps):
    """Check the shipped frame and dump check totals before trusting fields.

    A frame line's check total covers its first eight columns as written; a
    dump's check total covers the check totals of its fault frames alone, so
    the trailer padding that follows them is left out of both checks.
    """
    for dump in dumps:
        total = 0
        for cols in dump.written:
            expected = frame_check_total(cols)
            shipped = int(cols[8], 16)
            if expected != shipped:
                raise ValueError(
                    "frame check total mismatch in dump %s (%s != %s)"
                    % (dump.dump_id, expected, shipped)
                )
            total = (total + shipped) % CHECK_MODULUS
        if total != dump.check_total:
            raise ValueError("dump check total mismatch in dump %s" % dump.dump_id)


def main(argv):
    if len(argv) > 3:
        sys.stderr.write("usage: transform.py [DATA_DIR] [OUT_DIR]\n")
        return 2
    data_dir = argv[1] if len(argv) > 1 else "/app/data"
    out_dir = argv[2] if len(argv) > 2 else "/app/out"
    run(data_dir, out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
