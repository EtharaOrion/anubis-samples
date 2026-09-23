#!/bin/bash
# Oracle dispatcher for 64d3d25b-74db-5ac0-a47d-f1b1073cca81.
set -euo pipefail

mkdir -p /app/out

# Phase 1: derive every artifact from the shipped installation snapshot.
python3 /solution/solve.py /app/data /app/out

# Phase 2: publish the reference program at its declared artifact path. This is the
# last write on purpose. integrity_manifest.json records the sha256 of the program's
# own bytes, so the file that lands here has to be the file that ran.
cp /solution/solve.py /app/out/plan.py

# Phase 3: self-check the delivered tree the way the verifier will read it.
python3 - <<'PY'
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

out = Path("/app/out")
jsonl = (
    "canonical_objects.jsonl", "normalized_geometry.jsonl", "admissibility_matrix.jsonl",
    "bay_candidate_matrix.jsonl", "selected_placement.jsonl", "rejected_placement.jsonl",
    "conservation_adjusted.jsonl", "crate_candidate_matrix.jsonl", "crate_assignment.jsonl",
    "crate_capacity_ledger.jsonl", "crate_contention_report.jsonl", "transport_leg_manifest.jsonl",
    "dependency_edges.jsonl", "cycle_resolution_audit.jsonl", "install_order.jsonl",
    "crew_requirement_ledger.jsonl", "install_window_table.jsonl", "unschedulable_report.jsonl",
    "gallery_occupancy_map.jsonl", "deterministic_audit.jsonl",
)
jsonf = ("penalty_summary.json", "objective_tuple.json", "integrity_manifest.json", "final_state.json")
expected = ("plan.py",) + jsonl + jsonf
missing = [n for n in expected if not (out / n).is_file()]
if missing:
    raise SystemExit("oracle did not write: %s" % missing)

manifest = json.loads((out / "integrity_manifest.json").read_text(encoding="utf-8"))
for name, digest in manifest.items():
    actual = hashlib.sha256((out / name).read_bytes()).hexdigest()
    if actual != digest:
        raise SystemExit("manifest digest mismatch for %s" % name)

chain = "0" * 64
for name in sorted(manifest):
    chain = hashlib.sha256((chain + manifest[name]).encode("utf-8")).hexdigest()
state = json.loads((out / "final_state.json").read_text(encoding="utf-8"))
if state["chain"] != chain:
    raise SystemExit("final_state.json chain does not fold the manifest")
if state["repair_converged"] is not True:
    raise SystemExit("cycle resolution did not converge")
if state["cycle_edges_removed"] <= 0:
    raise SystemExit("the shipped graph was already acyclic, nothing was resolved")
if state["displacement_count"] <= 0:
    raise SystemExit("the shipped snapshot showed no crate contention")
if state["final_cycle_count"] != 0:
    raise SystemExit("resolved graph still carries a cycle")

# Phase 4: the delivered program regenerates the same tree from scratch.
with tempfile.TemporaryDirectory() as scratch:
    again = Path(scratch) / "again"
    subprocess.run([sys.executable, str(out / "plan.py"), "/app/data", str(again)],
                   check=True, timeout=600)
    for name in expected[1:]:
        if (again / name).read_bytes() != (out / name).read_bytes():
            raise SystemExit("rerun produced different bytes for %s" % name)
PY
