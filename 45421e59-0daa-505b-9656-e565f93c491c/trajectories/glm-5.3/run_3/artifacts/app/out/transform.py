#!/usr/bin/env python3
"""Crash-reporter triage set builder.

Usage:
    python3 transform.py [DATA_DIR] [OUT_DIR]

Reads a corpus directory (a reporter dump export, a runtime probe export, an
uplink export and the five reference tables), reconstructs the fault ledger and
every roll-up that hangs off it, and writes the triage set under the output
directory.

Nothing here depends on the identifiers, counts or totals of any particular
corpus: the withheld wire parameters are settled against the control slice that
ships beside the corpus before any incident figure is computed, and the whole
derivation is then re-run from the settled parameter set alone.
"""

import json
import os
import sys

# --------------------------------------------------------------------------
# Fleet constants (clock-and-sequencing.md, wire-and-dumps.md)
# --------------------------------------------------------------------------

TICKS_PER_BUILD_INDEX = 900000000          # 1000 ticks/ms across one build index
CHECK_MOD = 0x10000                        # check totals are sixteen bit
SIGNATURE_WIDTH = 16                       # fault signatures are 16 hex characters
SIGNATURE_MOD = 16                         # signature rotation modulus
COUNT_BIAS_MOD = 9                         # declared-count bias modulus
SIGNATURE_SUM_MOD = 1000000007
RADIX_DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"

# Dump flag word.
D_SPAN_DELTA = 0x001
D_ENTRY_CHAIN = 0x002
D_UNWIND_DRIFT = 0x004
D_LINE_ROTATE = 0x008
D_COUNT_BIAS = 0x010
D_BASE_RADIX = 0x020
D_MODULE_ALIAS = 0x100
D_SPLIT_BASE = 0x800

# Frame flag word.
F_INDEX_IN_SYMBOL = 0x040
F_SIG_ROTATE = 0x080
F_ALLOC_COARSE = 0x200
F_SIG_RING = 0x400

# The nine withheld wire parameters, the control figure each one governs, and
# the candidate domain the control slice publishes for it.
PARAMETERS = [
    ("entry_clock_modulus", "CTRL-D", [1 << e for e in range(22, 35)]),
    ("drift_period", "CTRL-E", [48, 96, 192, 384, 768]),
    ("rotation_offset", "CTRL-C", list(range(0, 8))),
    ("count_multiplier", "CTRL-A", list(range(0, 9))),
    ("alternate_radix", "CTRL-B", list(range(11, 36))),
    ("signature_offset", "CTRL-G", list(range(0, 8))),
    ("coarse_unit", "CTRL-F", [64, 128, 256, 512, 1024]),
    ("ring_width", "CTRL-G", [4, 8, 12, 16]),
    ("base_shift_width", "CTRL-B", [8, 10, 12, 16]),
]
DOMAIN = dict((name, domain) for name, _, domain in PARAMETERS)
PARAM_CONTROL = dict((name, control) for name, control, _ in PARAMETERS)
PARAM_CONTROLS = {
    "count_multiplier": ["CTRL-A", "CTRL-H"],   # both are pure frame-set figures
}
PARAM_NAMES = [name for name, _, _ in PARAMETERS]

# Stage order for the parameter search.  A stage's control figure never depends
# on a parameter that a later stage settles, so each stage can be judged before
# the rest of the assignment is known.
STAGES = [
    ("count_multiplier",),
    ("rotation_offset",),
    ("alternate_radix", "base_shift_width"),
    ("entry_clock_modulus",),
    ("drift_period",),
    ("coarse_unit",),
    ("signature_offset", "ring_width"),
]

CONTROL_ORDER = ["CTRL-" + letter for letter in "ABCDEFGHIJK"]

# Control figure each recovery stage reproduces.
FIGURE_OF = {
    "CTRL-A": "frame_count",
    "CTRL-B": "base_total",
    "CTRL-C": "span_total",
    "CTRL-D": "entry_total",
    "CTRL-E": "unwind_total",
    "CTRL-F": "allocated_total",
    "CTRL-G": "signature_total",
    "CTRL-H": "index_total",
    "CTRL-I": "accepted_count",
    "CTRL-J": "quarantined_modules",
    "CTRL-K": "residual_bytes",
}


class DecodeError(Exception):
    """A candidate assignment cannot describe the export at all."""


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def rotate_left(text, k):
    k %= len(text)
    return text[k:] + text[:k]


def rotate_right(text, k):
    k %= len(text)
    return text[-k:] + text[:-k] if k else text


def from_radix(text, radix, strict=True):
    value = 0
    for ch in text:
        pos = RADIX_DIGITS.index(ch)
        if pos >= radix:
            if strict:
                raise DecodeError("digit %r is out of radix %d" % (ch, radix))
            return None
        value = value * radix + pos
    return value


def read_csv(path):
    with open(path, "r", encoding="utf-8") as fh:
        rows = [line.rstrip("\n").split(",") for line in fh if line.strip()]
    head = rows[0]
    return [dict(zip(head, row)) for row in rows[1:]]


def read_jsonl(path):
    out = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                out.append(json.loads(line))
    return out


def frame_check_total(columns):
    return sum(ord(ch) for ch in "|".join(columns[:8])) % CHECK_MOD


# --------------------------------------------------------------------------
# Reporter export parsing and decoding
# --------------------------------------------------------------------------

def parse_export(path):
    """Split a pipe-delimited export into dumps of raw nine-column lines."""
    dumps = []
    current = None
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.rstrip("\n")
            if not line.strip() or line.startswith("#"):
                continue
            columns = line.split("|")
            if columns[0] == "@":
                current = {
                    "ordinal": len(dumps) + 1,
                    "dump_id": columns[1],
                    "module": columns[2],
                    "declared": int(columns[3]),
                    "base_field": columns[4],
                    "flags": int(columns[5], 16),
                    "key": int(columns[6]),
                    "check": int(columns[7], 16),
                    "lines": [],
                }
                dumps.append(current)
            else:
                if current is None:
                    raise DecodeError("frame line before any dump header")
                if len(columns) != 9:
                    raise DecodeError("frame line carrying %d columns" % len(columns))
                current["lines"].append(columns)
    if not dumps:
        raise DecodeError("no dumps in export")
    return dumps


def split_dump(dump, count_multiplier):
    """Return (fault frames in written order, trailer lines, true count)."""
    count = dump["declared"]
    if dump["flags"] & D_COUNT_BIAS:
        count = count - (dump["ordinal"] * count_multiplier) % COUNT_BIAS_MOD
    if count < 1:
        raise DecodeError("dump %s resolves to %d fault frames"
                          % (dump["dump_id"], count))
    if count > len(dump["lines"]):
        raise DecodeError("dump %s claims %d of %d lines"
                          % (dump["dump_id"], count, len(dump["lines"])))
    written = dump["lines"][:count]
    trailers = dump["lines"][count:]

    # Check the shipped totals against the columns they cover before trusting
    # any decoded field.
    total = 0
    for columns in written:
        if frame_check_total(columns) != int(columns[8], 16):
            raise DecodeError("frame check total mismatch in dump %s"
                              % dump["dump_id"])
        total = (total + int(columns[8], 16)) % CHECK_MOD
    if total != dump["check"]:
        raise DecodeError("dump check total mismatch in dump %s" % dump["dump_id"])
    if (dump["flags"] & D_SPLIT_BASE) and not trailers:
        raise DecodeError("split-base dump %s carries no trailer" % dump["dump_id"])
    return written, trailers, count


def decode_export(path, params, strict=True):
    """Recover every fault frame of an export, in export order.

    In a non-strict decode a field whose governing parameters are not yet
    settled is recovered as None rather than raising, so that a search stage
    can reproduce the figures its own control covers while the rest of the
    assignment is still open.
    """
    entry_mod = params["entry_clock_modulus"]
    drift_period = params["drift_period"]
    rotation_offset = params["rotation_offset"]
    count_multiplier = params["count_multiplier"]
    alternate_radix = params["alternate_radix"]
    base_shift_width = params["base_shift_width"]
    signature_offset = params["signature_offset"]
    coarse_unit = params["coarse_unit"]
    ring_width = params["ring_width"]

    frames = []
    running_span = 0
    ring = [None] * ring_width
    write_pos = 0

    for dump in parse_export(path):
        flags = dump["flags"]
        written, trailers, count = split_dump(dump, count_multiplier)

        if flags & D_LINE_ROTATE:
            shift = (dump["key"] + rotation_offset) % count
            logical = [written[(j - shift) % count] for j in range(count)]
        else:
            logical = list(written)

        if flags & D_BASE_RADIX:
            base_number = from_radix(dump["base_field"], alternate_radix, strict)
        else:
            base_number = int(dump["base_field"])
        if base_number is None:
            base = None
        elif flags & D_SPLIT_BASE:
            low = int(trailers[0][0])
            if strict and low >> base_shift_width:
                raise DecodeError("split-base low part overflows in dump %s"
                                  % dump["dump_id"])
            base = (base_number << base_shift_width) | low
        else:
            base = base_number
        if base is not None and base < 0:
            raise DecodeError("negative dump base in dump %s" % dump["dump_id"])

        raw_spans = [int(line[3]) for line in logical]
        if flags & D_SPAN_DELTA:
            # Second-order differences started at the dump's first fault frame.
            spans = [raw_spans[0]]
            if count > 1:
                delta = raw_spans[1]
                spans.append(spans[0] + delta)
                for i in range(2, count):
                    delta += raw_spans[i]
                    spans.append(spans[-1] + delta)
        else:
            spans = list(raw_spans)

        raw_entries = [int(line[1]) for line in logical]
        if flags & D_ENTRY_CHAIN:
            entries = [raw_entries[0]]
            for i in range(1, count):
                entries.append((raw_entries[i] - entries[i - 1]) % entry_mod)
        else:
            entries = list(raw_entries)

        first_entry = entries[0]

        for i, line in enumerate(logical):
            fflags = int(line[6], 16)
            symbol = line[7]
            frame_index = (int(symbol.rsplit(":", 1)[1]) if fflags & F_INDEX_IN_SYMBOL
                           else int(line[0]))

            if fflags & F_ALLOC_COARSE:
                if not line[4].startswith("~"):
                    raise DecodeError("coarse allocation without a tilde")
                allocated = int(line[4][1:]) * coarse_unit
            else:
                allocated = int(line[4])

            column = line[5]
            if fflags & F_SIG_RING:
                if not column.startswith("^"):
                    raise DecodeError("ring signature without a caret")
                slot = (write_pos - int(column[1:])) % ring_width
                signature = ring[slot]
                if signature is None and strict:
                    raise DecodeError("ring slot %d is empty in dump %s"
                                      % (slot, dump["dump_id"]))
            elif fflags & F_SIG_ROTATE:
                signature = rotate_right(column, (frame_index + signature_offset)
                                         % SIGNATURE_MOD)
            else:
                signature = column
            if signature is not None and len(signature) != SIGNATURE_WIDTH:
                raise DecodeError("recovered signature %r is not %d characters"
                                  % (signature, SIGNATURE_WIDTH))

            unwind_column = int(line[2])
            unwind = (unwind_column - running_span % drift_period
                      if flags & D_UNWIND_DRIFT else unwind_column)

            frames.append({
                "source_dump": dump["dump_id"],
                "dump_module": dump["module"],
                "position": i,
                "frame_index": frame_index,
                "symbol": symbol.split(":", 1)[0],
                "entry": entries[i],
                "unwind": unwind,
                "span": spans[i],
                "allocated": allocated,
                "signature": signature,
                "base": base,
                "capture_tick": None if base is None
                else base + (entries[i] - first_entry) % entry_mod,
            })
            # The running span total and the signature ring advance over the
            # export as a whole, whatever flags this frame's own dump sets.
            running_span += spans[i]
            if signature is not None:
                ring[write_pos] = signature
                write_pos = (write_pos + 1) % ring_width

    return frames


# --------------------------------------------------------------------------
# Reference tables
# --------------------------------------------------------------------------

class Registry(object):
    """Build registry: interval cover, canonical resolution, retirement."""

    def __init__(self, rows):
        self.by_tag = {}
        for row in rows:
            self.by_tag.setdefault(row["module_tag"], []).append(row)

    def _row_for(self, tag, build):
        for row in self.by_tag.get(tag, ()):
            if int(row["effective_from_build"]) <= build <= int(row["effective_to_build"]):
                return row
        return None

    def resolve(self, tag, build):
        """Return the terminal registry row for a tag on a build index."""
        seen = []
        current = tag
        while True:
            if current in seen:
                return None
            seen.append(current)
            row = self._row_for(current, build)
            if row is None:
                return None
            canonical = row["canonical_tag"]
            if canonical == current:
                return row
            current = canonical


# --------------------------------------------------------------------------
# Triage
# --------------------------------------------------------------------------

def classify(frames, registry, windows, probe, uplink):
    """Split frames into accepted records and per-condition rejections."""
    accepted = []
    rejections = {"unresolved_module": 0, "retired_build": 0, "outside_support": 0,
                  "unprobed_frame": 0, "unreported_dump": 0}
    for frame in frames:
        build = frame["capture_tick"] // TICKS_PER_BUILD_INDEX
        row = registry.resolve(frame["dump_module"], build)
        if row is None:
            rejections["unresolved_module"] += 1
            continue
        if row["status"] != "supported":
            rejections["retired_build"] += 1
            continue

        component = row["component_key"]
        window = windows.get(component)
        if window is None:
            rejections["unresolved_module"] += 1
            continue

        key = (row["canonical_tag"], frame["frame_index"])
        faulted = False
        if not window["open"] < frame["capture_tick"] < window["close"]:
            rejections["outside_support"] += 1
            faulted = True
        residency = probe.get(key)
        if residency is None:
            rejections["unprobed_frame"] += 1
            faulted = True
        report = uplink.get(key)
        if report is None:
            rejections["unreported_dump"] += 1
            faulted = True
        if faulted:
            continue

        accepted.append({
            "frame": frame,
            "module_tag": row["canonical_tag"],
            "component_key": component,
            "declared_tier": int(row["tier_index"]),
            "samples": residency["samples"],
            "peak_ns": residency["peak_ns"],
            "host_id": report["host_id"],
            "tenant_class": report["tenant_class"],
            "bytes_uploaded": int(report["bytes_uploaded"]),
        })
    return accepted, rejections


def settle(accepted, ladder):
    """Demote effective tiers by passes until no module is left to demote."""
    modules = {}
    for record in accepted:
        module = modules.setdefault(record["module_tag"], {
            "module_tag": record["module_tag"],
            "component_key": record["component_key"],
            "declared_tier": record["declared_tier"],
            "frames": [],
        })
        module["frames"].append(record)
    for module in modules.values():
        module["effective_tier"] = module["declared_tier"]

    passes = 0
    while True:
        passes += 1
        lowest = {}
        for module in modules.values():
            budget = ladder[module["component_key"]][module["effective_tier"]]
            if module["component_key"] not in lowest or budget < lowest[module["component_key"]]:
                lowest[module["component_key"]] = budget
        envelope = dict((component, budget * len(ladder[component]))
                        for component, budget in lowest.items())

        demote = []
        for module in modules.values():
            allowance = min(ladder[module["component_key"]][module["effective_tier"]],
                            envelope[module["component_key"]])
            module["allowance_ns"] = allowance
            within = sum(1 for record in module["frames"]
                         if record["peak_ns"] <= allowance)
            module["within_budget_count"] = within
            module["over_budget_count"] = len(module["frames"]) - within
            if (module["effective_tier"] > 0
                    and module["over_budget_count"] > module["within_budget_count"]):
                demote.append(module)
        if not demote:
            break
        for module in demote:
            module["effective_tier"] -= 1

    quarantined = sorted(
        module["module_tag"] for module in modules.values()
        if module["effective_tier"] == 0
        and module["over_budget_count"] > module["within_budget_count"])
    return modules, passes, quarantined


def build_ledger(accepted, modules, zones, tenant):
    ledger = []
    for record in accepted:
        module = modules[record["module_tag"]]
        frame = record["frame"]
        ledger.append({
            "bytes_allocated": frame["allocated"],
            "bytes_uploaded": record["bytes_uploaded"],
            "capture_tick": frame["capture_tick"],
            "component_key": record["component_key"],
            "cost_ns": frame["span"],
            "effective_tier": module["effective_tier"],
            "enter_ns": frame["entry"],
            "fault_signature": frame["signature"],
            "frame_index": frame["frame_index"],
            "host_zone": zones[record["host_id"]],
            "module_tag": record["module_tag"],
            "peak_ns": record["peak_ns"],
            "samples": record["samples"],
            "source_dump": frame["source_dump"],
            "symbol": frame["symbol"],
            "tenant_segment": tenant[record["tenant_class"]],
            "unwind_ns": frame["unwind"],
            "within_budget": record["peak_ns"] <= module["allowance_ns"],
        })
    ledger.sort(key=lambda row: row["capture_tick"])
    return ledger


# --------------------------------------------------------------------------
# Reports
# --------------------------------------------------------------------------

def _slice_report(rows, second_key, second_label, slice_label, slice_value):
    by_component = {}
    by_second = {}
    for row in rows:
        by_component[row["component_key"]] = (by_component.get(row["component_key"], 0)
                                              + row["bytes_uploaded"])
        by_second[row[second_key]] = (by_second.get(row[second_key], 0)
                                      + row["bytes_uploaded"])
    return {
        "accepted_count": len(rows),
        "by_component": by_component,
        second_label: by_second,
        "modules": sorted(set(row["module_tag"] for row in rows)),
        "total_bytes_uploaded": sum(row["bytes_uploaded"] for row in rows),
        "total_cost_ns": sum(row["cost_ns"] for row in rows),
        "within_budget_count": sum(1 for row in rows if row["within_budget"]),
        slice_label: slice_value,
    }


def zone_report(zone, ledger):
    rows = [row for row in ledger if row["host_zone"] == zone]
    return _slice_report(rows, "tenant_segment", "by_tenant_segment", "zone", zone)


def segment_report(segment, ledger):
    rows = [row for row in ledger if row["tenant_segment"] == segment]
    return _slice_report(rows, "host_zone", "by_zone", "tenant_segment", segment)


def product_summary(ledger, modules, ladder):
    rows = []
    for tag in sorted(modules):
        module = modules[tag]
        frames = [row for row in ledger if row["module_tag"] == tag]
        rows.append({
            "module_tag": tag,
            "component_key": module["component_key"],
            "declared_tier": module["declared_tier"],
            "effective_tier": module["effective_tier"],
            "budget_ns": ladder[module["component_key"]][module["effective_tier"]],
            "allowance_ns": module["allowance_ns"],
            "quarantined": (module["effective_tier"] == 0
                            and module["over_budget_count"] > module["within_budget_count"]),
            "accepted_count": len(frames),
            "within_budget_count": sum(1 for row in frames if row["within_budget"]),
            "net_samples": sum(row["samples"] for row in frames),
            "total_bytes_uploaded": sum(row["bytes_uploaded"] for row in frames),
        })
    return {"modules": rows}


def incident_summary(ledger, rejected_count):
    by_component = {}
    by_segment = {}
    by_zone = {}
    by_tier = {}
    for row in ledger:
        by_component[row["component_key"]] = (by_component.get(row["component_key"], 0)
                                              + row["bytes_uploaded"])
        by_segment[row["tenant_segment"]] = (by_segment.get(row["tenant_segment"], 0)
                                             + row["bytes_uploaded"])
        by_zone[row["host_zone"]] = (by_zone.get(row["host_zone"], 0)
                                     + row["bytes_uploaded"])
        tier = str(row["effective_tier"])
        by_tier[tier] = by_tier.get(tier, 0) + 1
    return {
        "accepted_count": len(ledger),
        "rejected_count": rejected_count,
        "by_component": by_component,
        "by_tenant_segment": by_segment,
        "by_zone": by_zone,
        "by_effective_tier": by_tier,
        "total_bytes_allocated": sum(row["bytes_allocated"] for row in ledger),
        "total_bytes_uploaded": sum(row["bytes_uploaded"] for row in ledger),
        "total_cost_ns": sum(row["cost_ns"] for row in ledger),
        "within_budget_count": sum(1 for row in ledger if row["within_budget"]),
    }


# --------------------------------------------------------------------------
# Wire figures and the parameter search
# --------------------------------------------------------------------------

def wire_figures(path, params, wanted=None):
    """Recover an export and total up the wire figures the control slice
    publishes.  Only the figures asked for are computed, so a figure whose
    governing parameters are still open never blocks the others."""
    if wanted is None:
        wanted = set(FIGURE_OF.values())
    frames = decode_export(path, params, strict=False)
    figures = {"frame_count": len(frames)}
    if "index_total" in wanted:
        figures["index_total"] = sum(f["frame_index"] for f in frames)
    if "span_total" in wanted:
        figures["span_total"] = sum(f["span"] for f in frames)
    if "entry_total" in wanted:
        figures["entry_total"] = sum(f["entry"] for f in frames)
    if "unwind_total" in wanted:
        figures["unwind_total"] = sum(f["unwind"] for f in frames)
    if "allocated_total" in wanted:
        figures["allocated_total"] = sum(f["allocated"] for f in frames)
    if "base_total" in wanted:
        bases = {}
        for frame in frames:
            bases.setdefault(frame["source_dump"], frame["base"])
        if any(value is None for value in bases.values()):
            raise DecodeError("a dump base is unrecoverable")
        figures["base_total"] = sum(bases.values())
    if "signature_total" in wanted:
        if any(f["signature"] is None for f in frames):
            raise DecodeError("a fault signature is unrecoverable")
        figures["signature_total"] = sum(int(f["signature"], 16)
                                         for f in frames) % SIGNATURE_SUM_MOD
    return frames, figures


def reproduce(export, params, tables, control):
    """Reproduce one published control figure under a candidate assignment."""
    figure = FIGURE_OF[control]
    if figure in ("accepted_count", "quarantined_modules", "residual_bytes"):
        frames = decode_export(export["fault_frames"], params, strict=False)
        if any(f["base"] is None or f["signature"] is None for f in frames):
            raise DecodeError("a recovered field the ledger needs is missing")
        result = triage_corpus(export, tables, params)
        if figure == "accepted_count":
            return len(result["ledger"])
        if figure == "quarantined_modules":
            return len(result["quarantined"])
        return (sum(row["bytes_allocated"] for row in result["ledger"])
                - sum(row["bytes_uploaded"] for row in result["ledger"]))
    _, figures = wire_figures(export["fault_frames"], params, {figure})
    return figures[figure]


def search_parameters(export, tables, published):
    """Settle the nine withheld wire parameters against the control slice.

    The search runs as stages over a frontier of partial assignments.  Each
    stage enumerates every candidate value of the parameters it settles, and a
    partial assignment survives only while it reproduces every control figure
    the parameters settled so far govern.
    """
    state = {"evaluations": 0}

    def controls_for(names):
        controls = set()
        for name in names:
            controls.update(PARAM_CONTROLS.get(name, (PARAM_CONTROL[name],)))
        return sorted(controls)

    def holds(assignment, controls):
        for control in controls:
            state["evaluations"] += 1
            try:
                if reproduce(export, assignment, tables, control) != published[control]:
                    return False
            except DecodeError:
                return False
        return True

    def fill(partial):
        values = {}
        for name in PARAM_NAMES:
            values[name] = partial.get(name, DOMAIN[name][0])
        return values

    frontier = [{}]
    for stage in STAGES:
        settled = set()
        for partial in frontier:
            settled.update(partial)
        watched = controls_for(settled | set(stage))
        nxt = []
        for partial in frontier:
            combos = [{}]
            for name in stage:
                combos = [dict(combo, **{name: value})
                          for combo in combos for value in DOMAIN[name]]
            for combo in combos:
                candidate = dict(partial)
                candidate.update(combo)
                if holds(fill(candidate), watched):
                    nxt.append(candidate)
        if not nxt:
            raise SystemExit("no candidate assignment reproduces the control "
                             "slice at stage %s" % ", ".join(stage))
        frontier = nxt

    # The control slice pins the nine parameters to a single assignment.  If a
    # corpus leaves one of them unconstrained, fall back to the first candidate
    # its domain offers rather than abandoning the derivation.
    rank = dict((name, dict((value, i) for i, value in enumerate(domain)))
                for name, domain in DOMAIN.items())
    frontier.sort(key=lambda partial: tuple(rank[n][partial[n]] for n in PARAM_NAMES
                                             if n in partial))
    selected = fill(frontier[0])

    # A recorded verdict for every candidate value in every parameter's domain,
    # judged with the rest of the assignment settled.
    record = []
    for name, control, domain in PARAMETERS:
        rejected = []
        for value in domain:
            if value == selected[name]:
                continue
            candidate = dict(selected)
            candidate[name] = value
            falsifier = ""
            for other in CONTROL_ORDER:
                state["evaluations"] += 1
                try:
                    if reproduce(export, candidate, tables, other) != published[other]:
                        falsifier = other
                        break
                except DecodeError:
                    falsifier = other
                    break
            if falsifier:
                rejected.append({"value": value, "control": falsifier})
        record.append({
            "name": name,
            "candidates": len(domain),
            "control": control,
            "selected": selected[name],
            "rejected": rejected,
        })

    return selected, record, state["evaluations"]


# --------------------------------------------------------------------------
# Corpus loading
# --------------------------------------------------------------------------

def corpus_paths(directory, prefix=""):
    return {
        "fault_frames": os.path.join(directory, prefix + "fault_frames.psv"),
        "runtime_probe": os.path.join(directory, prefix + "runtime_probe.csv"),
        "uplink": os.path.join(directory, prefix + "uplink_reports.jsonl"),
    }


def load_probe(path):
    table = {}
    for row in read_csv(path):
        table[(row["module_tag"], int(row["frame_index"]))] = {
            "samples": int(row["samples"]),
            "peak_ns": int(row["peak_ns"]),
        }
    return table


def load_uplink(path):
    table = {}
    for entry in read_jsonl(path):
        table[(entry["module_tag"], int(entry["frame_index"]))] = entry
    return table


def load_ladder(path):
    ladder = {}
    for row in read_csv(path):
        ladder.setdefault(row["component_key"], {})[int(row["tier_index"])] = int(row["budget_ns"])
    return ladder


def triage_corpus(corpus, tables, params):
    frames = decode_export(corpus["fault_frames"], params)
    accepted, rejections = classify(frames, tables["registry"], tables["windows"],
                                    load_probe(corpus["runtime_probe"]),
                                    load_uplink(corpus["uplink"]))
    modules, passes, quarantined = settle(accepted, tables["ladder"])
    ledger = build_ledger(accepted, modules, tables["zones"], tables["tenant"])
    return {
        "frame_count": len(frames),
        "ledger": ledger,
        "modules": modules,
        "passes": passes,
        "quarantined": quarantined,
        "rejections": rejections,
        "rejected_count": len(frames) - len(ledger),
    }


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, sort_keys=True)
        fh.write("\n")


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True, separators=(",", ":")))
            fh.write("\n")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main(argv):
    data_dir = argv[1] if len(argv) > 1 else "/app/data"
    out_dir = argv[2] if len(argv) > 2 else "/app/out"
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)

    tables = {
        "registry": Registry(read_csv(os.path.join(data_dir, "build_registry.csv"))),
        "ladder": load_ladder(os.path.join(data_dir, "severity_ladder.csv")),
        "windows": dict((row["component_key"],
                         {"open": int(row["open_tick"]), "close": int(row["close_tick"])})
                        for row in read_csv(os.path.join(data_dir, "support_windows.csv"))),
        "zones": dict((row["host_id"], row["zone"])
                      for row in read_csv(os.path.join(data_dir, "host_zones.csv"))),
        "tenant": dict((row["tenant_class"], row["tenant_segment"])
                       for row in read_csv(os.path.join(data_dir, "tenant_classes.csv"))),
    }
    zones = sorted(set(tables["zones"].values()))
    segments = sorted(set(tables["tenant"].values()))

    # Settle the withheld wire parameters against the published control
    # figures before computing any figure for the incident corpus.
    control_path = os.path.join(data_dir, "control-slice-figures.json")
    calibration = corpus_paths(data_dir, "calibration_")
    comparison = []
    search_record = []
    evaluations = 0
    params = dict((name, DOMAIN[name][0]) for name in PARAM_NAMES)

    if os.path.exists(control_path) and all(os.path.exists(p) for p in calibration.values()):
        with open(control_path, "r", encoding="utf-8") as fh:
            published = json.load(fh)
        params, search_record, evaluations = search_parameters(calibration, tables, published)

        for control in CONTROL_ORDER:
            comparison.append({
                "control": control,
                "published": published[control],
                "reproduced": reproduce(calibration, params, tables, control),
            })

    # With the parameter set settled, run the whole derivation on the incident
    # corpus from whatever the output directory held before.
    incident = corpus_paths(data_dir)
    result = triage_corpus(incident, tables, params)
    ledger = result["ledger"]

    write_jsonl(os.path.join(out_dir, "normalized.jsonl"), ledger)
    write_json(os.path.join(out_dir, "summary.json"),
               incident_summary(ledger, result["rejected_count"]))
    write_json(os.path.join(out_dir, "product_summary.json"),
               product_summary(ledger, result["modules"], tables["ladder"]))
    for zone in zones:
        write_json(os.path.join(out_dir, "region_%s.json" % zone), zone_report(zone, ledger))
    for segment in segments:
        write_json(os.path.join(out_dir, "segment_%s.json" % segment),
                   segment_report(segment, ledger))

    allocated = sum(row["bytes_allocated"] for row in ledger)
    uploaded = sum(row["bytes_uploaded"] for row in ledger)
    write_json(os.path.join(out_dir, "data_quality.json"), {
        "accepted_count": len(ledger),
        "frame_count": result["frame_count"],
        "rejections": dict(sorted(result["rejections"].items())),
        "reconciliation": {
            "allocated_bytes": allocated,
            "uploaded_bytes": uploaded,
            "residual_bytes": allocated - uploaded,
        },
        "settlement": {
            "passes": result["passes"],
            "quarantined_modules": result["quarantined"],
        },
        "calibration_comparison": comparison,
        "candidate_search": {
            "evaluations": evaluations,
            "parameters": search_record,
        },
    })


if __name__ == "__main__":
    main(sys.argv)
