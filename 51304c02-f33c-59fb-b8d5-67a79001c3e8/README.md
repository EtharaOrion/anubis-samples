# 51304c02-f33c-59fb-b8d5-67a79001c3e8

## Task Metadata

- **Difficulty:** `hard`
- **Category:** `System Administration` (internal taxonomy: `Software` / `Systems`)
- **Tags:** ["python", "cli", "configuration", "debugging", "stdlib"]
- **Expert time:** 8 hours (480.0 min)
- **Junior time:** 24 hours (1440.0 min)
- **Agent timeout:** 2 hours (7200s)
- **Generator seed:** `0000000000016384`
- **Package name:** `task`
- **Format tag:** `TASK`

## Difficulty explanation

The bundle is a CLI configuration-repair task. The agent receives a stdlib-only
Python command in `/app/data/tool.zip` with independent defects in subcommand
dispatch, schema loading, value normalization, flat-key validation, and JSON
serialization. Six flat JSON sidecars provide an external schema summary,
defaults, environment overrides, two sample profile configs, and validation
rules. A successful solver must repair the tool and drive it locally to produce
`/app/out/config.json`, `/app/out/report.json`, a deterministic
`/app/out/tool.zip`, normalized dev/prod profile artifacts,
`/app/out/validation_report.json`, and `/app/out/summary.json`.

The hidden checker derives the expected policy from `4268352045810440771`, so a solver
that only preserves the visible defaults or hand-writes plausible artifacts does
not receive full credit.

## Solution explanation

The oracle rewrites the local CLI to support `init`, `set`, `validate`, and
`export-report` commands with deterministic JSON output. It then runs those
commands to create a flat profile containing the generated tag, local producer,
seed-derived limits, retry policy, checksum mode, and canonical trailer.
It also normalizes the dev and prod sidecar samples using the same seeded policy
and writes deterministic validation and summary artifacts.

## Verification explanation

`tests/test_output.py` declares 18 tests (14 positive summing to 48, four
negative summing to -16). Positive tests cover artifact existence, flat data,
seeded policy correctness, truthful command reporting, deterministic files,
regular permissions, offline-safe content, actual CLI repair evidence,
round-trip validation, the canonical trailer, normalized profile artifacts,
validation reporting, summary consistency, and exact artifact coverage. Negative
tests target hardcoded hidden policy constants, nested or dynamic configuration
shapes, fake transcripts, and network or secret material.

## Measured scores

Measured over 8 trials per cohort (16 total), judged by `gpt-5.6-sol`.

| Cohort | n | Tests (Channel A) | Rubric (Channel B) | Combined |
|---|---:|---:|---:|---:|
| `anthropic/claude-opus-5` | 8 | 38.54 | 7.27 | 22.91 |
| `zai/glm-5.3` | 8 | 31.77 | 0.46 | 16.11 |
| **Mean** | 16 | 35.16 | 3.87 | **19.51** |

No trial solved the task. The best single-trial test score across both cohorts
was 43.75 of 100. The author's calibration target of holding Opus under roughly
half was met, at 38.54. Rubric scores sit near the floor on both cohorts, which
is what pulls the combined score into the expert band.

## Additional Notes

- The verifier re-derives hidden values from `4268352045810440771` and never reads an
  answer file from the agent-visible data.
- Rendered bundles contain seven flat input files in `environment/data`:
  `tool.zip` plus six JSON sidecars, with no input subdirectories.
