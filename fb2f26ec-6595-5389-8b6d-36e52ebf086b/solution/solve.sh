#!/bin/bash
set -euo pipefail

mkdir -p /app/out
cp /solution/solve.py /app/out/scorer.py
chmod +x /app/out/scorer.py

python3 - <<'PY'
import json
import subprocess
import sys
import tempfile
from pathlib import Path

DATA = Path("/app/data")
OUT = Path("/app/out")
MASK64 = (1 << 64) - 1


def seal_of(plan, rows):
    canonical = json.dumps({"plan": plan, "rows": rows},
                           sort_keys=True, separators=(",", ":"))
    h = 1469598103934665603
    for byte in canonical.encode("utf-8"):
        h = ((h ^ byte) * 1099511628211) & MASK64
    return "%016x" % h


def score_one(source):
    with tempfile.TemporaryDirectory() as work:
        target = Path(work) / "board.json"
        subprocess.run([sys.executable, str(OUT / "scorer.py"), str(source), str(target)],
                       check=True)
        return json.loads(target.read_text(encoding="utf-8"))


sealed = sorted(p.stem.split("-", 1)[1] for p in DATA.glob("seal-*.json"))
for name in sealed:
    want = json.loads((DATA / ("seal-%s.json" % name)).read_text(encoding="utf-8"))["seal"]
    produced = score_one(DATA / ("batch-%s.json" % name))
    if seal_of(produced["plan"], produced["rows"]) != want:
        raise SystemExit("the recovered scorer misses the archive seal for %s" % name)
print("[oracle] the recovered scorer seals to all %d archived batches" % len(sealed))

work = sorted(p.stem.split("-", 1)[1] for p in DATA.glob("batch-*.json")
              if p.stem.split("-", 1)[1] not in sealed)
for name in work:
    produced = score_one(DATA / ("batch-%s.json" % name))
    (OUT / ("board-%s.json" % name)).write_text(
        json.dumps(produced, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print("[oracle] %d work scoreboards written under /app/out" % len(work))
PY
