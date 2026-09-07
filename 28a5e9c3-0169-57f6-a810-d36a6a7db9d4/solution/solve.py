#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

SOURCE_ARCHIVE = Path("/app/data/service.zip")
DATA = Path("/app/data")
SERVICE = Path("/app/service")
OUT = Path("/app/out")
BASE = "http://127.0.0.1:8765"
ARTIFACTS = [
    "/app/out/final_state.json",
    "/app/out/report.json",
    "/app/out/scenario_alpha.json",
    "/app/out/scenario_beta.json",
    "/app/out/scenario_mode.json",
    "/app/out/transcript_summary.json",
    "/app/out/summary.json",
]
SIDE_INPUTS = [
    "api_contract.md",
    "sample_requests.jsonl",
    "expected_schema.json",
    "seed_config.json",
    "scenarios.jsonl",
    "auth_policy.json",
    "service.zip",
]


def extract() -> None:
    if SERVICE.exists():
        shutil.rmtree(SERVICE)
    SERVICE.mkdir(parents=True)
    with zipfile.ZipFile(SOURCE_ARCHIVE) as zf:
        for info in zf.infolist():
            rel = PurePosixPath(info.filename)
            mode = (info.external_attr >> 16) & 0xFFFF
            if rel.is_absolute() or ".." in rel.parts or stat.S_ISLNK(mode):
                raise SystemExit(f"unsafe source member: {info.filename!r}")
            dest = (SERVICE / Path(*rel.parts)).resolve()
            if not dest.is_relative_to(SERVICE):
                raise SystemExit(f"escaping source member: {info.filename!r}")
            if info.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(zf.read(info))


def request_json(path: str, payload: dict | None = None) -> dict:
    if payload is None:
        with urllib.request.urlopen(BASE + path, timeout=5) as response:
            return json.loads(response.read().decode())
    data = json.dumps(payload).encode()
    request = urllib.request.Request(BASE + path, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode())


def seed_config() -> dict:
    return json.loads((DATA / "seed_config.json").read_text(encoding="utf-8"))


def trailer() -> str:
    return str(seed_config()["trailer"])


def hidden_amounts() -> dict[str, int]:
    config = seed_config()
    return {
        "alpha_bonus": int(config["alpha_bonus"]),
        "beta_bonus": int(config["beta_bonus"]),
    }


def run_cli(action: str, key: str, amount: int) -> dict:
    cmd = [sys.executable, str(SERVICE / "client.py"), "--base", BASE, action, key, str(amount)]
    result = subprocess.run(cmd, cwd=SERVICE, text=True, capture_output=True, timeout=10, check=False)
    return {"channel": "cli", "action": action, "key": key, "amount": amount, "returncode": result.returncode, "stdout": result.stdout.strip()}


def run_api(action: str, key: str, amount: int) -> dict:
    body = {"action": action, "key": key, "amount": amount}
    response = request_json("/apply", body)
    return {"channel": "api", "action": action, "key": key, "amount": amount, "returncode": 0, "response": response}


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def source_channels(transcript: list[dict], key: str) -> list[str]:
    return sorted({row["channel"] for row in transcript if row.get("key") == key})


def scenario_payload(name: str, key: str, value: int, transcript: list[dict]) -> dict:
    return {
        "key": key,
        "name": name,
        "operation_count": sum(1 for row in transcript if row.get("key") == key),
        "source_channels": source_channels(transcript, key),
        "trailer": trailer(),
        "value": value,
    }


def main() -> None:
    extract()
    OUT.mkdir(parents=True, exist_ok=True)
    proc: subprocess.Popen[str] | None = None
    state: dict | None = None
    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                str(SERVICE / "simulator.py"),
                "--host",
                "127.0.0.1",
                "--port",
                "8765",
            ],
            cwd=SERVICE,
            start_new_session=True,
            text=True,
        )
        for _ in range(50):
            try:
                if request_json("/health").get("ok") is True:
                    break
            except Exception:
                time.sleep(0.1)
        else:
            raise SystemExit("service did not become ready")
        plan = json.loads((SERVICE / "plan.json").read_text(encoding="utf-8"))
        transcript = []
        operations = [
            {
                "channel": plan[f"op{idx:02d}_channel"],
                "action": plan[f"op{idx:02d}_action"],
                "key": plan[f"op{idx:02d}_key"],
                "amount": plan[f"op{idx:02d}_amount"],
            }
            for idx in range(1, 5)
        ]
        for op in operations:
            if op["channel"] == "cli":
                transcript.append(run_cli(op["action"], op["key"], int(op["amount"])))
            else:
                transcript.append(run_api(op["action"], op["key"], int(op["amount"])))
        extras = hidden_amounts()
        transcript.append(run_cli("add", "alpha", extras["alpha_bonus"]))
        transcript.append(run_api("add", "beta", extras["beta_bonus"]))
        state = request_json("/state")
    finally:
        if proc is not None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=5)
    if state is None:
        raise RuntimeError("service returned no final state")
    final = {
        "alpha": state["alpha"],
        "beta": state["beta"],
        "container": "",
        "mode_code": state["mode_code"],
        "producer": "",
        "service": "TASK",
        "trailer": trailer(),
    }
    state_path = OUT / "final_state.json"
    write_json(state_path, final)
    digest = hashlib.sha256(state_path.read_bytes()).hexdigest()
    report = {"ok": True, "base_url": BASE, "state_sha256": digest, "interactions": transcript, "trailer": trailer()}
    write_json(OUT / "report.json", report)

    scenarios = {
        "scenario_alpha.json": scenario_payload("alpha", "alpha", int(state["alpha"]), transcript),
        "scenario_beta.json": scenario_payload("beta", "beta", int(state["beta"]), transcript),
        "scenario_mode.json": scenario_payload("mode", "mode_code", int(state["mode_code"]), transcript),
    }
    for filename, payload in scenarios.items():
        write_json(OUT / filename, payload)

    transcript_summary = {
        "all_returncodes_zero": all(row.get("returncode") == 0 for row in transcript),
        "channels": sorted({row["channel"] for row in transcript}),
        "operation_count": len(transcript),
        "signature": [f'{row["channel"]}:{row["action"]}:{row["key"]}' for row in transcript],
        "trailer": trailer(),
    }
    write_json(OUT / "transcript_summary.json", transcript_summary)

    summary = {
        "artifact_count": len(ARTIFACTS),
        "artifacts": ARTIFACTS,
        "inputs": SIDE_INPUTS,
        "scenario_files": sorted(scenarios),
        "state_sha256": digest,
        "trailer": trailer(),
    }
    write_json(OUT / "summary.json", summary)


if __name__ == "__main__":
    main()
