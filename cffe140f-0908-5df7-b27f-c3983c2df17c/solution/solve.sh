#!/bin/bash
# Oracle dispatcher for cffe140f-0908-5df7-b27f-c3983c2df17c.
set -euo pipefail

mkdir -p /app/out
cp /solution/solve.py /app/out/derive_report.py
python3 /app/out/derive_report.py /app/data /app/out
