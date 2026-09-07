#!/usr/bin/env python3
"""Crash-reporter triage transformer.

Rebuilds the incident fault ledger and its roll-ups from a fleet
crash-reporter export directory.

    python3 transform.py [DATA_DIR] [OUT_DIR]

With no arguments DATA_DIR defaults to /app/data and OUT_DIR to /app/out.

The derivation runs in three stages.

1.  The withheld wire parameters (count multiplier, rotation offset, entry
    clock modulus, drift period, alternate radix, base shift width, coarse
    unit, signature offset, ring width) are settled against the control
    slice: every candidate value in a parameter's published domain receives
    a verdict, and a candidate is rejected only against a published control
    figure it fails to reproduce.  Check totals carried by the dumps and
    their frames gate every decoded field, so a candidate that breaks the
    shipped totals never survives.

2.  The incident export is decoded under the settled parameters, every
    frame is resolved against the build registry and either accepted or
    counted against each rejection condition it breaks, and the support
    settlement runs to a fixed point.

3.  The ledger and every report are written to OUT_DIR.

All clock, byte and residency arithmetic is carried in Python integers.
Standard library only.
"""

import csv
import json
import os
import sys

# ---------------------------------------------------------------------------
# fleet constants (wire-and-dumps.md, clock-and-sequencing.md,
# identity-and-builds.md, invariants-and-closure.md)
# ---------------------------------------------------------------------------

TICKS_PER_BUILD_INDEX = 900_000_000   # clock ticks per build index (15 min)
CHECK_TOTAL_MODULUS = 65536           # frame and dump check-total modulus
COUNT_BIAS_MODULUS = 9                # declared-count bias modulus
SIGNATURE_WIDTH = 16                  # fault signature width, hex digits
SIGNATURE_SUM_MODULUS = 1_000_000_007 # CTRL-G reduction modulus

DUMP_FLAG_SPAN_DELTA = 0x001     # span column holds second differences
DUMP_FLAG_ENTRY_CHAIN = 0x002    # entry column holds chain differences
DUMP_FLAG_UNWIND_DRIFT = 0x004   # unwind column carries a running drift
DUMP_FLAG_ROTATED = 0x008        # fault frames rotated left within the dump
DUMP_FLAG_COUNT_BIAS = 0x010     # declared count biased per dump ordinal
DUMP_FLAG_ALT_RADIX = 0x020      # base field written in alternate radix
DUMP_FLAG_ALIAS = 0x100          # module column holds a registry alias
DUMP_FLAG_SPLIT_BASE = 0x800     # base split across header and trailer

FRAME_FLAG_AUTH_INDEX = 0x040    # authoritative index rides the symbol column
FRAME_FLAG_SIG_ROTATE = 0x080    # signature rotated left
FRAME_FLAG_COARSE_ALLOC = 0x200  # alloc column holds coarse units
FRAME_FLAG_SIG_RING = 0x400      # signature column holds a ring caret

REJECTION_CONDITIONS = (
    "unresolved_module",
    "retired_build",
    "outside_support",
    "unprobed_frame",
    "unreported_dump",
)

# The nine withheld wire parameters with the candidate domains published for
# them, in the order the search settles them.
PARAMETER_DOMAINS = [
    ("count_multiplier", list(range(0, 9))),
    ("rotation_offset", list(range(0, 8))),
    ("entry_modulus", [1 << exponent for exponent in range(22, 35)]),
    ("drift_period", [48, 96, 192, 384, 768]),
    ("alternate_radix", list(range(11, 36))),
    ("base_shift_width", [8, 10, 12, 16]),
    ("coarse_unit", [64, 128, 256, 512, 1024]),
    ("signature_offset", list(range(0, 8))),
    ("ring_width", [4, 8, 12, 16]),
]
PARAMETER_NAMES = [name for name, _ in PARAMETER_DOMAINS]
PARAMETER_DOMAIN_SIZE = {name: len(domain) for name, domain in PARAMETER_DOMAINS}

CONTROL_ORDER = ["CTRL-%s" % letter for letter in "ABCDEFGHIJK"]
ALL_PARAMETERS = set(PARAMETER_NAMES)

# Which parameters each published control figure depends on.  A control is
# examined as soon as every parameter it depends on has been settled.
CONTROL_DEPENDENCIES = {
    "CTRL-A": {"count_multiplier"},
    "CTRL-B": {"count_multiplier", "alternate_radix", "base_shift_width"},
    "CTRL-C": {"count_multiplier", "rotation_offset"},
    "CTRL-D": {"count_multiplier", "rotation_offset", "entry_modulus"},
    "CTRL-E": {"count_multiplier", "rotation_offset", "drift_period"},
    "CTRL-F": {"count_multiplier", "coarse_unit"},
    "CTRL-G": {"count_multiplier", "rotation_offset", "signature_offset", "ring_width"},
    "CTRL-H": {"count_multiplier"},
    "CTRL-I": ALL_PARAMETERS,
    "CTRL-J": ALL_PARAMETERS,
    "CTRL-K": ALL_PARAMETERS,
}


class DecodeError(Exception):
    """A candidate parameter set contradicts the shipped check totals."""


class Undetermined(Exception):
    """A decoding rule needs a parameter that is not settled yet."""


# ---------------------------------------------------------------------------
# readers
# ---------------------------------------------------------------------------

def read_dump_export(path):
    """Parse a dump export into dump records with their raw line columns."""
    dumps = []
    with open(path) as handle:
        for raw in handle:
            line = raw.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            columns = line.split("|")
            if columns[0] == "@":
                dumps.append({
                    "dump_id": columns[1],
                    "module": columns[2],
                    "declared_count": int(columns[3]),
                    "base_field": columns[4],
                    "flags": int(columns[5], 16),
                    "key": int(columns[6]),
                    "dump_checksum": int(columns[7], 16),
                    "lines": [],
                })
            else:
                dumps[-1]["lines"].append(columns)
    return dumps


def read_probe(path):
    """Runtime probe facts keyed by (canonical module tag, frame index)."""
    facts = {}
    with open(path) as handle:
        for row in csv.DictReader(handle):
            key = (row["module_tag"], int(row["frame_index"]))
            facts[key] = (int(row["samples"]), int(row["peak_ns"]))
    return facts


def read_uplink(path):
    """Uplink reports keyed by (canonical module tag, frame index)."""
    reports = {}
    with open(path) as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            report = json.loads(line)
            key = (report["module_tag"], int(report["frame_index"]))
            reports[key] = report
    return reports


def read_build_registry(path):
    """Build registry rows indexed by the module tag they are written under."""
    by_tag = {}
    with open(path) as handle:
        for row in csv.DictReader(handle):
            by_tag.setdefault(row["module_tag"], []).append({
                "module_tag": row["module_tag"],
                "canonical_tag": row["canonical_tag"],
                "component_key": row["component_key"],
                "tier_index": int(row["tier_index"]),
                "from_build": int(row["effective_from_build"]),
                "to_build": int(row["effective_to_build"]),
                "status": row["status"],
            })
    return by_tag


def read_severity_ladder(path):
    """Component ladders: component key -> tier index -> budget."""
    ladder = {}
    with open(path) as handle:
        for row in csv.DictReader(handle):
            ladder.setdefault(row["component_key"], {})[int(row["tier_index"])] = \
                int(row["budget_ns"])
    return ladder


def read_support_windows(path):
    """Support windows: component key -> (open tick, close tick)."""
    windows = {}
    with open(path) as handle:
        for row in csv.DictReader(handle):
            windows[row["component_key"]] = (
                int(row["open_tick"]), int(row["close_tick"]))
    return windows


def read_host_zones(path):
    """Host id -> zone."""
    with open(path) as handle:
        return {row["host_id"]: row["zone"] for row in csv.DictReader(handle)}


def read_tenant_classes(path):
    """Tenant class -> tenant segment."""
    with open(path) as handle:
        return {row["tenant_class"]: row["tenant_segment"]
                for row in csv.DictReader(handle)}


def locate_corpus(data_dir, prefix, fallback_predicate):
    """Find the frame/probe/uplink triple for a corpus by export convention."""
    frames = os.path.join(data_dir, prefix + "fault_frames.psv")
    probe = os.path.join(data_dir, prefix + "runtime_probe.csv")
    uplink = os.path.join(data_dir, prefix + "uplink_reports.jsonl")
    if not os.path.exists(frames):
        matches = [
            name for name in sorted(os.listdir(data_dir))
            if name.endswith("_fault_frames.psv") and fallback_predicate(name)
        ]
        if len(matches) != 1:
            raise SystemExit("cannot locate a fault frame export in %s" % data_dir)
        stem = matches[0][:-len("_fault_frames.psv")]
        frames = os.path.join(data_dir, stem + "_fault_frames.psv")
        probe = os.path.join(data_dir, stem + "_runtime_probe.csv")
        uplink = os.path.join(data_dir, stem + "_uplink_reports.jsonl")
    return frames, probe, uplink


# ---------------------------------------------------------------------------
# decoding under (possibly partial) wire parameters
# ---------------------------------------------------------------------------

def line_check_total(columns):
    """Check total of one frame line: the columns it covers, joined."""
    return sum(ord(ch) for ch in "|".join(columns[:8])) % CHECK_TOTAL_MODULUS


def recovered_frame_count(dump, ordinal, params):
    """Fault frame count of a dump, or None while the multiplier is unsettled."""
    if dump["flags"] & DUMP_FLAG_COUNT_BIAS:
        multiplier = params.get("count_multiplier")
        if multiplier is None:
            return None
        return dump["declared_count"] - (ordinal * multiplier) % COUNT_BIAS_MODULUS
    return dump["declared_count"]


def recovered_base(dump, trailers, params):
    """Recovered dump base, or None while its parameters are unsettled."""
    flags = dump["flags"]
    if flags & DUMP_FLAG_ALT_RADIX:
        radix = params.get("alternate_radix")
        if radix is None:
            return None
        try:
            high = int(dump["base_field"], radix)
        except ValueError:
            raise DecodeError("base field %r is not a radix-%d number"
                              % (dump["base_field"], radix))
    else:
        high = int(dump["base_field"])
    if flags & DUMP_FLAG_SPLIT_BASE:
        shift = params.get("base_shift_width")
        if shift is None or not trailers:
            return None
        return (high << shift) | int(trailers[0][0])
    return high


def logical_order(dump, frame_lines, params):
    """Fault frames in logical order (rotation undone), or None while the
    rotation offset is unsettled."""
    if not dump["flags"] & DUMP_FLAG_ROTATED:
        return list(frame_lines)
    offset = params.get("rotation_offset")
    if offset is None:
        return None
    count = len(frame_lines)
    rotation = (dump["key"] + offset) % count
    return [frame_lines[(index - rotation) % count] for index in range(count)]


def recover_spans(dump, ordered):
    """Resident spans in logical order (second differences undone)."""
    columns = [int(line[3]) for line in ordered]
    if not dump["flags"] & DUMP_FLAG_SPAN_DELTA:
        return columns
    spans = []
    for index, value in enumerate(columns):
        if index == 0:
            spans.append(value)
        elif index == 1:
            spans.append(value + spans[0])
        else:
            spans.append(value + 2 * spans[index - 1] - spans[index - 2])
    return spans


def recover_entries(dump, ordered, params):
    """Entry stamps in logical order (chain differences undone), or None
    while the entry clock modulus is unsettled."""
    if not dump["flags"] & DUMP_FLAG_ENTRY_CHAIN:
        return [int(line[1]) for line in ordered]
    modulus = params.get("entry_modulus")
    if modulus is None:
        return None
    entries = []
    for index, line in enumerate(ordered):
        written = int(line[1])
        if index == 0:
            entries.append(written)
        else:
            entries.append((written - entries[index - 1]) % modulus)
    return entries


def decode_frames(dumps, params):
    """Decode a dump export under the given wire parameters.

    Parameters that are not settled yet (absent or None) leave the fields
    their rules govern unknown (None), so a partially parameterized decode
    still yields the fields no withheld rule covers.  A rule whose
    parameters are settled but contradicted by the shipped check totals
    raises DecodeError.  Frames come back in export order: dumps in file
    order, frames of each dump in logical order.
    """
    frames = []
    ring = None
    ring_width = params.get("ring_width")
    if ring_width is not None:
        ring = [None] * ring_width
    write_position = 0
    running_span_total = 0
    for ordinal, dump in enumerate(dumps, start=1):
        count = recovered_frame_count(dump, ordinal, params)
        if count is None:
            raise Undetermined("count multiplier")
        if not 0 < count <= len(dump["lines"]):
            raise DecodeError("dump %s declares %d fault frames over %d lines"
                              % (dump["dump_id"], count, len(dump["lines"])))
        frame_lines = dump["lines"][:count]
        trailers = dump["lines"][count:]

        # Check totals gate every decoded field: the frames' own totals and
        # the dump total they roll up to.
        dump_total = 0
        for line in frame_lines:
            total = int(line[8], 16)
            if line_check_total(line) != total:
                raise DecodeError("frame check total mismatch in dump %s"
                                  % dump["dump_id"])
            dump_total += total
        if dump_total % CHECK_TOTAL_MODULUS != dump["dump_checksum"]:
            raise DecodeError("dump check total mismatch for %s" % dump["dump_id"])

        base = recovered_base(dump, trailers, params)
        ordered = logical_order(dump, frame_lines, params)
        order_known = ordered is not None
        if not order_known:
            # The rotation offset is unsettled: emit the fault frames in the
            # order they were written, with the order-dependent fields unknown.
            ordered, spans, entries, first_entry = frame_lines, None, None, None
        else:
            spans = recover_spans(dump, ordered)
            entries = recover_entries(dump, ordered, params)
            first_entry = entries[0] if entries is not None else None

        drift_period = params.get("drift_period")
        coarse_unit = params.get("coarse_unit")
        signature_offset = params.get("signature_offset")
        entry_modulus = params.get("entry_modulus")

        for position, line in enumerate(ordered):
            frame_flags = int(line[6], 16)
            symbol, _, index_text = line[7].rpartition(":")
            if frame_flags & FRAME_FLAG_AUTH_INDEX:
                frame_index = int(index_text)
            else:
                frame_index = int(line[0])

            unwind = int(line[2])
            if dump["flags"] & DUMP_FLAG_UNWIND_DRIFT:
                if drift_period is None or running_span_total is None:
                    unwind = None
                else:
                    unwind -= running_span_total % drift_period

            if frame_flags & FRAME_FLAG_COARSE_ALLOC:
                if coarse_unit is None:
                    allocated = None
                else:
                    allocated = int(line[4][1:]) * coarse_unit
            else:
                allocated = int(line[4])

            signature_column = line[5]
            if frame_flags & FRAME_FLAG_SIG_RING:
                if ring is None or not order_known:
                    signature = None
                else:
                    distance = int(signature_column[1:])
                    signature = ring[(write_position - distance) % ring_width]
            elif frame_flags & FRAME_FLAG_SIG_ROTATE:
                if signature_offset is None:
                    signature = None
                else:
                    rotation = (frame_index + signature_offset) % SIGNATURE_WIDTH
                    signature = (signature_column[SIGNATURE_WIDTH - rotation:]
                                 + signature_column[:SIGNATURE_WIDTH - rotation])
            else:
                signature = signature_column

            if ring is not None and order_known:
                ring[write_position] = signature
                write_position = (write_position + 1) % ring_width

            capture_tick = None
            if base is not None and entries is not None:
                offset = entries[position] - first_entry
                if entry_modulus is not None:
                    offset %= entry_modulus
                capture_tick = base + offset

            frames.append({
                "source_dump": dump["dump_id"],
                "module_tag": dump["module"],
                "dump_base": base,
                "capture_tick": capture_tick,
                "enter_ns": entries[position] if entries is not None else None,
                "cost_ns": spans[position] if spans is not None else None,
                "unwind_ns": unwind,
                "bytes_allocated": allocated,
                "fault_signature": signature,
                "frame_index": frame_index,
                "symbol": symbol,
            })
            if spans is None or running_span_total is None:
                # a span the running total needs is still unknown
                running_span_total = None
            else:
                running_span_total += spans[position]
    return frames


# ---------------------------------------------------------------------------
# module resolution, acceptance, settlement
# ---------------------------------------------------------------------------

def resolve_module(registry, module_tag, build_index):
    """Follow the registry alias chain for a module at a build index.

    Returns the terminal row (canonical tag, component, tier, status), or
    None when the chain does not terminate on a canonical row.
    """
    tag = module_tag
    seen = set()
    while True:
        if tag in seen:
            return None
        seen.add(tag)
        rows = [row for row in registry.get(tag, ())
                if row["from_build"] <= build_index <= row["to_build"]]
        if len(rows) != 1:
            return None
        row = rows[0]
        if row["canonical_tag"] == row["module_tag"]:
            return row
        tag = row["canonical_tag"]


def resolve_frames(frames, reference):
    """Attach the terminal registry row to every frame that resolves."""
    resolved = []
    for frame in frames:
        build_index = frame["capture_tick"] // TICKS_PER_BUILD_INDEX
        row = resolve_module(reference["registry"], frame["module_tag"], build_index)
        resolved.append((frame, row, build_index))
    return resolved


def accept_frames(resolved, reference, probe, uplink):
    """Split resolved frames into accepted frames and rejection counts.

    A frame that does not resolve counts only against unresolved_module, and
    a frame whose build has the module retired counts only against
    retired_build: neither can carry a ledger row, so no further condition
    is examined for it.  The remaining conditions are independent and a
    frame is counted against each one it breaks.
    """
    rejections = dict.fromkeys(REJECTION_CONDITIONS, 0)
    accepted = []
    for frame, row, build_index in resolved:
        if row is None:
            rejections["unresolved_module"] += 1
            continue
        if row["status"] != "supported":
            rejections["retired_build"] += 1
            continue
        window = reference["windows"].get(row["component_key"])
        key = (row["canonical_tag"], frame["frame_index"])
        broken = False
        if window is None or not window[0] < frame["capture_tick"] < window[1]:
            rejections["outside_support"] += 1
            broken = True
        if key not in probe:
            rejections["unprobed_frame"] += 1
            broken = True
        if key not in uplink:
            rejections["unreported_dump"] += 1
            broken = True
        if not broken:
            samples, peak_ns = probe[key]
            accepted.append({
                "frame": frame,
                "row": row,
                "samples": samples,
                "peak_ns": peak_ns,
                "report": uplink[key],
            })
    return accepted, rejections


def settle_support(accepted, ladder):
    """Run the support settlement to its fixed point.

    Every module starts at its declared tier.  Each pass sizes the
    containment envelope of a component from the effective tier budgets of
    the component's modules that hold at least one accepted fault frame,
    and demotes by one, simultaneously, every module still above tier zero
    whose over-budget fault frames outnumber its within-budget ones.  The
    final pass, which demotes nothing, still counts as a pass.

    Returns module results keyed by canonical module tag.
    """
    members = {}
    for entry in accepted:
        tag = entry["row"]["canonical_tag"]
        member = members.get(tag)
        if member is None:
            member = members[tag] = {
                "module_tag": tag,
                "component_key": entry["row"]["component_key"],
                "declared_tier": entry["row"]["tier_index"],
                "entries": [],
            }
        member["entries"].append(entry)

    for member in members.values():
        component = ladder[member["component_key"]]
        if member["declared_tier"] not in component:
            raise SystemExit("component %s carries no tier %d for module %s"
                             % (member["component_key"], member["declared_tier"],
                                member["module_tag"]))
        member["effective_tier"] = member["declared_tier"]

    def tier_results(member):
        component = ladder[member["component_key"]]
        return component[member["effective_tier"]], len(component)

    def component_groups():
        groups = {}
        for member in members.values():
            groups.setdefault(member["component_key"], []).append(member)
        return groups

    passes = 0
    while True:
        passes += 1
        demoted = []
        for group in component_groups().values():
            budgets = [tier_results(member)[0] for member in group]
            envelope = min(budgets) * len(ladder[group[0]["component_key"]])
            for member in group:
                budget = tier_results(member)[0]
                allowance = min(budget, envelope)
                within = sum(1 for entry in member["entries"]
                             if entry["peak_ns"] <= allowance)
                over = len(member["entries"]) - within
                if member["effective_tier"] > 0 and over > within:
                    demoted.append(member)
        if not demoted:
            break
        for member in demoted:
            member["effective_tier"] -= 1

    quarantined = []
    for group in component_groups().values():
        budgets = [tier_results(member)[0] for member in group]
        envelope = min(budgets) * len(ladder[group[0]["component_key"]])
        for member in group:
            budget, _ = tier_results(member)
            allowance = min(budget, envelope)
            within = sum(1 for entry in member["entries"]
                         if entry["peak_ns"] <= allowance)
            over = len(member["entries"]) - within
            member["budget_ns"] = budget
            member["allowance_ns"] = allowance
            member["within_budget_count"] = within
            if member["effective_tier"] == 0 and over > within:
                quarantined.append(member["module_tag"])

    return members, passes, sorted(quarantined)


def build_ledger(accepted, members, host_zones, tenant_classes):
    """Ledger rows for the accepted frames, ordered by capture tick."""
    rows = []
    for entry in accepted:
        frame = entry["frame"]
        member = members[entry["row"]["canonical_tag"]]
        report = entry["report"]
        rows.append({
            "bytes_allocated": frame["bytes_allocated"],
            "bytes_uploaded": int(report["bytes_uploaded"]),
            "capture_tick": frame["capture_tick"],
            "component_key": member["component_key"],
            "cost_ns": frame["cost_ns"],
            "effective_tier": member["effective_tier"],
            "enter_ns": frame["enter_ns"],
            "fault_signature": frame["fault_signature"],
            "frame_index": frame["frame_index"],
            "host_zone": host_zones[report["host_id"]],
            "module_tag": member["module_tag"],
            "peak_ns": entry["peak_ns"],
            "samples": entry["samples"],
            "source_dump": frame["source_dump"],
            "symbol": frame["symbol"],
            "tenant_segment": tenant_classes[report["tenant_class"]],
            "unwind_ns": frame["unwind_ns"],
            "within_budget": entry["peak_ns"] <= member["allowance_ns"],
        })
    rows.sort(key=lambda row: (row["capture_tick"], row["source_dump"],
                               row["frame_index"]))
    return rows


# ---------------------------------------------------------------------------
# control figures
# ---------------------------------------------------------------------------

class CalibrationBench:
    """Reproduces the published control figures for candidate parameters."""

    def __init__(self, dumps, reference, probe, uplink):
        self.dumps = dumps
        self.reference = reference
        self.probe = probe
        self.uplink = uplink
        self.cache = {}

    def frames(self, params):
        key = tuple(sorted((name, params.get(name)) for name in PARAMETER_NAMES))
        if key not in self.cache:
            self.cache[key] = decode_frames(self.dumps, params)
        return self.cache[key]

    def reproduce(self, control, params):
        """Value of one control figure under the given parameters.

        Raises DecodeError or Undetermined when the candidate contradicts
        the shipped check totals or leaves a needed parameter unset.
        """
        frames = self.frames(params)

        def known(field):
            if any(frame[field] is None for frame in frames):
                raise Undetermined(field)

        if control == "CTRL-A":
            return len(frames)
        if control == "CTRL-B":
            bases = {}
            for frame in frames:
                if frame["dump_base"] is None:
                    raise Undetermined("dump base")
                bases[frame["source_dump"]] = frame["dump_base"]
            return sum(bases.values())
        if control == "CTRL-C":
            known("cost_ns")
            return sum(frame["cost_ns"] for frame in frames)
        if control == "CTRL-D":
            known("enter_ns")
            return sum(frame["enter_ns"] for frame in frames)
        if control == "CTRL-E":
            known("unwind_ns")
            return sum(frame["unwind_ns"] for frame in frames)
        if control == "CTRL-F":
            known("bytes_allocated")
            return sum(frame["bytes_allocated"] for frame in frames)
        if control == "CTRL-G":
            known("fault_signature")
            total = 0
            for frame in frames:
                total = (total + int(frame["fault_signature"], 16)) % \
                    SIGNATURE_SUM_MODULUS
            return total
        if control == "CTRL-H":
            return sum(frame["frame_index"] for frame in frames)
        if control in ("CTRL-I", "CTRL-J", "CTRL-K"):
            known("capture_tick")
            accepted, _ = accept_frames(
                resolve_frames(frames, self.reference), self.reference,
                self.probe, self.uplink)
            if control == "CTRL-I":
                return len(accepted)
            _members, _passes, quarantined = settle_support(
                accepted, self.reference["ladder"])
            if control == "CTRL-J":
                return len(quarantined)
            allocated = sum(entry["frame"]["bytes_allocated"] for entry in accepted)
            uploaded = sum(int(entry["report"]["bytes_uploaded"]) for entry in accepted)
            return allocated - uploaded
        raise ValueError(control)


# ---------------------------------------------------------------------------
# parameter search
# ---------------------------------------------------------------------------

def newly_determined_controls(settled_before, parameter):
    """Controls that `parameter` completes the dependency set of."""
    settled_after = settled_before | {parameter}
    determined = []
    for control in CONTROL_ORDER:
        dependencies = CONTROL_DEPENDENCIES[control]
        if dependencies <= settled_after and not dependencies <= settled_before:
            determined.append(control)
    return determined


def settle_parameters(bench, published):
    """Settle the nine wire parameters against the published control figures.

    The search walks the parameters in order, trying every candidate of a
    parameter's published domain under the parameters already settled.  A
    candidate that makes a determined control figure disagree with its
    published value, or contradicts the shipped check totals, is rejected
    against that control; a candidate that survives recurses to the next
    parameter.  Exactly one complete assignment is expected to reproduce
    every published figure.

    Returns the assignment, the number of candidate evaluations performed
    and a per-parameter record of every candidate's verdict.
    """
    statistics = {"evaluations": 0}

    def reproduces(control, assignment):
        try:
            return bench.reproduce(control, assignment) == published[control]
        except (DecodeError, Undetermined):
            return False

    def explore(level, assignment):
        """Try every candidate of parameter `level`; return survivors and a
        trial tree recording each candidate's verdict."""
        name, domain = PARAMETER_DOMAINS[level]
        settled_before = set(assignment)
        determined = newly_determined_controls(settled_before, name)
        node = {"parameter": name, "trials": []}
        survivors = []
        for value in domain:
            assignment[name] = value
            statistics["evaluations"] += 1
            trial = {"value": value, "falsifier": None, "children": None}
            rejected_by = None
            for control in determined:
                if not reproduces(control, assignment):
                    rejected_by = control
                    break
            if rejected_by is None:
                if level + 1 == len(PARAMETER_DOMAINS):
                    survivors.append(dict(assignment))
                else:
                    deeper, child = explore(level + 1, assignment)
                    trial["children"] = child
                    survivors.extend(deeper)
            else:
                trial["falsifier"] = rejected_by
            node["trials"].append(trial)
            del assignment[name]
        return survivors, node

    survivors, root = explore(0, {})
    if not survivors:
        raise SystemExit("no assignment of the wire parameters reproduces the "
                         "published control figures")
    if len(survivors) > 1:
        raise SystemExit("%d assignments of the wire parameters reproduce the "
                         "published control figures" % len(survivors))
    assignment = survivors[0]

    def trial_falsifier(trial):
        """Control a rejected trial dies against: its own, or the one that
        kills every completion below it."""
        if trial["falsifier"] is not None:
            return trial["falsifier"]
        counts = {}
        for child in trial["children"]["trials"]:
            falsifier = trial_falsifier(child)
            counts[falsifier] = counts.get(falsifier, 0) + 1
        return max(sorted(counts), key=counts.get)

    records = []
    node = root
    for level, (name, _domain) in enumerate(PARAMETER_DOMAINS):
        selected = assignment[name]
        determined = newly_determined_controls(
            set(PARAMETER_NAMES[:level]), name)
        rejected = []
        chosen_trial = None
        falsifier_counts = {}
        for trial in node["trials"]:
            if trial["value"] == selected:
                chosen_trial = trial
                continue
            falsifier = trial_falsifier(trial)
            rejected.append({"value": trial["value"], "control": falsifier})
            falsifier_counts[falsifier] = falsifier_counts.get(falsifier, 0) + 1
        # The control that settled the parameter is the one its rejected
        # candidates die against, or the first control its assignment
        # determines when every candidate survived to a deeper level.
        if falsifier_counts:
            settling = max(sorted(falsifier_counts), key=falsifier_counts.get)
        else:
            settling = (determined or [CONTROL_ORDER[0]])[0]
        records.append({
            "name": name,
            "candidates": PARAMETER_DOMAIN_SIZE[name],
            "control": settling,
            "selected": selected,
            "rejected": rejected,
        })
        if chosen_trial is not None and chosen_trial["children"] is not None:
            node = chosen_trial["children"]
        else:
            break
    return assignment, statistics["evaluations"], records


# ---------------------------------------------------------------------------
# reports
# ---------------------------------------------------------------------------

def bucket_count(rows, key):
    counts = {}
    for row in rows:
        counts[row[key]] = counts.get(row[key], 0) + 1
    return counts


def bucket_sum(rows, key, field):
    totals = {}
    for row in rows:
        totals[row[key]] = totals.get(row[key], 0) + row[field]
    return totals


def write_report(path, payload):
    with open(path, "w") as handle:
        handle.write(json.dumps(payload, sort_keys=True, indent=2) + "\n")


def build_reports(rows, members, rejections, passes, quarantined, frame_count,
                  zones, segments, calibration_comparison, candidate_search):
    """Assemble every report payload, keyed by file name."""
    allocated = sum(row["bytes_allocated"] for row in rows)
    uploaded = sum(row["bytes_uploaded"] for row in rows)

    summary = {
        "accepted_count": len(rows),
        "by_component": bucket_sum(rows, "component_key", "bytes_uploaded"),
        "by_effective_tier": {
            str(tier): count
            for tier, count in bucket_count(rows, "effective_tier").items()},
        "by_tenant_segment": bucket_sum(rows, "tenant_segment", "bytes_uploaded"),
        "by_zone": bucket_sum(rows, "host_zone", "bytes_uploaded"),
        "rejected_count": frame_count - len(rows),
        "total_bytes_allocated": allocated,
        "total_bytes_uploaded": uploaded,
        "total_cost_ns": sum(row["cost_ns"] for row in rows),
        "within_budget_count": sum(1 for row in rows if row["within_budget"]),
    }

    reports = {"summary.json": summary}

    for zone in zones:
        scoped = [row for row in rows if row["host_zone"] == zone]
        reports["region_%s.json" % zone] = {
            "accepted_count": len(scoped),
            "by_component": bucket_sum(scoped, "component_key", "bytes_uploaded"),
            "by_tenant_segment": bucket_sum(scoped, "tenant_segment",
                                            "bytes_uploaded"),
            "modules": sorted({row["module_tag"] for row in scoped}),
            "total_bytes_uploaded": sum(row["bytes_uploaded"] for row in scoped),
            "total_cost_ns": sum(row["cost_ns"] for row in scoped),
            "within_budget_count": sum(1 for row in scoped if row["within_budget"]),
            "zone": zone,
        }

    for segment in segments:
        scoped = [row for row in rows if row["tenant_segment"] == segment]
        reports["segment_%s.json" % segment] = {
            "accepted_count": len(scoped),
            "by_component": bucket_sum(scoped, "component_key", "bytes_uploaded"),
            "by_zone": bucket_sum(scoped, "host_zone", "bytes_uploaded"),
            "modules": sorted({row["module_tag"] for row in scoped}),
            "total_bytes_uploaded": sum(row["bytes_uploaded"] for row in scoped),
            "total_cost_ns": sum(row["cost_ns"] for row in scoped),
            "within_budget_count": sum(1 for row in scoped if row["within_budget"]),
            "tenant_segment": segment,
        }

    module_reports = []
    for tag in sorted(members):
        member = members[tag]
        module_reports.append({
            "accepted_count": len(member["entries"]),
            "allowance_ns": member["allowance_ns"],
            "budget_ns": member["budget_ns"],
            "component_key": member["component_key"],
            "declared_tier": member["declared_tier"],
            "effective_tier": member["effective_tier"],
            "module_tag": tag,
            "net_samples": sum(entry["samples"] for entry in member["entries"]),
            "quarantined": tag in quarantined,
            "total_bytes_uploaded": sum(
                int(entry["report"]["bytes_uploaded"])
                for entry in member["entries"]),
            "within_budget_count": member["within_budget_count"],
        })
    reports["product_summary.json"] = {"modules": module_reports}

    reports["data_quality.json"] = {
        "accepted_count": len(rows),
        "calibration_comparison": calibration_comparison,
        "candidate_search": candidate_search,
        "frame_count": frame_count,
        "reconciliation": {
            "allocated_bytes": allocated,
            "residual_bytes": allocated - uploaded,
            "uploaded_bytes": uploaded,
        },
        "rejections": rejections,
        "settlement": {
            "passes": passes,
            "quarantined_modules": list(quarantined),
        },
    }
    return reports


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------

def load_context(data_dir):
    """Read every table and corpus the derivation needs."""
    def path(name):
        return os.path.join(data_dir, name)

    reference = {
        "registry": read_build_registry(path("build_registry.csv")),
        "ladder": read_severity_ladder(path("severity_ladder.csv")),
        "windows": read_support_windows(path("support_windows.csv")),
    }
    host_zones = read_host_zones(path("host_zones.csv"))
    tenant_classes = read_tenant_classes(path("tenant_classes.csv"))
    with open(path("control-slice-figures.json")) as handle:
        published = json.load(handle)

    calibration_paths = locate_corpus(
        data_dir, "calibration_", lambda name: name.startswith("calibration"))
    incident_paths = locate_corpus(
        data_dir, "", lambda name: not name.startswith(("calibration", "sample")))

    calibration = {
        "dumps": read_dump_export(calibration_paths[0]),
        "probe": read_probe(calibration_paths[1]),
        "uplink": read_uplink(calibration_paths[2]),
    }
    incident = {
        "dumps": read_dump_export(incident_paths[0]),
        "probe": read_probe(incident_paths[1]),
        "uplink": read_uplink(incident_paths[2]),
    }
    return {
        "reference": reference,
        "host_zones": host_zones,
        "tenant_classes": tenant_classes,
        "published": published,
        "calibration": calibration,
        "incident": incident,
        "zones": sorted(set(host_zones.values())),
        "segments": sorted(set(tenant_classes.values())),
    }


def run(data_dir, out_dir):
    context = load_context(data_dir)
    reference = context["reference"]

    # Stage 1: settle the wire parameters against the published figures.
    bench = CalibrationBench(context["calibration"]["dumps"], reference,
                             context["calibration"]["probe"],
                             context["calibration"]["uplink"])
    params, evaluations, records = settle_parameters(bench, context["published"])

    calibration_comparison = []
    for control in CONTROL_ORDER:
        reproduced = bench.reproduce(control, params)
        calibration_comparison.append({
            "control": control,
            "published": context["published"][control],
            "reproduced": reproduced,
        })
        if reproduced != context["published"][control]:
            raise SystemExit("settled parameters fail to reproduce %s" % control)
    candidate_search = {"evaluations": evaluations, "parameters": records}

    # Stage 2: decode the incident export and run the triage pipeline.
    frames = decode_frames(context["incident"]["dumps"], params)
    accepted, rejections = accept_frames(
        resolve_frames(frames, reference), reference,
        context["incident"]["probe"], context["incident"]["uplink"])
    members, passes, quarantined = settle_support(accepted, reference["ladder"])
    rows = build_ledger(accepted, members, context["host_zones"],
                        context["tenant_classes"])

    # Stage 3: write the ledger and every report.
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    with open(os.path.join(out_dir, "normalized.jsonl"), "w") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True,
                                    separators=(",", ":")) + "\n")
    reports = build_reports(rows, members, rejections, passes, quarantined,
                            len(frames), context["zones"], context["segments"],
                            calibration_comparison, candidate_search)
    for name, payload in sorted(reports.items()):
        write_report(os.path.join(out_dir, name), payload)
    return rows, reports


def main(argv):
    if len(argv) > 3:
        raise SystemExit("usage: transform.py [DATA_DIR] [OUT_DIR]")
    data_dir = argv[1] if len(argv) > 1 else "/app/data"
    out_dir = argv[2] if len(argv) > 2 else "/app/out"
    run(data_dir, out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
