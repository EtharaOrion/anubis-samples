#!/bin/bash
set -euo pipefail
SOLUTION_ROOT="${SOLUTION_ROOT:-/solution}"
mkdir -p /app/out
cp "${SOLUTION_ROOT}/solve.py" /app/out/transform.py
chmod +x /app/out/transform.py
python3 /app/out/transform.py /app/data /app/out
