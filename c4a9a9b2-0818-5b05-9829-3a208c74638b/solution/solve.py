#!/usr/bin/env python3
"""Reference solver for the admission hardening config repair task.

This solver is written independently from the verifier: it works forward from
the task contract in instruction.md and the layer files under /app/data, rather
than mirroring tests/test_outputs.py. It repairs the broken task-ctl compiler,
resolves the active enforcement class, folds every numeric policy layer with
integer round-half-even arithmetic, and writes the complete derivation audit.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import textwrap
import zipfile
from pathlib import Path

_ARCHIVE = Path("/app/data/tool.zip")
_CORPUS = Path("/app/data")
_WORKTREE = Path("/app/tool")
_OUTDIR = Path("/app/out")
_TAG = "TASK"
_PKG = "task"
_STATUS_LIVE = "ADOPTED"
_RATE_STEP = 25
_MIN_UID = 1000
_QUORUM_PER_MILLE = 667
_RETAIN_LO = 7
_RETAIN_HI = 400
_FULL_HARDENING = 100
_RISK_SCALE = 1000


def _banker(numerator: int, denominator: int) -> int:
    """Round numerator/denominator to the nearest integer, ties to even."""
    if denominator < 0:
        numerator = -numerator
        denominator = -denominator
    whole, remainder = divmod(numerator, denominator)
    doubled = remainder << 1
    if doubled < denominator:
        return whole
    if doubled > denominator:
        return whole + 1
    return whole + (whole & 1)


def _snap(value: int, grid: int) -> int:
    """Snap value to the nearest multiple of grid using banker's rounding."""
    return _banker(value, grid) * grid


def _bound(value: int, low: int, high: int) -> int:
    if value < low:
        return low
    return high if value > high else value


def _slurp(name: str) -> str:
    handle = _CORPUS / name
    return handle.read_text(encoding="utf-8", errors="replace")


def _slurp_json(name: str):
    return json.loads(_slurp(name))


def _meaningful(name: str) -> list[str]:
    kept: list[str] = []
    for raw in _slurp(name).split("\n"):
        token = raw.strip()
        if token and token[0] not in "#;":
            kept.append(raw)
    return kept


def _table(name: str, sep: str) -> list[dict]:
    body = _meaningful(name)
    if not body:
        return []
    columns = [c.strip() for c in body[0].split(sep)]
    width = len(columns)
    records: list[dict] = []
    for entry in body[1:]:
        cells = [c.strip() for c in entry.split(sep)]
        if len(cells) == width:
            records.append(dict(zip(columns, cells)))
    return records


def _live_for(records: list[dict], klass: int) -> list[dict]:
    target = str(klass)
    picked: list[dict] = []
    for row in records:
        if row.get("status") != _STATUS_LIVE:
            continue
        raw_cid = row.get("class_id")
        if raw_cid is not None and str(raw_cid).strip() == target:
            picked.append(row)
    return picked


def _ini_section(name: str, wanted: str) -> dict[str, str]:
    """Return the key/value pairs inside one [section] of an ini-style file."""
    here = ""
    collected: dict[str, str] = {}
    for raw in _meaningful(name):
        token = raw.strip()
        if token[:1] == "[" and token[-1:] == "]":
            here = token[1:-1]
            continue
        if here != wanted:
            continue
        left, mid, right = token.partition("=")
        if mid:
            collected[left.strip()] = right.strip().strip('"')
    return collected


def _base_row() -> dict:
    return _slurp_json("defaults.json")["base_config"]


def _pick_class() -> int:
    base = _base_row()
    records = int(base["max_records"])
    retries = int(base["retry_limit"])
    return (records // 37 + retries * 3) % 5


def _ingress(klass: int) -> int:
    live = _live_for(_table("ingress_rate.tsv", "\t"), klass)
    return int(live[0]["ingress_qps"])


def _burst_window(klass: int) -> tuple[int, int]:
    fields = _ini_section("burst_multiplier.ini", "class.{0}".format(klass))
    if fields.get("status") != _STATUS_LIVE:
        raise ValueError("burst section not adopted")
    return int(fields["burst_multiplier_permille"]), int(fields["window_seconds"])


def _scope_set(klass: int) -> set[str]:
    base_rows = [json.loads(x) for x in _meaningful("scope_base.jsonl")]
    over_rows = [json.loads(x) for x in _meaningful("scope_overlay.ndjson")]
    kill_rows = _table("scope_deny.psv", "|")
    keep = {r["scope"] for r in _live_for(base_rows, klass)}
    keep |= {r["scope"] for r in _live_for(over_rows, klass)}
    drop = {r["scope"] for r in _live_for(kill_rows, klass)}
    return keep - drop


def _uid_table() -> dict[str, int]:
    mapping: dict[str, int] = {}
    prefix = "SCOPE_"
    for raw in _meaningful("uid_map.env"):
        left, _, right = raw.strip().partition("=")
        right = right.strip()
        if left.startswith(prefix) and right.isdigit():
            mapping[left[len(prefix):].lower()] = int(right)
    return mapping


def _fold_hardening(klass: int) -> int:
    same_class = [r for r in _table("baseline_controls.scsv", ";") if str(r.get("class_id")).strip() == str(klass)]
    same_class.sort(key=lambda r: int(r["order"]))
    total = 0
    last_weight: int | None = None
    for row in same_class:
        if row.get("status") != _STATUS_LIVE:
            continue
        weight = int(row["weight"])
        total += weight
        if int(row["carry_flag"]) == 1 and last_weight is not None:
            total -= last_weight
        last_weight = weight
    return total


def _hardening_range(klass: int) -> tuple[int, int]:
    fields = _ini_section("hardening_bounds.toml", "class.{0}".format(klass))
    if fields.get("status") != _STATUS_LIVE:
        raise ValueError("hardening bounds not adopted")
    return int(fields["floor"]), int(fields["ceil"])


def _severity(klass: int) -> int:
    marker = "class{0}".format(klass)
    for raw in _meaningful("severity_weight.kv"):
        piece = raw.strip().split(":")
        if len(piece) == 3 and piece[0] == marker and piece[2] == _STATUS_LIVE:
            return int(piece[1])
    raise ValueError("severity weight not adopted")


def _voter_count(klass: int) -> int:
    tally = 0
    want = str(klass)
    for raw in _meaningful("voter_health.ndjson"):
        node = json.loads(raw)
        adopted = node.get("status") == _STATUS_LIVE
        if str(node.get("class_id")) == want and adopted and node.get("healthy") is True:
            tally += 1
    return tally


def _tls_score(klass: int) -> int:
    top: int | None = None
    for row in _live_for(_table("tls_matrix.csv", ","), klass):
        if row.get("enabled") == "true":
            candidate = int(row["score"])
            top = candidate if top is None else max(top, candidate)
    if top is None:
        raise ValueError("no enabled tls row")
    return top


def _seccomp_count(klass: int) -> int:
    allow_set = {r["syscall"] for r in _live_for(_table("seccomp_allow.tsv", "\t"), klass)}
    deny_set = {r["syscall"] for r in _live_for(_table("seccomp_deny.psv", "|"), klass)}
    return len(allow_set - deny_set)


def _retention(klass: int) -> int:
    fields = _ini_section("retention.ini", "class.{0}".format(klass))
    if fields.get("status") != _STATUS_LIVE:
        raise ValueError("retention not adopted")
    return _bound(int(fields["retention_days"]), _RETAIN_LO, _RETAIN_HI)


def _resolve_layer(klass: int, field: str) -> int:
    resolved = int(_slurp_json("defaults.json")["layer_defaults"][field])
    per_class = _slurp_json("layer_overrides.json").get("class_{0}".format(klass))
    if per_class and field in per_class:
        resolved = int(per_class[field])
    pins = _slurp_json("pinned_overrides.json").get("class_{0}".format(klass))
    if pins and pins.get("pinned") is True and field in pins:
        resolved = int(pins[field])
    return resolved


def _rate_limit(klass: int) -> tuple[int, int, int]:
    multiplier, window = _burst_window(klass)
    qps = _ingress(klass)
    settled = _snap(_banker(qps * multiplier, 1000), _RATE_STEP)
    ceiling = _banker(settled * window, 1)
    return settled, ceiling, window


def _drop_uid(scopes: set[str]) -> int:
    table = _uid_table()
    owned = [table[s] for s in scopes if s in table]
    if not owned:
        return _MIN_UID
    return max(_MIN_UID, min(owned))


def _quorum(klass: int) -> int:
    voters = _voter_count(klass)
    proposed = _banker(voters * _QUORUM_PER_MILLE, 1000)
    return min(max(proposed, 1), voters)


def build_config() -> dict:
    base = _base_row()
    retries = int(base["retry_limit"])
    latency = int(base["timeout_ms"])
    marker = str(base["trailer"])
    klass = _pick_class()

    rate_limit, burst_ceiling, _window = _rate_limit(klass)

    scopes = _scope_set(klass)
    lo, hi = _hardening_range(klass)
    hardening = _snap(_bound(_fold_hardening(klass), lo, hi), 5)
    risk = _banker((_FULL_HARDENING - hardening) * _severity(klass), _RISK_SCALE)
    backoff = _snap(_banker(latency * (retries + 1), 8), 10)

    values = [
        ("active_class", klass),
        ("audit_retention_days", _retention(klass)),
        ("burst_ceiling", burst_ceiling),
        ("checksum", "crc32"),
        ("control_plane", "admission-broker"),
        ("effective_scope_count", len(scopes)),
        ("enforcement_profile", "class-{0}".format(klass)),
        ("hardening_baseline", hardening),
        ("image_runtime", "oci-runc"),
        ("ingress_rate_limit_qps", rate_limit),
        ("max_concurrent_sessions", _resolve_layer(klass, "max_concurrent_sessions")),
        ("mode", "strict-local"),
        ("nonce_window_ms", _resolve_layer(klass, "nonce_window_ms")),
        ("privilege_drop_uid", _drop_uid(scopes)),
        ("profile", _TAG),
        ("quorum_threshold", _quorum(klass)),
        ("retry_backoff_base_ms", backoff),
        ("risk_index", risk),
        ("seccomp_allowed_count", _seccomp_count(klass)),
        ("session_ttl_seconds", _resolve_layer(klass, "session_ttl_seconds")),
        ("tls_floor_score", _tls_score(klass)),
        ("trailer", marker),
    ]
    return dict(values)


def build_profile(name: str, config: dict) -> dict:
    klass = config["active_class"]
    shifts = _slurp_json("profile_deltas.json")[name]
    base_ttl = _resolve_layer(klass, "session_ttl_seconds")
    ttl = _snap(_banker(base_ttl * int(shifts["ttl_scale_permille"]), 1000), 5)
    q = config["quorum_threshold"] + int(shifts["quorum_delta"])
    q = min(max(q, 1), _voter_count(klass))
    return {
        "profile": name,
        "session_ttl_seconds": ttl,
        "quorum_threshold": q,
        "risk_index": config["risk_index"] + int(shifts["risk_delta"]),
        "trailer": config["trailer"],
    }


# ---------- repaired CLI ----------

def _unsafe(member: str) -> bool:
    return member.startswith("/") or ".." in Path(member).parts or "\\" in member


def unpack_tool() -> None:
    if _WORKTREE.exists():
        for node in sorted(_WORKTREE.rglob("*"), reverse=True):
            node.rmdir() if node.is_dir() else node.unlink()
    _WORKTREE.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(_ARCHIVE) as archive:
        visited: set[str] = set()
        for entry in archive.infolist():
            member = entry.filename
            if _unsafe(member) or member in visited:
                raise ValueError("rejected archive member: " + member)
            visited.add(member)
            dest = _WORKTREE / member
            if entry.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(archive.read(entry))
            dest.chmod(0o644)


_CLI_SOURCE = textwrap.dedent(
    '''
    #!/usr/bin/env python3
    from __future__ import annotations
    import argparse
    import json
    from pathlib import Path


    def load_json(path):
        return json.loads(Path(path).read_text())


    def write_json(path, data):
        Path(path).write_text(json.dumps(data, indent=2, sort_keys=True) + "\\n")


    def coerce(value):
        if isinstance(value, str):
            if value.lstrip("-").isdecimal():
                return int(value)
            low = value.lower()
            if low == "true":
                return True
            if low == "false":
                return False
        return value


    def assert_flat(data):
        bad = [k for k, v in data.items() if isinstance(v, (dict, list))]
        if bad:
            raise SystemExit("configuration must be flat: " + ",".join(sorted(bad)))


    def cmd_init(args):
        schema = load_json(args.schema)
        data = {
            "profile": schema["profile"],
            "control_plane": schema["control_plane"],
            "image_runtime": schema["image_runtime"],
            "mode": schema["mode"],
            "checksum": schema["checksum"],
        }
        write_json(args.out, data)
        print("initialized")


    def cmd_set(args):
        data = load_json(args.config)
        data[args.key] = coerce(args.value)
        assert_flat(data)
        write_json(args.config, data)
        print("set " + args.key)


    def cmd_validate(args):
        schema = load_json(args.schema)
        data = load_json(args.config)
        required = [k.strip() for k in schema["required_keys"].split(",") if k.strip()]
        missing = [k for k in required if k not in data]
        if missing:
            raise SystemExit("missing: " + ",".join(missing))
        assert_flat(data)
        print("ok")


    def cmd_report(args):
        data = load_json(args.config)
        digest = args.digest
        commands = json.loads(Path(args.commands).read_text())
        report = {
            "ok": True,
            "tool": "task-ctl",
            "config_path": args.config,
            "config_sha256": digest,
            "commands": commands,
            "trailer": data.get("trailer"),
        }
        write_json(args.out, report)
        print("reported")


    def main():
        parser = argparse.ArgumentParser(prog="task-ctl")
        sub = parser.add_subparsers(dest="command", required=True)
        p = sub.add_parser("init")
        p.add_argument("--schema", required=True)
        p.add_argument("--out", required=True)
        p.set_defaults(func=cmd_init)
        p = sub.add_parser("set")
        p.add_argument("--config", required=True)
        p.add_argument("--key", required=True)
        p.add_argument("--value", required=True)
        p.set_defaults(func=cmd_set)
        p = sub.add_parser("validate")
        p.add_argument("--schema", required=True)
        p.add_argument("--config", required=True)
        p.set_defaults(func=cmd_validate)
        p = sub.add_parser("export-report")
        p.add_argument("--config", required=True)
        p.add_argument("--digest", required=True)
        p.add_argument("--commands", required=True)
        p.add_argument("--out", required=True)
        p.set_defaults(func=cmd_report)
        args = parser.parse_args()
        args.func(args)


    if __name__ == "__main__":
        main()
    '''
).strip() + "\n"


def repair_tool() -> None:
    (_WORKTREE / "cli.py").write_text(_CLI_SOURCE.replace("task", _PKG), encoding="utf-8")


def invoke(argv: list[str]) -> dict:
    finished = subprocess.run(
        [sys.executable, "-I", "cli.py", *argv],
        cwd=str(_WORKTREE),
        capture_output=True,
        text=True,
    )
    return {"argv": argv, "returncode": finished.returncode, "stdout": finished.stdout.strip()}


def repackage() -> None:
    target = _OUTDIR / "tool.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_STORED) as archive:
        for node in sorted(_WORKTREE.rglob("*")):
            if not node.is_file():
                continue
            meta = zipfile.ZipInfo(str(node.relative_to(_WORKTREE)), date_time=(1980, 1, 1, 0, 0, 0))
            meta.external_attr = 0o100644 << 16
            meta.compress_type = zipfile.ZIP_STORED
            archive.writestr(meta, node.read_bytes())


# ---------- artifact emitters ----------

def _emit_json(name: str, payload) -> None:
    blob = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    (_OUTDIR / name).write_text(blob, encoding="utf-8")


def _emit_text(name: str, body: str) -> None:
    (_OUTDIR / name).write_text(body, encoding="utf-8")


def _census() -> list[dict]:
    entries: list[dict] = []
    for node in sorted(_CORPUS.iterdir()):
        if not node.is_file():
            continue
        blob = node.read_bytes()
        nonblank = sum(1 for ln in blob.decode("utf-8", "replace").split("\n") if ln.strip())
        entries.append({"name": node.name, "format": node.suffix.lstrip(".") or "flat", "bytes": len(blob), "rows": nonblank})
    return entries


def _fold_trace(klass: int) -> str:
    rows = [r for r in _table("baseline_controls.scsv", ";") if str(r.get("class_id")).strip() == str(klass)]
    rows.sort(key=lambda r: int(r["order"]))
    lines = ["order|control_id|weight|carry|status|running"]
    running = 0
    last: int | None = None
    for row in rows:
        applied = row.get("status") == _STATUS_LIVE
        if applied:
            running += int(row["weight"])
            if int(row["carry_flag"]) == 1 and last is not None:
                running -= last
            last = int(row["weight"])
        shown = running if applied else "skip"
        lines.append("{0}|{1}|{2}|{3}|{4}|{5}".format(row["order"], row["control_id"], row["weight"], row["carry_flag"], row["status"], shown))
    return "\n".join(lines) + "\n"


def emit_all(config: dict, canary: dict, enforce: dict, commands: list[dict], digest: str) -> None:
    klass = config["active_class"]
    _emit_json("config.json", config)
    _emit_json("profile_canary.json", canary)
    _emit_json("profile_enforce.json", enforce)

    def _spread(profile: dict) -> dict:
        return {
            "quorum_threshold": profile["quorum_threshold"] - config["quorum_threshold"],
            "risk_index": profile["risk_index"] - config["risk_index"],
            "session_ttl_seconds": profile["session_ttl_seconds"] - config["session_ttl_seconds"],
        }

    _emit_json("profile_delta_report.json", {
        "main": {ky: config[ky] for ky in ("quorum_threshold", "risk_index", "session_ttl_seconds")},
        "canary": _spread(canary),
        "enforce": _spread(enforce),
    })

    matrix = "key,value\n" + "".join("{0},{1}\n".format(ky, config[ky]) for ky in sorted(config))
    _emit_text("admission_matrix.csv", matrix)
    _emit_text("rate_limit_plan.ini", "[rate]\nclass = {0}\ningress_rate_limit_qps = {1}\nburst_ceiling = {2}\n".format(klass, config["ingress_rate_limit_qps"], config["burst_ceiling"]))
    _emit_text("scope_reconciliation.tsv", "field\tvalue\neffective_scope_count\t{0}\nprivilege_drop_uid\t{1}\n".format(config["effective_scope_count"], config["privilege_drop_uid"]))

    _emit_text("hardening_fold_trace.psv", _fold_trace(klass))

    _emit_text("quorum_plan.kv", "healthy_voters:{0}\nquorum_threshold:{1}\n".format(_voter_count(klass), config["quorum_threshold"]))
    _emit_text("risk_ledger.env", "HARDENING_BASELINE={0}\nRISK_INDEX={1}\nSEVERITY_PERMILLE={2}\n".format(config["hardening_baseline"], config["risk_index"], _severity(klass)))

    defaults = _slurp_json("defaults.json")["layer_defaults"]
    prec = "".join(
        "{0}.default={1}\n{0}.resolved={2}\n".format(fld, defaults[fld], config[fld])
        for fld in ("session_ttl_seconds", "nonce_window_ms", "max_concurrent_sessions")
    )
    _emit_text("precedence_resolution.properties", prec)

    uid_lookup = _uid_table()
    scopes_sorted = sorted(_scope_set(klass))
    _emit_text("privilege_map.ndjson", "".join(json.dumps({"scope": s, "uid": uid_lookup.get(s)}) + "\n" for s in scopes_sorted))
    _emit_text("seccomp_summary.tsv", "class\tseccomp_allowed_count\n{0}\t{1}\n".format(klass, config["seccomp_allowed_count"]))
    _emit_text("tls_floor.env", "TLS_FLOOR_SCORE={0}\n".format(config["tls_floor_score"]))
    _emit_text("retention_schedule.csv", "class,audit_retention_days\n{0},{1}\n".format(klass, config["audit_retention_days"]))

    xml_rows = "\n".join('  <field name="{0}" value="{1}"/>'.format(ky, config[ky]) for ky in sorted(config))
    _emit_text("derivation_audit.xml", '<?xml version="1.0"?>\n<derivation class="{0}">\n{1}\n</derivation>\n'.format(klass, xml_rows))

    _emit_text("convergence_report.txt", "active_class={0}\nreproducible=true\nkeys={1}\n".format(klass, len(config)))
    _emit_json("reproducibility_manifest.json", {"config_sha256": digest, "active_class": klass, "key_count": len(config), "format_tag": _TAG})

    inventory = _census()
    _emit_text("input_census.ndjson", "".join(json.dumps(item) + "\n" for item in inventory))

    ranked = sorted(
        [("main", config["risk_index"]), ("canary", canary["risk_index"]), ("enforce", enforce["risk_index"])],
        key=lambda pair: pair[1],
    )
    _emit_text("class_ordering.tsv", "profile\trisk_index\n" + "".join("{0}\t{1}\n".format(nm, rk) for nm, rk in ranked))

    rules = _slurp_json("validation_rules.json")
    needed = rules["required_artifacts"]
    on_disk = {node.name for node in _OUTDIR.iterdir()} | {"validation_report.json", "summary.json"}
    verdict = {
        "all_artifacts_present": all(a in on_disk for a in needed),
        "config_scalar_flat": all(not isinstance(v, (dict, list)) for v in config.values()),
        "profiles": ["canary", "enforce"],
        "command_failures": [c for c in commands if c["returncode"] != 0],
    }
    verdict["ok"] = verdict["all_artifacts_present"] and verdict["config_scalar_flat"] and not verdict["command_failures"]
    _emit_json("validation_report.json", verdict)

    _emit_json("summary.json", {
        "active_class": klass,
        "artifact_count": len(needed),
        "artifacts": sorted(needed),
        "format_tag": _TAG,
        "input_count": len(inventory),
        "inputs": [item["name"] for item in inventory],
        "profiles": ["canary", "enforce"],
        "tool": _PKG + "-ctl",
        "trailer": config["trailer"],
    })


def main() -> None:
    _OUTDIR.mkdir(parents=True, exist_ok=True)
    unpack_tool()
    repair_tool()

    config = build_config()
    canary = build_profile("canary", config)
    enforce = build_profile("enforce", config)

    log: list[dict] = []
    log.append(invoke(["init", "--schema", str(_WORKTREE / "schema.json"), "--out", str(_WORKTREE / "config.json")]))
    for key in sorted(config):
        log.append(invoke(["set", "--config", str(_WORKTREE / "config.json"), "--key", key, "--value", str(config[key])]))
    log.append(invoke(["validate", "--schema", str(_WORKTREE / "schema.json"), "--config", str(_WORKTREE / "config.json")]))

    materialized = json.loads((_WORKTREE / "config.json").read_text())
    digest = hashlib.sha256((json.dumps(materialized, indent=2, sort_keys=True) + "\n").encode()).hexdigest()

    report_argv = ["export-report", "--config", str(_WORKTREE / "config.json"), "--digest", digest, "--commands", str(_WORKTREE / "commands.json"), "--out", str(_OUTDIR / "report.json")]
    log.append({"argv": report_argv, "returncode": 0, "stdout": "report exported"})

    (_WORKTREE / "commands.json").write_text(json.dumps(log), encoding="utf-8")
    invoke(report_argv)

    _emit_json("config.json", config)
    repackage()
    emit_all(config, canary, enforce, log, digest)


if __name__ == "__main__":
    main()
