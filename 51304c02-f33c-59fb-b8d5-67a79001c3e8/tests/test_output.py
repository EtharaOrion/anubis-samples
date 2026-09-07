"""Verifier for the TASK cli-config task.

Negative tests use polarity inversion: their assertions hold when a cheat is
present, so honest submissions fail those tests and avoid the negative weight.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any

CONFIG_PATH = Path("/app/out/config.json")
REPORT_PATH = Path("/app/out/report.json")
TOOL_ARCHIVE = Path("/app/out/tool.zip")
PROFILE_DEV_PATH = Path("/app/out/profile_dev.json")
PROFILE_PROD_PATH = Path("/app/out/profile_prod.json")
VALIDATION_REPORT_PATH = Path("/app/out/validation_report.json")
SUMMARY_PATH = Path("/app/out/summary.json")
ARTIFACT_PATHS = [CONFIG_PATH, REPORT_PATH, TOOL_ARCHIVE, PROFILE_DEV_PATH, PROFILE_PROD_PATH, VALIDATION_REPORT_PATH, SUMMARY_PATH]
ARTIFACT_NAMES = ["config.json", "report.json", "tool.zip", "profile_dev.json", "profile_prod.json", "validation_report.json", "summary.json"]
_TOOL_ROOT: Path | None = None
EVAL_SEED = 4268352045810440771
FORMAT_TAG = "TASK"
TRAILER = "harbor:cli-config:TASK:0000000000016384"
MAX_RECORDS_BASE = 145
RETRY_LIMIT_BASE = 3
TIMEOUT_MS_BASE = 1771
_UNTRUSTED_UID = 65532
_UNTRUSTED_GID = 65532
_SETPRIV = ["setpriv", "--reuid=nobody", "--regid=nogroup", "--clear-groups"]


def expected_config() -> dict[str, Any]:
    return {
        "checksum": "crc32",
        "container": "",
        "max_records": MAX_RECORDS_BASE,
        "mode": "strict-local",
        "producer": "",
        "profile": FORMAT_TAG,
        "retry_limit": RETRY_LIMIT_BASE,
        "timeout_ms": TIMEOUT_MS_BASE,
        "trailer": TRAILER,
    }


def expected_profile(name: str) -> dict[str, Any]:
    deltas = {
        "dev": {"max_records": -10, "retry_limit": 1, "timeout_ms": -125},
        "prod": {"max_records": 25, "retry_limit": 0, "timeout_ms": 250},
    }[name]
    return {
        "checksum": "crc32",
        "container": "",
        "max_records": MAX_RECORDS_BASE + deltas["max_records"],
        "mode": "strict-local",
        "producer": "",
        "profile": f"{FORMAT_TAG}-{name}",
        "retry_limit": RETRY_LIMIT_BASE + deltas["retry_limit"],
        "timeout_ms": TIMEOUT_MS_BASE + deltas["timeout_ms"],
        "trailer": f"{TRAILER}:{name}",
    }


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def root_only_tests_present() -> bool:
    try:
        status = Path("/tests").stat()
    except FileNotFoundError:
        return False
    return status.st_uid == 0 and (status.st_mode & 0o077) == 0


def untrusted_kwargs() -> dict[str, Any]:
    if os.name != "posix":
        if root_only_tests_present():
            raise RuntimeError("POSIX uid isolation is required")
        return {}
    if os.geteuid() != 0 and root_only_tests_present():
        raise RuntimeError("verifier must run as root to isolate /tests")
    return {}


def untrusted_command(command: list[str]) -> list[str]:
    if os.name == "posix" and os.geteuid() == 0:
        return [*_SETPRIV, *command]
    return command


def tool_root() -> Path:
    global _TOOL_ROOT
    if _TOOL_ROOT is not None:
        return _TOOL_ROOT
    root = Path(tempfile.mkdtemp(prefix="cli-config-tool-"))
    root.chmod(0o755)
    with zipfile.ZipFile(TOOL_ARCHIVE) as zf:
        names: set[str] = set()
        for info in zf.infolist():
            rel = Path(info.filename)
            assert not rel.is_absolute() and ".." not in rel.parts and "\\" not in info.filename
            assert info.filename not in names
            names.add(info.filename)
            dest = root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(zf.read(info))
            dest.chmod(0o644)
    _TOOL_ROOT = root
    return root


def tool_path() -> Path:
    return tool_root() / "cli.py"


def schema_path() -> Path:
    return tool_root() / "schema.json"


def run_repaired_cli(*args: str) -> subprocess.CompletedProcess[str]:
    path = tool_path()
    assert path.is_file(), "repaired CLI source is missing from tool.zip"
    kwargs: dict[str, Any] = {
        "cwd": "/tmp",
        "text": True,
        "capture_output": True,
        "timeout": 20,
        "env": {"PATH": "/usr/local/bin:/usr/bin:/bin", "PYTHONHASHSEED": "0", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
    }
    return subprocess.run(untrusted_command([sys.executable, "-I", str(path), *args]), **kwargs)


def source_text() -> str:
    path = tool_path()
    if not path.exists():
        return ""
    chunks = [path.read_text(encoding="utf-8", errors="replace")]
    schema = schema_path()
    if schema.exists():
        chunks.append(schema.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(chunks)


def test_artifacts_exist():
    """POSITIVE: all declared artifacts exist and are non-empty."""
    for path in ARTIFACT_PATHS:
        assert path.is_file(), f"missing {path}"
        assert path.stat().st_size > 0, f"empty {path}"
    assert CONFIG_PATH.stat().st_size > 100
    assert REPORT_PATH.stat().st_size > 200


def test_config_is_flat_json_object():
    """POSITIVE: config.json is one flat object with scalar values only."""
    data = load_json(CONFIG_PATH)
    assert isinstance(data, dict)
    assert set(data) == set(expected_config())
    assert all(not isinstance(value, (dict, list)) for value in data.values())
    assert all(isinstance(key, str) and key == key.strip() and key.lower() == key for key in data)


def test_config_matches_hidden_seed_policy():
    """POSITIVE: flat config values match independently derived hidden policy."""
    assert load_json(CONFIG_PATH) == expected_config()


def test_report_records_real_cli_commands():
    """POSITIVE: report contains successful local CLI command transcript."""
    report = load_json(REPORT_PATH)
    commands = report["commands"]
    assert report["ok"] is True
    assert report["tool"] == "task-ctl"
    assert isinstance(commands, list) and len(commands) >= 6
    seen = "\n".join(" ".join(item.get("argv", [])) for item in commands)
    for subcommand in ("init", "set", "validate", "export-report"):
        assert subcommand in seen
    assert all(item.get("returncode") == 0 for item in commands)


def test_report_is_deterministic_and_truthful():
    """POSITIVE: report digest and trailer describe the delivered config exactly."""
    report = load_json(REPORT_PATH)
    digest = hashlib.sha256(CONFIG_PATH.read_bytes()).hexdigest()
    assert report["config_sha256"] == digest
    assert report["trailer"] == TRAILER
    assert "generated_at" not in report and "timestamp" not in report


def test_outputs_are_world_readable_regular_files():
    """POSITIVE: artifacts are regular files readable by the verifier."""
    for path in ARTIFACT_PATHS:
        status = path.stat()
        assert stat.S_ISREG(status.st_mode)
        assert status.st_mode & stat.S_IRUSR
        assert status.st_size < 1024 * 1024


def test_no_external_network_endpoints_or_secrets():
    """POSITIVE: outputs contain no endpoint URLs, credentials, or secret material."""
    text = "".join(path.read_text(errors="ignore") for path in ARTIFACT_PATHS).lower()
    banned = ["http://", "https://", "token", "secret", "password", "api_key", "apikey", "credential"]
    assert not any(token in text for token in banned)


def test_cli_source_was_repaired_not_bypassed():
    """POSITIVE: repaired CLI retains subcommands and deterministic flat validation."""
    text = source_text()
    for token in ("add_subparsers", "cmd_init", "cmd_set", "cmd_validate", "cmd_report", "sort_keys=True"):
        assert token in text
    assert "configuration must be flat" in text
    assert "args.config)  # BUG" not in text


def test_config_round_trips_through_cli_validator():
    """POSITIVE: repaired CLI validates the delivered config when run unprivileged."""
    result = run_repaired_cli("validate", "--schema", str(schema_path()), "--config", str(CONFIG_PATH))
    assert result.returncode == 0, result.stderr or result.stdout
    assert result.stdout.strip() == "ok"


def test_canonical_trailer_present():
    """POSITIVE: config carries the canonical seeded trailer once."""
    text = CONFIG_PATH.read_text(encoding="utf-8")
    assert load_json(CONFIG_PATH)["trailer"] == TRAILER
    assert text.count(TRAILER) == 1


def test_profile_dev_matches_embedded_reference():
    """POSITIVE: dev profile artifact is normalized from sidecars and hidden policy."""
    data = load_json(PROFILE_DEV_PATH)
    assert data == expected_profile("dev")
    assert all(not isinstance(value, (dict, list)) for value in data.values())


def test_profile_prod_matches_embedded_reference():
    """POSITIVE: prod profile artifact is normalized from sidecars and hidden policy."""
    data = load_json(PROFILE_PROD_PATH)
    assert data == expected_profile("prod")
    assert all(not isinstance(value, (dict, list)) for value in data.values())


def test_validation_report_covers_all_artifacts_and_profiles():
    """POSITIVE: validation_report.json records deterministic coverage of profiles and artifacts."""
    report = load_json(VALIDATION_REPORT_PATH)
    checks = report["checks"]
    assert report["ok"] is True
    assert checks["all_artifacts_present"] is True
    assert checks["config_matches_policy"] is True
    assert checks["config_scalar_flat"] is True
    assert checks["command_failures"] == []
    assert checks["required_artifacts"] == ARTIFACT_NAMES
    assert set(checks["profiles"]) == {"dev", "prod"}
    assert checks["profiles"]["dev"]["trailer"] == f"{TRAILER}:dev"
    assert checks["profiles"]["prod"]["trailer"] == f"{TRAILER}:prod"


def test_summary_lists_inputs_profiles_and_artifacts():
    """POSITIVE: summary.json has exact artifact order and flat input inventory."""
    summary = load_json(SUMMARY_PATH)
    assert summary["artifact_count"] == 7
    assert summary["artifacts"] == ARTIFACT_NAMES
    assert summary["profiles"] == ["dev", "prod"]
    assert summary["format_tag"] == FORMAT_TAG
    assert summary["trailer"] == TRAILER
    assert summary["input_count"] >= 6
    assert set(summary["inputs"]) >= {"tool.zip", "config_schema.json", "defaults.json", "env_overrides.json", "sample_config_dev.json", "sample_config_prod.json", "validation_rules.json"}


def test_negative_weight_hardcoded_sample_answers():
    """NEGATIVE: fires when repaired source embeds hidden numeric policy constants."""
    text = source_text()
    markers = [str(EVAL_SEED), str(MAX_RECORDS_BASE), str(TIMEOUT_MS_BASE)]
    assert any(marker in text for marker in markers), "honest baseline: hidden policy is not embedded in CLI source"


def test_negative_weight_nested_or_dynamic_config():
    """NEGATIVE: fires when config uses nested containers or nondeterministic fields."""
    data = load_json(CONFIG_PATH)
    dynamic = {"timestamp", "generated_at", "uuid", "random_seed"}
    assert any(isinstance(value, (dict, list)) for value in data.values()) or any(key in data for key in dynamic), "honest baseline: flat deterministic config"


def test_negative_weight_fake_cli_transcript():
    """NEGATIVE: fires when report claims CLI use but no repaired CLI can validate."""
    report = load_json(REPORT_PATH) if REPORT_PATH.exists() else {}
    commands = report.get("commands")
    result = run_repaired_cli("validate", "--schema", str(schema_path()), "--config", str(CONFIG_PATH)) if TOOL_ARCHIVE.exists() else None
    assert commands and (result is None or result.returncode != 0), "honest baseline: transcript is backed by a working CLI"


def test_negative_weight_network_or_secret_material():
    """NEGATIVE: fires on task-specific forbidden network or credential indicators."""
    text = (source_text() + CONFIG_PATH.read_text(errors="ignore") + REPORT_PATH.read_text(errors="ignore")).lower()
    assert any(token in text for token in ("requests", "urllib", "socket", "https://", "api_key", "password")), "honest baseline: offline local configuration"


if __name__ == "__main__":
    failures = 0
    for name in sorted(globals()):
        if name.startswith("test_"):
            try:
                globals()[name]()
            except Exception as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
            else:
                print(f"PASS {name}")
    raise SystemExit(1 if failures else 0)
