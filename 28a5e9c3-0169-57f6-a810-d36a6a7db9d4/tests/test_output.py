"""Verifier for the TASK service-interaction task."""

from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path
from typing import Any

STATE_PATH = Path("/app/out/final_state.json")
REPORT_PATH = Path("/app/out/report.json")
SCENARIO_ALPHA_PATH = Path("/app/out/scenario_alpha.json")
SCENARIO_BETA_PATH = Path("/app/out/scenario_beta.json")
SCENARIO_MODE_PATH = Path("/app/out/scenario_mode.json")
TRANSCRIPT_SUMMARY_PATH = Path("/app/out/transcript_summary.json")
SUMMARY_PATH = Path("/app/out/summary.json")
EVAL_SEED = 149825609825463980
TRAILER = "harbor:service-interaction:TASK:0000000000016386"
ALPHA_BONUS = 2
BETA_BONUS = 1
ARTIFACT_PATHS = [
    STATE_PATH,
    REPORT_PATH,
    SCENARIO_ALPHA_PATH,
    SCENARIO_BETA_PATH,
    SCENARIO_MODE_PATH,
    TRANSCRIPT_SUMMARY_PATH,
    SUMMARY_PATH,
]
ARTIFACT_STRINGS = [
    "/app/out/final_state.json",
    "/app/out/report.json",
    "/app/out/scenario_alpha.json",
    "/app/out/scenario_beta.json",
    "/app/out/scenario_mode.json",
    "/app/out/transcript_summary.json",
    "/app/out/summary.json",
]
INPUT_FILENAMES = [
    "api_contract.md",
    "sample_requests.jsonl",
    "expected_schema.json",
    "seed_config.json",
    "scenarios.jsonl",
    "auth_policy.json",
    "service.zip",
]


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def expected_state() -> dict[str, Any]:
    return {
        "alpha": (3 * 2) + ALPHA_BONUS,
        "beta": 5 + BETA_BONUS,
        "container": "",
        "mode_code": 0,
        "producer": "",
        "service": "TASK",
        "trailer": TRAILER,
    }


def combined_text() -> str:
    chunks = []
    for path in ARTIFACT_PATHS:
        if path.exists():
            chunks.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(chunks)


def interactions() -> list[dict[str, Any]]:
    report = load_json(REPORT_PATH)
    value = report.get("interactions")
    assert isinstance(value, list)
    return value


def test_artifacts_exist():
    """POSITIVE: all declared service artifacts exist and are non-empty."""
    for path in ARTIFACT_PATHS:
        assert path.is_file(), f"missing {path}"
        assert path.stat().st_size > 40, f"empty or tiny artifact: {path}"
    assert REPORT_PATH.stat().st_size > 300


def test_final_state_is_flat_and_deterministic():
    """POSITIVE: final_state.json is flat, scalar-only, and free of dynamic fields."""
    state = load_json(STATE_PATH)
    assert isinstance(state, dict)
    assert set(state) == set(expected_state())
    assert all(not isinstance(value, (dict, list)) for value in state.values())
    assert not ({"timestamp", "generated_at", "uuid", "random_seed"} & set(state))


def test_final_state_matches_hidden_plan():
    """POSITIVE: final state matches the independently re-derived hidden plan."""
    assert load_json(STATE_PATH) == expected_state()


def test_report_contains_cli_and_api_interactions():
    """POSITIVE: report proves mixed CLI and direct API service interaction."""
    rows = interactions()
    channels = {row.get("channel") for row in rows}
    assert {"cli", "api"} <= channels
    assert sum(1 for row in rows if row.get("returncode") == 0) == len(rows)
    assert len(rows) >= 6


def test_report_transcript_order_is_valid():
    """POSITIVE: transcript preserves the expected operation order."""
    signature = [(row["channel"], row["action"], row["key"]) for row in interactions()]
    assert signature == [
        ("cli", "add", "alpha"),
        ("api", "add", "beta"),
        ("cli", "multiply", "alpha"),
        ("api", "set", "mode_code"),
        ("cli", "add", "alpha"),
        ("api", "add", "beta"),
    ]


def test_service_snapshot_has_canonical_trailer():
    """POSITIVE: final state and report carry the canonical trailer once each."""
    assert load_json(STATE_PATH)["trailer"] == TRAILER
    assert load_json(REPORT_PATH)["trailer"] == TRAILER
    assert STATE_PATH.read_text().count(TRAILER) == 1
    assert REPORT_PATH.read_text().count(TRAILER) == 1


def test_no_external_network_or_credentials():
    """POSITIVE: artifacts contain no external targets or credential material."""
    text = combined_text().lower()
    banned = ["https://", "http://example", "0.0.0.0", "token", "secret", "password", "api_key", "credential"]
    assert not any(item in text for item in banned)
    assert "127.0.0.1" in text or "localhost" in text


def test_replay_report_checksum_matches_state():
    """POSITIVE: report checksum is for the exact delivered final_state.json."""
    report = load_json(REPORT_PATH)
    assert report["ok"] is True
    assert report["state_sha256"] == hashlib.sha256(STATE_PATH.read_bytes()).hexdigest()
    assert report["base_url"] in {"http://127.0.0.1:8765", "http://localhost:8765"}


def test_artifacts_are_regular_bounded_files():
    """POSITIVE: output artifacts are bounded regular files."""
    for path in ARTIFACT_PATHS:
        status = path.stat()
        assert stat.S_ISREG(status.st_mode)
        assert status.st_size < 1024 * 1024


def test_scenario_alpha_matches_reference():
    """POSITIVE: alpha scenario artifact records the reference alpha result."""
    scenario = load_json(SCENARIO_ALPHA_PATH)
    assert scenario == {
        "key": "alpha",
        "name": "alpha",
        "operation_count": 3,
        "source_channels": ["cli"],
        "trailer": TRAILER,
        "value": expected_state()["alpha"],
    }


def test_scenario_beta_matches_reference():
    """POSITIVE: beta scenario artifact records the reference beta result."""
    scenario = load_json(SCENARIO_BETA_PATH)
    assert scenario == {
        "key": "beta",
        "name": "beta",
        "operation_count": 2,
        "source_channels": ["api"],
        "trailer": TRAILER,
        "value": expected_state()["beta"],
    }


def test_scenario_mode_matches_reference():
    """POSITIVE: mode scenario artifact records the reference mode result."""
    scenario = load_json(SCENARIO_MODE_PATH)
    assert scenario == {
        "key": "mode_code",
        "name": "mode",
        "operation_count": 1,
        "source_channels": ["api"],
        "trailer": TRAILER,
        "value": expected_state()["mode_code"],
    }


def test_transcript_summary_matches_report():
    """POSITIVE: transcript_summary.json compactly matches the delivered report."""
    summary = load_json(TRANSCRIPT_SUMMARY_PATH)
    rows = interactions()
    assert summary == {
        "all_returncodes_zero": True,
        "channels": ["api", "cli"],
        "operation_count": len(rows),
        "signature": [f'{row["channel"]}:{row["action"]}:{row["key"]}' for row in rows],
        "trailer": TRAILER,
    }


def test_summary_declares_inputs_outputs_and_hash():
    """POSITIVE: summary.json covers the flat inputs and ordered artifact set."""
    summary = load_json(SUMMARY_PATH)
    assert summary["artifact_count"] == 7
    assert summary["artifacts"] == ARTIFACT_STRINGS
    assert summary["inputs"] == INPUT_FILENAMES
    assert summary["scenario_files"] == ["scenario_alpha.json", "scenario_beta.json", "scenario_mode.json"]
    assert summary["state_sha256"] == hashlib.sha256(STATE_PATH.read_bytes()).hexdigest()
    assert summary["trailer"] == TRAILER


def test_negative_weight_hardcoded_sample_answers():
    """NEGATIVE: fires when submitted artifacts disclose the hidden verifier seed."""
    assert str(EVAL_SEED) in combined_text(), "honest baseline: hidden seed not disclosed in artifacts"


def test_negative_weight_no_real_service_transcript():
    """NEGATIVE: fires when a delivered report lacks mixed successful interaction evidence."""
    if not REPORT_PATH.exists():
        raise AssertionError("honest baseline: no report delivered")
    try:
        rows = interactions()
    except Exception:
        rows = []
    channels = {row.get("channel") for row in rows if isinstance(row, dict)}
    assert not ({"cli", "api"} <= channels) or any(
        row.get("returncode") != 0 for row in rows if isinstance(row, dict)
    ), "honest baseline: mixed service transcript present"


def test_negative_weight_external_network_target():
    """NEGATIVE: fires on non-local network targets in artifacts or source."""
    text = combined_text().lower()
    assert any(item in text for item in ("https://", "http://example", "0.0.0.0", "requests.get", "token=")), "honest baseline: localhost-only service use"


def test_negative_weight_nested_or_nondeterministic_state():
    """NEGATIVE: fires on nested or nondeterministic final-state fabrication."""
    state = load_json(STATE_PATH) if STATE_PATH.exists() else {}
    assert any(isinstance(value, (dict, list)) for value in state.values()) or any(key in state for key in ("timestamp", "generated_at", "uuid")), "honest baseline: flat deterministic state"


if __name__ == "__main__":
    failed = 0
    for name in sorted(globals()):
        if name.startswith("test_"):
            try:
                globals()[name]()
            except Exception as exc:
                failed += 1
                print(f"FAIL {name}: {exc}")
            else:
                print(f"PASS {name}")
    raise SystemExit(1 if failed else 0)
