#!/bin/bash
# Oracle dispatcher for a8d5a61c-4126-58f3-8e29-edb360f79699.
set -euo pipefail

mkdir -p /app/out

# Phase 1: derive every artifact from the shipped manifests.
python3 /solution/solve.py /app/data /app/out

# Phase 2: publish the reference program at its declared artifact path. This is
# the last write on purpose. integrity_manifest.json records the sha256 of the
# program's own bytes, so the file that lands here has to be the file that ran.
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
    "canonical_sources.jsonl", "eligibility_ledger.jsonl", "demand_score_ledger.jsonl",
    "phase_membership.jsonl", "proportional_allocation.jsonl", "protected_floor_ledger.jsonl",
    "capacity_binding_trace.jsonl", "precedence_closure.jsonl", "cycle_report.jsonl",
    "cycle_break_decisions.jsonl", "precedence_overturn_trace.jsonl", "carry_state_ledger.jsonl",
    "pre_reconciliation_ledger.jsonl", "global_reconciliation_trace.jsonl", "final_allocation.jsonl",
    "language_allocation_summary.jsonl", "safety_allocation_summary.jsonl",
    "source_allocation_summary.jsonl", "rational_example_weights.jsonl", "fixed_point_weights.jsonl",
    "rounding_residual_ledger.jsonl", "microbatch_manifest.jsonl", "phase_token_totals.jsonl",
    "invariant_report.jsonl", "exception_trace.jsonl",
)
jsonf = ("objective_tuple.json", "integrity_manifest.json", "final_state.json")
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
if state["invariants_hold"] is not True:
    raise SystemExit("declared invariants do not hold")
if state["capacity_displacements"] <= 0:
    raise SystemExit("the shipped snapshot did not bind capacity, nothing to displace")
if state["cycle_edges_removed"] <= 0:
    raise SystemExit("the shipped curriculum carried no cycle")
if state["global_clawbacks"] <= 0:
    raise SystemExit("the shipped snapshot needed no reconciliation")

# Phase 4: the delivered program regenerates the same tree from scratch.
with tempfile.TemporaryDirectory() as scratch:
    again = Path(scratch) / "again"
    subprocess.run([sys.executable, str(out / "plan.py"), "/app/data", str(again)],
                   check=True, timeout=600)
    for name in expected[1:]:
        if (again / name).read_bytes() != (out / name).read_bytes():
            raise SystemExit("rerun produced different bytes for %s" % name)
PY
