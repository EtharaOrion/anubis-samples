The flat files under `/app/data` are what a service fleet left behind after one incident window: what its crash reporter captured, what its runtime probe measured, what its hosts uploaded, the build registry its modules live under, and the written rules its subsystems run under. Your job is to reconstruct that window's fault ledger and the roll-ups that hang off it. The rules are complete and the materials are self-describing, but nothing tells you which of them bears on which figure, or in what order they bite, so treat the directory as something to explore rather than a list handed to you. The crash reporter holds back several wire parameters; the fleet published enough held-out control figures for exactly one assignment of them to survive.

Deliver a reusable program at `/app/out/transform.py`. It runs as a script over an input directory and an output directory and writes the whole triage set under the output directory, so that the same code produces correct results for crash corpora it has never seen. Nothing in it may depend on the particular identifiers, counts or totals present in the shipped corpus.

```bash
python3 /app/out/transform.py <DATA_DIR> <OUT_DIR>
```

Called with no arguments it falls back to reading `/app/data` and writing `/app/out`. Use only the Python standard library; anything installed at solve time is absent at grading time, and the delivered program may not read `/app/data` when it is pointed somewhere else.

The fault ledger goes to `/app/out/normalized.jsonl`, one JSON object per accepted fault frame. The incident roll-up goes to `/app/out/summary.json` and the per-module roll-up to `/app/out/product_summary.json`. One report goes to each of `/app/out/region_central.json`, `/app/out/region_east.json`, `/app/out/region_north.json`, `/app/out/region_south.json` and `/app/out/region_west.json` for the fleet's deployment zones, and one to each of `/app/out/segment_consumer.json`, `/app/out/segment_corporate.json`, `/app/out/segment_enterprise.json` and `/app/out/segment_smb.json` for its tenant segments. The shipped materials fix what those files hold and how they are written.

`/app/out/data_quality.json` is the reconciliation and derivation record. Beyond the corpus's acceptance and rejection tallies and its byte conservation, it records the work: the candidate space you enumerated, the verdict you reached on every candidate, the control figure that falsified each rejection, the number of passes your quarantine settlement took to reach a stable assignment, and your comparison against every published control figure. It has this shape:

```json
{
  "accepted_count": 0,
  "calibration_comparison": [{"control": "", "published": 0, "reproduced": 0}],
  "candidate_search": {
    "evaluations": 0,
    "parameters": [
      {"name": "", "candidates": 0, "control": "", "selected": 0,
       "rejected": [{"value": 0, "control": ""}]}
    ]
  },
  "frame_count": 0,
  "reconciliation": {"allocated_bytes": 0, "residual_bytes": 0, "uploaded_bytes": 0},
  "rejections": {"": 0},
  "settlement": {"passes": 0, "quarantined_modules": [""]}
}
```

Five rules govern how you work. Settle the withheld wire parameters against the published control figures before you compute any figure for the incident corpus. Record a verdict for every candidate value in a parameter's domain before you select that parameter's value. Check the shipped dump and frame check totals against the columns they cover before you trust a decoded field. Once the parameter set is settled, re-run your whole derivation from an empty output directory. Carry the clock, byte and residency arithmetic in Python integers rather than in binary floating point.

Leave everything under `/app/data` as you found it. The program writes nothing outside the output directory it was given.

You have 21600 seconds to complete this task. Do not cheat by using online solutions or hints specific to this task.
