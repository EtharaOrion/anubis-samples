#!/usr/bin/env python3
"""Fault-ledger reconstruction for a crash-reporter incident window.

Usage:  python3 transform.py [DATA_DIR] [OUT_DIR]

Reads the crash reporter dump export, the runtime probe export, the uplink
export and the five reference tables from DATA_DIR; settles the nine withheld
wire parameters against the published control-slice figures; then writes the
fault ledger, the roll-ups and the reconciliation record into OUT_DIR.

Standard library only.  All clock, byte and residency arithmetic is done in
Python integers.
"""

import csv
import io
import itertools
import json
import os
import sys

# --------------------------------------------------------------------------
# wire constants
# --------------------------------------------------------------------------

FLAG_DIFF2 = 0x001        # resident span column carries second-order differences
FLAG_ENTRYSUM = 0x002     # entry column carries running pairwise sums
FLAG_UNWINDRUN = 0x004    # unwind column carries the running resident total
FLAG_ROT = 0x008          # fault frame lines rotated left
FLAG_COUNTBIAS = 0x010    # declared count carries an ordinal bias
FLAG_RADIX = 0x020        # base field written in the alternate radix
FLAG_ALIAS = 0x100        # module column carries a registry alias
FLAG_SPLITBASE = 0x800    # base field carries the high part of the dump base

F_IDXPLACE = 0x040        # frame index column is a dump-local placeholder
F_SIGROT = 0x080          # fault signature column rotated left
F_COARSE = 0x200          # allocation column carries coarse units
F_RING = 0x400            # fault signature column carries a ring distance

DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"
HEXSET = frozenset("0123456789abcdef")

CHECK_MODULUS = 65536
SIGNATURE_NIBBLES = 16
SIGNATURE_SUM_MODULUS = 1000000007
COUNT_BIAS_MODULUS = 9
TICKS_PER_BUILD_INDEX = 900000000

REJECTION_REASONS = (
    "outside_support",
    "retired_build",
    "unprobed_frame",
    "unreported_dump",
    "unresolved_module",
)

ZONE_REPORT_PREFIX = "region_"
SEGMENT_REPORT_PREFIX = "segment_"


class WireError(Exception):
    """A candidate parameter assignment contradicts the shipped wire data."""


# --------------------------------------------------------------------------
# candidate domains for the nine withheld wire parameters
# --------------------------------------------------------------------------

CANDIDATE_DOMAINS = {
    "entry_clock_modulus": [1 << e for e in range(22, 35)],
    "drift_period": [48, 96, 192, 384, 768],
    "rotation_offset": list(range(0, 8)),
    "count_multiplier": list(range(0, 9)),
    "alternate_radix": list(range(11, 36)),
    "signature_offset": list(range(0, 8)),
    "coarse_unit": [64, 128, 256, 512, 1024],
    "ring_width": [4, 8, 12, 16],
    "base_shift_width": [8, 10, 12, 16],
}

# Each stage settles one group of parameters against the control figure that
# is the first published figure sensitive to it and to nothing still unknown.
SEARCH_STAGES = (
    (("count_multiplier",), "CTRL-A"),
    (("rotation_offset",), "CTRL-C"),
    (("entry_clock_modulus",), "CTRL-D"),
    (("drift_period",), "CTRL-E"),
    (("alternate_radix", "base_shift_width"), "CTRL-B"),
    (("coarse_unit",), "CTRL-F"),
    (("signature_offset", "ring_width"), "CTRL-G"),
)

CONTROL_NAMES = ("CTRL-A", "CTRL-B", "CTRL-C", "CTRL-D", "CTRL-E", "CTRL-F",
                 "CTRL-G", "CTRL-H", "CTRL-I", "CTRL-J", "CTRL-K")


# --------------------------------------------------------------------------
# reporter export reader
# --------------------------------------------------------------------------

def read_dump_export(path):
    """Split a pipe-delimited reporter export into dumps and their lines."""
    dumps = []
    current = None
    with io.open(path, "r", encoding="utf-8") as handle:
        for raw in handle:
            line = raw.rstrip("\n").rstrip("\r")
            if not line or line.startswith("#"):
                continue
            cols = line.split("|")
            if cols[0] == "@":
                if len(cols) != 8:
                    raise WireError("malformed dump header")
                current = {
                    "dump_id": cols[1],
                    "module_tag": cols[2],
                    "declared_count": int(cols[3], 10),
                    "base_field": cols[4],
                    "flags": int(cols[5], 16),
                    "dump_key": int(cols[6], 10),
                    "check_total": int(cols[7], 16),
                    "lines": [],
                }
                dumps.append(current)
            else:
                if current is None:
                    raise WireError("frame line before any dump header")
                current["lines"].append(cols)
    return dumps


def frame_check_total(cols):
    """Sum of the character codes of the eight preceding columns, mod 65536."""
    return sum(ord(ch) for ch in "|".join(cols[0:8])) % CHECK_MODULUS


def parse_in_radix(text, radix):
    value = 0
    if not text:
        raise WireError("empty numeral")
    for ch in text:
        digit = DIGITS.find(ch)
        if digit < 0 or digit >= radix:
            raise WireError("digit outside the radix")
        value = value * radix + digit
    return value


# --------------------------------------------------------------------------
# phase 0: frame / trailer split, wire closure checks, authoritative indices
# --------------------------------------------------------------------------

def split_dumps(dumps, count_multiplier):
    groups = []
    for ordinal, dump in enumerate(dumps, start=1):
        count = dump["declared_count"]
        if dump["flags"] & FLAG_COUNTBIAS:
            count -= (ordinal * count_multiplier) % COUNT_BIAS_MODULUS
        if count < 1 or count > len(dump["lines"]):
            raise WireError("fault frame count outside the lines of the dump")
        frames = dump["lines"][:count]
        trailers = dump["lines"][count:]
        for cols in frames:
            if len(cols) != 9:
                raise WireError("fault frame does not carry nine columns")
            if frame_check_total(cols) != int(cols[8], 16):
                raise WireError("frame check total disagrees with its columns")
        running = sum(int(cols[8], 16) for cols in frames) % CHECK_MODULUS
        if running != dump["check_total"]:
            raise WireError("dump check total disagrees with its fault frames")
        if (dump["flags"] & FLAG_SPLITBASE) and not trailers:
            raise WireError("split-base dump carries no trailer line")
        indices = []
        for cols in frames:
            fflags = int(cols[6], 16)
            if fflags & F_IDXPLACE:
                indices.append(int(cols[7].rsplit(":", 1)[1], 10))
            else:
                indices.append(int(cols[0], 10))
        groups.append({
            "dump": dump,
            "count": count,
            "frames": frames,
            "trailers": trailers,
            "indices": indices,
        })
    return groups


def logical_order(group, rotation_offset):
    """Positions of the dump's written frame lines in logical order."""
    dump = group["dump"]
    count = group["count"]
    if dump["flags"] & FLAG_ROT:
        shift = (dump["dump_key"] + rotation_offset) % count
        return [(j - shift) % count for j in range(count)]
    return list(range(count))


def resident_spans(group, order):
    raw = [int(group["frames"][i][3], 10) for i in order]
    if not (group["dump"]["flags"] & FLAG_DIFF2):
        return raw
    spans = [raw[0]]
    if len(raw) > 1:
        delta = raw[1]
        spans.append(spans[0] + delta)
        for i in range(2, len(raw)):
            delta += raw[i]
            spans.append(spans[-1] + delta)
    return spans


def dump_base(group, alternate_radix, base_shift_width):
    dump = group["dump"]
    field = dump["base_field"]
    if dump["flags"] & FLAG_RADIX:
        high = parse_in_radix(field, alternate_radix)
    else:
        if not field.isdigit():
            raise WireError("base field is not a decimal numeral")
        high = int(field, 10)
    if dump["flags"] & FLAG_SPLITBASE:
        low = int(group["trailers"][0][0], 10)
        if low < 0 or low >= (1 << base_shift_width):
            raise WireError("low part does not fit the shifted-in positions")
        base = (high << base_shift_width) | low
    else:
        base = high
    if base < 0:
        raise WireError("dump base is negative")
    return base


def entry_stamps(group, order, entry_clock_modulus):
    column = [int(group["frames"][i][1], 10) for i in order]
    if not (group["dump"]["flags"] & FLAG_ENTRYSUM):
        return column
    stamps = [column[0]]
    for i in range(1, len(column)):
        stamps.append((column[i] - stamps[i - 1]) % entry_clock_modulus)
    return stamps


def allocated_bytes(cols, coarse_unit):
    fflags = int(cols[6], 16)
    field = cols[4]
    if fflags & F_COARSE:
        if not field.startswith("~"):
            raise WireError("coarse allocation without its marker")
        return int(field[1:], 10) * coarse_unit
    if field.startswith("~"):
        raise WireError("unexpected coarse allocation marker")
    return int(field, 10)


def recover_signature(cols, index, ring, position, ring_width, signature_offset):
    """Return (signature, new_position) after writing it into the ring."""
    field = cols[5]
    fflags = int(cols[6], 16)
    if fflags & F_RING:
        if not field.startswith("^"):
            raise WireError("ring reference without its marker")
        distance = int(field[1:], 10)
        if distance < 1:
            raise WireError("ring distance is not positive")
        signature = ring[(position - distance) % ring_width]
        if signature is None:
            raise WireError("ring distance reaches a slot never written")
    else:
        if field.startswith("^"):
            raise WireError("unexpected ring reference marker")
        if len(field) != SIGNATURE_NIBBLES or not set(field) <= HEXSET:
            raise WireError("fault signature is not sixteen hex characters")
        if fflags & F_SIGROT:
            turn = (index + signature_offset) % SIGNATURE_NIBBLES
            signature = field[-turn:] + field[:-turn] if turn else field
        else:
            signature = field
    if len(signature) != SIGNATURE_NIBBLES or not set(signature) <= HEXSET:
        raise WireError("recovered signature is not sixteen hex characters")
    ring[position] = signature
    return signature, (position + 1) % ring_width


# --------------------------------------------------------------------------
# per-control evaluation, used by the candidate search
# --------------------------------------------------------------------------

def evaluate_control(dumps, params, control):
    """Reproduce a single wire-level control figure, or raise WireError."""
    groups = split_dumps(dumps, params["count_multiplier"])

    if control == "CTRL-A":
        return sum(g["count"] for g in groups)
    if control == "CTRL-H":
        return sum(sum(g["indices"]) for g in groups)
    if control == "CTRL-B":
        return sum(dump_base(g, params["alternate_radix"],
                             params["base_shift_width"]) for g in groups)
    if control == "CTRL-F":
        return sum(allocated_bytes(cols, params["coarse_unit"])
                   for g in groups for cols in g["frames"])

    orders = [logical_order(g, params["rotation_offset"]) for g in groups]

    if control == "CTRL-C":
        return sum(sum(resident_spans(g, o)) for g, o in zip(groups, orders))
    if control == "CTRL-D":
        modulus = params["entry_clock_modulus"]
        return sum(sum(entry_stamps(g, o, modulus))
                   for g, o in zip(groups, orders))
    if control == "CTRL-E":
        period = params["drift_period"]
        total = 0
        running = 0
        for group, order in zip(groups, orders):
            spans = resident_spans(group, order)
            shifted = bool(group["dump"]["flags"] & FLAG_UNWINDRUN)
            for slot, i in enumerate(order):
                column = int(group["frames"][i][2], 10)
                total += column - (running % period) if shifted else column
                running += spans[slot]
        return total
    if control == "CTRL-G":
        width = params["ring_width"]
        offset = params["signature_offset"]
        ring = [None] * width
        position = 0
        total = 0
        for group, order in zip(groups, orders):
            for i in order:
                signature, position = recover_signature(
                    group["frames"][i], group["indices"][i], ring, position,
                    width, offset)
                total += int(signature, 16)
        return total % SIGNATURE_SUM_MODULUS
    raise WireError("control figure is not reproducible on its own")


# --------------------------------------------------------------------------
# full reporter decode
# --------------------------------------------------------------------------

def decode_export(dumps, params):
    """Recover every fault frame of an export, in logical order."""
    groups = split_dumps(dumps, params["count_multiplier"])
    orders = [logical_order(g, params["rotation_offset"]) for g in groups]
    modulus = params["entry_clock_modulus"]
    period = params["drift_period"]
    width = params["ring_width"]
    offset = params["signature_offset"]

    ring = [None] * width
    position = 0
    running = 0
    frames = []
    bases = []
    for group, order in zip(groups, orders):
        dump = group["dump"]
        base = dump_base(group, params["alternate_radix"],
                         params["base_shift_width"])
        bases.append(base)
        spans = resident_spans(group, order)
        stamps = entry_stamps(group, order, modulus)
        shifted = bool(dump["flags"] & FLAG_UNWINDRUN)
        for slot, i in enumerate(order):
            cols = group["frames"][i]
            column = int(cols[2], 10)
            unwind = column - (running % period) if shifted else column
            allocation = allocated_bytes(cols, params["coarse_unit"])
            signature, position = recover_signature(
                cols, group["indices"][i], ring, position, width, offset)
            running += spans[slot]
            if unwind > stamps[slot]:
                raise WireError("unwind stamp stands after its entry stamp")
            frames.append({
                "source_dump": dump["dump_id"],
                "reported_tag": dump["module_tag"],
                "frame_index": group["indices"][i],
                "enter_ns": stamps[slot],
                "unwind_ns": unwind,
                "cost_ns": spans[slot],
                "bytes_allocated": allocation,
                "fault_signature": signature,
                "symbol": cols[7].rsplit(":", 1)[0],
                "capture_tick": base + ((stamps[slot] - stamps[0]) % modulus),
            })
    return frames, bases


# --------------------------------------------------------------------------
# reference tables
# --------------------------------------------------------------------------

def read_csv_rows(path):
    with io.open(path, "r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def load_reference_tables(data_dir):
    registry = {}
    for row in read_csv_rows(os.path.join(data_dir, "build_registry.csv")):
        registry.setdefault(row["module_tag"], []).append({
            "canonical_tag": row["canonical_tag"],
            "component_key": row["component_key"],
            "tier_index": int(row["tier_index"]),
            "from_build": int(row["effective_from_build"]),
            "to_build": int(row["effective_to_build"]),
            "status": row["status"],
        })
    ladder = {}
    for row in read_csv_rows(os.path.join(data_dir, "severity_ladder.csv")):
        ladder.setdefault(row["component_key"], {})[int(row["tier_index"])] = \
            int(row["budget_ns"])
    windows = {}
    for row in read_csv_rows(os.path.join(data_dir, "support_windows.csv")):
        windows[row["component_key"]] = (int(row["open_tick"]),
                                         int(row["close_tick"]))
    zones = {}
    zone_names = []
    for row in read_csv_rows(os.path.join(data_dir, "host_zones.csv")):
        zones[row["host_id"]] = row["zone"]
        if row["zone"] not in zone_names:
            zone_names.append(row["zone"])
    segments = {}
    segment_names = []
    for row in read_csv_rows(os.path.join(data_dir, "tenant_classes.csv")):
        segments[row["tenant_class"]] = row["tenant_segment"]
        if row["tenant_segment"] not in segment_names:
            segment_names.append(row["tenant_segment"])
    return {
        "registry": registry,
        "ladder": ladder,
        "windows": windows,
        "zones": zones,
        "segments": segments,
        "zone_names": sorted(zone_names),
        "segment_names": sorted(segment_names),
    }


def resolve_module(registry, tag, build_index):
    """Follow the registry chain on a build index; None when unresolved."""
    seen = set()
    current = tag
    while True:
        if current in seen:
            return None
        seen.add(current)
        chosen = None
        for row in registry.get(current, ()):
            if row["from_build"] <= build_index <= row["to_build"]:
                chosen = row
                break
        if chosen is None:
            return None
        if chosen["canonical_tag"] == current:
            return {
                "module_tag": current,
                "component_key": chosen["component_key"],
                "declared_tier": chosen["tier_index"],
                "status": chosen["status"],
            }
        current = chosen["canonical_tag"]


def load_side_exports(data_dir, prefix):
    probe = {}
    path = os.path.join(data_dir, prefix + "runtime_probe.csv")
    for row in read_csv_rows(path):
        probe[(row["module_tag"], int(row["frame_index"]))] = (
            int(row["samples"]), int(row["peak_ns"]))
    uplink = {}
    path = os.path.join(data_dir, prefix + "uplink_reports.jsonl")
    with io.open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            uplink[(obj["module_tag"], int(obj["frame_index"]))] = (
                obj["host_id"], obj["tenant_class"], int(obj["bytes_uploaded"]))
    return probe, uplink


# --------------------------------------------------------------------------
# acceptance
# --------------------------------------------------------------------------

def screen_frames(frames, tables, probe, uplink):
    registry = tables["registry"]
    windows = tables["windows"]
    zones = tables["zones"]
    segments = tables["segments"]

    accepted = []
    rejections = dict((reason, 0) for reason in REJECTION_REASONS)
    rejected_count = 0

    for frame in frames:
        build_index = frame["capture_tick"] // TICKS_PER_BUILD_INDEX
        resolved = resolve_module(registry, frame["reported_tag"], build_index)
        reasons = []
        if resolved is None:
            reasons.append("unresolved_module")
        elif resolved["status"] != "supported":
            reasons.append("retired_build")
        else:
            component = resolved["component_key"]
            open_tick, close_tick = windows[component]
            if not (open_tick < frame["capture_tick"] < close_tick):
                reasons.append("outside_support")
            key = (resolved["module_tag"], frame["frame_index"])
            if key not in probe:
                reasons.append("unprobed_frame")
            if key not in uplink:
                reasons.append("unreported_dump")
        if reasons:
            for reason in reasons:
                rejections[reason] += 1
            rejected_count += 1
            continue
        samples, peak_ns = probe[key]
        host_id, tenant_class, bytes_uploaded = uplink[key]
        row = dict(frame)
        row.update({
            "module_tag": resolved["module_tag"],
            "component_key": resolved["component_key"],
            "declared_tier": resolved["declared_tier"],
            "samples": samples,
            "peak_ns": peak_ns,
            "host_zone": zones[host_id],
            "tenant_segment": segments[tenant_class],
            "bytes_uploaded": bytes_uploaded,
        })
        accepted.append(row)

    accepted.sort(key=lambda r: r["capture_tick"])
    return accepted, rejections, rejected_count


# --------------------------------------------------------------------------
# quarantine settlement
# --------------------------------------------------------------------------

def _allowances(modules, tiers, ladder):
    envelope = {}
    for tag, info in modules.items():
        component = info["component_key"]
        budget = ladder[component][tiers[tag]]
        if component in envelope:
            envelope[component] = min(envelope[component], budget)
        else:
            envelope[component] = budget
    envelope = dict((c, b * len(ladder[c])) for c, b in envelope.items())
    allowance = {}
    for tag, info in modules.items():
        component = info["component_key"]
        allowance[tag] = min(ladder[component][tiers[tag]], envelope[component])
    return allowance


def settle(accepted, ladder):
    modules = {}
    for row in accepted:
        info = modules.setdefault(row["module_tag"], {
            "component_key": row["component_key"],
            "declared_tier": row["declared_tier"],
            "frames": [],
        })
        info["frames"].append(row)

    tiers = dict((tag, info["declared_tier"]) for tag, info in modules.items())
    passes = 0
    while True:
        passes += 1
        allowance = _allowances(modules, tiers, ladder)
        demote = []
        for tag, info in modules.items():
            within = sum(1 for f in info["frames"]
                         if f["peak_ns"] <= allowance[tag])
            over = len(info["frames"]) - within
            if tiers[tag] > 0 and over > within:
                demote.append(tag)
        if not demote:
            break
        for tag in demote:
            tiers[tag] -= 1

    allowance = _allowances(modules, tiers, ladder)
    quarantined = []
    for tag, info in modules.items():
        within = sum(1 for f in info["frames"] if f["peak_ns"] <= allowance[tag])
        if tiers[tag] == 0 and (len(info["frames"]) - within) > within:
            quarantined.append(tag)
    return modules, tiers, allowance, sorted(quarantined), passes


# --------------------------------------------------------------------------
# corpus derivation
# --------------------------------------------------------------------------

def derive_corpus(data_dir, prefix, params, tables):
    dumps = read_dump_export(os.path.join(data_dir, prefix + "fault_frames.psv"))
    frames, bases = decode_export(dumps, params)
    probe, uplink = load_side_exports(data_dir, prefix)
    accepted, rejections, rejected_count = screen_frames(
        frames, tables, probe, uplink)
    modules, tiers, allowance, quarantined, passes = settle(
        accepted, tables["ladder"])
    for row in accepted:
        row["effective_tier"] = tiers[row["module_tag"]]
        row["within_budget"] = row["peak_ns"] <= allowance[row["module_tag"]]
    allocated = sum(r["bytes_allocated"] for r in accepted)
    uploaded = sum(r["bytes_uploaded"] for r in accepted)
    return {
        "dumps": dumps,
        "bases": bases,
        "frames": frames,
        "accepted": accepted,
        "rejections": rejections,
        "rejected_count": rejected_count,
        "modules": modules,
        "tiers": tiers,
        "allowance": allowance,
        "quarantined": quarantined,
        "passes": passes,
        "allocated_bytes": allocated,
        "uploaded_bytes": uploaded,
        "residual_bytes": allocated - uploaded,
    }


# --------------------------------------------------------------------------
# candidate search over the withheld wire parameters
# --------------------------------------------------------------------------

def corpus_control(derived, control):
    if control == "CTRL-I":
        return len(derived["accepted"])
    if control == "CTRL-J":
        return len(derived["quarantined"])
    if control == "CTRL-K":
        return derived["residual_bytes"]
    raise WireError("not a corpus-level control figure")


def settle_parameters(data_dir, tables, published):
    """Enumerate every candidate of every withheld parameter and settle them.

    A stage sweeps one group of parameters against the control figure that is
    the first published figure sensitive to that group and to nothing still
    unknown.  A stage that leaves several candidates standing does not choose;
    it carries them all forward, and a later stage - or, in the last resort,
    the corpus-level figures - narrows them.  Every candidate of every domain
    ends up either selected or rejected against a named control figure.
    """
    dumps = read_dump_export(
        os.path.join(data_dir, "calibration_fault_frames.psv"))

    survivors = [{}]                 # partial assignments still standing
    settled_order = []               # parameter names, in the order swept
    stage_control = {}               # name -> control figure that swept it
    eliminated = dict((name, {}) for name in CANDIDATE_DOMAINS)
    evaluations = 0

    def prune(control):
        """Record every candidate that no surviving assignment still keeps."""
        for name in settled_order:
            alive = set(a[name] for a in survivors)
            for value in CANDIDATE_DOMAINS[name]:
                if value not in alive and value not in eliminated[name]:
                    eliminated[name][value] = control

    for group, control in SEARCH_STAGES:
        if control not in published:
            continue
        combos = list(itertools.product(*[CANDIDATE_DOMAINS[n] for n in group]))
        nxt = []
        for partial in survivors:
            for combo in combos:
                trial = dict(partial)
                trial.update(dict(zip(group, combo)))
                evaluations += 1
                try:
                    value = evaluate_control(dumps, trial, control)
                except (WireError, ValueError, IndexError, KeyError):
                    value = None
                if value == published[control]:
                    nxt.append(trial)
        survivors = nxt
        settled_order.extend(group)
        for name in group:
            stage_control[name] = control
        if not survivors:
            raise WireError("no candidate assignment reproduces %s" % control)
        prune(control)

    # Any assignment still standing now reproduces every wire-level figure the
    # stages used; the remaining published figures break the last ties.
    for control in ("CTRL-H", "CTRL-I", "CTRL-J", "CTRL-K"):
        if len(survivors) <= 1 or control not in published:
            continue
        kept = []
        for trial in survivors:
            evaluations += 1
            try:
                if control == "CTRL-H":
                    value = evaluate_control(dumps, trial, control)
                else:
                    value = corpus_control(
                        derive_corpus(data_dir, "calibration_", trial, tables),
                        control)
            except (WireError, ValueError, IndexError, KeyError):
                value = None
            if value == published[control]:
                kept.append(trial)
        survivors = kept
        if not survivors:
            raise WireError("no candidate assignment reproduces %s" % control)
        prune(control)

    if len(survivors) != 1:
        raise WireError("the published figures leave %d assignments standing"
                        % len(survivors))
    params = survivors[0]

    records = []
    for name in settled_order:
        records.append({
            "name": name,
            "candidates": len(CANDIDATE_DOMAINS[name]),
            "control": stage_control[name],
            "selected": params[name],
            "rejected": [{"value": value, "control": eliminated[name][value]}
                         for value in CANDIDATE_DOMAINS[name]
                         if value != params[name]],
        })
    return params, {"evaluations": evaluations, "parameters": records}


def compare_against_controls(data_dir, params, tables, published):
    dumps = read_dump_export(
        os.path.join(data_dir, "calibration_fault_frames.psv"))
    derived = derive_corpus(data_dir, "calibration_", params, tables)
    reproduced = {}
    for control in CONTROL_NAMES:
        if control in ("CTRL-I", "CTRL-J", "CTRL-K"):
            reproduced[control] = corpus_control(derived, control)
        else:
            reproduced[control] = evaluate_control(dumps, params, control)
    comparison = []
    for control in CONTROL_NAMES:
        if control not in published:
            continue
        comparison.append({
            "control": control,
            "published": published[control],
            "reproduced": reproduced[control],
        })
    mismatched = [c["control"] for c in comparison
                  if c["published"] != c["reproduced"]]
    if mismatched:
        raise WireError("settled parameters miss control figures: %s"
                        % ", ".join(mismatched))
    return comparison


# --------------------------------------------------------------------------
# reports
# --------------------------------------------------------------------------

LEDGER_FIELDS = ("bytes_allocated", "bytes_uploaded", "capture_tick",
                 "component_key", "cost_ns", "effective_tier", "enter_ns",
                 "fault_signature", "frame_index", "host_zone", "module_tag",
                 "peak_ns", "samples", "source_dump", "symbol",
                 "tenant_segment", "unwind_ns", "within_budget")


def ledger_rows(accepted):
    return [dict((f, row[f]) for f in LEDGER_FIELDS) for row in accepted]


def _bucket_bytes(rows, key):
    out = {}
    for row in rows:
        out[row[key]] = out.get(row[key], 0) + row["bytes_uploaded"]
    return out


def build_summary(derived):
    accepted = derived["accepted"]
    by_tier = {}
    for row in accepted:
        name = str(row["effective_tier"])
        by_tier[name] = by_tier.get(name, 0) + 1
    return {
        "accepted_count": len(accepted),
        "by_component": _bucket_bytes(accepted, "component_key"),
        "by_effective_tier": by_tier,
        "by_tenant_segment": _bucket_bytes(accepted, "tenant_segment"),
        "by_zone": _bucket_bytes(accepted, "host_zone"),
        "rejected_count": derived["rejected_count"],
        "total_bytes_allocated": derived["allocated_bytes"],
        "total_bytes_uploaded": derived["uploaded_bytes"],
        "total_cost_ns": sum(r["cost_ns"] for r in accepted),
        "within_budget_count": sum(1 for r in accepted if r["within_budget"]),
    }


def build_product_summary(derived, ladder):
    modules = []
    for tag in sorted(derived["modules"]):
        info = derived["modules"][tag]
        tier = derived["tiers"][tag]
        allowance = derived["allowance"][tag]
        rows = info["frames"]
        modules.append({
            "accepted_count": len(rows),
            "allowance_ns": allowance,
            "budget_ns": ladder[info["component_key"]][tier],
            "component_key": info["component_key"],
            "declared_tier": info["declared_tier"],
            "effective_tier": tier,
            "module_tag": tag,
            "net_samples": sum(r["samples"] for r in rows),
            "quarantined": tag in derived["quarantined"],
            "total_bytes_uploaded": sum(r["bytes_uploaded"] for r in rows),
            "within_budget_count": sum(1 for r in rows if r["within_budget"]),
        })
    return {"modules": modules}


def build_zone_report(accepted, zone):
    rows = [r for r in accepted if r["host_zone"] == zone]
    return {
        "accepted_count": len(rows),
        "by_component": _bucket_bytes(rows, "component_key"),
        "by_tenant_segment": _bucket_bytes(rows, "tenant_segment"),
        "modules": sorted(set(r["module_tag"] for r in rows)),
        "total_bytes_uploaded": sum(r["bytes_uploaded"] for r in rows),
        "total_cost_ns": sum(r["cost_ns"] for r in rows),
        "within_budget_count": sum(1 for r in rows if r["within_budget"]),
        "zone": zone,
    }


def build_segment_report(accepted, segment):
    rows = [r for r in accepted if r["tenant_segment"] == segment]
    return {
        "accepted_count": len(rows),
        "by_component": _bucket_bytes(rows, "component_key"),
        "by_zone": _bucket_bytes(rows, "host_zone"),
        "modules": sorted(set(r["module_tag"] for r in rows)),
        "tenant_segment": segment,
        "total_bytes_uploaded": sum(r["bytes_uploaded"] for r in rows),
        "total_cost_ns": sum(r["cost_ns"] for r in rows),
        "within_budget_count": sum(1 for r in rows if r["within_budget"]),
    }


def build_data_quality(derived, comparison, search):
    return {
        "accepted_count": len(derived["accepted"]),
        "calibration_comparison": comparison,
        "candidate_search": search,
        "frame_count": len(derived["frames"]),
        "reconciliation": {
            "allocated_bytes": derived["allocated_bytes"],
            "residual_bytes": derived["residual_bytes"],
            "uploaded_bytes": derived["uploaded_bytes"],
        },
        "rejections": dict(derived["rejections"]),
        "settlement": {
            "passes": derived["passes"],
            "quarantined_modules": list(derived["quarantined"]),
        },
    }


# --------------------------------------------------------------------------
# serialisation
# --------------------------------------------------------------------------

def write_json(out_dir, name, payload):
    path = os.path.join(out_dir, name)
    with io.open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True, indent=2))
        handle.write("\n")


def write_ledger(out_dir, name, rows):
    path = os.path.join(out_dir, name)
    with io.open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True,
                                    separators=(",", ":")))
            handle.write("\n")


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def run(data_dir, out_dir):
    tables = load_reference_tables(data_dir)

    controls_path = os.path.join(data_dir, "control-slice-figures.json")
    calibration_path = os.path.join(data_dir, "calibration_fault_frames.psv")
    have_controls = (os.path.exists(controls_path) and
                     os.path.exists(calibration_path))

    if have_controls:
        with io.open(controls_path, "r", encoding="utf-8") as handle:
            published = json.load(handle)
        params, search = settle_parameters(data_dir, tables, published)
        comparison = compare_against_controls(data_dir, params, tables,
                                              published)
    else:
        # No control slice ships beside this corpus; the flag words that the
        # withheld parameters govern are then unexercised.
        params = dict((n, d[0]) for n, d in CANDIDATE_DOMAINS.items())
        search = {"evaluations": 0, "parameters": []}
        comparison = []

    derived = derive_corpus(data_dir, "", params, tables)

    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)

    write_ledger(out_dir, "normalized.jsonl", ledger_rows(derived["accepted"]))
    write_json(out_dir, "summary.json", build_summary(derived))
    write_json(out_dir, "product_summary.json",
               build_product_summary(derived, tables["ladder"]))
    for zone in tables["zone_names"]:
        write_json(out_dir, "%s%s.json" % (ZONE_REPORT_PREFIX, zone),
                   build_zone_report(derived["accepted"], zone))
    for segment in tables["segment_names"]:
        write_json(out_dir, "%s%s.json" % (SEGMENT_REPORT_PREFIX, segment),
                   build_segment_report(derived["accepted"], segment))
    write_json(out_dir, "data_quality.json",
               build_data_quality(derived, comparison, search))
    return derived


def main(argv):
    data_dir = argv[1] if len(argv) > 1 else "/app/data"
    out_dir = argv[2] if len(argv) > 2 else "/app/out"
    run(data_dir, out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
