#!/usr/bin/env python3
"""Reconstruct a crash-reporter incident window: the fault ledger, the roll-ups
that hang off it, and the calibration record that settles the withheld wire
parameters.

    python3 transform.py [DATA_DIR] [OUT_DIR]

DATA_DIR holds the fleet's flat files: the reporter dump exports in three
tiers, the build registry and the attribution tables.  The nine withheld wire
parameters are settled against the published control figures for the control
slice before any figure is computed for the incident corpus, and every
candidate value in every parameter's published domain is put to a verdict
before that parameter's value is selected.  All clock, byte and residency
arithmetic is carried in Python integers.

Nothing here depends on the identifiers, counts or totals of any one corpus:
dump names, module tags, zones, segments and figures are all read from the
materials the fleet ships.
"""

import csv
import json
import os
import sys
from itertools import product

# --------------------------------------------------------------------------
# Written rules that are not corpus data
# --------------------------------------------------------------------------

TICKS_PER_MS = 1000                 # the reporter clock
BUILD_INDEX_MS = 900_000            # "a build index covers 900000000 ticks"
TICKS_PER_BUILD_INDEX = TICKS_PER_MS * BUILD_INDEX_MS

SIGNATURE_HEX_MODULUS = 1_000_000_007
CHECK_MODULUS = 65536
COUNT_BIAS_MODULUS = 9

# A control figure's whole candidate space is enumerated to put a verdict on
# every candidate value, but only while that space stays enumerable; a figure
# that reads all nine parameters is settled jointly instead.
EXHAUSTIVE_LIMIT = 20000

# Dump flag word (wire-and-dumps.md).
DUMP_SPAN_DELTA = 0x001
DUMP_ENTRY_DELTA = 0x002
DUMP_UNWIND_DRIFT = 0x004
DUMP_LINE_ROTATION = 0x008
DUMP_COUNT_BIAS = 0x010
DUMP_ALT_RADIX = 0x020
DUMP_MODULE_ALIAS = 0x100
DUMP_SPLIT_BASE = 0x800

# Frame flag word.
FRAME_INDEX_PLACEHOLDER = 0x040
FRAME_SIGNATURE_ROTATION = 0x080
FRAME_COARSE_ALLOCATION = 0x200
FRAME_SIGNATURE_RING = 0x400

# The nine withheld wire parameters and the candidate domains the control
# slice publishes for them (calibration-controls.md).
PARAMETER_SPECS = [
    ("entry_clock_modulus", "entry clock modulus",
     [1 << e for e in range(22, 35)]),
    ("drift_period", "drift period", [48, 96, 192, 384, 768]),
    ("rotation_offset", "rotation offset", list(range(0, 8))),
    ("count_multiplier", "count multiplier", list(range(0, 9))),
    ("alternate_radix", "alternate radix", list(range(11, 36))),
    ("base_shift_width", "base shift width", [8, 10, 12, 16]),
    ("signature_offset", "signature offset", list(range(0, 8))),
    ("coarse_unit", "coarse unit", [64, 128, 256, 512, 1024]),
    ("ring_width", "ring width", [4, 8, 12, 16]),
]
PARAMETER_ORDER = [name for name, _, _ in PARAMETER_SPECS]
PARAMETER_LABEL = {name: label for name, label, _ in PARAMETER_SPECS}
DOMAIN = {name: values for name, _, values in PARAMETER_SPECS}
ALL_PARAMETERS = frozenset(PARAMETER_ORDER)

# The order the candidate search assigns parameters in; chosen so that each
# control figure becomes decidable as early as possible.
SEARCH_ORDER = ["count_multiplier", "rotation_offset", "alternate_radix",
                "base_shift_width", "coarse_unit", "entry_clock_modulus",
                "drift_period", "signature_offset", "ring_width"]

# The eleven published control figures, and the wire parameters each one reads.
# A figure is only ever computed once every parameter it depends on is pinned
# down, which is what lets the candidate search prune.
CONTROL_SPECS = [
    ("CTRL-A", "frame_count", ("count_multiplier",)),
    ("CTRL-B", "base_total", ("count_multiplier", "alternate_radix",
                              "base_shift_width")),
    ("CTRL-C", "span_total", ("count_multiplier", "rotation_offset")),
    ("CTRL-D", "entry_total", ("count_multiplier", "rotation_offset",
                               "entry_clock_modulus")),
    ("CTRL-E", "unwind_total", ("count_multiplier", "rotation_offset",
                                "drift_period")),
    ("CTRL-F", "alloc_total", ("count_multiplier", "coarse_unit")),
    ("CTRL-G", "signature_total", ("count_multiplier", "rotation_offset",
                                   "signature_offset", "ring_width")),
    ("CTRL-H", "frame_index_total", ("count_multiplier",)),
    ("CTRL-I", "accepted_count", tuple(PARAMETER_ORDER)),
    ("CTRL-J", "quarantined_module_count", tuple(PARAMETER_ORDER)),
    ("CTRL-K", "upload_residual", tuple(PARAMETER_ORDER)),
]
CONTROL_FIGURE = {name: fig for name, fig, _ in CONTROL_SPECS}
CONTROL_DEPS = {name: frozenset(deps) for name, _, deps in CONTROL_SPECS}
FIGURE_DEPS = {fig: frozenset(deps) for _, fig, deps in CONTROL_SPECS}
FULL_FIGURES = frozenset(
    fig for fig, deps in FIGURE_DEPS.items() if deps == ALL_PARAMETERS)

REJECTION_REASONS = ["outside_support", "retired_build", "unprobed_frame",
                     "unreported_dump", "unresolved_module"]

LEDGER_FIELDS = ["bytes_allocated", "bytes_uploaded", "capture_tick",
                 "component_key", "cost_ns", "effective_tier", "enter_ns",
                 "fault_signature", "frame_index", "host_zone", "module_tag",
                 "peak_ns", "samples", "source_dump", "symbol",
                 "tenant_segment", "unwind_ns", "within_budget"]

FIXED_OUTPUTS = ["normalized.jsonl", "summary.json", "product_summary.json",
                 "data_quality.json"]


# --------------------------------------------------------------------------
# Wire helpers
# --------------------------------------------------------------------------

class WireError(ValueError):
    """A candidate assignment cannot decode the export at all."""


def parse_radix(text, radix):
    """Read a numeral written in the alternate radix, 0-9 then a-z."""
    value = 0
    for ch in text:
        digit = int(ch) if ch.isdigit() else ord(ch) - ord("a") + 10
        if digit >= radix:
            raise WireError("digit %s does not fit radix %d" % (ch, radix))
        value = value * radix + digit
    return value


def frame_checksum(columns):
    """The frame check total: char codes of the eight preceding columns."""
    return sum(ord(ch) for ch in "|".join(columns[:8])) % CHECK_MODULUS


def rotate_left(text, n):
    n %= len(text)
    return text[n:] + text[:n]


def rotate_right(text, n):
    return rotate_left(text, len(text) - (n % len(text)))


# --------------------------------------------------------------------------
# Reporter dump export
# --------------------------------------------------------------------------

class Dump(object):
    __slots__ = ("ordinal", "name", "module", "count_field", "base_field",
                 "flags", "key", "checksum_field", "lines", "count",
                 "count_verified", "frames", "trailers", "attested")

    def __init__(self, ordinal, columns):
        self.ordinal = ordinal               # one-based ordinal in the export
        self.name = columns[1]
        self.module = columns[2]
        self.count_field = columns[3]
        self.base_field = columns[4]
        self.flags = int(columns[5], 16)
        self.key = int(columns[6])
        self.checksum_field = columns[7]
        self.lines = []
        self.count = 0
        self.count_verified = False
        self.frames = []
        self.trailers = []
        self.attested = None


def read_export(path):
    """Split a pipe-delimited dump export into dumps and their raw lines."""
    dumps = []
    with open(path, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.rstrip("\n")
            if not line.strip() or line.startswith("#"):
                continue
            columns = line.split("|")
            if columns[0] == "@":
                dumps.append(Dump(len(dumps) + 1, columns))
            elif dumps:
                dumps[-1].lines.append(columns)
            else:
                raise ValueError("frame line before any dump header")
    return dumps


def dump_count_candidates(dump):
    """Prefix lengths whose running frame-check sum equals the dump total.

    The count a dump's check total attests is a property of the shipped bytes
    alone, so it is worked out once per dump and cached.
    """
    if dump.attested is None:
        total = 0
        hits = []
        for index, line in enumerate(dump.lines):
            total = (total + frame_checksum(line)) % CHECK_MODULUS
            if "%04x" % total == dump.checksum_field:
                hits.append(index + 1)
        dump.attested = hits
    return dump.attested


def settle_frame_counts(dumps, params):
    """Apply the count-bias rule and the shipped dump check totals together."""
    for dump in dumps:
        available = len(dump.lines)
        count = int(dump.count_field)
        if dump.flags & DUMP_COUNT_BIAS:
            bias = (dump.ordinal * params["count_multiplier"]) % COUNT_BIAS_MODULUS
            count -= bias
        biased = [c for c in (count, count + COUNT_BIAS_MODULUS)
                  if 1 <= c <= available]
        attested = dump_count_candidates(dump)
        chosen = None
        for candidate in biased:
            if candidate in attested:
                chosen = candidate
                break
        if chosen is None:
            # Neither the rule's count nor its modulo-nine wrap is what the
            # dump's check total attests, so this assignment is not the one the
            # reporter wrote under.
            raise WireError("%s declared count is not attested" % dump.name)
        dump.count = chosen
        dump.count_verified = True
        dump.frames = dump.lines[:chosen]
        dump.trailers = dump.lines[chosen:]


def logical_frames(dump, params):
    """Undo the left rotation the dump wrote its fault frame lines under."""
    frames = dump.frames
    if dump.flags & DUMP_LINE_ROTATION:
        shift = (dump.key + params["rotation_offset"]) % dump.count
        frames = [frames[(i - shift) % dump.count] for i in range(dump.count)]
    return frames


def recover_bases(dumps, params):
    """Recover each dump's base: alternate radix, and the split low part."""
    bases = {}
    for dump in dumps:
        if dump.flags & DUMP_ALT_RADIX:
            high = parse_radix(dump.base_field, params["alternate_radix"])
        else:
            high = int(dump.base_field)
        if dump.flags & DUMP_SPLIT_BASE:
            if not dump.trailers:
                raise WireError("%s splits its base but holds no trailer"
                                % dump.name)
            base = (high << params["base_shift_width"]) | int(
                dump.trailers[0][0])
        else:
            base = high
        bases[dump.name] = base
    return bases


def recover_spans(dump, frames):
    """Undo the second-order-difference encoding of the resident span column."""
    raw = [int(line[3]) for line in frames]
    if not (dump.flags & DUMP_SPAN_DELTA):
        return raw
    spans = []
    for i, value in enumerate(raw):
        if i == 0:
            spans.append(value)
        elif i == 1:
            spans.append(spans[0] + value)
        else:
            spans.append(2 * spans[i - 1] - spans[i - 2] + value)
    return spans


def recover_entries(dump, frames, params):
    """Undo the entry-stamp accumulation on the wrapping clock."""
    modulus = params["entry_clock_modulus"]
    chained = bool(dump.flags & DUMP_ENTRY_DELTA)
    entries = []
    for i, line in enumerate(frames):
        written = int(line[1])
        if i == 0 or not chained:
            entries.append(written)
        else:
            entries.append((written - entries[-1]) % modulus)
    return entries


def decode_export(dumps, params, want):
    """Recover the export's fault frames.

    ``want`` names the figures to produce.  Every decoding step is skipped
    unless some wanted figure reads it, so a figure never observes a parameter
    outside its declared dependency set.
    """
    want = set(want)
    full = bool(want & FULL_FIGURES)
    use_bases = full or "base_total" in want
    use_order = full or bool(
        want & {"span_total", "entry_total", "unwind_total", "signature_total"})
    use_entries = full or "entry_total" in want
    use_spans = full or "span_total" in want or "unwind_total" in want
    use_unwind = full or "unwind_total" in want
    use_allocs = full or "alloc_total" in want
    use_signatures = full or "signature_total" in want

    settle_frame_counts(dumps, params)
    bases = recover_bases(dumps, params) if use_bases else {}

    records = []
    running_span = 0
    ring = [None] * params["ring_width"] if use_signatures else ()
    ring_pos = 0

    for dump in dumps:
        frames = logical_frames(dump, params) if use_order else dump.frames
        spans = recover_spans(dump, frames) if use_spans else None
        entries = (recover_entries(dump, frames, params) if use_entries
                   else None)
        first_entry = entries[0] if entries else 0

        for i, line in enumerate(frames):
            frame_flags = int(line[6], 16)
            symbol_field = line[7]
            if frame_flags & FRAME_INDEX_PLACEHOLDER:
                frame_index = int(symbol_field.rsplit(":", 1)[1])
            else:
                frame_index = int(line[0])
            record = {
                "source_dump": dump.name,
                "module_field": dump.module,
                "frame_index": frame_index,
                "symbol": symbol_field.rsplit(":", 1)[0],
            }
            if use_bases:
                record["dump_base"] = bases[dump.name]
            if use_entries:
                record["enter_ns"] = entries[i]
            if full:
                # The capture tick needs both a recovered base and a recovered
                # entry stamp, so only the whole-derivation figures read it.
                record["capture_tick"] = bases[dump.name] + (
                    (entries[i] - first_entry) % params["entry_clock_modulus"])
            if use_spans:
                record["cost_ns"] = spans[i]
            if use_unwind:
                written = int(line[2])
                if dump.flags & DUMP_UNWIND_DRIFT:
                    record["unwind_ns"] = (written
                                           - running_span % params["drift_period"])
                else:
                    record["unwind_ns"] = written
            if use_allocs:
                column = line[4]
                if frame_flags & FRAME_COARSE_ALLOCATION:
                    record["bytes_allocated"] = (int(column[1:])
                                                 * params["coarse_unit"])
                else:
                    record["bytes_allocated"] = int(column)
            if use_signatures:
                column = line[5]
                if frame_flags & FRAME_SIGNATURE_RING:
                    distance = int(column[1:])
                    signature = ring[(ring_pos - distance) % len(ring)]
                    if signature is None:
                        raise WireError("signature ring read before write")
                else:
                    signature = column
                    if frame_flags & FRAME_SIGNATURE_ROTATION:
                        signature = rotate_right(
                            column, frame_index + params["signature_offset"])
                record["fault_signature"] = signature
                ring[ring_pos] = signature
                ring_pos = (ring_pos + 1) % len(ring)
            if use_spans:
                running_span += spans[i]
            records.append(record)
    return records


def check_wire_totals(dumps):
    """Check the shipped frame and dump check totals before trusting a field.

    This is parameter-free: a dump's fault frame count is the prefix length
    whose running frame-check sum equals the dump's check total.
    """
    problems = []
    for dump in dumps:
        attested = dump_count_candidates(dump)
        if not attested:
            problems.append("%s dump check total" % dump.name)
        for count in attested or []:
            if not 1 <= count <= len(dump.lines):
                problems.append("%s declared count" % dump.name)
                continue
            for line in dump.lines[:count]:
                if "%04x" % frame_checksum(line) != line[8]:
                    problems.append("%s frame check total" % dump.name)
                    break
    return sorted(set(problems))


def check_decoded_counts(dumps):
    """Every dump's settled count must be the one its check total attests."""
    problems = []
    for dump in dumps:
        if not dump.count_verified:
            problems.append("%s dump check total" % dump.name)
        if dump.flags & DUMP_SPLIT_BASE and not dump.trailers:
            problems.append("%s split base" % dump.name)
    return sorted(set(problems))


# --------------------------------------------------------------------------
# Reference tables
# --------------------------------------------------------------------------

def read_csv_rows(path):
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def read_jsonl(path):
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


class Registry(object):
    """The build registry: effective intervals, renames and retirements."""

    def __init__(self, rows):
        self.rows = [(r["module_tag"], r["canonical_tag"], r["component_key"],
                      int(r["tier_index"]), int(r["effective_from_build"]),
                      int(r["effective_to_build"]), r["status"]) for r in rows]

    def resolve(self, tag, build_index):
        """Follow the canonical hand-offs; None when the tag does not resolve."""
        seen = set()
        current = tag
        while True:
            if current in seen:
                return None                    # a chain that repeats a tag
            seen.add(current)
            covering = [r for r in self.rows
                        if r[0] == current and r[4] <= build_index <= r[5]]
            if not covering:
                return None                    # covered by no row
            row = covering[0]
            if row[1] == row[0]:
                return {"module_tag": row[1], "component_key": row[2],
                        "declared_tier": row[3], "status": row[6]}
            current = row[1]


class Tables(object):
    """The five reference tables that govern every corpus alike."""

    def __init__(self, data_dir):
        self.registry = Registry(read_csv_rows(
            os.path.join(data_dir, "build_registry.csv")))
        self.budget = {}
        self.ladder_tiers = {}
        for row in read_csv_rows(os.path.join(data_dir, "severity_ladder.csv")):
            self.budget[(row["component_key"], int(row["tier_index"]))] = int(
                row["budget_ns"])
            self.ladder_tiers.setdefault(
                row["component_key"], set()).add(int(row["tier_index"]))
        self.windows = {}
        for row in read_csv_rows(os.path.join(data_dir, "support_windows.csv")):
            self.windows[row["component_key"]] = (int(row["open_tick"]),
                                                  int(row["close_tick"]))
        self.host_zone = {row["host_id"]: row["zone"] for row in read_csv_rows(
            os.path.join(data_dir, "host_zones.csv"))}
        self.tenant_segment = {
            row["tenant_class"]: row["tenant_segment"]
            for row in read_csv_rows(os.path.join(data_dir,
                                                  "tenant_classes.csv"))}
        self.zones = sorted(set(self.host_zone.values()))
        self.segments = sorted(set(self.tenant_segment.values()))

    def load_sides(self, data_dir, prefix):
        """Load the runtime probe and uplink exports for one corpus."""
        self.probe = {}
        for row in read_csv_rows(
                os.path.join(data_dir, prefix + "runtime_probe.csv")):
            self.probe[(row["module_tag"], int(row["frame_index"]))] = (
                int(row["samples"]), int(row["peak_ns"]))
        self.uplink = {}
        for row in read_jsonl(
                os.path.join(data_dir, prefix + "uplink_reports.jsonl")):
            self.uplink[(row["module_tag"], int(row["frame_index"]))] = row


# --------------------------------------------------------------------------
# Acceptance and quarantine settlement
# --------------------------------------------------------------------------

def accept_frames(records, tables):
    """Split the export's fault frames into a ledger and its rejection tally.

    A frame that breaks more than one acceptance condition is counted against
    each condition it breaks, and once in the rejected total.
    """
    accepted = []
    rejections = {reason: 0 for reason in REJECTION_REASONS}
    for record in records:
        build_index = record["capture_tick"] // TICKS_PER_BUILD_INDEX
        resolved = tables.registry.resolve(record["module_field"], build_index)
        if resolved is None:
            rejections["unresolved_module"] += 1
            continue
        if resolved["status"] != "supported":
            # A retired row does not hand on a still-carried module, so the
            # frame has no component, no probe row and no report to test.
            rejections["retired_build"] += 1
            continue
        record["module_tag"] = resolved["module_tag"]
        record["component_key"] = resolved["component_key"]
        record["declared_tier"] = resolved["declared_tier"]
        broken = False
        window = tables.windows.get(resolved["component_key"])
        if window is None or not (window[0] < record["capture_tick"]
                                  < window[1]):
            rejections["outside_support"] += 1
            broken = True
        key = (record["module_tag"], record["frame_index"])
        if key not in tables.probe:
            rejections["unprobed_frame"] += 1
            broken = True
        if key not in tables.uplink:
            rejections["unreported_dump"] += 1
            broken = True
        if broken:
            continue
        samples, peak = tables.probe[key]
        report = tables.uplink[key]
        record["samples"] = samples
        record["peak_ns"] = peak
        record["bytes_uploaded"] = int(report["bytes_uploaded"])
        record["host_zone"] = tables.host_zone[report["host_id"]]
        record["tenant_segment"] = tables.tenant_segment[
            report["tenant_class"]]
        accepted.append(record)
    accepted.sort(key=lambda r: r["capture_tick"])
    return accepted, rejections


def settle_quarantine(accepted, tables):
    """Demote effective tiers until no tiered module is mostly over budget."""
    modules = {}
    for record in accepted:
        module = modules.get(record["module_tag"])
        if module is None:
            module = modules[record["module_tag"]] = {
                "module_tag": record["module_tag"],
                "component_key": record["component_key"],
                "declared_tier": record["declared_tier"],
                "effective_tier": record["declared_tier"],
                "frames": [],
            }
        module["frames"].append(record)

    passes = 0
    while True:
        passes += 1
        envelopes = {}
        for component in {m["component_key"] for m in modules.values()}:
            in_service = [m for m in modules.values()
                          if m["component_key"] == component]
            lowest = min(tables.budget[(component, m["effective_tier"])]
                         for m in in_service)
            envelopes[component] = lowest * len(tables.ladder_tiers[component])
        demoted = False
        for module in modules.values():
            component = module["component_key"]
            own = tables.budget[(component, module["effective_tier"])]
            allowance = min(own, envelopes[component])
            module["allowance_ns"] = allowance
            over = sum(1 for f in module["frames"] if f["peak_ns"] > allowance)
            within = len(module["frames"]) - over
            if module["effective_tier"] > 0 and over > within:
                module["effective_tier"] -= 1
                demoted = True
        if not demoted:
            break

    quarantined = []
    for module in modules.values():
        component = module["component_key"]
        module["budget_ns"] = tables.budget[
            (component, module["effective_tier"])]
        over = sum(1 for f in module["frames"]
                   if f["peak_ns"] > module["allowance_ns"])
        module["quarantined"] = (module["effective_tier"] == 0
                                 and over > len(module["frames"]) - over)
        if module["quarantined"]:
            quarantined.append(module["module_tag"])
    return modules, passes, sorted(quarantined)


def derive(dumps, tables, params):
    """Run the whole derivation for one corpus under settled parameters."""
    records = decode_export(dumps, params, sorted(FULL_FIGURES))
    accepted, rejections = accept_frames(records, tables)
    modules, passes, quarantined = settle_quarantine(accepted, tables)
    for record in accepted:
        module = modules[record["module_tag"]]
        record["effective_tier"] = module["effective_tier"]
        record["within_budget"] = record["peak_ns"] <= module["allowance_ns"]
    return {"records": records, "accepted": accepted, "rejections": rejections,
            "modules": modules, "passes": passes, "quarantined": quarantined}


# --------------------------------------------------------------------------
# Control figures and the candidate search
# --------------------------------------------------------------------------

def _dump_base_total(records):
    bases = {}
    for record in records:
        bases.setdefault(record["source_dump"], record["dump_base"])
    return sum(bases.values())


SIMPLE_EXTRACTORS = {
    "frame_count": lambda records: len(records),
    "frame_index_total": lambda r: sum(x["frame_index"] for x in r),
    "base_total": _dump_base_total,
    "span_total": lambda r: sum(x["cost_ns"] for x in r),
    "entry_total": lambda r: sum(x["enter_ns"] for x in r),
    "unwind_total": lambda r: sum(x["unwind_ns"] for x in r),
    "alloc_total": lambda r: sum(x["bytes_allocated"] for x in r),
    "signature_total": lambda r: sum(int(x["fault_signature"], 16) for x in r)
                              % SIGNATURE_HEX_MODULUS,
}


def control_figures(dumps, tables, params, figures):
    """Compute the named figures for one corpus under one assignment.

    Each figure is decoded on its own, so a figure only ever observes the
    parameters its dependency set names.
    """
    out = {}
    derived = None
    for figure in figures:
        if figure in SIMPLE_EXTRACTORS:
            out[figure] = SIMPLE_EXTRACTORS[figure](
                decode_export(dumps, params, [figure]))
        else:
            if derived is None:
                derived = derive(dumps, tables, params)
            if figure == "accepted_count":
                out[figure] = len(derived["accepted"])
            elif figure == "quarantined_module_count":
                out[figure] = len(derived["quarantined"])
            elif figure == "upload_residual":
                out[figure] = (
                    sum(r["bytes_allocated"] for r in derived["accepted"])
                    - sum(r["bytes_uploaded"] for r in derived["accepted"]))
            else:
                raise KeyError(figure)
    return out


class Calibration(object):
    """The control slice and its published figures."""

    def __init__(self, data_dir):
        self.dumps = read_export(
            os.path.join(data_dir, "calibration_fault_frames.psv"))
        self.tables = Tables(data_dir)
        self.tables.load_sides(data_dir, "calibration_")
        with open(os.path.join(data_dir, "control-slice-figures.json"),
                  encoding="utf-8") as handle:
            self.published = json.load(handle)
        self.controls = [name for name in CONTROL_FIGURE
                         if name in self.published]
        self.evaluations = set()

    def matches(self, controls, params):
        """Recompute the named controls under one (possibly partial) assignment.

        An assignment that cannot decode the export at all is simply one that
        does not reproduce the figures.
        """
        figures = [CONTROL_FIGURE[name] for name in controls]
        self.evaluations.add(tuple(sorted(params.items())))
        try:
            produced = control_figures(self.dumps, self.tables, params,
                                       figures)
        except (WireError, ValueError, KeyError, IndexError):
            return False
        return all(produced[CONTROL_FIGURE[name]] == self.published[name]
                   for name in controls)

    def search(self, fixed, controls):
        """Every complete assignment under `fixed` reproducing `controls`.

        A branch is cut as soon as some control whose dependencies are all
        assigned fails to reproduce.
        """
        results = []

        def step(index, params, settled):
            if index == len(SEARCH_ORDER):
                pending = [c for c in controls if c not in settled]
                if not pending or self.matches(pending, params):
                    results.append(dict(params))
                return
            name = SEARCH_ORDER[index]
            values = [fixed[name]] if name in fixed else DOMAIN[name]
            for value in values:
                params[name] = value
                fresh = [c for c in controls
                         if c not in settled and name in CONTROL_DEPS[c]
                         and all(p in params for p in CONTROL_DEPS[c])]
                if fresh and not self.matches(fresh, params):
                    del params[name]
                    continue
                step(index + 1, params, settled | set(fresh))
                del params[name]

        step(0, {}, frozenset())
        return results


def calibrate(data_dir):
    """Settle the nine withheld wire parameters against the control slice."""
    calibration = Calibration(data_dir)
    if not calibration.controls:
        raise SystemExit("no published control figures to calibrate against")

    problems = check_wire_totals(calibration.dumps)
    if problems:
        raise SystemExit("export check totals disagree: %s" % ", ".join(problems))

    # Enumerate each control's whole candidate space, so every candidate value
    # in every parameter's domain is put to a verdict before anything is
    # selected.  A value is viable for a control when some assignment of that
    # control's remaining parameters carrying it reproduces the figure.
    viable = {}
    enumerable = set()
    for control in calibration.controls:
        dependencies = sorted(CONTROL_DEPS[control])
        space = 1
        for name in dependencies:
            space *= len(DOMAIN[name])
        if space > EXHAUSTIVE_LIMIT:
            continue                       # settled jointly, further down
        enumerable.add(control)
        for combo in product(*(DOMAIN[name] for name in dependencies)):
            params = dict(zip(dependencies, combo))
            if calibration.matches([control], params):
                for name, value in zip(dependencies, combo):
                    viable.setdefault((control, name), set()).add(value)

    solutions = calibration.search({}, calibration.controls)
    if len(solutions) != 1:
        raise SystemExit("expected exactly one assignment of the wire "
                         "parameters, found %d" % len(solutions))
    selected = solutions[0]
    settle_frame_counts(calibration.dumps, selected)
    problems = check_decoded_counts(calibration.dumps)
    if problems:
        raise SystemExit("export check totals disagree: %s" % ", ".join(problems))

    # Verdict on every candidate: the first control figure that no assignment
    # carrying the value can reproduce is the one that falsified it.
    verdicts = {name: {} for name in PARAMETER_ORDER}
    for name in PARAMETER_ORDER:
        for value in DOMAIN[name]:
            if value == selected[name]:
                verdicts[name][value] = None
                continue
            falsified = [control for control in sorted(enumerable)
                         if name in CONTROL_DEPS[control]
                         and value not in viable.get((control, name), set())]
            if not falsified:
                falsified = joint_falsifiers(calibration, name, value,
                                             enumerable)
            verdicts[name][value] = falsified[0] if falsified else None

    parameters = []
    for name in PARAMETER_ORDER:
        rejected = []
        counts = {}
        for value in DOMAIN[name]:
            control = verdicts[name][value]
            if control is None:
                continue
            rejected.append({"value": value, "control": control})
            counts[control] = counts.get(control, 0) + 1
        others = [v for v in DOMAIN[name] if v != selected[name]]
        pinning = [c for c in calibration.controls
                   if counts.get(c) == len(others)]
        parameters.append({
            "name": PARAMETER_LABEL[name],
            "candidates": len(DOMAIN[name]),
            "control": pinning[0] if pinning else (
                max(sorted(counts), key=lambda c: counts[c])
                if counts else calibration.controls[0]),
            "selected": selected[name],
            "rejected": rejected,
        })

    figures = [CONTROL_FIGURE[name] for name in calibration.controls]
    calibration.evaluations.add(tuple(sorted(selected.items())))
    reproduced = control_figures(calibration.dumps, calibration.tables,
                                 selected, figures)
    comparison = [{"control": control,
                   "published": calibration.published[control],
                   "reproduced": reproduced[CONTROL_FIGURE[control]]}
                  for control in calibration.controls]

    return selected, {"evaluations": len(calibration.evaluations),
                      "parameters": parameters}, comparison


def joint_falsifiers(calibration, name, value, enumerable):
    """Controls a candidate only falls foul of jointly with the others."""
    carriers = calibration.search(
        {name: value}, [c for c in calibration.controls if c in enumerable])
    falsified = []
    for control in calibration.controls:
        if control in enumerable:
            continue
        if not any(calibration.matches([control], params)
                   for params in carriers):
            falsified.append(control)
    if not falsified and not carriers:
        falsified = [c for c in sorted(enumerable)
                     if name in CONTROL_DEPS[c]][:1]
    return falsified


# --------------------------------------------------------------------------
# Roll-ups
# --------------------------------------------------------------------------

def group_sum(rows, key, value):
    out = {}
    for row in rows:
        out[row[key]] = out.get(row[key], 0) + row[value]
    return out


def rollup(rows):
    return {
        "accepted_count": len(rows),
        "total_bytes_uploaded": sum(r["bytes_uploaded"] for r in rows),
        "total_cost_ns": sum(r["cost_ns"] for r in rows),
        "within_budget_count": sum(1 for r in rows if r["within_budget"]),
    }


def zone_report(zone, accepted):
    rows = [r for r in accepted if r["host_zone"] == zone]
    report = rollup(rows)
    report["by_component"] = group_sum(rows, "component_key", "bytes_uploaded")
    report["by_tenant_segment"] = group_sum(rows, "tenant_segment",
                                            "bytes_uploaded")
    report["modules"] = sorted({r["module_tag"] for r in rows})
    report["zone"] = zone
    return report


def segment_report(segment, accepted):
    rows = [r for r in accepted if r["tenant_segment"] == segment]
    report = rollup(rows)
    report["by_component"] = group_sum(rows, "component_key", "bytes_uploaded")
    report["by_zone"] = group_sum(rows, "host_zone", "bytes_uploaded")
    report["modules"] = sorted({r["module_tag"] for r in rows})
    report["tenant_segment"] = segment
    return report


def summary_report(accepted, rejected_count):
    tiers = {}
    for row in accepted:
        key = str(row["effective_tier"])
        tiers[key] = tiers.get(key, 0) + 1
    return {
        "accepted_count": len(accepted),
        "by_component": group_sum(accepted, "component_key", "bytes_uploaded"),
        "by_effective_tier": tiers,
        "by_tenant_segment": group_sum(accepted, "tenant_segment",
                                       "bytes_uploaded"),
        "by_zone": group_sum(accepted, "host_zone", "bytes_uploaded"),
        "rejected_count": rejected_count,
        "total_bytes_allocated": sum(r["bytes_allocated"] for r in accepted),
        "total_bytes_uploaded": sum(r["bytes_uploaded"] for r in accepted),
        "total_cost_ns": sum(r["cost_ns"] for r in accepted),
        "within_budget_count": sum(1 for r in accepted if r["within_budget"]),
    }


def product_report(modules):
    rows = []
    for tag in sorted(modules):
        module = modules[tag]
        frames = module["frames"]
        rows.append({
            "accepted_count": len(frames),
            "allowance_ns": module["allowance_ns"],
            "budget_ns": module["budget_ns"],
            "component_key": module["component_key"],
            "declared_tier": module["declared_tier"],
            "effective_tier": module["effective_tier"],
            "module_tag": tag,
            "net_samples": sum(f["samples"] for f in frames),
            "quarantined": module["quarantined"],
            "total_bytes_uploaded": sum(f["bytes_uploaded"] for f in frames),
            "within_budget_count": sum(
                1 for f in frames if f["within_budget"]),
        })
    return {"modules": rows}


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def write_json(path, payload):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True, indent=2))
        handle.write("\n")


def write_ledger(path, accepted):
    with open(path, "w", encoding="utf-8") as handle:
        for record in accepted:
            row = {field: record[field] for field in LEDGER_FIELDS}
            handle.write(json.dumps(row, sort_keys=True))
            handle.write("\n")


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main(argv):
    data_dir = argv[1] if len(argv) > 1 else "/app/data"
    out_dir = argv[2] if len(argv) > 2 else "/app/out"
    if not os.path.isdir(data_dir):
        raise SystemExit("data directory not found: %s" % data_dir)

    # Settle the withheld wire parameters before computing any figure for the
    # incident corpus.
    params, candidate_search, comparison = calibrate(data_dir)

    dumps = read_export(os.path.join(data_dir, "fault_frames.psv"))
    tables = Tables(data_dir)
    tables.load_sides(data_dir, "")
    problems = check_wire_totals(dumps)
    settle_frame_counts(dumps, params)
    problems += check_decoded_counts(dumps)
    if problems:
        raise SystemExit("export check totals disagree: %s" % ", ".join(problems))

    # With the parameters settled, run the whole derivation from an empty
    # output directory: every report this run is about to write is cleared
    # first, and nothing outside the given directory is touched.
    outputs = (list(FIXED_OUTPUTS)
               + ["region_%s.json" % z for z in tables.zones]
               + ["segment_%s.json" % s for s in tables.segments])
    os.makedirs(out_dir, exist_ok=True)
    for name in outputs:
        path = os.path.join(out_dir, name)
        if os.path.isfile(path):
            os.remove(path)

    result = derive(dumps, tables, params)
    accepted = result["accepted"]
    records = result["records"]
    rejected_count = len(records) - len(accepted)
    if len({r["capture_tick"] for r in accepted}) != len(accepted):
        raise SystemExit("two ledger rows share a capture tick")

    write_ledger(os.path.join(out_dir, "normalized.jsonl"), accepted)
    write_json(os.path.join(out_dir, "summary.json"),
               summary_report(accepted, rejected_count))
    write_json(os.path.join(out_dir, "product_summary.json"),
               product_report(result["modules"]))
    for zone in tables.zones:
        write_json(os.path.join(out_dir, "region_%s.json" % zone),
                   zone_report(zone, accepted))
    for segment in tables.segments:
        write_json(os.path.join(out_dir, "segment_%s.json" % segment),
                   segment_report(segment, accepted))

    allocated = sum(r["bytes_allocated"] for r in accepted)
    uploaded = sum(r["bytes_uploaded"] for r in accepted)
    write_json(os.path.join(out_dir, "data_quality.json"), {
        "accepted_count": len(accepted),
        "calibration_comparison": comparison,
        "candidate_search": candidate_search,
        "frame_count": len(records),
        "reconciliation": {"allocated_bytes": allocated,
                           "residual_bytes": allocated - uploaded,
                           "uploaded_bytes": uploaded},
        "rejections": {reason: result["rejections"][reason]
                       for reason in REJECTION_REASONS},
        "settlement": {"passes": result["passes"],
                       "quarantined_modules": result["quarantined"]},
    })
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
