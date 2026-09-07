#!/usr/bin/env python3
"""Fault-ledger reconstruction for a crash-reporter incident window.

Usage:  python3 transform.py [DATA_DIR] [OUT_DIR]

Reads the crash reporter dump export, the runtime probe export, the uplink
export and the five reference tables out of DATA_DIR; settles the nine withheld
wire parameters against the published control-slice figures; then writes the
triage set (fault ledger, roll-ups, zone/segment reports and the data-quality
record) into OUT_DIR.

Standard library only.  All clock, byte and residency arithmetic is carried in
Python integers.
"""

import csv
import json
import os
import sys

# --------------------------------------------------------------------------
# constants fixed by the reporter notes (not corpus specific)
# --------------------------------------------------------------------------

TICKS_PER_BUILD_INDEX = 900000000        # 1000 ticks/ms * 900000 ms
SIGNATURE_WIDTH = 16                     # hex chars in a fault signature
CHECK_MODULUS = 65536
SIGNATURE_MODULUS = 1000000007           # CTRL-G reduction
COUNT_BIAS_MODULUS = 9
HEXDIGITS = set("0123456789abcdef")
RADIX_DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"

# dump flag word
F_SECOND_ORDER_SPANS = 0x001
F_ENTRY_CHAIN        = 0x002
F_UNWIND_DRIFT       = 0x004
F_ROTATED            = 0x008
F_COUNT_BIAS         = 0x010
F_ALT_RADIX          = 0x020
F_ALIAS              = 0x100
F_SPLIT_BASE         = 0x800

# frame flag word
G_INDEX_IN_SYMBOL    = 0x040
G_SIG_ROTATED        = 0x080
G_COARSE_ALLOC       = 0x200
G_SIG_RING           = 0x400

# candidate domains published with the control slice
CANDIDATE_DOMAINS = {
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

# Which control figure settles which parameter, and in what order.  Each stage
# is settled with every earlier stage already fixed; a stage's control never
# depends on a parameter settled in a later stage.
SEARCH_PLAN = [
    (["count_multiplier"], "CTRL-A"),
    (["rotation_offset"], "CTRL-C"),
    (["alternate_radix", "base_shift_width"], "CTRL-B"),
    (["entry_clock_modulus"], "CTRL-D"),
    (["drift_period"], "CTRL-E"),
    (["coarse_unit"], "CTRL-F"),
    (["signature_offset", "ring_width"], "CTRL-G"),
]

REJECTION_KINDS = [
    "outside_support",
    "retired_build",
    "unprobed_frame",
    "unreported_dump",
    "unresolved_module",
]


class WireError(Exception):
    """The export cannot be decoded under the parameter values offered."""


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def parse_radix(text, radix):
    """Read a numeral written with digits 0-9 then a-z in the given radix."""
    if not text:
        raise WireError("empty numeral")
    value = 0
    for ch in text:
        digit = RADIX_DIGITS.find(ch)
        if digit < 0 or digit >= radix:
            raise WireError("digit outside radix")
        value = value * radix + digit
    return value


def rotate_right(text, amount):
    amount %= len(text)
    if amount == 0:
        return text
    return text[-amount:] + text[:-amount]


def frame_check(columns):
    """Sum of the character codes of the eight columns joined by the pipe."""
    return sum(ord(c) for c in "|".join(columns[:8])) % CHECK_MODULUS


def read_csv_rows(path):
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def read_jsonl(path):
    rows = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


# --------------------------------------------------------------------------
# reporter dump export
# --------------------------------------------------------------------------

def load_dumps(path):
    """Split the pipe-delimited export into dumps of header + following lines."""
    dumps = []
    current = None
    with open(path, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.rstrip("\n")
            if not line.strip() or line.startswith("#"):
                continue
            cols = line.split("|")
            if cols[0] == "@":
                current = {
                    "dump_id": cols[1],
                    "module_column": cols[2],
                    "declared_count": int(cols[3]),
                    "base_field": cols[4],
                    "flags": int(cols[5], 16),
                    "key": int(cols[6]),
                    "check": int(cols[7], 16),
                    "lines": [],
                }
                dumps.append(current)
            else:
                if current is None:
                    raise WireError("frame line before any dump header")
                current["lines"].append(cols)
    return dumps


def dump_structure(dumps, count_multiplier, rotation_offset):
    """Split each dump into its fault frames (logical order) and its trailers.

    The declared count -- de-biased when 0x010 is set -- says how many of the
    lines following a header are fault frames; the rest are trailers.  The dump
    and frame check totals are verified against the columns they cover before
    any decoded field is trusted, and the rotation of 0x008 is undone.
    """
    structure = []
    for ordinal, dump in enumerate(dumps, 1):
        flags = dump["flags"]
        count = dump["declared_count"]
        if flags & F_COUNT_BIAS:
            count -= (ordinal * count_multiplier) % COUNT_BIAS_MODULUS
        if count < 1 or count > len(dump["lines"]):
            raise WireError("declared count outside the dump's line range")

        written = dump["lines"][:count]
        trailers = dump["lines"][count:]

        for cols in written:
            if len(cols) != 9:
                raise WireError("fault frame is not nine columns")
            if frame_check(cols) != int(cols[8], 16):
                raise WireError("frame check total mismatch")
        if sum(frame_check(c) for c in written) % CHECK_MODULUS != dump["check"]:
            raise WireError("dump check total mismatch")
        if flags & F_SPLIT_BASE and not trailers:
            raise WireError("split-base dump carries no trailer line")

        if flags & F_ROTATED:
            shift = (dump["key"] + rotation_offset) % count
            logical = [written[(j - shift) % count] for j in range(count)]
        else:
            logical = list(written)

        indices = []
        for cols in logical:
            if int(cols[6], 16) & G_INDEX_IN_SYMBOL:
                indices.append(int(cols[7].rsplit(":", 1)[1]))
            else:
                indices.append(int(cols[0]))

        structure.append({
            "dump": dump,
            "ordinal": ordinal,
            "flags": flags,
            "frames": logical,
            "trailers": trailers,
            "indices": indices,
        })
    return structure


def build_structure(dumps, params):
    return dump_structure(dumps, params["count_multiplier"], params["rotation_offset"])


def recover_bases(structure, params):
    """Recover each dump's base tick from its base field and trailer line."""
    radix = params["alternate_radix"]
    shift_width = params["base_shift_width"]
    bases = []
    for group in structure:
        flags = group["flags"]
        field = group["dump"]["base_field"]
        if flags & F_ALT_RADIX:
            high = parse_radix(field, radix)
        else:
            if not field.isdigit():
                raise WireError("base field is not a decimal numeral")
            high = int(field)
        if flags & F_SPLIT_BASE:
            low = int(group["trailers"][0][0])
            if low < 0 or low >= (1 << shift_width):
                raise WireError("low part does not fit the base shift width")
            base = (high << shift_width) | low
        else:
            base = high
        if base < 0:
            raise WireError("negative dump base")
        bases.append(base)
    return bases


def recover_spans(structure):
    """Resident spans, undoing the second-order differences of bit 0x001."""
    spans = []
    for group in structure:
        written = [int(c[3]) for c in group["frames"]]
        if group["flags"] & F_SECOND_ORDER_SPANS:
            values = [written[0]]
            if len(written) > 1:
                values.append(written[0] + written[1])
            for i in range(2, len(written)):
                values.append(2 * values[i - 1] - values[i - 2] + written[i])
        else:
            values = list(written)
        spans.append(values)
    return spans


def recover_entries(structure, params):
    """Entry stamps, undoing the running sum of bit 0x002 on the wrapping clock."""
    modulus = params["entry_clock_modulus"]
    entries = []
    for group in structure:
        written = [int(c[1]) for c in group["frames"]]
        if group["flags"] & F_ENTRY_CHAIN:
            values = [written[0]]
            for i in range(1, len(written)):
                values.append((written[i] - values[i - 1]) % modulus)
        else:
            values = list(written)
        entries.append(values)
    return entries


def recover_unwinds(structure, spans, params):
    """Unwind stamps, undoing the export-wide resident drift of bit 0x004.

    The running resident-span total advances over every fault frame of the
    export, in logical order, whatever flags the frame's own dump sets.
    """
    period = params["drift_period"]
    running = 0
    unwinds = []
    for group, dump_spans in zip(structure, spans):
        values = []
        drifted = bool(group["flags"] & F_UNWIND_DRIFT)
        for i, cols in enumerate(group["frames"]):
            written = int(cols[2])
            values.append(written - (running % period) if drifted else written)
            running += dump_spans[i]
        unwinds.append(values)
    return unwinds


def recover_allocations(structure, params):
    """Allocated byte counts, undoing the coarse unit of bit 0x200."""
    unit = params["coarse_unit"]
    allocations = []
    for group in structure:
        values = []
        for cols in group["frames"]:
            field = cols[4]
            if int(cols[6], 16) & G_COARSE_ALLOC:
                if not field.startswith("~"):
                    raise WireError("coarse allocation without its tilde")
                values.append(int(field[1:]) * unit)
            else:
                if field.startswith("~"):
                    raise WireError("plain allocation carrying a tilde")
                values.append(int(field))
        allocations.append(values)
    return allocations


def recover_signatures(structure, params):
    """Fault signatures, undoing the rotation of 0x080 and the ring of 0x400.

    The ring advances over every fault frame of the export, in logical order;
    each recovered signature is written into it in turn, including one that was
    itself recovered by reaching back into the ring.
    """
    offset = params["signature_offset"]
    width = params["ring_width"]
    ring = [None] * width
    position = 0
    signatures = []
    for group in structure:
        values = []
        for index, cols in zip(group["indices"], group["frames"]):
            flags = int(cols[6], 16)
            field = cols[5]
            if flags & G_SIG_RING:
                if not field.startswith("^"):
                    raise WireError("ring reference without its caret")
                distance = int(field[1:])
                if distance < 1:
                    raise WireError("non-positive ring distance")
                signature = ring[(position - distance) % width]
                if signature is None:
                    raise WireError("ring reference reaches an unwritten slot")
            else:
                if field.startswith("^"):
                    raise WireError("plain signature carrying a caret")
                signature = field
                if flags & G_SIG_ROTATED:
                    signature = rotate_right(signature, index + offset)
            if len(signature) != SIGNATURE_WIDTH or \
                    any(ch not in HEXDIGITS for ch in signature):
                raise WireError("recovered signature is not sixteen hex digits")
            ring[position] = signature
            position = (position + 1) % width
            values.append(signature)
        signatures.append(values)
    return signatures


def decode_export(dumps, params):
    """Fully decode a reporter export into a flat list of fault frame records."""
    structure = build_structure(dumps, params)
    bases = recover_bases(structure, params)
    spans = recover_spans(structure)
    entries = recover_entries(structure, params)
    unwinds = recover_unwinds(structure, spans, params)
    allocations = recover_allocations(structure, params)
    signatures = recover_signatures(structure, params)

    modulus = params["entry_clock_modulus"]
    frames = []
    for gi, group in enumerate(structure):
        base = bases[gi]
        first_entry = entries[gi][0]
        for i, cols in enumerate(group["frames"]):
            entry = entries[gi][i]
            frames.append({
                "source_dump": group["dump"]["dump_id"],
                "module_column": group["dump"]["module_column"],
                "dump_base": base,
                "frame_index": group["indices"][i],
                "enter_ns": entry,
                "unwind_ns": unwinds[gi][i],
                "cost_ns": spans[gi][i],
                "bytes_allocated": allocations[gi][i],
                "fault_signature": signatures[gi][i],
                "symbol": cols[7].rsplit(":", 1)[0],
                "capture_tick": base + ((entry - first_entry) % modulus),
            })
    return frames, bases


# --------------------------------------------------------------------------
# control figures
# --------------------------------------------------------------------------

def control_figures(dumps, params, wanted):
    """Compute as many of CTRL-A..CTRL-H as `wanted` asks for.

    Each figure is computed in its own guard so that a parameter which has not
    been settled yet cannot falsify a figure that does not depend on it.
    """
    out = dict.fromkeys(wanted)
    try:
        structure = build_structure(dumps, params)
    except (WireError, ValueError, IndexError):
        return out

    count = sum(len(g["frames"]) for g in structure)
    if "CTRL-A" in out:
        out["CTRL-A"] = count
    if "CTRL-H" in out:
        out["CTRL-H"] = sum(sum(g["indices"]) for g in structure)

    def guard(key, fn):
        if key not in out:
            return None
        try:
            out[key] = fn()
        except (WireError, ValueError, IndexError, ZeroDivisionError):
            out[key] = None
        return out[key]

    guard("CTRL-B", lambda: sum(recover_bases(structure, params)))

    spans = None
    if "CTRL-C" in out or "CTRL-E" in out:
        try:
            spans = recover_spans(structure)
        except (WireError, ValueError, IndexError):
            spans = None
    if "CTRL-C" in out:
        out["CTRL-C"] = None if spans is None else sum(sum(s) for s in spans)
    guard("CTRL-D", lambda: sum(sum(e) for e in recover_entries(structure, params)))
    if "CTRL-E" in out:
        if spans is None:
            out["CTRL-E"] = None
        else:
            guard("CTRL-E",
                  lambda: sum(sum(u) for u in recover_unwinds(structure, spans, params)))
    guard("CTRL-F",
          lambda: sum(sum(a) for a in recover_allocations(structure, params)))
    guard("CTRL-G",
          lambda: sum(int(s, 16) for g in recover_signatures(structure, params)
                      for s in g) % SIGNATURE_MODULUS)
    return out


# --------------------------------------------------------------------------
# parameter settlement
# --------------------------------------------------------------------------

def settle_parameters(calibration_dumps, controls):
    """Search the nine candidate domains against the published control figures.

    Parameters are settled in the order of SEARCH_PLAN.  Every candidate value
    in a parameter's domain gets a verdict before that parameter is selected,
    and every rejection records the control figure that falsified it.
    """
    settled = {}
    records = []
    evaluations = 0

    for names, control in SEARCH_PLAN:
        domains = [CANDIDATE_DOMAINS[n] for n in names]
        survivors = {n: set() for n in names}

        combos = [()]
        for domain in domains:
            combos = [c + (v,) for c in combos for v in domain]

        for combo in combos:
            trial = dict(settled)
            trial.update(zip(names, combo))
            # parameters not yet settled cannot affect this stage's figure;
            # give them a placeholder so the decoder can run at all.
            for name, domain in CANDIDATE_DOMAINS.items():
                trial.setdefault(name, domain[0])
            evaluations += 1
            figures = control_figures(calibration_dumps, trial, {control})
            if figures[control] is not None and figures[control] == controls[control]:
                for name, value in zip(names, combo):
                    survivors[name].add(value)

        for name in names:
            good = sorted(survivors[name])
            if len(good) != 1:
                raise WireError(
                    "control %s does not settle %s (survivors: %r)"
                    % (control, name, good))
            settled[name] = good[0]
            records.append({
                "name": name,
                "candidates": len(CANDIDATE_DOMAINS[name]),
                "control": control,
                "selected": good[0],
                "rejected": [{"value": v, "control": control}
                             for v in CANDIDATE_DOMAINS[name] if v != good[0]],
            })

    records.sort(key=lambda r: r["name"])
    return settled, {"evaluations": evaluations, "parameters": records}


# --------------------------------------------------------------------------
# identity, attribution and the fault ledger
# --------------------------------------------------------------------------

class Registry(object):
    def __init__(self, rows):
        self.by_tag = {}
        for row in rows:
            self.by_tag.setdefault(row["module_tag"], []).append({
                "canonical_tag": row["canonical_tag"],
                "component_key": row["component_key"],
                "tier_index": int(row["tier_index"]),
                "from": int(row["effective_from_build"]),
                "to": int(row["effective_to_build"]),
                "status": row["status"],
            })

    def row_for(self, tag, build_index):
        for row in self.by_tag.get(tag, ()):
            if row["from"] <= build_index <= row["to"]:
                return row
        return None

    def resolve(self, tag, build_index):
        """Follow the alias chain on one build index to a terminal row."""
        seen = set()
        current = tag
        while True:
            if current in seen:
                return None
            seen.add(current)
            row = self.row_for(current, build_index)
            if row is None:
                return None
            if row["canonical_tag"] == current:
                return row
            current = row["canonical_tag"]


def build_ledger(frames, registry, windows, probe, uplink,
                 host_zones, tenant_segments):
    """Apply the four acceptance conditions to every decoded fault frame."""
    accepted = []
    rejections = dict.fromkeys(REJECTION_KINDS, 0)
    rejected_total = 0

    for frame in frames:
        build_index = frame["capture_tick"] // TICKS_PER_BUILD_INDEX
        row = registry.resolve(frame["module_column"], build_index)
        broken = []

        # Module identity comes first: a frame whose module does not resolve
        # onto a still-carried module has no live module to ask the window, the
        # probe or the uplink about, so it breaks that condition alone.
        if row is None:
            broken.append("unresolved_module")
            canonical = None
        elif row["status"] != "supported":
            broken.append("retired_build")
            canonical = row["canonical_tag"]
        else:
            canonical = row["canonical_tag"]
            window = windows.get(row["component_key"])
            if window is None or not (
                    window[0] < frame["capture_tick"] < window[1]):
                broken.append("outside_support")

            key = (canonical, frame["frame_index"])
            residency = probe.get(key)
            if residency is None:
                broken.append("unprobed_frame")
            report = uplink.get(key)
            if report is None:
                broken.append("unreported_dump")

        if broken:
            for kind in broken:
                rejections[kind] += 1
            rejected_total += 1
            continue

        accepted.append({
            "bytes_allocated": frame["bytes_allocated"],
            "bytes_uploaded": report["bytes_uploaded"],
            "capture_tick": frame["capture_tick"],
            "component_key": row["component_key"],
            "cost_ns": frame["cost_ns"],
            "declared_tier": row["tier_index"],
            "enter_ns": frame["enter_ns"],
            "fault_signature": frame["fault_signature"],
            "frame_index": frame["frame_index"],
            "host_zone": host_zones[report["host_id"]],
            "module_tag": canonical,
            "peak_ns": residency["peak_ns"],
            "samples": residency["samples"],
            "source_dump": frame["source_dump"],
            "symbol": frame["symbol"],
            "tenant_segment": tenant_segments[report["tenant_class"]],
            "unwind_ns": frame["unwind_ns"],
        })

    accepted.sort(key=lambda r: r["capture_tick"])
    return accepted, rejections, rejected_total


def settle_quarantine(accepted, ladders):
    """Demote effective tiers until no live module is mostly over budget.

    Worked as passes over the module set; the pass that finds no module left to
    demote is counted with the rest.
    """
    modules = {}
    for row in accepted:
        entry = modules.setdefault(row["module_tag"], {
            "component_key": row["component_key"],
            "declared_tier": row["declared_tier"],
            "frames": [],
        })
        entry["declared_tier"] = max(entry["declared_tier"], row["declared_tier"])
        entry["frames"].append(row)

    effective = {tag: m["declared_tier"] for tag, m in modules.items()}

    def budget(tag, tier):
        return ladders[modules[tag]["component_key"]][tier]

    def allowances():
        envelopes = {}
        for tag, module in modules.items():
            component = module["component_key"]
            floor = budget(tag, effective[tag])
            if component not in envelopes or floor < envelopes[component]:
                envelopes[component] = floor
        out = {}
        for tag, module in modules.items():
            component = module["component_key"]
            envelope = envelopes[component] * len(ladders[component])
            out[tag] = min(budget(tag, effective[tag]), envelope)
        return out

    passes = 0
    while True:
        passes += 1
        allowed = allowances()
        demote = []
        for tag, module in modules.items():
            if effective[tag] <= 0:
                continue
            within = sum(1 for f in module["frames"]
                         if f["peak_ns"] <= allowed[tag])
            if len(module["frames"]) - within > within:
                demote.append(tag)
        if not demote:
            break
        for tag in demote:
            effective[tag] -= 1

    allowed = allowances()
    summaries = []
    quarantined = []
    for tag in sorted(modules):
        module = modules[tag]
        within = sum(1 for f in module["frames"] if f["peak_ns"] <= allowed[tag])
        over = len(module["frames"]) - within
        is_quarantined = effective[tag] == 0 and over > within
        if is_quarantined:
            quarantined.append(tag)
        for f in module["frames"]:
            f["effective_tier"] = effective[tag]
            f["within_budget"] = f["peak_ns"] <= allowed[tag]
        summaries.append({
            "accepted_count": len(module["frames"]),
            "allowance_ns": allowed[tag],
            "budget_ns": budget(tag, effective[tag]),
            "component_key": module["component_key"],
            "declared_tier": module["declared_tier"],
            "effective_tier": effective[tag],
            "module_tag": tag,
            "net_samples": sum(f["samples"] for f in module["frames"]),
            "quarantined": is_quarantined,
            "total_bytes_uploaded": sum(f["bytes_uploaded"] for f in module["frames"]),
            "within_budget_count": within,
        })
    return summaries, quarantined, passes


# --------------------------------------------------------------------------
# roll-ups
# --------------------------------------------------------------------------

LEDGER_FIELDS = [
    "bytes_allocated", "bytes_uploaded", "capture_tick", "component_key",
    "cost_ns", "effective_tier", "enter_ns", "fault_signature", "frame_index",
    "host_zone", "module_tag", "peak_ns", "samples", "source_dump", "symbol",
    "tenant_segment", "unwind_ns", "within_budget",
]


def totals_by(rows, key, value="bytes_uploaded"):
    out = {}
    for row in rows:
        out[row[key]] = out.get(row[key], 0) + row[value]
    return out


def make_summary(accepted, rejected_total):
    tiers = {}
    for row in accepted:
        k = str(row["effective_tier"])
        tiers[k] = tiers.get(k, 0) + 1
    return {
        "accepted_count": len(accepted),
        "by_component": totals_by(accepted, "component_key"),
        "by_effective_tier": tiers,
        "by_tenant_segment": totals_by(accepted, "tenant_segment"),
        "by_zone": totals_by(accepted, "host_zone"),
        "rejected_count": rejected_total,
        "total_bytes_allocated": sum(r["bytes_allocated"] for r in accepted),
        "total_bytes_uploaded": sum(r["bytes_uploaded"] for r in accepted),
        "total_cost_ns": sum(r["cost_ns"] for r in accepted),
        "within_budget_count": sum(1 for r in accepted if r["within_budget"]),
    }


def make_zone_report(zone, rows):
    return {
        "accepted_count": len(rows),
        "by_component": totals_by(rows, "component_key"),
        "by_tenant_segment": totals_by(rows, "tenant_segment"),
        "modules": sorted({r["module_tag"] for r in rows}),
        "total_bytes_uploaded": sum(r["bytes_uploaded"] for r in rows),
        "total_cost_ns": sum(r["cost_ns"] for r in rows),
        "within_budget_count": sum(1 for r in rows if r["within_budget"]),
        "zone": zone,
    }


def make_segment_report(segment, rows):
    return {
        "accepted_count": len(rows),
        "by_component": totals_by(rows, "component_key"),
        "by_zone": totals_by(rows, "host_zone"),
        "modules": sorted({r["module_tag"] for r in rows}),
        "tenant_segment": segment,
        "total_bytes_uploaded": sum(r["bytes_uploaded"] for r in rows),
        "total_cost_ns": sum(r["cost_ns"] for r in rows),
        "within_budget_count": sum(1 for r in rows if r["within_budget"]),
    }


# --------------------------------------------------------------------------
# corpus loading and the derivation itself
# --------------------------------------------------------------------------

class Reference(object):
    """The five tables that govern every corpus alike."""

    def __init__(self, data_dir):
        self.registry = Registry(read_csv_rows(os.path.join(data_dir, "build_registry.csv")))

        self.ladders = {}
        for row in read_csv_rows(os.path.join(data_dir, "severity_ladder.csv")):
            self.ladders.setdefault(row["component_key"], {})[int(row["tier_index"])] = \
                int(row["budget_ns"])

        self.windows = {}
        for row in read_csv_rows(os.path.join(data_dir, "support_windows.csv")):
            self.windows[row["component_key"]] = (int(row["open_tick"]),
                                                  int(row["close_tick"]))

        self.host_zones = {}
        for row in read_csv_rows(os.path.join(data_dir, "host_zones.csv")):
            self.host_zones[row["host_id"]] = row["zone"]

        self.tenant_segments = {}
        for row in read_csv_rows(os.path.join(data_dir, "tenant_classes.csv")):
            self.tenant_segments[row["tenant_class"]] = row["tenant_segment"]

        self.zones = sorted(set(self.host_zones.values()))
        self.segments = sorted(set(self.tenant_segments.values()))


def load_corpus(data_dir, prefix):
    dumps = load_dumps(os.path.join(data_dir, prefix + "fault_frames.psv"))

    probe = {}
    for row in read_csv_rows(os.path.join(data_dir, prefix + "runtime_probe.csv")):
        probe[(row["module_tag"], int(row["frame_index"]))] = {
            "samples": int(row["samples"]),
            "peak_ns": int(row["peak_ns"]),
        }

    uplink = {}
    for row in read_jsonl(os.path.join(data_dir, prefix + "uplink_reports.jsonl")):
        uplink[(row["module_tag"], int(row["frame_index"]))] = {
            "host_id": row["host_id"],
            "tenant_class": row["tenant_class"],
            "bytes_uploaded": int(row["bytes_uploaded"]),
        }

    return dumps, probe, uplink


def derive(dumps, probe, uplink, reference, params):
    """The whole derivation for one corpus, from decoded wire to triage set."""
    frames, _bases = decode_export(dumps, params)
    accepted, rejections, rejected_total = build_ledger(
        frames, reference.registry, reference.windows,
        probe, uplink, reference.host_zones, reference.tenant_segments)
    modules, quarantined, passes = settle_quarantine(accepted, reference.ladders)

    ticks = [r["capture_tick"] for r in accepted]
    if any(a >= b for a, b in zip(ticks, ticks[1:])):
        raise WireError("ledger is not strictly increasing in capture tick")

    ledger = [{k: row[k] for k in LEDGER_FIELDS} for row in accepted]

    allocated = sum(r["bytes_allocated"] for r in accepted)
    uploaded = sum(r["bytes_uploaded"] for r in accepted)

    reports = {"summary.json": make_summary(accepted, rejected_total),
               "product_summary.json": {"modules": modules}}
    for zone in reference.zones:
        rows = [r for r in accepted if r["host_zone"] == zone]
        reports["region_%s.json" % zone] = make_zone_report(zone, rows)
    for segment in reference.segments:
        rows = [r for r in accepted if r["tenant_segment"] == segment]
        reports["segment_%s.json" % segment] = make_segment_report(segment, rows)

    quality = {
        "accepted_count": len(accepted),
        "frame_count": len(frames),
        "reconciliation": {
            "allocated_bytes": allocated,
            "residual_bytes": allocated - uploaded,
            "uploaded_bytes": uploaded,
        },
        "rejections": rejections,
        "settlement": {"passes": passes, "quarantined_modules": quarantined},
    }
    return ledger, reports, quality


# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------

def write_json(path, payload):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def write_ledger(path, ledger):
    with open(path, "w", encoding="utf-8") as handle:
        for row in ledger:
            handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")))
            handle.write("\n")


def clear_output_dir(out_dir, names):
    """Start the settled derivation from an empty output directory."""
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)
        return
    for name in names:
        path = os.path.join(out_dir, name)
        if os.path.isfile(path):
            os.remove(path)


# --------------------------------------------------------------------------

def main(argv):
    data_dir = argv[1] if len(argv) > 1 else "/app/data"
    out_dir = argv[2] if len(argv) > 2 else "/app/out"

    reference = Reference(data_dir)
    controls = json.load(open(os.path.join(data_dir, "control-slice-figures.json"),
                              encoding="utf-8"))

    # 1. settle the withheld wire parameters against the control slice, before
    #    any figure is computed for the incident corpus.
    cal_dumps, cal_probe, cal_uplink = load_corpus(data_dir, "calibration_")
    params, search = settle_parameters(cal_dumps, controls)

    # 2. reproduce every published control figure with the settled set.
    figures = control_figures(cal_dumps, params,
                              {"CTRL-A", "CTRL-B", "CTRL-C", "CTRL-D",
                               "CTRL-E", "CTRL-F", "CTRL-G", "CTRL-H"})
    _cal_ledger, _cal_reports, cal_quality = derive(
        cal_dumps, cal_probe, cal_uplink, reference, params)
    figures["CTRL-I"] = cal_quality["accepted_count"]
    figures["CTRL-J"] = len(cal_quality["settlement"]["quarantined_modules"])
    figures["CTRL-K"] = cal_quality["reconciliation"]["residual_bytes"]

    comparison = [{"control": name, "published": controls[name],
                   "reproduced": figures[name]}
                  for name in sorted(controls)]
    mismatched = [c["control"] for c in comparison
                  if c["published"] != c["reproduced"]]
    if mismatched:
        raise WireError("settled parameters miss control figures: %s"
                        % ", ".join(mismatched))

    # 3. with the parameter set settled, empty the output directory and re-run
    #    the whole derivation for the incident corpus from scratch.
    expected = ["normalized.jsonl", "data_quality.json", "summary.json",
                "product_summary.json"]
    expected += ["region_%s.json" % z for z in reference.zones]
    expected += ["segment_%s.json" % s for s in reference.segments]
    clear_output_dir(out_dir, expected)

    dumps, probe, uplink = load_corpus(data_dir, "")
    rerun = derive(dumps, probe, uplink, reference, params)
    ledger, reports, quality = derive(dumps, probe, uplink, reference, params)
    if rerun != (ledger, reports, quality):
        raise WireError("the settled derivation is not reproducible")

    quality["calibration_comparison"] = comparison
    quality["candidate_search"] = search
    reports["data_quality.json"] = quality
    if sorted(list(reports) + ["normalized.jsonl"]) != sorted(expected):
        raise WireError("triage set does not match the expected output names")

    write_ledger(os.path.join(out_dir, "normalized.jsonl"), ledger)
    for name in sorted(reports):
        write_json(os.path.join(out_dir, name), reports[name])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
