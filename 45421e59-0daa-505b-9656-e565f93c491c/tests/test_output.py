"""Verifier for 45421e59-0daa-505b-9656-e565f93c491c.

Self-contained: stdlib only, a fully specified xorshift64 generator, and an
independent reference derivation. Every corpus this file grades is rebuilt from
a declared seed, so the verifier never reads the agent image's data tree.

    python3 test_output.py environment    # regenerate environment/data
"""
import ast
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

SAMPLE_SEED = 7771041135375677920
CALIBRATION_SEED = 18648390340332234
CHALLENGE_SEED = 8229228829899356040
EVAL_SEED = 4740970459127879360

APP_ROOT = Path(os.environ.get("APP_ROOT", "/app"))
OUT_DIR = APP_ROOT / "out"
_SETPRIV = ["setpriv", "--reuid=nobody", "--regid=nogroup", "--clear-groups"]

FALLBACK_INPUT = "/app/data"
FALLBACK_OUTPUT = "/app/out"

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


class RNG:
    """xorshift64: contractually stable across CPython releases."""

    def __init__(self, seed: int) -> None:
        self.s = (seed & 0xFFFFFFFFFFFFFFFF) or 0x9E3779B97F4A7C15

    def next(self) -> int:
        x = self.s
        x ^= (x << 13) & 0xFFFFFFFFFFFFFFFF
        x ^= x >> 7
        x ^= (x << 17) & 0xFFFFFFFFFFFFFFFF
        self.s = x
        return x

    def below(self, n: int) -> int:
        return self.next() % n if n > 0 else 0

    def between(self, lo: int, hi: int) -> int:
        return lo + self.below(hi - lo + 1)

    def pick(self, seq):
        return seq[self.below(len(seq))]


def rotate_list(items, k):
    if not items:
        return list(items)
    k %= len(items)
    return list(items[k:]) + list(items[:k])


COMPONENTS = ("AURORA", "BASALT", "CIPHER", "DYNAMO")

LADDER = {
    "AURORA": (24000, 46000, 92000, 184000),
    "BASALT": (20000, 40000, 80000, 160000),
    "CIPHER": (28000, 54000, 108000, 216000),
    "DYNAMO": (18000, 36000, 72000, 144000),
}

HOSTS = (
    ("host-cen-01", "central"), ("host-cen-02", "central"),
    ("host-eas-01", "east"), ("host-eas-02", "east"),
    ("host-nor-01", "north"), ("host-nor-02", "north"),
    ("host-sou-01", "south"), ("host-wes-01", "west"),
    ("host-wes-02", "west"),
)

TENANT_CLASSES = (
    ("trial-single", "consumer"), ("retail-single", "consumer"),
    ("shop-floor", "smb"), ("shop-back", "smb"),
    ("dept-desk", "corporate"), ("dept-lab", "corporate"),
    ("carrier-core", "enterprise"), ("carrier-edge", "enterprise"),
)

SYMBOL_KINDS = ("unwind", "signal", "alloc", "sched")


def registry_rows(mutation=None):
    """(module_tag, canonical_tag, component_key, tier_index, from_build, to_build, status)."""
    rows = [
        ("AURORA-V1", "AURORA-V1", "AURORA", 1, 0, 400, "supported"),
        ("AURORA-V2", "AURORA-V2", "AURORA", 2, 0, 400, "supported"),
        ("AURORA-V3", "AURORA-V3", "AURORA", 3, 0, 26, "supported"),
        ("AURORA-V3", "AURORA-V3", "AURORA", 3, 27, 400, "retired"),
        ("AUR-HOTFIX", "AURORA-V2", "AURORA", 2, 0, 18, "supported"),
        ("AUR-HOTFIX", "AURORA-V3", "AURORA", 3, 19, 400, "supported"),
        ("BASALT-V1", "BASALT-V1", "BASALT", 1, 0, 400, "supported"),
        ("BASALT-V2", "BASALT-V2", "BASALT", 2, 0, 400, "supported"),
        ("BASALT-V3", "BASALT-V3", "BASALT", 3, 0, 400, "supported"),
        ("BAS-LEGACY", "BAS-SHIM", "BASALT", 2, 0, 400, "supported"),
        ("BAS-SHIM", "BASALT-V2", "BASALT", 2, 0, 400, "supported"),
        ("CIPHER-V1", "CIPHER-V1", "CIPHER", 1, 0, 400, "supported"),
        ("CIPHER-V2", "CIPHER-V2", "CIPHER", 2, 0, 400, "supported"),
        ("CIPHER-V3", "CIPHER-V3", "CIPHER", 3, 0, 400, "supported"),
        ("CIP-BACKPORT", "CIPHER-V1", "CIPHER", 1, 0, 22, "supported"),
        ("CIP-BACKPORT", "CIPHER-V3", "CIPHER", 3, 23, 400, "supported"),
        ("DYNAMO-V1", "DYNAMO-V1", "DYNAMO", 1, 0, 400, "supported"),
        ("DYNAMO-V2", "DYNAMO-V2", "DYNAMO", 2, 0, 400, "supported"),
        ("DYNAMO-V3", "DYNAMO-V3", "DYNAMO", 3, 0, 400, "supported"),
        ("DYN-SPARE", "DYNAMO-V1", "DYNAMO", 1, 5, 400, "supported"),
    ]
    if mutation == "cycle":
        rows.append(("CIP-LOOP", "CIP-RING", "CIPHER", 2, 0, 400, "supported"))
        rows.append(("CIP-RING", "CIP-LOOP", "CIPHER", 2, 0, 400, "supported"))
    if mutation == "rule_order":
        rows = list(reversed(rows))
    if mutation == "new_entity":
        rows.append(("DYNAMO-V0", "DYNAMO-V0", "DYNAMO", 0, 0, 400, "supported"))
    return rows


ALIAS_TAGS = ("AUR-HOTFIX", "BAS-LEGACY", "CIP-BACKPORT", "DYN-SPARE")

CANONICAL_TAGS = (
    "AURORA-V1", "AURORA-V2", "AURORA-V3",
    "BASALT-V1", "BASALT-V2", "BASALT-V3",
    "CIPHER-V1", "CIPHER-V2", "CIPHER-V3",
    "DYNAMO-V1", "DYNAMO-V2", "DYNAMO-V3",
)


def support_windows(mutation=None):
    base = {
        "AURORA": (2 * TICKS_PER_BUILD, 34 * TICKS_PER_BUILD),
        "BASALT": (1 * TICKS_PER_BUILD, 33 * TICKS_PER_BUILD),
        "CIPHER": (3 * TICKS_PER_BUILD, 35 * TICKS_PER_BUILD),
        "DYNAMO": (0, 32 * TICKS_PER_BUILD),
    }
    if mutation == "boundary_shift":
        base = dict((k, (v[0] - 1, v[1] - 1)) for k, v in base.items())
    if mutation == "contradict_calibration":
        base = dict((k, (v[0] + 6 * TICKS_PER_BUILD, v[1] - 7 * TICKS_PER_BUILD))
                    for k, v in base.items())
    return base


def ladder_table(mutation=None):
    table = {k: list(v) for k, v in LADDER.items()}
    if mutation == "tie_config":
        table["AURORA"] = [24000, 46000, 70000, 184000]
        table["BASALT"] = [20000, 40000, 58000, 160000]
    if mutation == "cap_edge":
        table["CIPHER"] = [28000, 54000, 76000, 216000]
    return table


DUMP_PROFILES = (
    0,
    F_DELTA2 | F_ROTATE,
    F_CHAIN_ENTER | F_COUNT_BIAS,
    F_DRIFT_UNWIND | F_ALT_RADIX,
    F_ALIAS | F_DELTA2,
    F_ROTATE | F_SPLIT_BASE | F_ALT_RADIX,
    F_CHAIN_ENTER | F_DRIFT_UNWIND,
    F_COUNT_BIAS | F_SPLIT_BASE,
    F_ALIAS | F_CHAIN_ENTER | F_ROTATE,
    F_DELTA2 | F_DRIFT_UNWIND | F_COUNT_BIAS,
    F_ALT_RADIX | F_SPLIT_BASE | F_CHAIN_ENTER,
    F_ALIAS | F_ROTATE | F_DRIFT_UNWIND,
    F_DELTA2 | F_CHAIN_ENTER | F_ALT_RADIX | F_COUNT_BIAS,
)

FRAME_PROFILES = (
    0,
    F_SHADOW_IDX,
    F_ROT_SIG,
    F_COARSE_ALLOC,
    F_SHADOW_IDX | F_ROT_SIG,
    F_RING_SIG,
    F_ROT_SIG | F_COARSE_ALLOC,
    F_SHADOW_IDX | F_COARSE_ALLOC,
    F_SHADOW_IDX | F_RING_SIG,
    F_SHADOW_IDX | F_ROT_SIG | F_COARSE_ALLOC,
)

ALIAS_SPANS = {
    "AUR-HOTFIX": ((6, 17, "AURORA-V2"), (20, 25, "AURORA-V3")),
    "BAS-LEGACY": ((4, 30, "BASALT-V2"),),
    "CIP-BACKPORT": ((8, 21, "CIPHER-V1"), (24, 31, "CIPHER-V3")),
    "DYN-SPARE": ((7, 29, "DYNAMO-V1"),),
}

MODULE_TIER = {
    "AURORA-V1": 1, "AURORA-V2": 2, "AURORA-V3": 3,
    "BASALT-V1": 1, "BASALT-V2": 2, "BASALT-V3": 3,
    "CIPHER-V1": 1, "CIPHER-V2": 2, "CIPHER-V3": 3,
    "DYNAMO-V1": 1, "DYNAMO-V2": 2, "DYNAMO-V3": 3,
}

PEAK_BAND = {
    "AURORA-V1": (520, 760), "AURORA-V2": (900, 1240), "AURORA-V3": (560, 880),
    "BASALT-V1": (520, 760), "BASALT-V2": (600, 880), "BASALT-V3": (540, 820),
    "CIPHER-V1": (960, 1300), "CIPHER-V2": (600, 880), "CIPHER-V3": (560, 880),
    "DYNAMO-V1": (520, 760), "DYNAMO-V2": (900, 1240), "DYNAMO-V3": (560, 880),
}


def _component_of(tag: str) -> str:
    return tag.split("-")[0]


def build_plan(seed, tier, mutation=None, force_d=None, force_f=None):
    """Return the logical corpus: dumps with true field values, plus side tables."""
    rng = RNG(seed)
    ladder = ladder_table(mutation)
    windows = support_windows(mutation)
    modulus = PARAMS["entry_modulus"]

    if tier == "sample":
        n_dumps = 7
    elif tier == "calibration":
        n_dumps = 11
    else:
        n_dumps = 27
    if mutation == "scale_population":
        n_dumps = 64
    if mutation == "partition_count":
        n_dumps = 21

    dumps = []
    ring_state = {}
    ring_pos = {}
    width = PARAMS["ring_width"]

    for i in range(n_dumps):
        profile = 0 if tier == "sample" else DUMP_PROFILES[i % len(DUMP_PROFILES)]
        if mutation == "rule_order":
            profile = 0 if tier == "sample" else DUMP_PROFILES[(i * 5 + 3) % len(DUMP_PROFILES)]
        if force_d is not None:
            profile = force_d

        if profile & F_ALIAS:
            alias = sorted(ALIAS_SPANS)[i % len(ALIAS_SPANS)]
            span = ALIAS_SPANS[alias][i % len(ALIAS_SPANS[alias])]
            build = rng.between(span[0], span[1])
            canonical = span[2]
            written = alias
        else:
            canonical = CANONICAL_TAGS[(i * 5 + 1) % len(CANONICAL_TAGS)]
            written = canonical
            build = rng.between(0, 37)
        component = _component_of(canonical)

        n = rng.between(5, 11)
        dump_key = rng.between(0, 4095)
        # An unresolvable alias reference: no registry row covers this build index.
        if tier == "challenge" and i == 15:
            written, build = "DYN-SPARE", 2
        if tier == "challenge" and i == 20:
            written, build, canonical, component = "AURORA-V3", 31, "AURORA-V3", "AURORA"
        if tier == "challenge" and mutation == "cycle" and i == 6:
            written, build = "CIP-LOOP", 12
        base_windows = support_windows(None)
        base = build * TICKS_PER_BUILD + rng.between(0, 40) * 900000 + i * 7 + 13

        base_enter = rng.between(modulus // 2, modulus // 2 + (modulus // 8))
        frames = []
        enter = base_enter
        for j in range(n):
            cost = 48000 + rng.between(0, 10) * 3600
            band = PEAK_BAND[canonical]
            if mutation == "cap_edge" and canonical == "CIPHER-V2":
                band = (1000, 1000)
            budget = ladder[component][MODULE_TIER[canonical]]
            peak = budget * rng.between(band[0], band[1]) // 1000
            peak = (peak // 100) * 100
            samples = cost // 1200
            alloc = peak * cost // 96000
            reorder = (0, cost // 3, cost // 2)[rng.below(3)]
            fprofile = 0 if tier == "sample" else FRAME_PROFILES[(i * 7 + j * 3) % len(FRAME_PROFILES)]
            if force_f is not None:
                fprofile = force_f

            pos = ring_pos.get("", 0)
            if (fprofile & F_RING_SIG) and pos < width:
                fprofile &= ~F_RING_SIG
            if fprofile & F_COARSE_ALLOC:
                alloc = (alloc // PARAMS["coarse_unit"]) * PARAMS["coarse_unit"]

            ringref = None
            if fprofile & F_RING_SIG:
                ringref = rng.between(1, width + 3)
                sig = ring_state[""][(pos - ringref) % width]
            else:
                sig = "".join(DIGITS[rng.below(16)] for _ in range(SIG_WIDTH))

            ring = ring_state.setdefault("", [""] * width)
            ring[pos % width] = sig
            ring_pos[""] = pos + 1

            frames.append({
                "frame_index": j + (i * 100),
                "enter": enter % modulus,
                "unwind": (enter - reorder) % modulus,
                "cost": cost,
                "alloc": alloc,
                "peak": peak,
                "samples": samples,
                "sig": sig,
                "fflags": fprofile,
                "ringref": ringref,
                "symbol": SYMBOL_KINDS[(i + j) % len(SYMBOL_KINDS)],
            })
            enter += cost
        # planted boundary frames exercise the support rule at both of its ends
        if tier in ("challenge", "calibration") and i in (4, 17):
            base = base_windows[component][0]
        elif tier in ("challenge", "calibration") and i == 9 and len(frames) > 4:
            span = sum(f["cost"] for f in frames[:4])
            base = base_windows[component][1] - span
        dumps.append({
            "dump_id": "D%s%04d" % (tier[0].upper(), i),
            "written": written,
            "canonical": canonical,
            "component": component,
            "base": base,
            "dump_key": dump_key,
            "dflags": profile,
            "frames": frames,
            "trailer_pad": 1 if (profile & F_SPLIT_BASE) and (i % 3 == 0) else 0,
        })

    return {"dumps": dumps, "ladder": ladder, "windows": windows, "mutation": mutation}


def encode_dumps(plan, params=PARAMS):
    modulus = params["entry_modulus"]
    period = params["drift_period"]
    rot_off = params["rotation_offset"]
    cnt_mul = params["count_multiplier"]
    radix = params["alt_radix"]
    sig_off = params["sig_offset"]
    unit = params["coarse_unit"]
    width = params["ring_width"]
    shift = params["base_shift"]

    lines = ["# crash reporter dump export - one group per captured dump",
             "# @ header: dump, module, count, base, flags, key, sum"]
    running_cost = 0
    ring_state = {}
    ring_pos = {}

    for ordinal, dump in enumerate(plan["dumps"]):
        dflags = dump["dflags"]
        recs = dump["frames"]
        n = len(recs)
        costs = [r["cost"] for r in recs]
        if dflags & F_DELTA2:
            wire_cost = [costs[0]]
            if n > 1:
                wire_cost.append(costs[1] - costs[0])
            for j in range(2, n):
                wire_cost.append(costs[j] - 2 * costs[j - 1] + costs[j - 2])
        else:
            wire_cost = list(costs)

        if dflags & F_CHAIN_ENTER:
            wire_enter = [recs[0]["enter"]]
            for j in range(1, n):
                wire_enter.append((recs[j]["enter"] + recs[j - 1]["enter"]) % modulus)
        else:
            wire_enter = [r["enter"] for r in recs]

        wire_lines = []
        for j, rec in enumerate(recs):
            fflags = rec["fflags"]
            true_idx = rec["frame_index"]
            idx_field = str(j) if fflags & F_SHADOW_IDX else str(true_idx)
            sym_field = "%s:%d" % (rec["symbol"], true_idx if fflags & F_SHADOW_IDX else j)

            if dflags & F_DRIFT_UNWIND:
                unwind_field = rec["unwind"] + (running_cost % period)
            else:
                unwind_field = rec["unwind"]
            running_cost += rec["cost"]

            pos = ring_pos.get("", 0)
            ring = ring_state.setdefault("", [""] * width)
            if fflags & F_RING_SIG:
                sig_field = "^%d" % rec["ringref"]
            elif fflags & F_ROT_SIG:
                sig_field = rot_left(rec["sig"], (true_idx + sig_off) % SIG_WIDTH)
            else:
                sig_field = rec["sig"]
            ring[pos % width] = rec["sig"]
            ring_pos[""] = pos + 1

            if fflags & F_COARSE_ALLOC:
                alloc_field = "~%d" % (rec["alloc"] // unit)
            else:
                alloc_field = str(rec["alloc"])

            body = "|".join([idx_field, str(wire_enter[j]), str(unwind_field),
                             str(wire_cost[j]), alloc_field, sig_field,
                             "%03x" % fflags, sym_field])
            wire_lines.append(body + "|" + checksum(body))

        group_sum = sum(int(ln.rsplit("|", 1)[1], 16) for ln in wire_lines) % 65536

        if dflags & F_ROTATE:
            emitted = rotate_list(wire_lines, (dump["dump_key"] + rot_off) % n)
        else:
            emitted = list(wire_lines)

        base = dump["base"]
        if dflags & F_SPLIT_BASE:
            high, low = base >> shift, base & ((1 << shift) - 1)
        else:
            high, low = base, None
        base_field = to_radix(high, radix) if dflags & F_ALT_RADIX else str(high)

        bias = ((ordinal + 1) * cnt_mul) % 9 if dflags & F_COUNT_BIAS else 0
        header = "|".join(["@", dump["dump_id"], dump["written"], str(n + bias),
                           base_field, "%03x" % dflags, str(dump["dump_key"]),
                           "%04x" % group_sum])
        lines.append(header)
        lines.extend(emitted)
        if low is not None:
            lines.append("|".join([str(low), "0", "0", "0", "0", "-", "000",
                                   "pad:0", "0000"]))
        for _ in range(dump["trailer_pad"]):
            lines.append("|".join(["0", "0", "0", "0", "0", "-", "000",
                                   "pad:0", "0000"]))
    return "\n".join(lines) + "\n"


def side_tables(mutation=None):
    reg = "module_tag,canonical_tag,component_key,tier_index,effective_from_build,effective_to_build,status\n"
    for row in registry_rows(mutation):
        reg += ",".join(str(x) for x in row) + "\n"
    lad = "component_key,tier_index,budget_ns\n"
    table = ladder_table(mutation)
    for component in COMPONENTS:
        for idx, budget in enumerate(table[component]):
            lad += "%s,%d,%d\n" % (component, idx, budget)
    win = "component_key,open_tick,close_tick\n"
    for component, bounds in sorted(support_windows(mutation).items()):
        win += "%s,%d,%d\n" % (component, bounds[0], bounds[1])
    hst = "host_id,zone\n" + "".join("%s,%s\n" % h for h in HOSTS)
    tnt = "tenant_class,tenant_segment\n" + "".join("%s,%s\n" % c for c in TENANT_CLASSES)
    return {
        "build_registry.csv": reg,
        "severity_ladder.csv": lad,
        "support_windows.csv": win,
        "host_zones.csv": hst,
        "tenant_classes.csv": tnt,
    }


def materialise(plan, seed, mutation=None, prefix=""):
    """Emit the wire files for one tier; probe and uplink rows follow resolved identity."""
    rng = RNG(seed ^ 0x5DEECE66D)
    index = build_registry(registry_rows(mutation))
    modulus = PARAMS["entry_modulus"]
    probe_rows, uplink = [], []
    ordinal = 0
    hosts = HOSTS
    if mutation == "empty_group":
        hosts = tuple(h for h in HOSTS if h[1] != "south")
    if mutation == "partition_count":
        hosts = tuple(h for h in HOSTS if h[1] in ("central", "north", "west"))
    drop_probe = 23 if mutation != "nonapplying_rule" else 13
    drop_uplink = 29 if mutation != "nonapplying_rule" else 19
    partial = 17 if mutation != "nonapplying_rule" else 7
    for dump in plan["dumps"]:
        first = dump["frames"][0]["enter"]
        for rec in dump["frames"]:
            ordinal += 1
            tick = dump["base"] + ((rec["enter"] - first) % modulus)
            build = tick // TICKS_PER_BUILD
            try:
                canonical = resolve_identity(index, dump["written"], build)[0]
            except ValueError:
                continue
            if ordinal % drop_probe != 5:
                probe_rows.append((canonical, rec["frame_index"], rec["samples"],
                                   rec["peak"]))
            if ordinal % drop_uplink != 11:
                sent = rec["alloc"]
                if ordinal % partial == 3:
                    sent = max(0, sent - (500 + (ordinal * 37) % 4500))
                uplink.append({
                    "module_tag": canonical,
                    "frame_index": rec["frame_index"],
                    "host_id": hosts[rng.below(len(hosts))][0],
                    "tenant_class": TENANT_CLASSES[rng.below(len(TENANT_CLASSES))][0],
                    "bytes_uploaded": sent,
                })
    probe_text = "module_tag,frame_index,samples,peak_ns\n"
    for row in probe_rows:
        probe_text += "%s,%d,%d,%d\n" % row
    uplink_text = "".join(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n"
                          for r in uplink)
    return {
        prefix + "fault_frames.psv": encode_dumps(plan),
        prefix + "runtime_probe.csv": probe_text,
        prefix + "uplink_reports.jsonl": uplink_text,
    }


DELIVERABLES = ["transform.py"] + list(ARTIFACT_NAMES)
ANSWER_FILES = ("sample_ledger_answer", "sample_reports_answer",
                "example_zone_report")

MUTATIONS = (
    ("fresh", None), ("tie_config", "tie_config"), ("cap_edge", "cap_edge"),
    ("rule_order", "rule_order"), ("empty_group", "empty_group"),
    ("cycle", "cycle"), ("boundary_shift", "boundary_shift"),
    ("new_entity", "new_entity"), ("nonapplying_rule", "nonapplying_rule"),
    ("partition_count", "partition_count"),
    ("scale_population", "scale_population"),
    ("contradict_calibration", "contradict_calibration"),
)

TRAP_CARDS = (
    ("delta2", F_DELTA2, None), ("chain_enter", F_CHAIN_ENTER, None),
    ("drift_unwind", F_DRIFT_UNWIND, None), ("rotate", F_ROTATE, None),
    ("count_bias", F_COUNT_BIAS, None), ("alt_radix", F_ALT_RADIX, None),
    ("alias", F_ALIAS, None), ("split_base", F_SPLIT_BASE, None),
    ("shadow_idx", None, F_SHADOW_IDX), ("rot_sig", None, F_ROT_SIG),
    ("coarse_alloc", None, F_COARSE_ALLOC), ("ring_sig", None, F_RING_SIG),
)


def _untrusted_command(command):
    if os.name == "posix" and os.geteuid() == 0:
        return [*_SETPRIV, *command]
    return command


def _mix(seed, label):
    return int(hashlib.blake2b(label.encode(), digest_size=8,
                               key=str(seed).encode()).hexdigest(), 16) >> 1


def _population(tier, seed, mutation=None, prefix="", force_d=None, force_f=None):
    plan = build_plan(seed, tier, mutation, force_d, force_f)
    files = materialise(plan, seed, mutation, prefix)
    files.update(side_tables(mutation))
    return files


def shipped_population():
    files = _population("challenge", CHALLENGE_SEED)
    files.update(_population("calibration", CALIBRATION_SEED, None, "calibration_"))
    files.update(_population("sample", SAMPLE_SEED, None, "sample_"))
    files["control-slice-figures.json"] = json.dumps(
        controls(files, "calibration_"), sort_keys=True, indent=2) + "\n"
    return files


def reference(files, prefix=""):
    return render(derive(files, PARAMS, prefix))


def _reachable(path):
    """Grading stages its corpora as root and runs the graded program under an
    unprivileged account, so every path handed to that program has to stay
    readable from outside the staging account."""
    try:
        path.chmod(0o755 if path.is_dir() else 0o644)
    except OSError:
        pass


def _stage(files, name=None):
    root = Path(tempfile.mkdtemp(prefix="triage-in-"))
    _reachable(root)
    if name is not None:
        root = root / name
        root.mkdir(parents=True)
        _reachable(root)
    for fname in sorted(files):
        path = root / fname
        path.write_text(files[fname], encoding="utf-8", newline="\n")
        _reachable(path)
    return root


def _run(input_dir, output_dir, script=None):
    script = script or (OUT_DIR / "transform.py")
    assert Path(script).is_file(), "transform.py is missing"
    _reachable(Path(script))
    output_dir = Path(output_dir)
    parent = output_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    try:
        parent.chmod(0o777)
    except PermissionError:
        pass
    before = set(p.resolve() for p in parent.rglob("*"))
    proc = subprocess.run(
        _untrusted_command([sys.executable, str(script), str(input_dir), str(output_dir)]),
        text=True, capture_output=True, timeout=240)
    assert proc.returncode == 0, proc.stderr[-600:]
    stray = [p for p in set(p.resolve() for p in parent.rglob("*")) - before
             if output_dir.resolve() not in [p, *p.parents]]
    assert not stray, "the program wrote outside its output directory: %s" % stray
    return dict((name, (output_dir / name).read_text(encoding="utf-8"))
                for name in ARTIFACT_NAMES)


def _delivered():
    return dict((name, (OUT_DIR / name).read_text(encoding="utf-8"))
                for name in ARTIFACT_NAMES)


def _delivered_code():
    """The delivered program's executable text, comments and docstrings dropped.
    A name that survives here is one the program can act on; a name an honest
    author only mentioned in prose is not a runtime read and must not be
    penalised."""
    text = (OUT_DIR / "transform.py").read_text(encoding="utf-8")
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return text
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                              ast.ClassDef))
                and body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            body[0].value.value = ""
    return ast.unparse(tree)


def _residual_holds(rendered):
    ledger = [json.loads(line) for line in rendered["normalized.jsonl"].splitlines() if line]
    quality = json.loads(rendered["data_quality.json"])
    allocated = sum(row["bytes_allocated"] for row in ledger)
    uploaded = sum(row["bytes_uploaded"] for row in ledger)
    assert quality["reconciliation"]["allocated_bytes"] == allocated
    assert quality["reconciliation"]["uploaded_bytes"] == uploaded
    assert quality["reconciliation"]["residual_bytes"] == allocated - uploaded
    assert quality["accepted_count"] == len(ledger)


def _match(got, want, names):
    for name in names:
        assert got[name] == want[name], "%s differs from the reference" % name


GRADED_QUALITY_KEYS = ("accepted_count", "frame_count", "rejections",
                       "reconciliation")
TRIAGE_REPORTS = [n for n in ARTIFACT_NAMES if n != "data_quality.json"]


def _match_triage(got, want):
    _match(got, want, TRIAGE_REPORTS)
    mine = json.loads(got["data_quality.json"])
    theirs = json.loads(want["data_quality.json"])
    for key in GRADED_QUALITY_KEYS:
        assert mine[key] == theirs[key], "data_quality %s differs" % key


def _end_to_end(label, mutation):
    seed = _mix(EVAL_SEED, label)
    files = _population("challenge", seed, mutation)
    root = _stage(files)
    out = Path(tempfile.mkdtemp(prefix="triage-out-")) / "triage"
    got = _run(root, out)
    _match_triage(got, reference(files))
    _residual_holds(got)


def _trap_probe(label, dump_flag, frame_flag):
    seed = _mix(EVAL_SEED, "trap-" + label)
    files = _population("challenge", seed, None,
                        force_d=dump_flag or 0, force_f=frame_flag or 0)
    root = _stage(files)
    out = Path(tempfile.mkdtemp(prefix="triage-trap-")) / "triage"
    got = _run(root, out)
    _match_triage(got, reference(files))
    _residual_holds(got)


def test_triage_set_is_complete():
    """POSITIVE: every declared deliverable of the triage set is present."""
    for name in DELIVERABLES:
        assert (OUT_DIR / name).is_file(), name


def test_triage_program_carries_its_promised_fallback_locations():
    """POSITIVE: the triage program carries the no-argument fallback locations it owes."""
    text = (OUT_DIR / "transform.py").read_text(encoding="utf-8")
    assert FALLBACK_INPUT in text, "no-argument fallback input location absent"
    assert FALLBACK_OUTPUT in text, "no-argument fallback output location absent"


def test_shipped_ledger_matches_the_reference():
    """POSITIVE: the delivered fault ledger for the shipped corpus is exact."""
    files = shipped_population()
    got = _delivered()
    _match(got, reference(files), ["normalized.jsonl"])


def test_shipped_incident_and_module_rollups_match_the_reference():
    """POSITIVE: the delivered incident and per-module roll-ups are exact."""
    files = shipped_population()
    got = _delivered()
    _match(got, reference(files), ["summary.json", "product_summary.json"])


def test_shipped_zone_reports_match_the_reference():
    """POSITIVE: every delivered zone report for the shipped corpus is exact."""
    files = shipped_population()
    got = _delivered()
    _match(got, reference(files), ["region_%s.json" % z for z in ZONES])


def test_shipped_tenant_segment_reports_match_the_reference():
    """POSITIVE: every delivered tenant segment report for the shipped corpus is exact."""
    files = shipped_population()
    got = _delivered()
    _match(got, reference(files), ["segment_%s.json" % s for s in TENANT_SEGMENTS])


def test_shipped_reconciliation_record_matches_the_reference():
    """POSITIVE: the delivered reconciliation record carries the shipped tallies and its schema."""
    files = shipped_population()
    got = _delivered()
    want = json.loads(reference(files)["data_quality.json"])
    quality = json.loads(got["data_quality.json"])
    for key in GRADED_QUALITY_KEYS:
        assert quality[key] == want[key], key
    assert isinstance(quality["calibration_comparison"], list)
    assert isinstance(quality["candidate_search"], dict)
    assert isinstance(quality["candidate_search"].get("evaluations"), int)
    assert isinstance(quality["candidate_search"].get("parameters"), list)
    assert isinstance(quality["settlement"], dict)
    assert isinstance(quality["settlement"].get("passes"), int)
    assert isinstance(quality["settlement"].get("quarantined_modules"), list)
    _residual_holds(got)


def test_trap_second_order_cost_inverse():
    """POSITIVE: resident spans written as second-order differences are recovered."""
    _trap_probe("delta2", F_DELTA2, None)


def test_trap_chained_entry_inverse():
    """POSITIVE: chained entry stamps are recovered on the wrapping clock."""
    _trap_probe("chain_enter", F_CHAIN_ENTER, None)


def test_trap_running_unwind_drift_inverse():
    """POSITIVE: the running-cost drift on the unwind column is removed."""
    _trap_probe("drift_unwind", F_DRIFT_UNWIND, None)


def test_trap_rotated_frame_lines_inverse():
    """POSITIVE: rotated frame lines are restored to logical order."""
    _trap_probe("rotate", F_ROTATE, None)


def test_trap_biased_declared_count_inverse():
    """POSITIVE: the biased declared count is corrected before frames split from trailers."""
    _trap_probe("count_bias", F_COUNT_BIAS, None)


def test_trap_alternate_radix_base_inverse():
    """POSITIVE: a dump base numeral in the alternate radix is read correctly."""
    _trap_probe("alt_radix", F_ALT_RADIX, None)


def test_trap_registry_alias_column_inverse():
    """POSITIVE: an aliased module column resolves through the registry."""
    _trap_probe("alias", F_ALIAS, None)


def test_trap_split_dump_base_inverse():
    """POSITIVE: a dump base split across header and trailer is rejoined."""
    _trap_probe("split_base", F_SPLIT_BASE, None)


def test_trap_shadow_frame_index_inverse():
    """POSITIVE: the authoritative frame index is taken from the symbol column."""
    _trap_probe("shadow_idx", None, F_SHADOW_IDX)


def test_trap_rotated_fault_signature_inverse():
    """POSITIVE: a rotated fault signature is restored after its check total holds."""
    _trap_probe("rot_sig", None, F_ROT_SIG)


def test_trap_coarse_allocation_escape_inverse():
    """POSITIVE: an allocation escaped into a coarse unit is restored."""
    _trap_probe("coarse_alloc", None, F_COARSE_ALLOC)


def test_trap_ring_back_reference_inverse():
    """POSITIVE: a signature written as a ring back reference is resolved."""
    _trap_probe("ring_sig", None, F_RING_SIG)


def test_generalises_to_a_fresh_crash_corpus():
    """POSITIVE: an unseen crash corpus is triaged exactly."""
    _end_to_end("fresh", None)


def test_generalises_when_budget_ties_move():
    """POSITIVE: a corpus whose budget ladder moves its ties is triaged exactly."""
    _end_to_end("tie_config", "tie_config")


def test_generalises_when_peaks_sit_on_the_budget():
    """POSITIVE: a corpus whose peaks sit on the budget edge is triaged exactly."""
    _end_to_end("cap_edge", "cap_edge")


def test_generalises_when_rule_presentation_order_inverts():
    """POSITIVE: a corpus whose registry rows arrive in inverted order is triaged exactly."""
    _end_to_end("rule_order", "rule_order")


def test_generalises_when_a_deployment_zone_empties():
    """POSITIVE: a corpus that leaves one deployment zone empty is triaged exactly."""
    _end_to_end("empty_group", "empty_group")


def test_generalises_when_the_registry_carries_a_cycle():
    """POSITIVE: a corpus whose registry carries an alias cycle is triaged exactly."""
    _end_to_end("cycle", "cycle")


def test_generalises_when_the_support_boundary_shifts():
    """POSITIVE: a corpus whose support boundary shifts by one tick is triaged exactly."""
    _end_to_end("boundary_shift", "boundary_shift")


def test_generalises_when_a_new_module_enters():
    """POSITIVE: a corpus carrying a module the registry newly admits is triaged exactly."""
    _end_to_end("new_entity", "new_entity")


def test_generalises_when_a_dormant_rule_fires():
    """POSITIVE: a corpus that wakes a dormant rejection rule is triaged exactly."""
    _end_to_end("nonapplying_rule", "nonapplying_rule")


def test_generalises_when_the_partition_count_changes():
    """POSITIVE: a corpus whose zone partition count changes is triaged exactly."""
    _end_to_end("partition_count", "partition_count")


def test_generalises_when_the_corpus_scales():
    """POSITIVE: a corpus an order of magnitude larger is triaged exactly."""
    _end_to_end("scale_population", "scale_population")


def test_generalises_when_the_corpus_contradicts_the_control_slice():
    """POSITIVE: a corpus whose windows contradict the shipped tallies is triaged exactly."""
    _end_to_end("contradict_calibration", "contradict_calibration")


def test_negative_weight_hardcoded_sample_answers():
    """NEGATIVE: detects shipped-corpus totals or corpus seeds baked into the program."""
    text = _delivered_code()
    files = shipped_population()
    want = json.loads(reference(files)["summary.json"])
    digest = hashlib.sha256(json.dumps(want, sort_keys=True).encode()).hexdigest()
    markers = [digest, str(EVAL_SEED), str(CHALLENGE_SEED),
               str(want["total_bytes_uploaded"]), str(want["total_bytes_allocated"])]
    assert any(marker in text for marker in markers)


def test_negative_weight_filename_dependent_behaviour():
    """NEGATIVE: detects a program whose output depends on the input directory name."""
    files = _population("challenge", _mix(EVAL_SEED, "filename"))
    first = _stage(files)
    second = _stage(files, "reporter-drop-2")
    out_a = Path(tempfile.mkdtemp(prefix="triage-a-")) / "d"
    out_b = Path(tempfile.mkdtemp(prefix="triage-b-")) / "d"
    out_c = Path(tempfile.mkdtemp(prefix="triage-c-")) / "d"
    settled = _run(first, out_a)
    # the control run pins nondeterminism out, so only a program that answers
    # stably under one directory name and differently under another is keying
    # on the name; a nondeterministic program is punished by its own test alone
    assert settled == _run(first, out_c) and settled != _run(second, out_b)


def test_negative_weight_reads_supplied_answer_data_at_runtime():
    """NEGATIVE: detects the delivered program reaching for the labelled sample answers."""
    text = _delivered_code()
    assert any(name in text for name in ANSWER_FILES)


def test_negative_weight_nondeterministic_output():
    """NEGATIVE: detects a program whose triage set changes between identical runs."""
    files = _population("challenge", _mix(EVAL_SEED, "determinism"))
    root = _stage(files)
    out_a = Path(tempfile.mkdtemp(prefix="triage-d1-")) / "d"
    out_b = Path(tempfile.mkdtemp(prefix="triage-d2-")) / "d"
    assert _run(root, out_a) != _run(root, out_b)


if __name__ == "__main__":
    target = Path(sys.argv[1]) / "data" if len(sys.argv) > 1 else Path("environment/data")
    target.mkdir(parents=True, exist_ok=True)
    for _name, _text in sorted(shipped_population().items()):
        (target / _name).write_text(_text, encoding="utf-8", newline="\n")
    print(json.dumps({"written": len(list(target.iterdir()))}, sort_keys=True))
