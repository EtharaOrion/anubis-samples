#!/usr/bin/env python3
"""45421e59-0daa-505b-9656-e565f93c491c: recover the fault frame export, settle the quarantine
assignment, and write the triage set.

Run as a script over an input directory and an output directory:

    python3 transform.py <DATA_DIR> <OUT_DIR>

With no arguments it reads /app/data and writes /app/out.
"""
import json
import os
import sys
from collections import defaultdict

INPUT_DIR = "/app/data"
OUTPUT_DIR = "/app/out"

REQUIRED_FILES = (
    "fault_frames.psv",
    "runtime_probe.csv",
    "uplink_reports.jsonl",
    "build_registry.csv",
    "severity_ladder.csv",
    "support_windows.csv",
    "host_zones.csv",
    "tenant_classes.csv",
)

CONTROL_FILES = (
    "calibration_fault_frames.psv",
    "calibration_runtime_probe.csv",
    "calibration_uplink_reports.jsonl",
    "control-slice-figures.json",
)

TICKS_PER_MS = 1000
TICKS_PER_BUILD = 900000000  # one build index per 900 second stretch of the window

F_DELTA2 = 0x001        # dump  : cost column carries second-order differences
F_CHAIN_ENTER = 0x002   # dump  : entry column carries running pair sums
F_DRIFT_UNWIND = 0x004  # dump  : unwind column carries a running-cost drift
F_ROTATE = 0x008        # dump  : frame lines rotated
F_COUNT_BIAS = 0x010    # dump  : declared count carries a bias
F_ALT_RADIX = 0x020     # dump  : base high part written in an alternate radix
F_SHADOW_IDX = 0x040    # frame : frame index column is a decoy
F_ROT_SIG = 0x080       # frame : fault signature rotated before the check total
F_ALIAS = 0x100         # dump  : module column carries a registry alias
F_COARSE_ALLOC = 0x200  # frame : allocation column escaped into a coarse unit
F_RING_SIG = 0x400      # frame : signature written as a ring back reference
F_SPLIT_BASE = 0x800    # dump  : base split between header and trailer

DOMAIN_ENTRY_MODULUS = tuple(1 << k for k in range(22, 35))          # 13
DOMAIN_DRIFT_PERIOD = (48, 96, 192, 384, 768)                        # 5
DOMAIN_ROTATION_OFFSET = tuple(range(0, 8))                          # 8
DOMAIN_COUNT_MULTIPLIER = tuple(range(0, 9))                         # 9
DOMAIN_ALT_RADIX = tuple(range(11, 36))                              # 25
DOMAIN_SIG_OFFSET = tuple(range(0, 8))                               # 8
DOMAIN_COARSE_UNIT = (64, 128, 256, 512, 1024)                       # 5
DOMAIN_RING_WIDTH = (4, 8, 12, 16)                                   # 4
DOMAIN_BASE_SHIFT = (8, 10, 12, 16)                                  # 4

PARAMS = {
    "entry_modulus": 1 << 28,
    "drift_period": 192,
    "rotation_offset": 5,
    "count_multiplier": 7,
    "alt_radix": 29,
    "sig_offset": 3,
    "coarse_unit": 256,
    "ring_width": 8,
    "base_shift": 12,
}

ZONES = ("central", "east", "north", "south", "west")

TENANT_SEGMENTS = ("consumer", "corporate", "enterprise", "smb")

SIG_WIDTH = 16

DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"

REJECTION_REASONS = (
    "outside_support",
    "retired_build",
    "unprobed_frame",
    "unreported_dump",
    "unresolved_module",
)


def to_radix(value: int, radix: int) -> str:
    if value == 0:
        return "0"
    out = []
    while value:
        out.append(DIGITS[value % radix])
        value //= radix
    return "".join(reversed(out))


def from_radix(text: str, radix: int) -> int:
    value = 0
    for ch in text:
        pos = DIGITS.find(ch)
        if pos < 0 or pos >= radix:
            raise ValueError("digit out of range for radix")
        value = value * radix + pos
    return value


def checksum(text: str) -> str:
    return "%04x" % (sum(ord(c) for c in text) % 65536)


def rot_left(text: str, k: int) -> str:
    k %= len(text) or 1
    return text[k:] + text[:k]


def rot_right(text: str, k: int) -> str:
    k %= len(text) or 1
    return text[-k:] + text[:-k] if k else text


def unrotate_list(items, k):
    if not items:
        return list(items)
    k %= len(items)
    return list(items[-k:]) + list(items[:-k]) if k else list(items)


def build_registry(rows):
    index = defaultdict(list)
    for tag, canonical, component, tier, lo, hi, status in rows:
        index[tag].append((int(lo), int(hi), canonical, component, int(tier), status))
    return index


def resolve_identity(index, tag, build):
    """Return (canonical, component, tier) or raise ValueError(reason)."""
    seen = set()
    cur = tag
    for _ in range(8):
        if cur in seen:
            raise ValueError("unresolved_module")
        seen.add(cur)
        hits = [r for r in index.get(cur, ()) if r[0] <= build <= r[1]]
        if len(hits) != 1:
            raise ValueError("unresolved_module")
        lo, hi, canonical, component, tier, status = hits[0]
        if canonical == cur:
            if status != "supported":
                raise ValueError("retired_build")
            return canonical, component, tier
        cur = canonical
    raise ValueError("unresolved_module")


def decode_dumps(text, params=PARAMS, verify=True):
    """Recover every fault frame the export carries, in logical order."""
    modulus = params["entry_modulus"]
    period = params["drift_period"]
    rot_off = params["rotation_offset"]
    cnt_mul = params["count_multiplier"]
    radix = params["alt_radix"]
    sig_off = params["sig_offset"]
    unit = params["coarse_unit"]
    width = params["ring_width"]
    shift = params["base_shift"]

    groups = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("@|"):
            groups.append([line, []])
        elif groups:
            groups[-1][1].append(line)
        else:
            raise ValueError("frame line before any dump header")

    running_cost = 0
    ring = [""] * width
    ring_pos = 0
    out = []

    for ordinal, (header, body) in enumerate(groups):
        h = header.split("|")
        if len(h) != 8:
            raise ValueError("malformed dump header")
        dump_id, written, count_field, base_field = h[1], h[2], h[3], h[4]
        dflags = int(h[5], 16)
        dump_key = int(h[6])
        grp_sum = int(h[7], 16)

        bias = ((ordinal + 1) * cnt_mul) % 9 if dflags & F_COUNT_BIAS else 0
        n = int(count_field) - bias
        if n < 1 or n > len(body):
            raise ValueError("declared count outside the dump")
        frame_lines, trailers = body[:n], body[n:]

        if verify:
            got = sum(int(ln.rsplit("|", 1)[1], 16) for ln in frame_lines) % 65536
            if got != grp_sum:
                raise ValueError("dump check total mismatch")

        if dflags & F_ROTATE:
            frame_lines = unrotate_list(frame_lines, (dump_key + rot_off) % n)

        try:
            high = from_radix(base_field, radix) if dflags & F_ALT_RADIX else int(base_field)
            if dflags & F_SPLIT_BASE:
                base = (high << shift) | int(trailers[0].split("|")[0])
            else:
                base = high
        except (ValueError, IndexError):
            base = None

        parsed = []
        for line in frame_lines:
            f = line.split("|")
            if len(f) != 9:
                raise ValueError("malformed frame line")
            if verify and checksum("|".join(f[:8])) != f[8]:
                raise ValueError("frame check total mismatch")
            rflags = int(f[6], 16)
            true_idx = int(f[7].rsplit(":", 1)[1]) if rflags & F_SHADOW_IDX else int(f[0])
            parsed.append({
                "rflags": rflags, "frame_index": true_idx,
                "enter_wire": int(f[1]), "unwind_wire": int(f[2]),
                "cost_wire": int(f[3]), "alloc_wire": f[4], "sig_wire": f[5],
                "symbol": f[7].rsplit(":", 1)[0],
            })

        if dflags & F_DELTA2:
            costs = [parsed[0]["cost_wire"]]
            if n > 1:
                costs.append(parsed[1]["cost_wire"] + costs[0])
            for j in range(2, n):
                costs.append(parsed[j]["cost_wire"] + 2 * costs[j - 1] - costs[j - 2])
        else:
            costs = [p["cost_wire"] for p in parsed]

        if dflags & F_CHAIN_ENTER:
            enters = [parsed[0]["enter_wire"]]
            for j in range(1, n):
                enters.append((parsed[j]["enter_wire"] - enters[j - 1]) % modulus)
        else:
            enters = [p["enter_wire"] for p in parsed]

        frames = []
        for j, p in enumerate(parsed):
            rflags = p["rflags"]
            if dflags & F_DRIFT_UNWIND:
                unwind = p["unwind_wire"] - (running_cost % period)
            else:
                unwind = p["unwind_wire"]
            running_cost += costs[j]

            if p["sig_wire"].startswith("^"):
                sig = ring[(ring_pos - int(p["sig_wire"][1:])) % width]
            elif rflags & F_ROT_SIG:
                sig = rot_right(p["sig_wire"], (p["frame_index"] + sig_off) % SIG_WIDTH)
            else:
                sig = p["sig_wire"]
            ring[ring_pos % width] = sig
            ring_pos += 1

            if p["alloc_wire"].startswith("~"):
                alloc = int(p["alloc_wire"][1:]) * unit
            else:
                alloc = int(p["alloc_wire"])

            frames.append({
                "frame_index": p["frame_index"], "enter": enters[j],
                "unwind": unwind, "cost": costs[j], "alloc": alloc,
                "sig": sig, "symbol": p["symbol"],
            })

        out.append({"dump_id": dump_id, "written": written, "base": base,
                    "dflags": dflags, "frames": frames})
    return out


def read_csv_rows(text):
    lines = [ln for ln in text.splitlines() if ln.strip()]
    head = lines[0].split(",")
    return [dict(zip(head, ln.split(","))) for ln in lines[1:]]


def read_registry(text):
    rows = []
    for r in read_csv_rows(text):
        rows.append((r["module_tag"], r["canonical_tag"], r["component_key"],
                     int(r["tier_index"]), int(r["effective_from_build"]),
                     int(r["effective_to_build"]), r["status"]))
    return rows


def read_ladder(text):
    table = defaultdict(dict)
    for r in read_csv_rows(text):
        table[r["component_key"]][int(r["tier_index"])] = int(r["budget_ns"])
    return {c: [table[c][i] for i in sorted(table[c])] for c in table}


def read_windows(text):
    return {r["component_key"]: (int(r["open_tick"]), int(r["close_tick"]))
            for r in read_csv_rows(text)}


def derive(files, params=PARAMS, prefix="", search_record=None):
    dumps = decode_dumps(files[prefix + "fault_frames.psv"], params)
    index = build_registry(read_registry(files["build_registry.csv"]))
    ladder = read_ladder(files["severity_ladder.csv"])
    windows = read_windows(files["support_windows.csv"])
    zones = {r["host_id"]: r["zone"] for r in read_csv_rows(files["host_zones.csv"])}
    classes = {r["tenant_class"]: r["tenant_segment"]
               for r in read_csv_rows(files["tenant_classes.csv"])}
    probe_rows = {}
    for r in read_csv_rows(files[prefix + "runtime_probe.csv"]):
        probe_rows[(r["module_tag"], int(r["frame_index"]))] = (int(r["samples"]),
                                                                int(r["peak_ns"]))
    uplink = {}
    for line in files[prefix + "uplink_reports.jsonl"].splitlines():
        if line.strip():
            row = json.loads(line)
            uplink[(row["module_tag"], int(row["frame_index"]))] = row

    modulus = params["entry_modulus"]
    rejections = dict((name, 0) for name in REJECTION_REASONS)
    accepted = []
    frame_count = 0

    for dump in dumps:
        if dump["base"] is None:
            raise ValueError("dump base is unreadable under these parameters")
        first = dump["frames"][0]["enter"]
        for frame in dump["frames"]:
            frame_count += 1
            tick = dump["base"] + ((frame["enter"] - first) % modulus)
            build = tick // TICKS_PER_BUILD
            try:
                canonical, component, tier = resolve_identity(index, dump["written"], build)
            except ValueError as exc:
                rejections[str(exc)] += 1
                continue
            lo, hi = windows[component]
            bad = False
            if not (lo < tick < hi):
                rejections["outside_support"] += 1
                bad = True
            key = (canonical, frame["frame_index"])
            if key not in probe_rows:
                rejections["unprobed_frame"] += 1
                bad = True
            if key not in uplink:
                rejections["unreported_dump"] += 1
                bad = True
            if bad:
                continue
            samples, peak = probe_rows[key]
            report = uplink[key]
            accepted.append({
                "bytes_allocated": frame["alloc"],
                "bytes_uploaded": report["bytes_uploaded"],
                "capture_tick": tick,
                "component_key": component,
                "cost_ns": frame["cost"],
                "declared_tier": tier,
                "enter_ns": frame["enter"],
                "fault_signature": frame["sig"],
                "frame_index": frame["frame_index"],
                "host_zone": zones[report["host_id"]],
                "module_tag": canonical,
                "peak_ns": peak,
                "samples": samples,
                "source_dump": dump["dump_id"],
                "symbol": frame["symbol"],
                "tenant_segment": classes[report["tenant_class"]],
                "unwind_ns": frame["unwind"],
            })

    accepted.sort(key=lambda r: (r["capture_tick"], r["module_tag"]))

    by_module = defaultdict(list)
    for row in accepted:
        by_module[row["module_tag"]].append(row)
    effective = dict((tag, rows[0]["declared_tier"]) for tag, rows in by_module.items())
    component_of = dict((tag, rows[0]["component_key"]) for tag, rows in by_module.items())
    passes = 0
    while True:
        passes += 1
        env = envelopes(effective, component_of, ladder)
        changed = False
        for tag in sorted(effective):
            allow = min(ladder[component_of[tag]][effective[tag]], env[component_of[tag]])
            rows = by_module[tag]
            inside = sum(1 for r in rows if r["peak_ns"] <= allow)
            if (len(rows) - inside) > inside and effective[tag] > 0:
                effective[tag] -= 1
                changed = True
        if not changed:
            break

    env = envelopes(effective, component_of, ladder)
    allowance = dict((tag, min(ladder[component_of[tag]][effective[tag]],
                               env[component_of[tag]])) for tag in effective)
    quarantined = []
    for tag in sorted(effective):
        rows = by_module[tag]
        inside = sum(1 for r in rows if r["peak_ns"] <= allowance[tag])
        if effective[tag] == 0 and (len(rows) - inside) > inside:
            quarantined.append(tag)

    declared_tier = {}
    for row in accepted:
        tag = row["module_tag"]
        declared_tier[tag] = row["declared_tier"]
        row["effective_tier"] = effective[tag]
        row["within_budget"] = row["peak_ns"] <= allowance[tag]
        del row["declared_tier"]

    return build_reports(accepted, by_module, effective, allowance, declared_tier,
                         quarantined, ladder, rejections, frame_count, passes,
                         search_record)


def envelopes(effective, component_of, ladder):
    """A component holds its whole ladder to an envelope set by its lowest tier in service."""
    out = {}
    for tag, component in component_of.items():
        low = ladder[component][effective[tag]]
        if component not in out or low < out[component]:
            out[component] = low
    return dict((c, v * len(ladder[c])) for c, v in out.items())


def _sum_by(rows, key, field):
    out = defaultdict(int)
    for r in rows:
        out[r[key]] += r[field]
    return dict((k, out[k]) for k in sorted(out))


def _count_by(rows, key):
    out = defaultdict(int)
    for r in rows:
        out[str(r[key])] += 1
    return dict((k, out[k]) for k in sorted(out))


def _group_report(rows, label_key, label_value, other_key):
    return {
        "accepted_count": len(rows),
        "by_component": _sum_by(rows, "component_key", "bytes_uploaded"),
        ("by_tenant_segment" if other_key == "tenant_segment" else "by_zone"):
            _sum_by(rows, other_key, "bytes_uploaded"),
        label_key: label_value,
        "modules": sorted(set(r["module_tag"] for r in rows)),
        "total_bytes_uploaded": sum(r["bytes_uploaded"] for r in rows),
        "total_cost_ns": sum(r["cost_ns"] for r in rows),
        "within_budget_count": sum(1 for r in rows if r["within_budget"]),
    }


def build_reports(accepted, by_module, effective, allowance, declared_tier,
                  quarantined, ladder, rejections, frame_count, passes, search_record):
    reports = {"normalized.jsonl": accepted}

    allocated_total = sum(r["bytes_allocated"] for r in accepted)
    uploaded_total = sum(r["bytes_uploaded"] for r in accepted)

    reports["summary.json"] = {
        "accepted_count": len(accepted),
        "by_component": _sum_by(accepted, "component_key", "bytes_uploaded"),
        "by_effective_tier": _count_by(accepted, "effective_tier"),
        "by_tenant_segment": _sum_by(accepted, "tenant_segment", "bytes_uploaded"),
        "by_zone": _sum_by(accepted, "host_zone", "bytes_uploaded"),
        "rejected_count": frame_count - len(accepted),
        "total_bytes_allocated": allocated_total,
        "total_bytes_uploaded": uploaded_total,
        "total_cost_ns": sum(r["cost_ns"] for r in accepted),
        "within_budget_count": sum(1 for r in accepted if r["within_budget"]),
    }

    rows = []
    for tag in sorted(by_module):
        group = by_module[tag]
        rows.append({
            "accepted_count": len(group),
            "allowance_ns": allowance[tag],
            "budget_ns": ladder[group[0]["component_key"]][effective[tag]],
            "component_key": group[0]["component_key"],
            "declared_tier": declared_tier[tag],
            "effective_tier": effective[tag],
            "module_tag": tag,
            "net_samples": sum(r["samples"] for r in group),
            "quarantined": tag in quarantined,
            "total_bytes_uploaded": sum(r["bytes_uploaded"] for r in group),
            "within_budget_count": sum(1 for r in group if r["within_budget"]),
        })
    reports["product_summary.json"] = {"modules": rows}

    for zone in ZONES:
        picked = [r for r in accepted if r["host_zone"] == zone]
        reports["region_%s.json" % zone] = _group_report(
            picked, "zone", zone, "tenant_segment")
    for seg in TENANT_SEGMENTS:
        picked = [r for r in accepted if r["tenant_segment"] == seg]
        reports["segment_%s.json" % seg] = _group_report(
            picked, "tenant_segment", seg, "host_zone")

    reports["data_quality.json"] = {
        "accepted_count": len(accepted),
        "calibration_comparison": (search_record or {}).get("calibration_comparison", []),
        "candidate_search": (search_record or {}).get("candidate_search",
                                                      {"evaluations": 0, "parameters": []}),
        "frame_count": frame_count,
        "reconciliation": {
            "allocated_bytes": allocated_total,
            "residual_bytes": allocated_total - uploaded_total,
            "uploaded_bytes": uploaded_total,
        },
        "rejections": dict((k, rejections[k]) for k in sorted(rejections)),
        "settlement": {"passes": passes, "quarantined_modules": quarantined},
    }
    return reports


ARTIFACT_NAMES = (
    ["normalized.jsonl", "summary.json", "data_quality.json", "product_summary.json"]
    + ["region_%s.json" % z for z in ZONES]
    + ["segment_%s.json" % s for s in TENANT_SEGMENTS]
)


def render(reports):
    """Serialize every report exactly as the triage contract requires."""
    out = {}
    for name, payload in reports.items():
        if name.endswith(".jsonl"):
            out[name] = "".join(
                json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
                for row in payload)
        else:
            out[name] = json.dumps(payload, sort_keys=True, indent=2) + "\n"
    return out


CONTROL_NAMES = ("CTRL-A", "CTRL-B", "CTRL-C", "CTRL-D", "CTRL-E",
                 "CTRL-F", "CTRL-G", "CTRL-H", "CTRL-I", "CTRL-J", "CTRL-K")

STAGES = (
    ("count_multiplier", DOMAIN_COUNT_MULTIPLIER, None, None, "CTRL-A"),
    ("rotation_offset", DOMAIN_ROTATION_OFFSET, None, None, "CTRL-C"),
    ("entry_modulus", DOMAIN_ENTRY_MODULUS, None, None, "CTRL-D"),
    ("drift_period", DOMAIN_DRIFT_PERIOD, None, None, "CTRL-E"),
    ("coarse_unit", DOMAIN_COARSE_UNIT, None, None, "CTRL-F"),
    ("sig_offset", DOMAIN_SIG_OFFSET, "ring_width", DOMAIN_RING_WIDTH, "CTRL-G"),
    ("alt_radix", DOMAIN_ALT_RADIX, "base_shift", DOMAIN_BASE_SHIFT, "CTRL-B"),
)

PARAMETER_LABELS = {
    "entry_modulus": "entry clock modulus",
    "drift_period": "drift period",
    "rotation_offset": "rotation offset",
    "count_multiplier": "count multiplier",
    "alt_radix": "alternate radix",
    "sig_offset": "signature offset",
    "coarse_unit": "coarse unit",
    "ring_width": "ring width",
    "base_shift": "base shift width",
}

START = {
    "entry_modulus": DOMAIN_ENTRY_MODULUS[0],
    "drift_period": DOMAIN_DRIFT_PERIOD[0],
    "rotation_offset": DOMAIN_ROTATION_OFFSET[0],
    "count_multiplier": DOMAIN_COUNT_MULTIPLIER[0],
    "alt_radix": DOMAIN_ALT_RADIX[0],
    "sig_offset": DOMAIN_SIG_OFFSET[0],
    "coarse_unit": DOMAIN_COARSE_UNIT[0],
    "ring_width": DOMAIN_RING_WIDTH[0],
    "base_shift": DOMAIN_BASE_SHIFT[0],
}


def probe(text, params):
    """Return the observable figures a candidate parameter set produces."""
    dumps = decode_dumps(text, params)
    frames = [f for d in dumps for f in d["frames"]]
    bases = [d["base"] for d in dumps]
    return {
        "CTRL-A": len(frames),
        "CTRL-B": None if any(b is None for b in bases) else sum(bases),
        "CTRL-C": sum(f["cost"] for f in frames),
        "CTRL-D": sum(f["enter"] for f in frames),
        "CTRL-E": sum(f["unwind"] for f in frames),
        "CTRL-F": sum(f["alloc"] for f in frames),
        "CTRL-G": sum(int(f["sig"], 16) for f in frames) % 1000000007,
        "CTRL-H": sum(f["frame_index"] for f in frames),
    }


def controls(files, prefix="calibration_", params=None):
    params = params or PARAMS
    reports = derive(files, params, prefix)
    obs = probe(files[prefix + "fault_frames.psv"], params)
    dq = reports["data_quality.json"]
    obs["CTRL-I"] = dq["accepted_count"]
    obs["CTRL-J"] = len(dq["settlement"]["quarantined_modules"])
    obs["CTRL-K"] = dq["reconciliation"]["residual_bytes"]
    return dict((k, obs[k]) for k in CONTROL_NAMES)


def recover(text, published, verbose=False):
    """Stage the search; return (params, derivation record, evaluation count)."""
    params = dict(START)
    record = []
    evaluations = 0
    for name, domain, partner, pdomain, control in STAGES:
        combos = [(v, None) for v in domain] if partner is None else [
            (v, p) for v in domain for p in pdomain]
        survivors = []
        for value, pvalue in combos:
            evaluations += 1
            trial = dict(params)
            trial[name] = value
            if partner:
                trial[partner] = pvalue
            try:
                got = probe(text, trial)[control]
            except Exception:
                got = None
            if got == published[control]:
                survivors.append((value, pvalue))
        if len(survivors) != 1:
            raise SystemExit("stage %s left %d survivors: %s"
                             % (name, len(survivors), survivors[:8]))
        params[name] = survivors[0][0]
        record.append({"name": PARAMETER_LABELS[name], "candidates": len(domain),
                       "control": control, "selected": survivors[0][0],
                       "rejected": [{"control": control, "value": v}
                                    for v in domain if v != survivors[0][0]]})
        if partner:
            params[partner] = survivors[0][1]
            record.append({"name": PARAMETER_LABELS[partner], "candidates": len(pdomain),
                           "control": control, "selected": survivors[0][1],
                           "rejected": [{"control": control, "value": v}
                                        for v in pdomain if v != survivors[0][1]]})
        if verbose:
            extra = "" if partner is None else " + %s=%s" % (partner, survivors[0][1])
            print("  %-18s combos=%-4d survivors=1 -> %s%s"
                  % (name, len(combos), survivors[0][0], extra))
    return params, record, evaluations


def load_population(root):
    files = {}
    for name in REQUIRED_FILES + CONTROL_FILES:
        path = os.path.join(root, name)
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as handle:
                files[name] = handle.read()
    missing = [n for n in REQUIRED_FILES if n not in files]
    if missing:
        raise SystemExit("input directory is missing %s" % ", ".join(missing))
    return files


def carried_record():
    domains = dict(
        entry_modulus=DOMAIN_ENTRY_MODULUS, drift_period=DOMAIN_DRIFT_PERIOD,
        rotation_offset=DOMAIN_ROTATION_OFFSET,
        count_multiplier=DOMAIN_COUNT_MULTIPLIER, alt_radix=DOMAIN_ALT_RADIX,
        sig_offset=DOMAIN_SIG_OFFSET, coarse_unit=DOMAIN_COARSE_UNIT,
        ring_width=DOMAIN_RING_WIDTH, base_shift=DOMAIN_BASE_SHIFT)
    order = [name for name, _d, _p, _pd, _c in STAGES]
    order += [p for _n, _d, p, _pd, _c in STAGES if p]
    return {
        "candidate_search": {
            "evaluations": 0,
            "parameters": [{"name": PARAMETER_LABELS[name],
                            "candidates": len(domains[name]), "control": "",
                            "selected": PARAMS[name], "rejected": []}
                           for name in order],
        },
        "calibration_comparison": [],
    }


def settle(files):
    if not all(name in files for name in CONTROL_FILES):
        return dict(PARAMS), carried_record()
    published = json.loads(files["control-slice-figures.json"])
    params, parameters, evaluations = recover(
        files["calibration_fault_frames.psv"], published)
    reproduced = controls(files, "calibration_", params)
    record = {
        "candidate_search": {"evaluations": evaluations, "parameters": parameters},
        "calibration_comparison": [
            {"control": name, "published": published[name],
             "reproduced": reproduced[name]} for name in CONTROL_NAMES],
    }
    return params, record


def main(argv):
    source = argv[1] if len(argv) > 1 else INPUT_DIR
    target = argv[2] if len(argv) > 2 else OUTPUT_DIR
    files = load_population(source)
    params, record = settle(files)
    rendered = render(derive(files, params, "", record))
    os.makedirs(target, exist_ok=True)
    for name in ARTIFACT_NAMES:
        with open(os.path.join(target, name), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(rendered[name])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
