#!/usr/bin/env python3
"""Reconstruct an incident window's fault ledger and its roll-ups.

Usage:  python3 transform.py [DATA_DIR] [OUT_DIR]

Reads a crash-reporter dump export, a runtime probe export, an uplink export
and five reference tables from DATA_DIR; settles the crash reporter's nine
withheld wire parameters against the published control-slice figures; then
writes the fault ledger, the incident and per-module roll-ups, the per-zone
and per-segment reports and the reconciliation record into OUT_DIR.

Standard library only.  All clock, byte and residency arithmetic is carried in
Python integers.
"""

import csv
import json
import os
import sys
from collections import defaultdict

# --------------------------------------------------------------------------
# constants fixed by the written rules
# --------------------------------------------------------------------------

CHECK_MODULUS = 65536
TICKS_PER_BUILD = 900000000          # 1000 ticks/us * 900000 us
SIGNATURE_WIDTH = 16
SIGNATURE_ALPHABET = set("0123456789abcdef")
RADIX_DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"
COUNT_BIAS_MODULUS = 9
CTRL_G_MODULUS = 1000000007

DUMP_SECOND_ORDER = 0x001
DUMP_ENTRY_CHAIN = 0x002
DUMP_UNWIND_DRIFT = 0x004
DUMP_ROTATED = 0x008
DUMP_COUNT_BIAS = 0x010
DUMP_ALT_RADIX = 0x020
DUMP_ALIAS = 0x100
DUMP_SPLIT_BASE = 0x800

FRAME_PLACEHOLDER_INDEX = 0x040
FRAME_SIG_ROTATED = 0x080
FRAME_COARSE_ALLOC = 0x200
FRAME_SIG_RING = 0x400

REJECTION_KEYS = ("outside_support", "retired_build", "unprobed_frame",
                  "unreported_dump", "unresolved_module")

LEDGER_FIELDS = ("bytes_allocated", "bytes_uploaded", "capture_tick",
                 "component_key", "cost_ns", "effective_tier", "enter_ns",
                 "fault_signature", "frame_index", "host_zone", "module_tag",
                 "peak_ns", "samples", "source_dump", "symbol",
                 "tenant_segment", "unwind_ns", "within_budget")

# The nine withheld wire parameters and the candidate domain each is published
# with.  Order is the settlement order: every parameter is settled only after
# the parameters its decoding rule depends on.
PARAMETERS = (
    ("count_multiplier", tuple(range(0, 9))),
    ("rotation_offset", tuple(range(0, 8))),
    ("alternate_radix", tuple(range(11, 36))),
    ("base_shift_width", (8, 10, 12, 16)),
    ("entry_clock_modulus", tuple(1 << e for e in range(22, 35))),
    ("drift_period", (48, 96, 192, 384, 768)),
    ("coarse_unit", (64, 128, 256, 512, 1024)),
    ("ring_width", (4, 8, 12, 16)),
    ("signature_offset", tuple(range(0, 8))),
)
PARAMETER_DOMAINS = dict(PARAMETERS)

# Groups of parameters settled together, with the control figure that bears on
# them.  A group is searched exhaustively once every earlier group is settled.
SEARCH_GROUPS = (
    (("count_multiplier",), "CTRL-A"),
    (("rotation_offset",), "CTRL-C"),
    (("alternate_radix", "base_shift_width"), "CTRL-B"),
    (("entry_clock_modulus",), "CTRL-D"),
    (("drift_period",), "CTRL-E"),
    (("coarse_unit",), "CTRL-F"),
    (("ring_width", "signature_offset"), "CTRL-G"),
)

# Which parameters each published control figure is a function of.  A control
# is only evaluated once every parameter it depends on has been assigned.
ALL_PARAMETERS = tuple(name for name, _ in PARAMETERS)
CONTROL_DEPENDENCIES = {
    "CTRL-A": ("count_multiplier",),
    "CTRL-B": ("count_multiplier", "alternate_radix", "base_shift_width"),
    "CTRL-C": ("count_multiplier", "rotation_offset"),
    "CTRL-D": ("count_multiplier", "rotation_offset", "entry_clock_modulus"),
    "CTRL-E": ("count_multiplier", "rotation_offset", "drift_period"),
    "CTRL-F": ("count_multiplier", "coarse_unit"),
    "CTRL-G": ("count_multiplier", "rotation_offset", "ring_width",
               "signature_offset"),
    "CTRL-H": ("count_multiplier",),
    "CTRL-I": ALL_PARAMETERS,
    "CTRL-J": ALL_PARAMETERS,
    "CTRL-K": ALL_PARAMETERS,
}
CONTROL_ORDER = ("CTRL-A", "CTRL-B", "CTRL-C", "CTRL-D", "CTRL-E", "CTRL-F",
                 "CTRL-G", "CTRL-H", "CTRL-I", "CTRL-J", "CTRL-K")


class WireError(Exception):
    """A candidate parameter set contradicts the shape of the export."""


# --------------------------------------------------------------------------
# reference tables
# --------------------------------------------------------------------------

def read_csv_rows(path):
    with open(path, newline="") as handle:
        return list(csv.DictReader(handle))


class Reference(object):
    """The five tables that govern every corpus alike."""

    def __init__(self, data_dir):
        self.registry = defaultdict(list)
        for row in read_csv_rows(os.path.join(data_dir, "build_registry.csv")):
            self.registry[row["module_tag"]].append(
                (int(row["effective_from_build"]),
                 int(row["effective_to_build"]), row))

        self.ladder = defaultdict(dict)
        for row in read_csv_rows(os.path.join(data_dir, "severity_ladder.csv")):
            self.ladder[row["component_key"]][int(row["tier_index"])] = \
                int(row["budget_ns"])

        self.windows = {}
        for row in read_csv_rows(os.path.join(data_dir, "support_windows.csv")):
            self.windows[row["component_key"]] = (int(row["open_tick"]),
                                                  int(row["close_tick"]))

        self.host_zone = {}
        for row in read_csv_rows(os.path.join(data_dir, "host_zones.csv")):
            self.host_zone[row["host_id"]] = row["zone"]

        self.tenant_segment = {}
        for row in read_csv_rows(os.path.join(data_dir, "tenant_classes.csv")):
            self.tenant_segment[row["tenant_class"]] = row["tenant_segment"]

        self.zones = sorted(set(self.host_zone.values()))
        self.segments = sorted(set(self.tenant_segment.values()))

    def resolve(self, tag, build_index):
        """Walk the registry to the terminal row covering ``build_index``."""
        seen = set()
        current = tag
        while True:
            if current in seen:            # a chain arriving twice at a tag
                return None
            seen.add(current)
            row = None
            for low, high, candidate in self.registry.get(current, ()):
                if low <= build_index <= high:
                    row = candidate
                    break
            if row is None:
                return None
            if row["canonical_tag"] == current:
                return row
            current = row["canonical_tag"]


# --------------------------------------------------------------------------
# the crash reporter export
# --------------------------------------------------------------------------

def parse_radix(text, radix):
    value = 0
    for char in text:
        digit = RADIX_DIGITS.find(char)
        if digit < 0 or digit >= radix:
            raise WireError("digit %r outside radix %d" % (char, radix))
        value = value * radix + digit
    return value


def column_check_total(columns):
    return sum(ord(char) for char in "|".join(columns)) % CHECK_MODULUS


def rotate_right(text, places):
    places %= len(text)
    if places == 0:
        return text
    return text[-places:] + text[:-places]


def read_export(path):
    """Split the pipe-delimited export into dumps, headers and body lines."""
    dumps = []
    current = None
    with open(path) as handle:
        for raw in handle:
            line = raw.rstrip("\n")
            if not line.strip() or line.startswith("#"):
                continue
            columns = line.split("|")
            if columns[0] == "@":
                current = {
                    "dump_id": columns[1],
                    "module_tag": columns[2],
                    "declared_count": int(columns[3]),
                    "base_field": columns[4],
                    "flags": int(columns[5], 16),
                    "key": int(columns[6]),
                    "check_total": int(columns[7], 16),
                    "lines": [],
                }
                dumps.append(current)
            else:
                if current is None:
                    raise WireError("body line before any dump header")
                current["lines"].append(columns)
    return dumps


class Export(object):
    """Staged decoder.  Every stage takes exactly the parameters its rule
    names, so a control figure can be reproduced before the parameters it does
    not depend on have been settled."""

    def __init__(self, path):
        self.dumps = read_export(path)
        self._split = {}
        self._ordered = {}
        self._spans = {}

    # -- stage 1: which body lines are fault frames -------------------------
    def split(self, count_multiplier):
        cached = self._split.get(count_multiplier)
        if cached is not None:
            if isinstance(cached, WireError):
                raise cached
            return cached
        try:
            parts = self._do_split(count_multiplier)
        except WireError as exc:
            self._split[count_multiplier] = exc
            raise
        self._split[count_multiplier] = parts
        return parts

    def _do_split(self, count_multiplier):
        parts = []
        for ordinal, dump in enumerate(self.dumps, start=1):
            count = dump["declared_count"]
            if dump["flags"] & DUMP_COUNT_BIAS:
                count -= (ordinal * count_multiplier) % COUNT_BIAS_MODULUS
            if count < 1 or count > len(dump["lines"]):
                raise WireError("dump %s: fault frame count %d out of range"
                                % (dump["dump_id"], count))
            frames = dump["lines"][:count]
            trailers = dump["lines"][count:]
            if (dump["flags"] & DUMP_SPLIT_BASE) and not trailers:
                raise WireError("dump %s: split base without a trailer line"
                                % dump["dump_id"])
            # Rule: check the shipped dump and frame check totals against the
            # columns they cover before trusting any decoded field.
            running = 0
            for columns in frames:
                if len(columns) != 9:
                    raise WireError("dump %s: fault frame is not nine columns"
                                    % dump["dump_id"])
                stated = int(columns[8], 16)
                if column_check_total(columns[:8]) != stated:
                    raise WireError("dump %s: frame check total disagrees"
                                    % dump["dump_id"])
                running += stated
            if running % CHECK_MODULUS != dump["check_total"]:
                raise WireError("dump %s: dump check total disagrees"
                                % dump["dump_id"])
            parts.append({"dump": dump, "frames": frames,
                          "trailers": trailers})
        return parts

    # -- stage 2: logical order --------------------------------------------
    def ordered(self, count_multiplier, rotation_offset):
        key = (count_multiplier, rotation_offset)
        if key in self._ordered:
            return self._ordered[key]
        result = []
        for part in self.split(count_multiplier):
            frames = part["frames"]
            dump = part["dump"]
            if dump["flags"] & DUMP_ROTATED:
                places = (dump["key"] + rotation_offset) % len(frames)
                logical = [frames[(i - places) % len(frames)]
                           for i in range(len(frames))]
            else:
                logical = list(frames)
            result.append({"dump": dump, "logical": logical,
                           "trailers": part["trailers"]})
        self._ordered[key] = result
        return result

    # -- stage 3: resident spans -------------------------------------------
    def spans(self, count_multiplier, rotation_offset):
        key = (count_multiplier, rotation_offset)
        if key in self._spans:
            return self._spans[key]
        result = []
        for part in self.ordered(count_multiplier, rotation_offset):
            written = [int(columns[3]) for columns in part["logical"]]
            if part["dump"]["flags"] & DUMP_SECOND_ORDER:
                spans = []
                for i, value in enumerate(written):
                    if i == 0:
                        spans.append(value)
                    elif i == 1:
                        spans.append(spans[0] + value)
                    else:
                        spans.append(value + 2 * spans[i - 1] - spans[i - 2])
            else:
                spans = written
            result.append(spans)
        self._spans[key] = result
        return result

    # -- stage 4: entry stamps ---------------------------------------------
    def entries(self, count_multiplier, rotation_offset, modulus):
        result = []
        for part in self.ordered(count_multiplier, rotation_offset):
            written = [int(columns[1]) for columns in part["logical"]]
            if part["dump"]["flags"] & DUMP_ENTRY_CHAIN:
                stamps = [written[0]]
                for i in range(1, len(written)):
                    stamps.append((written[i] - stamps[i - 1]) % modulus)
            else:
                stamps = written
            result.append(stamps)
        return result

    # -- stage 5: unwind stamps --------------------------------------------
    def unwinds(self, count_multiplier, rotation_offset, drift_period):
        parts = self.ordered(count_multiplier, rotation_offset)
        spans = self.spans(count_multiplier, rotation_offset)
        result = []
        running = 0                     # advances over the export as a whole
        for part, dump_spans in zip(parts, spans):
            drifted = bool(part["dump"]["flags"] & DUMP_UNWIND_DRIFT)
            stamps = []
            for i, columns in enumerate(part["logical"]):
                value = int(columns[2])
                if drifted:
                    value -= running % drift_period
                stamps.append(value)
                running += dump_spans[i]
            result.append(stamps)
        return result

    # -- stage 6: dump bases -----------------------------------------------
    def bases(self, count_multiplier, alternate_radix, base_shift_width):
        result = []
        for part in self.split(count_multiplier):
            dump = part["dump"]
            field = dump["base_field"]
            if dump["flags"] & DUMP_ALT_RADIX:
                number = parse_radix(field, alternate_radix)
            else:
                number = int(field)
            if dump["flags"] & DUMP_SPLIT_BASE:
                low = int(part["trailers"][0][0])
                if low < 0 or low >= (1 << base_shift_width):
                    raise WireError("dump %s: low base part does not fit"
                                    % dump["dump_id"])
                base = (number << base_shift_width) | low
            else:
                base = number
            if base < 0:
                raise WireError("dump %s: negative dump base" % dump["dump_id"])
            result.append(base)
        return result

    # -- stage 7: authoritative frame indices and symbol kinds --------------
    def identities(self, count_multiplier, rotation_offset):
        result = []
        for part in self.ordered(count_multiplier, rotation_offset):
            entries = []
            for columns in part["logical"]:
                frame_flags = int(columns[6], 16)
                kind, _, tail = columns[7].rpartition(":")
                if frame_flags & FRAME_PLACEHOLDER_INDEX:
                    index = int(tail)
                else:
                    index = int(columns[0])
                entries.append((index, kind))
            result.append(entries)
        return result

    def index_total(self, count_multiplier):
        """Sum of authoritative frame indices; independent of line order."""
        total = 0
        for part in self.split(count_multiplier):
            for columns in part["frames"]:
                frame_flags = int(columns[6], 16)
                if frame_flags & FRAME_PLACEHOLDER_INDEX:
                    total += int(columns[7].rpartition(":")[2])
                else:
                    total += int(columns[0])
        return total

    # -- stage 8: allocated byte counts ------------------------------------
    def allocations(self, count_multiplier, coarse_unit):
        result = []
        for part in self.split(count_multiplier):
            counts = []
            for columns in part["frames"]:
                frame_flags = int(columns[6], 16)
                field = columns[4]
                if frame_flags & FRAME_COARSE_ALLOC:
                    if not field.startswith("~"):
                        raise WireError("coarse allocation without a tilde")
                    counts.append(int(field[1:]) * coarse_unit)
                else:
                    counts.append(int(field))
            result.append(counts)
        return result

    # -- stage 9: fault signatures -----------------------------------------
    def signatures(self, count_multiplier, rotation_offset, ring_width,
                   signature_offset):
        parts = self.ordered(count_multiplier, rotation_offset)
        identities = self.identities(count_multiplier, rotation_offset)
        ring = [None] * ring_width
        position = 0                    # advances over the export as a whole
        result = []
        for part, dump_identities in zip(parts, identities):
            signatures = []
            for i, columns in enumerate(part["logical"]):
                frame_flags = int(columns[6], 16)
                field = columns[5]
                if frame_flags & FRAME_SIG_RING:
                    if not field.startswith("^"):
                        raise WireError("ring reference without a caret")
                    distance = int(field[1:])
                    signature = ring[(position - distance) % ring_width]
                    if signature is None:
                        raise WireError("ring reference reaches an empty slot")
                else:
                    signature = field
                    if frame_flags & FRAME_SIG_ROTATED:
                        places = (dump_identities[i][0] + signature_offset) \
                            % SIGNATURE_WIDTH
                        signature = rotate_right(signature, places)
                if len(signature) != SIGNATURE_WIDTH or \
                        not set(signature) <= SIGNATURE_ALPHABET:
                    raise WireError("recovered signature is not sixteen hex "
                                    "characters")
                ring[position % ring_width] = signature
                position += 1
                signatures.append(signature)
            result.append(signatures)
        return result

    # -- assembly ------------------------------------------------------------
    def frames(self, params):
        """Every fault frame the export carries, in logical export order."""
        cm = params["count_multiplier"]
        ro = params["rotation_offset"]
        parts = self.ordered(cm, ro)
        spans = self.spans(cm, ro)
        entries = self.entries(cm, ro, params["entry_clock_modulus"])
        unwinds = self.unwinds(cm, ro, params["drift_period"])
        bases = self.bases(cm, params["alternate_radix"],
                           params["base_shift_width"])
        identities = self.identities(cm, ro)
        allocations = self.allocations(cm, params["coarse_unit"])
        signatures = self.signatures(cm, ro, params["ring_width"],
                                     params["signature_offset"])
        modulus = params["entry_clock_modulus"]
        out = []
        for d, part in enumerate(parts):
            base = bases[d]
            first_entry = entries[d][0]
            # allocations are read in written order; re-key them by line
            alloc_by_line = {}
            for columns, value in zip(self.split(cm)[d]["frames"],
                                      allocations[d]):
                alloc_by_line[tuple(columns)] = value
            for i, columns in enumerate(part["logical"]):
                index, kind = identities[d][i]
                out.append({
                    "source_dump": part["dump"]["dump_id"],
                    "dump_module_tag": part["dump"]["module_tag"],
                    "frame_index": index,
                    "symbol": kind,
                    "enter_ns": entries[d][i],
                    "unwind_ns": unwinds[d][i],
                    "cost_ns": spans[d][i],
                    "bytes_allocated": alloc_by_line[tuple(columns)],
                    "fault_signature": signatures[d][i],
                    "capture_tick": base +
                    ((entries[d][i] - first_entry) % modulus),
                })
        return out


# --------------------------------------------------------------------------
# the side exports
# --------------------------------------------------------------------------

def read_side_exports(data_dir, prefix):
    probe = {}
    with open(os.path.join(data_dir, prefix + "runtime_probe.csv"),
              newline="") as handle:
        for row in csv.DictReader(handle):
            probe[(row["module_tag"], int(row["frame_index"]))] = \
                (int(row["samples"]), int(row["peak_ns"]))
    uplink = {}
    with open(os.path.join(data_dir, prefix + "uplink_reports.jsonl")) as handle:
        for line in handle:
            if not line.strip():
                continue
            record = json.loads(line)
            uplink[(record["module_tag"], int(record["frame_index"]))] = record
    return probe, uplink


# --------------------------------------------------------------------------
# ledger, settlement and roll-ups
# --------------------------------------------------------------------------

def build_ledger(frames, reference, probe, uplink):
    rejections = {key: 0 for key in REJECTION_KEYS}
    accepted = []
    rejected_frames = 0
    for frame in frames:
        reasons = []
        build_index = frame["capture_tick"] // TICKS_PER_BUILD
        row = reference.resolve(frame["dump_module_tag"], build_index)
        record = None
        samples = peak = None
        if row is None:
            reasons.append("unresolved_module")
        elif row["status"] != "supported":
            reasons.append("retired_build")
        else:
            component = row["component_key"]
            open_tick, close_tick = reference.windows[component]
            if not (open_tick < frame["capture_tick"] < close_tick):
                reasons.append("outside_support")
            key = (row["canonical_tag"], frame["frame_index"])
            if key in probe:
                samples, peak = probe[key]
            else:
                reasons.append("unprobed_frame")
            if key in uplink:
                record = uplink[key]
            else:
                reasons.append("unreported_dump")
        if reasons:
            rejected_frames += 1
            for reason in reasons:
                rejections[reason] += 1
            continue
        row_out = dict(frame)
        row_out["module_tag"] = row["canonical_tag"]
        row_out["component_key"] = row["component_key"]
        row_out["declared_tier"] = int(row["tier_index"])
        row_out["samples"] = samples
        row_out["peak_ns"] = peak
        row_out["bytes_uploaded"] = int(record["bytes_uploaded"])
        row_out["host_zone"] = reference.host_zone[record["host_id"]]
        row_out["tenant_segment"] = \
            reference.tenant_segment[record["tenant_class"]]
        accepted.append(row_out)
    accepted.sort(key=lambda item: item["capture_tick"])
    return accepted, rejections, rejected_frames


def settle(accepted, reference):
    """Demote effective tiers to the single highest assignment under which no
    module above tier zero counts more frames over budget than within it."""
    modules = {}
    for row in accepted:
        entry = modules.get(row["module_tag"])
        if entry is None:
            entry = modules[row["module_tag"]] = {
                "component_key": row["component_key"],
                "declared_tier": row["declared_tier"],
                "effective_tier": row["declared_tier"],
                "frames": [],
            }
        entry["frames"].append(row)

    by_component = defaultdict(list)
    for tag, entry in modules.items():
        by_component[entry["component_key"]].append(tag)

    def allowances():
        envelope = {}
        for component, tags in by_component.items():
            ladder = reference.ladder[component]
            lowest = min(ladder[modules[tag]["effective_tier"]] for tag in tags)
            envelope[component] = lowest * len(ladder)
        return {
            tag: min(reference.ladder[entry["component_key"]]
                     [entry["effective_tier"]],
                     envelope[entry["component_key"]])
            for tag, entry in modules.items()
        }

    def tally(tag, allowance):
        frames = modules[tag]["frames"]
        within = sum(1 for f in frames if f["peak_ns"] <= allowance)
        return within, len(frames) - within

    passes = 0
    while True:
        passes += 1                      # the pass that finds nothing counts
        allowance = allowances()
        demote = []
        for tag, entry in modules.items():
            within, over = tally(tag, allowance[tag])
            if entry["effective_tier"] > 0 and over > within:
                demote.append(tag)
        if not demote:
            break
        for tag in demote:
            modules[tag]["effective_tier"] -= 1

    allowance = allowances()
    quarantined = []
    for tag, entry in modules.items():
        within, over = tally(tag, allowance[tag])
        entry["allowance_ns"] = allowance[tag]
        entry["budget_ns"] = \
            reference.ladder[entry["component_key"]][entry["effective_tier"]]
        entry["quarantined"] = entry["effective_tier"] == 0 and over > within
        if entry["quarantined"]:
            quarantined.append(tag)
    for row in accepted:
        entry = modules[row["module_tag"]]
        row["effective_tier"] = entry["effective_tier"]
        row["within_budget"] = row["peak_ns"] <= entry["allowance_ns"]
    return modules, passes, sorted(quarantined)


def derive(export, params, reference, probe, uplink):
    frames = export.frames(params)
    accepted, rejections, rejected = build_ledger(frames, reference, probe,
                                                  uplink)
    modules, passes, quarantined = settle(accepted, reference)
    return {
        "frames": frames, "accepted": accepted, "rejections": rejections,
        "rejected": rejected, "modules": modules, "passes": passes,
        "quarantined": quarantined,
    }


# --------------------------------------------------------------------------
# reproducing the published control figures
# --------------------------------------------------------------------------

def control_value(name, export, params, reference, probe, uplink, cache):
    if name == "CTRL-A":
        return sum(len(p["frames"])
                   for p in export.split(params["count_multiplier"]))
    if name == "CTRL-B":
        return sum(export.bases(params["count_multiplier"],
                                params["alternate_radix"],
                                params["base_shift_width"]))
    if name == "CTRL-C":
        return sum(sum(s) for s in export.spans(params["count_multiplier"],
                                                params["rotation_offset"]))
    if name == "CTRL-D":
        return sum(sum(s) for s in export.entries(
            params["count_multiplier"], params["rotation_offset"],
            params["entry_clock_modulus"]))
    if name == "CTRL-E":
        return sum(sum(s) for s in export.unwinds(
            params["count_multiplier"], params["rotation_offset"],
            params["drift_period"]))
    if name == "CTRL-F":
        return sum(sum(a) for a in export.allocations(
            params["count_multiplier"], params["coarse_unit"]))
    if name == "CTRL-G":
        total = 0
        for dump in export.signatures(params["count_multiplier"],
                                      params["rotation_offset"],
                                      params["ring_width"],
                                      params["signature_offset"]):
            for signature in dump:
                total += int(signature, 16)
        return total % CTRL_G_MODULUS
    if name == "CTRL-H":
        return export.index_total(params["count_multiplier"])
    key = tuple(sorted(params.items()))
    result = cache.get(key)
    if result is None:
        result = derive(export, params, reference, probe, uplink)
        cache.clear()
        cache[key] = result
    if name == "CTRL-I":
        return len(result["accepted"])
    if name == "CTRL-J":
        return len(result["quarantined"])
    if name == "CTRL-K":
        return (sum(r["bytes_allocated"] for r in result["accepted"]) -
                sum(r["bytes_uploaded"] for r in result["accepted"]))
    raise KeyError(name)


class Calibrator(object):
    """Settles the nine withheld wire parameters against the control slice."""

    def __init__(self, data_dir, reference):
        self.export = Export(os.path.join(data_dir,
                                          "calibration_fault_frames.psv"))
        self.probe, self.uplink = read_side_exports(data_dir, "calibration_")
        self.reference = reference
        with open(os.path.join(data_dir, "control-slice-figures.json")) as fh:
            self.published = json.load(fh)
        self.controls = [c for c in CONTROL_ORDER if c in self.published]
        self.cache = {}
        self.evaluations = 0
        self.parameter_records = []

    def falsifier(self, params, available):
        """The first published control figure this assignment contradicts."""
        self.evaluations += 1
        for name in self.controls:
            if not set(CONTROL_DEPENDENCIES[name]) <= available:
                continue
            try:
                value = control_value(name, self.export, params,
                                      self.reference, self.probe, self.uplink,
                                      self.cache)
            except WireError:
                return name
            except (ValueError, IndexError, KeyError, ZeroDivisionError):
                return name
            if value != self.published[name]:
                return name
        return None

    def settle_parameters(self):
        survivors = [{}]
        settled = set()
        for names, _control in SEARCH_GROUPS:
            domains = [PARAMETER_DOMAINS[name] for name in names]
            available = settled | set(names)
            nxt = []
            for prefix in survivors:
                for combination in product(domains):
                    trial = dict(prefix)
                    trial.update(zip(names, combination))
                    if self.falsifier(trial, available) is None:
                        nxt.append(trial)
            if not nxt:
                raise WireError("no assignment reproduces the control figures")
            survivors = nxt
            settled = available
        final = [p for p in survivors
                 if self.falsifier(p, set(ALL_PARAMETERS)) is None]
        if len(final) != 1:
            raise WireError("the control figures do not settle a unique "
                            "assignment (%d survived)" % len(final))
        return final[0]

    def verdicts(self, chosen):
        """A verdict for every candidate value in every parameter's domain,
        holding the rest of the settled assignment fixed."""
        records = []
        for name, domain in PARAMETERS:
            primary = next(c for c in self.controls
                           if name in CONTROL_DEPENDENCIES[c])
            rejected = []
            for value in domain:
                if value == chosen[name]:
                    continue
                trial = dict(chosen)
                trial[name] = value
                control = self.falsifier(trial, set(ALL_PARAMETERS))
                rejected.append({"value": value,
                                 "control": control or primary})
            records.append({
                "name": name,
                "candidates": len(domain),
                "control": primary,
                "selected": chosen[name],
                "rejected": rejected,
            })
        return records

    def comparison(self, chosen):
        return [{"control": name,
                 "published": self.published[name],
                 "reproduced": control_value(name, self.export, chosen,
                                             self.reference, self.probe,
                                             self.uplink, self.cache)}
                for name in self.controls]


def product(domains):
    if not domains:
        yield ()
        return
    for head in domains[0]:
        for tail in product(domains[1:]):
            yield (head,) + tail


# --------------------------------------------------------------------------
# roll-ups
# --------------------------------------------------------------------------

def bytes_by(rows, field):
    totals = defaultdict(int)
    for row in rows:
        totals[row[field]] += row["bytes_uploaded"]
    return dict(totals)


def make_summary(result):
    accepted = result["accepted"]
    tiers = defaultdict(int)
    for row in accepted:
        tiers[str(row["effective_tier"])] += 1
    return {
        "accepted_count": len(accepted),
        "by_component": bytes_by(accepted, "component_key"),
        "by_effective_tier": dict(tiers),
        "by_tenant_segment": bytes_by(accepted, "tenant_segment"),
        "by_zone": bytes_by(accepted, "host_zone"),
        "rejected_count": result["rejected"],
        "total_bytes_allocated": sum(r["bytes_allocated"] for r in accepted),
        "total_bytes_uploaded": sum(r["bytes_uploaded"] for r in accepted),
        "total_cost_ns": sum(r["cost_ns"] for r in accepted),
        "within_budget_count": sum(1 for r in accepted if r["within_budget"]),
    }


def make_zone_report(accepted, zone):
    rows = [r for r in accepted if r["host_zone"] == zone]
    return {
        "accepted_count": len(rows),
        "by_component": bytes_by(rows, "component_key"),
        "by_tenant_segment": bytes_by(rows, "tenant_segment"),
        "modules": sorted({r["module_tag"] for r in rows}),
        "total_bytes_uploaded": sum(r["bytes_uploaded"] for r in rows),
        "total_cost_ns": sum(r["cost_ns"] for r in rows),
        "within_budget_count": sum(1 for r in rows if r["within_budget"]),
        "zone": zone,
    }


def make_segment_report(accepted, segment):
    rows = [r for r in accepted if r["tenant_segment"] == segment]
    return {
        "accepted_count": len(rows),
        "by_component": bytes_by(rows, "component_key"),
        "by_zone": bytes_by(rows, "host_zone"),
        "modules": sorted({r["module_tag"] for r in rows}),
        "tenant_segment": segment,
        "total_bytes_uploaded": sum(r["bytes_uploaded"] for r in rows),
        "total_cost_ns": sum(r["cost_ns"] for r in rows),
        "within_budget_count": sum(1 for r in rows if r["within_budget"]),
    }


def make_product_summary(modules):
    out = []
    for tag in sorted(modules):
        entry = modules[tag]
        frames = entry["frames"]
        out.append({
            "accepted_count": len(frames),
            "allowance_ns": entry["allowance_ns"],
            "budget_ns": entry["budget_ns"],
            "component_key": entry["component_key"],
            "declared_tier": entry["declared_tier"],
            "effective_tier": entry["effective_tier"],
            "module_tag": tag,
            "net_samples": sum(f["samples"] for f in frames),
            "quarantined": entry["quarantined"],
            "total_bytes_uploaded": sum(f["bytes_uploaded"] for f in frames),
            "within_budget_count": sum(1 for f in frames if f["within_budget"]),
        })
    return {"modules": out}


def make_data_quality(result, calibrator, chosen):
    accepted = result["accepted"]
    allocated = sum(r["bytes_allocated"] for r in accepted)
    uploaded = sum(r["bytes_uploaded"] for r in accepted)
    return {
        "accepted_count": len(accepted),
        "calibration_comparison": calibrator.comparison(chosen),
        "candidate_search": {
            "evaluations": calibrator.evaluations,
            "parameters": calibrator.parameter_records,
        },
        "frame_count": len(result["frames"]),
        "reconciliation": {
            "allocated_bytes": allocated,
            "residual_bytes": allocated - uploaded,
            "uploaded_bytes": uploaded,
        },
        "rejections": dict(result["rejections"]),
        "settlement": {
            "passes": result["passes"],
            "quarantined_modules": result["quarantined"],
        },
    }


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------

def write_report(out_dir, name, payload):
    with open(os.path.join(out_dir, name), "w") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def write_ledger(out_dir, accepted):
    with open(os.path.join(out_dir, "normalized.jsonl"), "w") as handle:
        for row in accepted:
            handle.write(json.dumps({k: row[k] for k in LEDGER_FIELDS},
                                    sort_keys=True, separators=(",", ":")))
            handle.write("\n")


def check_invariants(result, reference, summary, zone_reports,
                     segment_reports):
    accepted = result["accepted"]
    ticks = [r["capture_tick"] for r in accepted]
    if any(a >= b for a, b in zip(ticks, ticks[1:])):
        raise WireError("the ledger is not strictly increasing in capture tick")
    if len(accepted) + result["rejected"] != len(result["frames"]):
        raise WireError("accepted and rejected do not close over the export")
    total = summary["total_bytes_uploaded"]
    if sum(r["total_bytes_uploaded"] for r in zone_reports.values()) != total:
        raise WireError("deployment zone byte totals do not close")
    if sum(r["total_bytes_uploaded"] for r in segment_reports.values()) != total:
        raise WireError("tenant segment byte totals do not close")
    if sum(r["accepted_count"] for r in zone_reports.values()) != len(accepted):
        raise WireError("deployment zone counts do not close")
    if sum(r["accepted_count"]
           for r in segment_reports.values()) != len(accepted):
        raise WireError("tenant segment counts do not close")


def run(data_dir, out_dir):
    reference = Reference(data_dir)

    # Rule: settle the withheld wire parameters against the published control
    # figures before computing any figure for the incident corpus.
    calibrator = Calibrator(data_dir, reference)
    chosen = calibrator.settle_parameters()
    calibrator.parameter_records = calibrator.verdicts(chosen)

    export = Export(os.path.join(data_dir, "fault_frames.psv"))
    probe, uplink = read_side_exports(data_dir, "")
    result = derive(export, chosen, reference, probe, uplink)

    summary = make_summary(result)
    zone_reports = {z: make_zone_report(result["accepted"], z)
                    for z in reference.zones}
    segment_reports = {s: make_segment_report(result["accepted"], s)
                       for s in reference.segments}
    check_invariants(result, reference, summary, zone_reports, segment_reports)

    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    write_ledger(out_dir, result["accepted"])
    write_report(out_dir, "summary.json", summary)
    write_report(out_dir, "product_summary.json",
                 make_product_summary(result["modules"]))
    for zone, report in zone_reports.items():
        write_report(out_dir, "region_%s.json" % zone, report)
    for segment, report in segment_reports.items():
        write_report(out_dir, "segment_%s.json" % segment, report)
    write_report(out_dir, "data_quality.json",
                 make_data_quality(result, calibrator, chosen))
    return chosen


def main(argv):
    data_dir = argv[1] if len(argv) > 1 else "/app/data"
    out_dir = argv[2] if len(argv) > 2 else "/app/out"
    run(data_dir, out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
