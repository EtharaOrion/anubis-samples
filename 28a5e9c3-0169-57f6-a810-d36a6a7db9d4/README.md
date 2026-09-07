# 28a5e9c3-0169-57f6-a810-d36a6a7db9d4

## Task Metadata

- **Difficulty:** `hard`
- **Category:** `Software Engineering` (internal taxonomy: `Software` / `Systems`)
- **Tags:** ["python", "service", "api", "cli", "stdlib"]
- **Expert time:** 8 hours (480.0 min)
- **Junior time:** 24 hours (1440.0 min)
- **Agent timeout:** 2 hours (7200s)
- **Generator seed:** `0000000000016386`
- **Package name:** `task`
- **Format tag:** `TASK`

## Difficulty explanation

The task is an offline service-interaction exercise. The agent receives a flat
`/app/data` directory with `service.zip` plus six sidecar files:
`api_contract.md`, `sample_requests.jsonl`, `expected_schema.json`,
`seed_config.json`, `scenarios.jsonl`, and `auth_policy.json`. The archive holds
a small Python HTTP simulator and client CLI that must be run locally. The final
state is not present as an answer file; it is obtained by executing a generated
operation plan through a mix of CLI and direct API calls.

The hidden verifier re-derives the expected seeded state from `149825609825463980` and
checks the transcript, so static JSON fabrication or single-channel interaction
earns only partial credit.

## Solution explanation

The oracle extracts the simulator, starts it on `127.0.0.1`, waits for `/health`,
applies seed-derived operations through both `client.py` and direct `urllib`
requests, snapshots `/state`, and writes seven deterministic artifacts in this
stable order: `/app/out/final_state.json`, `/app/out/report.json`,
`/app/out/scenario_alpha.json`, `/app/out/scenario_beta.json`,
`/app/out/scenario_mode.json`, `/app/out/transcript_summary.json`, and
`/app/out/summary.json`.

## Verification explanation

`tests/test_output.py` declares 18 tests (14 positive summing to 48, four
negative summing to -18). Positive tests cover all seven artifacts, flat state,
hidden plan correctness, mixed CLI/API transcript evidence, operation ordering,
canonical trailer, offline-safe content, report/state checksum consistency,
bounded regular artifacts, per-scenario values, transcript summary, and delivered
summary metadata. Negative tests target hidden verifier seed disclosure, fake
transcripts without mixed real interaction, external network targets, and nested
or nondeterministic state.

## Measured scores

Not yet measured. Fill this in after running the bundle as shipped; do not
present target or estimated figures as measurements. The calibration targets the
author is aiming for are oracle at full reward, nop at zero, a state-only solver
around one third, and Opus 4.8 held under roughly half.

## Additional Notes

- The service binds to localhost only and uses stdlib `http.server` plus
  `urllib.request`.
