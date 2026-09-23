#!/bin/bash
# Reference dispatcher for c4a9a9b2-0818-5b05-9829-3a208c74638b.
set -euo pipefail

python3 /solution/solve.py

echo "[reference] repaired task-ctl and wrote /app/out/config.json + /app/out/report.json"
echo "[reference] wrote profile_canary.json, profile_enforce.json, validation_report.json, summary.json, and tool.zip"
