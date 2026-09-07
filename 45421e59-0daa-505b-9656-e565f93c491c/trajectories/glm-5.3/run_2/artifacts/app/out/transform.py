#!/usr/bin/env python3
"""Reconstruct a crash-reporter fleet's fault ledger and triage roll-ups.

Usage:
    python3 transform.py [DATA_DIR] [OUT_DIR]

DATA_DIR holds a crash reporter dump export, its two side exports, the five
reference tables, and a held-out control slice with published control figures.
The nine wire parameters the crash reporter withholds are settled against those
figures first; the incident corpus is then derived from scratch.

With no arguments the program reads /app/data and writes /app/out.
"""

import collections
import csv
import itertools
import json
import os
import sys

DEFAULT_DATA_DIR = "/app/data"
DEFAULT_OUT_DIR = "/app/out"

# clock-and-sequencing.md: 1000 ticks per millisecond, so a build index covers
# 900000000 ticks.
TICKS_PER_BUILD = 900000000
# control-slice-figures.md, CTRL-G.
SIGNATURE_MODULUS = 1000000007
# wire-and-dumps.md, check totals.
CHECK_MODULUS = 0x10000
SIGNATURE_WIDTH = 16
HEX_DIGITS = frozenset("0123456789abcdef")

# Dump flag word (wire-and-dumps.md).
D_SPAN_SECOND_DIFF = 0x001
D_ENTRY_CUMULATIVE = 0x002
D_UNWIND_DRIFT = 0x004
D_LINES_ROTATED = 0x008
D_COUNT_BIASED = 0x010
D_BASE_ALT_RADIX = 0x020
# 0x100 marks the module column as an alias; the registry chain resolves
# aliases and canonical tags alike, so it needs no decode step of its own.
D_BASE_SPLIT = 0x800

# Frame flag word.
F_INDEX_PLACEHOLDER = 0x040
F_SIGNATURE_ROTATED = 0x080
F_ALLOC_COARSE = 0x200
F_SIGNATURE_RING = 0x400

# The ledger closure names a rejection for each broken acceptance condition.
# The two identity conditions gate the three attribution ones: a frame whose
# module never resolves has no component, window or module tag to look up.
IDENTITY_REASONS = ("unresolved_module", "retired_build")
ATTRIBUTION_REASONS = ("outside_support", "unprobed_frame", "unreported_dump")
REJECTION_REASONS = tuple(sorted(IDENTITY_REASONS + ATTRIBUTION_REASONS))

PARAM_ENTRY_MODULUS = "entry clock modulus"
PARAM_DRIFT_PERIOD = "drift period"
PARAM_ROTATION_OFFSET = "rotation offset"
PARAM_COUNT_MULTIPLIER = "count multiplier"
PARAM_ALTERNATE_RADIX = "alternate radix"
PARAM_BASE_SHIFT_WIDTH = "base shift width"
PARAM_SIGNATURE_OFFSET = "signature offset"
PARAM_COARSE_UNIT = "coarse unit"
PARAM_RING_WIDTH = "ring width"

# calibration-controls.md, candidate domains, in the order the notes name them.
PARAMETERS = (
    (PARAM_ENTRY_MODULUS, [1 << exponent for exponent in range(22, 35)]),
    (PARAM_DRIFT_PERIOD, [48, 96, 192, 384, 768]),
    (PARAM_ROTATION_OFFSET, list(range(8))),
    (PARAM_COUNT_MULTIPLIER, list(range(9))),
    (PARAM_ALTERNATE_RADIX, list(range(11, 36))),
    (PARAM_BASE_SHIFT_WIDTH, [8, 10, 12, 16]),
    (PARAM_SIGNATURE_OFFSET, list(range(8))),
    (PARAM_COARSE_UNIT, [64, 128, 256, 512, 1024]),
    (PARAM_RING_WIDTH, [4, 8, 12, 16]),
)
PARAM_DOMAIN = dict(PARAMETERS)
PARAM_NAMES = tuple(PARAM_DOMAIN)

# Filled in for parameters a trial has not settled yet; chosen so they never
# raise on materials a well-formed corpus carries.
PERMISSIVE_DEFAULTS = {
    PARAM_ENTRY_MODULUS: 1 << 34,
    PARAM_DRIFT_PERIOD: 768,
    PARAM_ROTATION_OFFSET: 0,
    PARAM_COUNT_MULTIPLIER: 0,
    PARAM_ALTERNATE_RADIX: 35,
    PARAM_BASE_SHIFT_WIDTH: 16,
    PARAM_SIGNATURE_OFFSET: 0,
    PARAM_COARSE_UNIT: 64,
    PARAM_RING_WIDTH: 16,
}

# The search settles one stage at a time, each against the control figure that
# stage's parameters alone move; later stages read the earlier ones. The whole
# published set stands behind the ledger figures CTRL-I, CTRL-J and CTRL-K.
SEARCH_STAGES = (
    ((PARAM_COUNT_MULTIPLIER,), "CTRL-A"),
    ((PARAM_ROTATION_OFFSET,), "CTRL-C"),
    ((PARAM_ENTRY_MODULUS,), "CTRL-D"),
    ((PARAM_DRIFT_PERIOD,), "CTRL-E"),
    ((PARAM_COARSE_UNIT,), "CTRL-F"),
    ((PARAM_ALTERNATE_RADIX, PARAM_BASE_SHIFT_WIDTH), "CTRL-B"),
    ((PARAM_SIGNATURE_OFFSET, PARAM_RING_WIDTH), "CTRL-G"),
)
STAGE_CONTROL = {name: control for stage, control in SEARCH_STAGES for name in stage}


class TriageError(RuntimeError):
    """The shipped materials broke an invariant the derivation relies on."""


# --------------------------------------------------------------------------
# reading the shipped materials
# --------------------------------------------------------------------------


def read_lines(path):
    with open(path, "r", encoding="utf-8") as handle:
        return [line.rstrip("\n") for line in handle]


def read_dumps(path):
    """Parse a pipe-delimited reporter export into headers and raw columns."""
    dumps = []
    current = None
    for line in read_lines(path):
        if not line or line.startswith("#"):
            continue
        cols = line.split("|")
        if cols[0] == "@":
            if len(cols) != 8:
                raise TriageError("%s: dump header carries %d columns" % (path, len(cols)))
            current = {
                "dump_id": cols[1],
                "module": cols[2],
                "declared": int(cols[3]),
                "base_field": cols[4],
                "flags": int(cols[5], 16),
                "key": int(cols[6]),
                "check_total": cols[7],
                "lines": [],
            }
            dumps.append(current)
        else:
            if current is None:
                raise TriageError("%s: frame line before any dump header" % path)
            if len(cols) != 9:
                raise TriageError("%s: frame line carries %d columns" % (path, len(cols)))
            current["lines"].append(cols)
    if not dumps:
        raise TriageError("%s: carries no dumps" % path)
    return dumps


def read_probe(path):
    probe = {}
    with open(path, "r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            probe[(row["module_tag"], int(row["frame_index"]))] = (
                int(row["samples"]),
                int(row["peak_ns"]),
            )
    return probe


def read_uplinks(path):
    uplinks = {}
    for line in read_lines(path):
        if not line.strip():
            continue
        report = json.loads(line)
        uplinks[(report["module_tag"], int(report["frame_index"]))] = report
    return uplinks


def read_table(path):
    with open(path, "r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


class Reference(object):
    """The five reference tables, which govern every corpus alike."""

    def __init__(self, data_dir):
        self.registry = collections.defaultdict(list)
        for row in read_table(os.path.join(data_dir, "build_registry.csv")):
            self.registry[row["module_tag"]].append(row)
        self.ladder = collections.defaultdict(dict)
        for row in read_table(os.path.join(data_dir, "severity_ladder.csv")):
            self.ladder[row["component_key"]][int(row["tier_index"])] = int(row["budget_ns"])
        self.windows = {
            row["component_key"]: (int(row["open_tick"]), int(row["close_tick"]))
            for row in read_table(os.path.join(data_dir, "support_windows.csv"))
        }
        self.host_zones = {
            row["host_id"]: row["zone"]
            for row in read_table(os.path.join(data_dir, "host_zones.csv"))
        }
        self.tenant_segments = {
            row["tenant_class"]: row["tenant_segment"]
            for row in read_table(os.path.join(data_dir, "tenant_classes.csv"))
        }
        # The fleet's deployment zones and tenant segments are the ones the two
        # attribution tables name.
        self.zones = sorted(set(self.host_zones.values()))
        self.segments = sorted(set(self.tenant_segments.values()))

    def resolve(self, tag, build_index):
        """Follow the registry chain for a tag on a build index.

        Returns the terminal row, or None when the tag is covered by no row on
        that index or the chain arrives twice at the same tag.
        """
        seen = set()
        current = tag
        while True:
            if current in seen:
                return None
            seen.add(current)
            row = None
            for candidate in self.registry.get(current, ()):
                if int(candidate["effective_from_build"]) <= build_index <= int(
                    candidate["effective_to_build"]
                ):
                    row = candidate
                    break
            if row is None:
                return None
            if row["canonical_tag"] == row["module_tag"]:
                return row
            current = row["canonical_tag"]


# --------------------------------------------------------------------------
# decoding a reporter export
# --------------------------------------------------------------------------


def parse_radix(text, radix):
    """Read a numeral written in the alternate radix, 0-9 then a-z."""
    value = 0
    for char in text:
        digit = int(char, 36)
        if digit >= radix:
            raise TriageError("digit %r exceeds radix %d" % (char, radix))
        value = value * radix + digit
    return value


def frame_check_total(cols):
    """The frame check total over the eight columns the frame carries."""
    return sum(ord(char) for char in "|".join(cols[:8])) % CHECK_MODULUS


def verify_check_totals(dumps):
    """Wire closure: check the shipped totals before trusting a decoded field.

    Trailer lines carry no reporter rule, so their written total stands.
    """
    for dump in dumps:
        total = 0
        for cols in dump["lines"]:
            written = int(cols[8], 16)
            if frame_check_total(cols) != written and cols[7].split(":")[0] != "pad":
                raise TriageError(
                    "frame check total mismatch in dump %s frame %s"
                    % (dump["dump_id"], cols[0])
                )
            total += written
        if total % CHECK_MODULUS != int(dump["check_total"], 16):
            raise TriageError("dump check total mismatch in dump %s" % dump["dump_id"])


def fault_frame_count(dump, ordinal, count_multiplier):
    """The dump's true fault frame count, with any count bias taken off."""
    if dump["flags"] & D_COUNT_BIASED:
        return dump["declared"] - ((ordinal * count_multiplier) % 9)
    return dump["declared"]


def authoritative_index(cols):
    """The authoritative frame index: the symbol numeral, or the index column."""
    if int(cols[6], 16) & F_INDEX_PLACEHOLDER:
        return int(cols[7].split(":")[-1])
    return int(cols[0])


def decode_export(path, params, strict=True):
    """Recover every fault frame a reporter export carries.

    Frames come back in export order: dump by dump as written, and inside a
    dump in logical order once any rotation is taken off. The resident span
    running total and the signature ring both advance over that order.
    """
    dumps = read_dumps(path)
    verify_check_totals(dumps)
    modulus = params[PARAM_ENTRY_MODULUS]
    drift = params[PARAM_DRIFT_PERIOD]
    rotation_offset = params[PARAM_ROTATION_OFFSET]
    count_multiplier = params[PARAM_COUNT_MULTIPLIER]
    radix = params[PARAM_ALTERNATE_RADIX]
    shift = params[PARAM_BASE_SHIFT_WIDTH]
    signature_offset = params[PARAM_SIGNATURE_OFFSET]
    coarse = params[PARAM_COARSE_UNIT]
    ring_width = params[PARAM_RING_WIDTH]

    for ordinal, dump in enumerate(dumps, start=1):
        flags = dump["flags"]
        count = fault_frame_count(dump, ordinal, count_multiplier)
        if count < 1 or count > len(dump["lines"]):
            raise TriageError(
                "dump %s holds %d fault frames over %d lines"
                % (dump["dump_id"], count, len(dump["lines"]))
            )
        fault_lines = dump["lines"][:count]
        trailers = dump["lines"][count:]
        if flags & D_BASE_SPLIT and not trailers:
            raise TriageError("dump %s splits its base but carries no trailer" % dump["dump_id"])

        if flags & D_BASE_ALT_RADIX:
            high = parse_radix(dump["base_field"], radix)
        else:
            high = int(dump["base_field"])
        if flags & D_BASE_SPLIT:
            low = int(trailers[0][0])
            if low >> shift:
                raise TriageError(
                    "dump %s low base part %d exceeds shift width %d"
                    % (dump["dump_id"], low, shift)
                )
            base = (high << shift) | low
        else:
            base = high

        if flags & D_LINES_ROTATED:
            turn = (dump["key"] + rotation_offset) % count
            logical = [fault_lines[(i - turn) % count] for i in range(count)]
        else:
            logical = list(fault_lines)

        entries = []
        if flags & D_ENTRY_CUMULATIVE:
            previous = 0
            for position, cols in enumerate(logical):
                stamp = int(cols[1]) if position == 0 else (int(cols[1]) - previous) % modulus
                entries.append(stamp)
                previous = stamp
        else:
            entries = [int(cols[1]) for cols in logical]

        if flags & D_SPAN_SECOND_DIFF:
            # Second-order differences over the fault frames in logical order,
            # started at the dump's first fault frame.
            spans = []
            for position, cols in enumerate(logical):
                written = int(cols[3])
                if position == 0:
                    spans.append(written)
                elif position == 1:
                    spans.append(spans[0] + written)
                else:
                    spans.append(2 * spans[position - 1] - spans[position - 2] + written)
        else:
            spans = [int(cols[3]) for cols in logical]

        recovered = {id(cols): pair for cols, pair in zip(logical, zip(entries, spans))}
        dump["base"] = base
        dump["first_entry"] = entries[0]
        dump["order"] = [
            (cols, authoritative_index(cols)) + recovered[id(cols)] for cols in logical
        ]

    frames = []
    ring = [None] * ring_width
    write_position = 0
    span_total = 0
    for dump in dumps:
        for cols, index, entry, span in dump["order"]:
            frame_flags = int(cols[6], 16)
            if dump["flags"] & D_UNWIND_DRIFT:
                unwind = int(cols[2]) - (span_total % drift)
            else:
                unwind = int(cols[2])
            allocated = int(cols[4][1:]) * coarse if frame_flags & F_ALLOC_COARSE else int(cols[4])
            written = cols[5]
            if frame_flags & F_SIGNATURE_RING:
                signature = ring[(write_position - int(written[1:])) % ring_width]
            elif frame_flags & F_SIGNATURE_ROTATED:
                turn = (index + signature_offset) % SIGNATURE_WIDTH
                signature = written[-turn:] + written[:-turn] if turn else written
            else:
                signature = written
            if strict and (
                signature is None
                or len(signature) != SIGNATURE_WIDTH
                or not HEX_DIGITS.issuperset(signature)
            ):
                raise TriageError(
                    "recovered signature %r is not sixteen hex characters" % (signature,)
                )
            ring[write_position % ring_width] = signature
            write_position += 1
            span_total += span
            frames.append(
                {
                    "source_dump": dump["dump_id"],
                    "dump_base": dump["base"],
                    "module_tag_written": dump["module"],
                    "frame_index": index,
                    "enter_ns": entry,
                    "unwind_ns": unwind,
                    "cost_ns": span,
                    "bytes_allocated": allocated,
                    "fault_signature": signature,
                    "symbol": cols[7].split(":")[0],
                    "capture_tick": dump["base"] + ((entry - dump["first_entry"]) % modulus),
                }
            )
    for frame in frames:
        frame["build_index"] = frame["capture_tick"] // TICKS_PER_BUILD
    return frames


# --------------------------------------------------------------------------
# acceptance, settlement, and the ledger
# --------------------------------------------------------------------------


def build_ledger(frames, reference, probe, uplinks):
    """Apply the ledger closure and count every broken acceptance condition."""
    ledger = []
    rejections = collections.Counter({reason: 0 for reason in REJECTION_REASONS})
    for frame in frames:
        row = reference.resolve(frame["module_tag_written"], frame["build_index"])
        if row is None:
            rejections["unresolved_module"] += 1
            continue
        if row["status"] != "supported":
            rejections["retired_build"] += 1
            continue
        # The module resolved, so its component, window and canonical tag are
        # known: the three attribution conditions are each counted as they stand.
        tag = row["canonical_tag"]
        open_tick, close_tick = reference.windows[row["component_key"]]
        broken = []
        if not open_tick < frame["capture_tick"] < close_tick:
            broken.append("outside_support")
        residency = probe.get((tag, frame["frame_index"]))
        if residency is None:
            broken.append("unprobed_frame")
        report = uplinks.get((tag, frame["frame_index"]))
        if report is None:
            broken.append("unreported_dump")
        for reason in broken:
            rejections[reason] += 1
        if broken:
            continue
        ledger.append(
            dict(
                frame,
                declared_tier=int(row["tier_index"]),
                module_tag=tag,
                component_key=row["component_key"],
                samples=residency[0],
                peak_ns=residency[1],
                bytes_uploaded=int(report["bytes_uploaded"]),
                host_zone=reference.host_zones[report["host_id"]],
                tenant_segment=reference.tenant_segments[report["tenant_class"]],
            )
        )
    ledger.sort(key=lambda row: row["capture_tick"])
    return ledger, rejections


def settle_quarantine(ledger, ladder):
    """Demote effective tiers until no module breaks its budget majority.

    Returns the module states and the pass count. Passes run over the module
    set, and the pass that finds no module left to demote counts with the rest.
    """
    modules = {}
    for row in ledger:
        state = modules.get(row["module_tag"])
        if state is None:
            state = {
                "module_tag": row["module_tag"],
                "component_key": row["component_key"],
                "declared_tier": row["declared_tier"],
                "effective_tier": row["declared_tier"],
                "frames": [],
            }
            modules[row["module_tag"]] = state
        state["frames"].append(row)

    def envelopes():
        """The containment envelope of each component with frames in service."""
        result = {}
        for component in {state["component_key"] for state in modules.values()}:
            standing = [
                ladder[component][state["effective_tier"]]
                for state in modules.values()
                if state["component_key"] == component
            ]
            result[component] = min(standing) * len(ladder[component])
        return result

    def budget(state, envelope):
        allowance = min(
            ladder[state["component_key"]][state["effective_tier"]],
            envelope[state["component_key"]],
        )
        over = sum(1 for row in state["frames"] if row["peak_ns"] > allowance)
        return allowance, over, len(state["frames"]) - over

    passes = 0
    while True:
        passes += 1
        envelope = envelopes()
        demoted = False
        for state in modules.values():
            if state["effective_tier"] == 0:
                continue
            _, over, within = budget(state, envelope)
            if over > within:
                state["effective_tier"] -= 1
                demoted = True
        if not demoted:
            break

    envelope = envelopes()
    for state in modules.values():
        allowance, over, within = budget(state, envelope)
        state["allowance_ns"] = allowance
        state["budget_ns"] = ladder[state["component_key"]][state["effective_tier"]]
        state["within_budget_count"] = within
        state["quarantined"] = state["effective_tier"] == 0 and over > within
        for row in state["frames"]:
            row["effective_tier"] = state["effective_tier"]
            row["within_budget"] = row["peak_ns"] <= allowance
    return modules, passes


# --------------------------------------------------------------------------
# control figures and the withheld wire parameters
# --------------------------------------------------------------------------

# CTRL-A through CTRL-H are figures a decode alone yields; CTRL-I, CTRL-J and
# CTRL-K need the whole acceptance and settlement pipeline behind them.


class Corpus(object):
    """One tier of the fleet's materials, under its filename prefix."""

    def __init__(self, data_dir, prefix, reference):
        self.reference = reference
        self.export_path = os.path.join(data_dir, prefix + "fault_frames.psv")
        self.probe = read_probe(os.path.join(data_dir, prefix + "runtime_probe.csv"))
        self.uplinks = read_uplinks(os.path.join(data_dir, prefix + "uplink_reports.jsonl"))

    def control_figure(self, name, params):
        """Reproduce one published control figure under a parameter assignment."""
        frames = decode_export(self.export_path, params, strict=False)
        if name == "CTRL-A":
            return len(frames)
        if name == "CTRL-B":
            # One recovered base per dump, not one per frame.
            bases = {frame["source_dump"]: frame["dump_base"] for frame in frames}
            return sum(bases.values())
        if name == "CTRL-C":
            return sum(frame["cost_ns"] for frame in frames)
        if name == "CTRL-D":
            return sum(frame["enter_ns"] for frame in frames)
        if name == "CTRL-E":
            return sum(frame["unwind_ns"] for frame in frames)
        if name == "CTRL-F":
            return sum(frame["bytes_allocated"] for frame in frames)
        if name == "CTRL-G":
            total = 0
            for frame in frames:
                if frame["fault_signature"] is None:
                    return None
                total += int(frame["fault_signature"], 16)
            return total % SIGNATURE_MODULUS
        if name == "CTRL-H":
            return sum(frame["frame_index"] for frame in frames)
        ledger, _ = build_ledger(frames, self.reference, self.probe, self.uplinks)
        if name == "CTRL-I":
            return len(ledger)
        modules, _ = settle_quarantine(ledger, self.reference.ladder)
        if name == "CTRL-J":
            return sum(1 for state in modules.values() if state["quarantined"])
        if name == "CTRL-K":
            return sum(row["bytes_allocated"] for row in ledger) - sum(
                row["bytes_uploaded"] for row in ledger
            )
        raise KeyError(name)


def solve_parameters(calibration, published):
    """Settle the nine withheld wire parameters against the published figures.

    Every candidate value in every parameter's domain is put through the stage
    control that parameter moves, and the falsifying figure is kept for each
    rejection. Exactly one assignment is expected to survive.
    """
    settled = {}
    alive = {name: list(domain) for name, domain in PARAMETERS}
    falsified = {}
    evaluations = 0

    def reproduces(control, trial):
        """True when a trial assignment reproduces one published figure."""
        try:
            figure = calibration.control_figure(control, trial)
        except TriageError:
            return False
        return figure is not None and figure == published[control]

    def trial_over(stage, combo):
        """A full assignment: what is settled plus this stage's candidates."""
        trial = dict(PERMISSIVE_DEFAULTS)
        trial.update(settled)
        trial.update(zip(stage, combo))
        return trial

    for stage, control in SEARCH_STAGES:
        survivors = {name: set() for name in stage}
        for combo in itertools.product(*(PARAM_DOMAIN[name] for name in stage)):
            evaluations += 1
            if reproduces(control, trial_over(stage, combo)):
                for name, value in zip(stage, combo):
                    survivors[name].add(value)
        for name in stage:
            kept = sorted(survivors[name])
            for value in PARAM_DOMAIN[name]:
                if value not in kept:
                    falsified[(name, value)] = control
            alive[name] = kept
            if len(kept) == 1:
                settled[name] = kept[0]
            elif not kept:
                raise TriageError(
                    "no candidate for %s reproduces %s" % (", ".join(stage), control)
                )

    outstanding = [name for name in PARAM_NAMES if name not in settled]
    if outstanding:
        # A stage left several candidates standing; settle them against the
        # whole published set at once, taking the first assignment in domain
        # order that reproduces every figure.
        found = None
        for combo in itertools.product(*(alive[name] for name in outstanding)):
            trial = dict(PERMISSIVE_DEFAULTS)
            trial.update(settled)
            trial.update(zip(outstanding, combo))
            for control in sorted(published):
                evaluations += 1
                if not reproduces(control, trial):
                    for name, value in zip(outstanding, combo):
                        falsified.setdefault((name, value), control)
                    break
            else:
                found = combo
                break
        if found is None:
            raise TriageError("no assignment reproduces every published figure")
        settled.update(zip(outstanding, found))

    reproduced = {}
    for control in sorted(published):
        figure = calibration.control_figure(control, settled)
        if figure != published[control]:
            raise TriageError(
                "settled parameters reproduce %s as %r, published %r"
                % (control, figure, published[control])
            )
        reproduced[control] = figure

    parameters = []
    for name in PARAM_NAMES:
        selected = settled[name]
        rejected = []
        for value in PARAM_DOMAIN[name]:
            if value == selected:
                continue
            rejected.append(
                {"value": value, "control": falsified.get((name, value)) or sorted(published)[0]}
            )
        parameters.append(
            {
                "name": name,
                "candidates": len(PARAM_DOMAIN[name]),
                "control": STAGE_CONTROL.get(name, sorted(published)[0]),
                "selected": selected,
                "rejected": rejected,
            }
        )
    search = {"evaluations": evaluations, "parameters": parameters}
    return settled, search, reproduced


# --------------------------------------------------------------------------
# the triage set
# --------------------------------------------------------------------------


def ledger_row(row):
    """One fault ledger row, exactly as the ledger is written."""
    return {
        "bytes_allocated": row["bytes_allocated"],
        "bytes_uploaded": row["bytes_uploaded"],
        "capture_tick": row["capture_tick"],
        "component_key": row["component_key"],
        "cost_ns": row["cost_ns"],
        "effective_tier": row["effective_tier"],
        "enter_ns": row["enter_ns"],
        "fault_signature": row["fault_signature"],
        "frame_index": row["frame_index"],
        "host_zone": row["host_zone"],
        "module_tag": row["module_tag"],
        "peak_ns": row["peak_ns"],
        "samples": row["samples"],
        "source_dump": row["source_dump"],
        "symbol": row["symbol"],
        "tenant_segment": row["tenant_segment"],
        "unwind_ns": row["unwind_ns"],
        "within_budget": row["within_budget"],
    }


def rollup(rows, key, value):
    """Sum a per-row figure grouped by one of the ledger's own keys."""
    totals = collections.Counter()
    for row in rows:
        totals[row[key]] += row[value]
    return dict(sorted(totals.items()))


# The ledger's own key for a group, and the name a report writes it under.
GROUP_KEY = {"host_zone": "zone", "tenant_segment": "tenant_segment"}


def group_report(ledger, field, value, other):
    """One deployment-zone or tenant-segment report.

    `field` is the ledger key the report slices on and `other` the one it
    breaks its byte total down by; both are reported under their roll-up names.
    """
    rows = [row for row in ledger if row[field] == value]
    return {
        GROUP_KEY[field]: value,
        "accepted_count": len(rows),
        "by_component": rollup(rows, "component_key", "bytes_uploaded"),
        "by_" + GROUP_KEY[other]: rollup(rows, other, "bytes_uploaded"),
        "modules": sorted({row["module_tag"] for row in rows}),
        "total_bytes_uploaded": sum(row["bytes_uploaded"] for row in rows),
        "total_cost_ns": sum(row["cost_ns"] for row in rows),
        "within_budget_count": sum(1 for row in rows if row["within_budget"]),
    }


def build_reports(frame_count, ledger, rejections, modules, passes, reference):
    allocated = sum(row["bytes_allocated"] for row in ledger)
    uploaded = sum(row["bytes_uploaded"] for row in ledger)
    by_tier = collections.Counter(row["effective_tier"] for row in ledger)
    summary = {
        "accepted_count": len(ledger),
        "rejected_count": frame_count - len(ledger),
        "by_component": rollup(ledger, "component_key", "bytes_uploaded"),
        "by_effective_tier": {str(tier): by_tier[tier] for tier in sorted(by_tier)},
        "by_tenant_segment": rollup(ledger, "tenant_segment", "bytes_uploaded"),
        "by_zone": rollup(ledger, "host_zone", "bytes_uploaded"),
        "total_bytes_allocated": allocated,
        "total_bytes_uploaded": uploaded,
        "total_cost_ns": sum(row["cost_ns"] for row in ledger),
        "within_budget_count": sum(1 for row in ledger if row["within_budget"]),
    }
    product = {
        "modules": [
            {
                "module_tag": state["module_tag"],
                "component_key": state["component_key"],
                "declared_tier": state["declared_tier"],
                "effective_tier": state["effective_tier"],
                "budget_ns": state["budget_ns"],
                "allowance_ns": state["allowance_ns"],
                "quarantined": state["quarantined"],
                "accepted_count": len(state["frames"]),
                "within_budget_count": state["within_budget_count"],
                "net_samples": sum(row["samples"] for row in state["frames"]),
                "total_bytes_uploaded": sum(
                    row["bytes_uploaded"] for row in state["frames"]
                ),
            }
            for state in sorted(modules.values(), key=lambda state: state["module_tag"])
        ]
    }
    regions = {
        "region_%s.json" % zone: group_report(
            ledger, "host_zone", zone, "tenant_segment"
        )
        for zone in reference.zones
    }
    segments = {
        "segment_%s.json" % segment: group_report(
            ledger, "tenant_segment", segment, "host_zone"
        )
        for segment in reference.segments
    }
    data_quality = {
        "frame_count": frame_count,
        "accepted_count": len(ledger),
        "rejections": {reason: rejections[reason] for reason in REJECTION_REASONS},
        "reconciliation": {
            "allocated_bytes": allocated,
            "uploaded_bytes": uploaded,
            "residual_bytes": allocated - uploaded,
        },
        "settlement": {
            "passes": passes,
            "quarantined_modules": sorted(
                state["module_tag"] for state in modules.values() if state["quarantined"]
            ),
        },
    }
    return summary, product, regions, segments, data_quality


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------

LEDGER_FILE = "normalized.jsonl"
SUMMARY_FILE = "summary.json"
PRODUCT_FILE = "product_summary.json"
QUALITY_FILE = "data_quality.json"
SINGLE_FILES = frozenset((LEDGER_FILE, SUMMARY_FILE, PRODUCT_FILE, QUALITY_FILE))


def clear_owned_files(out_dir):
    """Drop triage files an earlier run left behind, so a re-run starts empty."""
    for name in sorted(os.listdir(out_dir)):
        owned = name in SINGLE_FILES or (
            name.endswith(".json")
            and (name.startswith("region_") or name.startswith("segment_"))
        )
        if owned:
            os.remove(os.path.join(out_dir, name))


def write_all(out_dir, ledger, summary, product, regions, segments, data_quality):
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    else:
        clear_owned_files(out_dir)
    with open(os.path.join(out_dir, LEDGER_FILE), "w", encoding="utf-8") as handle:
        for row in ledger:
            handle.write(json.dumps(ledger_row(row), sort_keys=True, separators=(",", ":")))
            handle.write("\n")
    write_json(os.path.join(out_dir, SUMMARY_FILE), summary)
    write_json(os.path.join(out_dir, PRODUCT_FILE), product)
    for name, payload in regions.items():
        write_json(os.path.join(out_dir, name), payload)
    for name, payload in segments.items():
        write_json(os.path.join(out_dir, name), payload)
    write_json(os.path.join(out_dir, QUALITY_FILE), data_quality)


def write_json(path, payload):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, indent=2, sort_keys=True))
        handle.write("\n")


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


def run(data_dir, out_dir):
    """Settle the wire parameters, then derive and write the whole triage set."""
    reference = Reference(data_dir)
    with open(
        os.path.join(data_dir, "control-slice-figures.json"), "r", encoding="utf-8"
    ) as handle:
        published = json.load(handle)
    calibration = Corpus(data_dir, "calibration_", reference)
    params, candidate_search, reproduced = solve_parameters(calibration, published)

    # Parameters settled: derive the incident corpus from scratch.
    incident = Corpus(data_dir, "", reference)
    frames = decode_export(incident.export_path, params)
    ledger, rejections = build_ledger(frames, reference, incident.probe, incident.uplinks)
    modules, passes = settle_quarantine(ledger, reference.ladder)
    summary, product, regions, segments, data_quality = build_reports(
        len(frames), ledger, rejections, modules, passes, reference
    )
    data_quality["calibration_comparison"] = [
        {"control": control, "published": published[control], "reproduced": reproduced[control]}
        for control in sorted(published)
    ]
    data_quality["candidate_search"] = candidate_search
    write_all(out_dir, ledger, summary, product, regions, segments, data_quality)
    return data_quality


def main(argv):
    data_dir = argv[1] if len(argv) > 1 else DEFAULT_DATA_DIR
    out_dir = argv[2] if len(argv) > 2 else DEFAULT_OUT_DIR
    run(data_dir, out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
