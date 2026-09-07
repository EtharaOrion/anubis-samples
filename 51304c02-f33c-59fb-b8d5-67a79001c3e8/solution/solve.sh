#!/bin/bash
# Oracle dispatcher for 51304c02-f33c-59fb-b8d5-67a79001c3e8.
set -euo pipefail

python3 /solution/solve.py

echo "[oracle] repaired CLI and wrote /app/out/config.json + /app/out/report.json"
echo "[oracle] wrote profile_dev.json, profile_prod.json, validation_report.json, summary.json, and tool.zip"
