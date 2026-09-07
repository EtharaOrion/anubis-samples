# TRUTH.md - 28a5e9c3-0169-57f6-a810-d36a6a7db9d4

## Defect

Nothing in this task is broken. `/app/data/service.zip` holds a working local service
simulator, a working client CLI, and a machine-readable operation plan, and the whole
difficulty is that the required end state cannot be computed - it has to be *reached*, by
starting the simulator and driving it through two different channels in a specific order,
and then reporting truthfully on what happened. The plan file names four operations; the
end state requires six, and the last two are not in the plan at all. Their amounts live in
`/app/data/seed_config.json` as `alpha_bonus` = 2 and `beta_bonus` =
1, and the channel each one must travel is stated only in
`/app/data/scenarios.jsonl`, in prose. Every one of those numbers is drawn from generator
seed `0000000000016386`, so nothing below can be recalled from training.

1. (focal) The simulator is a real HTTP server that must actually be running. It binds
   `127.0.0.1` on the port named in `seed_config.json`, serves `GET /health`,
   `POST /apply`, and `GET /state`, and holds all state in a module-level dict that exists
   only for the lifetime of the process. There is no file to read and no state to
   reconstruct: `GET /state` against a live server is the only source of the final numbers.
   An agent that reads `simulator.py`, works out the arithmetic on paper, and writes
   `final_state.json` by hand can land the right values and still fail, because
   `test_report_contains_cli_and_api_interactions` demands six successful interactions with
   both channels present.
2. (focal) The plan is four operations; the trajectory is six. `plan.json` supplies
   `op01`..`op04` - CLI `add alpha`, API `add beta`, CLI `multiply alpha`, API
   `set mode_code`. `scenarios.jsonl` then adds two more in prose: alpha is changed by "a
   final local CLI bonus" and beta by "a final local API bonus", with the amounts in
   `seed_config.json`. The bonus channels are not interchangeable - the alpha bonus must go
   through the client CLI and the beta bonus through the direct API - and
   `test_report_transcript_order_is_valid` asserts the full six-element signature in exactly
   that order, so a swapped pair fails even though the arithmetic is identical.
3. (focal) Order is load-bearing arithmetic, not bookkeeping. Alpha is `add` then
   `multiply` then `add`, and multiplication does not commute with addition. Running the
   bonus before the multiply, or batching the two alpha operations, produces a different
   number for the same set of operations, which `test_final_state_matches_hidden_plan`
   catches by re-deriving the expected state independently.
4. (focal) `report.json` has to carry the sha256 of `final_state.json` **as delivered on
   disk**, not of the state dict in memory.
   `test_replay_report_checksum_matches_state` recomputes `sha256(STATE_PATH.read_bytes())`
   and compares. Because the state file is written with sorted keys, a two-space indent, and
   a trailing newline, hashing anything other than the exact bytes that were written -
   a re-serialised dict, a compact dump, a version without the newline - produces a digest
   that does not match a file which is otherwise perfectly correct. The state must be
   written first, then read back, then hashed.
5. (drift) `final_state.json` is not the simulator's `/state` response. The snapshot from
   the service has three keys - `alpha`, `beta`, `mode_code` - and the delivered artifact
   must have seven, adding `service`, `producer`, `container`, and `trailer` from
   `seed_config.json`, exactly as `expected_schema.json` lists them. The experienced
   instinct is to persist the API response verbatim because it is the authoritative source;
   `test_final_state_is_flat_and_deterministic` asserts the delivered key set matches the
   seven-key schema. Two of those four added fields are empty strings for this instance, so
   they look droppable and are not: the key set is compared, not the values.
6. (trap) `test_negative_weight_external_network_target` scans the artifacts and the
   delivered source for the bare substrings `https://`, `http://example`, `0.0.0.0`,
   `requests.get`, and `token=`. The service is reached over plain `http://` on a loopback
   address, so any of those appearing anywhere - a documentation link in a comment, a
   `0.0.0.0` bind used to "make the server reachable", an example URL in a docstring -
   fires the cheat detector on an otherwise honest run. Binding `0.0.0.0` is doubly wrong:
   `auth_policy.json` sets `network_scope` to `localhost-only`.
7. (trap) The trailer must appear **exactly once** in `final_state.json` and exactly once
   in `report.json`. `test_service_snapshot_has_canonical_trailer` does a literal
   `text.count(TRAILER) == 1` over each file's raw bytes. Adding the trailer to every
   transcript entry for consistency, or echoing it inside a nested object, fails a test
   that reads as a simple presence check.

The reference is authored in Python (Python 3.12, standard library only, a
226-line driver) with a Bash oracle dispatcher (`solve.sh`) that
extracts the archive, launches the simulator in its own process group, polls `/health`
until it answers, replays the plan and the two bonuses through the correct channels,
snapshots `/state`, and tears the server down in a `finally` block before writing a single
artifact. A deterministic channel observes the delivered state, transcript, and scenario
files (Channel A, `tests/test_output.py`, weighted through `tests/test_weights.json`); a
judged rubric channel grades the drive-the-service trajectory (Channel B,
`tests/rubric.json`). Counts and weights are not mirrored into the prose above - read them
from those files, or from the lists below, which are generated from them.

Out of bounds: writing a final state that was computed rather than observed, or a
transcript with no successful mixed-channel evidence behind it
(`test_negative_weight_no_real_service_transcript`); disclosing the evaluation seed in any
delivered artifact (`test_negative_weight_hardcoded_sample_answers`); naming a non-local
network target, a credential-shaped string, or a wildcard bind
(`test_negative_weight_external_network_target`); nesting objects or lists inside the final
state, or adding a timestamp, uuid, or generated-at field
(`test_negative_weight_nested_or_nondeterministic_state`); installing third-party packages;
and looking the plan up online - it is generated for this instance and exists nowhere else.

## Load-bearing work

7 artifacts are collected, and every one of them is graded:

- `/app/out/final_state.json`
- `/app/out/report.json`
- `/app/out/scenario_alpha.json`
- `/app/out/scenario_beta.json`
- `/app/out/scenario_mode.json`
- `/app/out/transcript_summary.json`
- `/app/out/summary.json`

- `/app/out/final_state.json` (new) - the seven keys from
  `expected_schema.json`'s `final_state_required`: `alpha`, `beta`, `mode_code` observed
  from `GET /state` after the sixth operation, plus `service`, `producer`, `container`, and
  `trailer` from `seed_config.json`. Flat, scalar-only, sorted keys, trailing newline.
- `/app/out/report.json` (new) - `ok`, `base_url` (the loopback URL actually used),
  `state_sha256` (of the delivered state file's bytes), `interactions` (the ordered
  six-entry transcript, each with `channel`, `action`, `key`, `amount`, and a
  `returncode` of `0`), and `trailer`.
- `/app/out/scenario_alpha.json`, `scenario_beta.json`, `scenario_mode.json` (new) - one
  per line of `scenarios.jsonl`, each carrying `name`, `key`, the final `value` for that
  key, `source_channels` (the sorted set of channels that touched it - two for alpha and
  beta, one for `mode_code`), `operation_count` for that key, and `trailer`.
- `/app/out/transcript_summary.json` (new) - the compact restatement:
  `all_returncodes_zero`, `channels` as the sorted list `["api", "cli"]`,
  `operation_count`, `signature` as one `channel:action:key` string per interaction in
  order, and `trailer`. It is compared field-for-field against the report, so it must be
  derived from the report rather than written alongside it.
- `/app/out/summary.json` (new) - `artifact_count`, the ordered `artifacts` list, the seven
  `inputs` consumed from `/app/data`, the sorted `scenario_files`, the state digest, and
  `trailer`.

Every rule those artifacts must satisfy lives in `tests/test_output.py` and is mirrored by
the reference `solution/solve.py`. This section names the deliverables so a reader knows the
shape of the work, not the text to type.

Out of bounds by design: a final state assembled from arithmetic rather than from
`GET /state`; a transcript describing operations no process performed; the two bonus
operations sent through the wrong channels or in the wrong position; artifacts larger than
a megabyte or that are not regular files; and any output whose bytes change between two
runs.

## Verification

Grader entrypoint: `tests/test.sh`. It does not use pytest. It preflights the bijection
between `tests/test_weights.json` and the `test_*` functions in `tests/test_output.py`,
writes a floor reward of `0.0`, runs each weighted test by direct function call, sums the
weights of the passers into `raw`, and writes
`reward = clamp(raw / positive_total, -1.0, 1.0)` to `/logs/verifier/reward.txt`,
`reward.json`, and a CTRF-shaped `ctrf.json`. Channel B is judged separately against
`tests/rubric.json`. The rule that combines the two channels is set by the runner harness,
not by files in this bundle - do not transcribe a formula here.

Channel A declares 14 positive and 4 negative tests, positive weight total
48 and negative weight total -18, so the
theoretical reward range is -18/48 = -0.375 to +1.000.

Positive tests (14):

- `test_artifacts_exist` (weight 1)
- `test_final_state_is_flat_and_deterministic` (weight 3)
- `test_final_state_matches_hidden_plan` (weight 5)
- `test_report_contains_cli_and_api_interactions` (weight 5)
- `test_report_transcript_order_is_valid` (weight 5)
- `test_service_snapshot_has_canonical_trailer` (weight 3)
- `test_no_external_network_or_credentials` (weight 3)
- `test_replay_report_checksum_matches_state` (weight 5)
- `test_artifacts_are_regular_bounded_files` (weight 3)
- `test_scenario_alpha_matches_reference` (weight 3)
- `test_scenario_beta_matches_reference` (weight 3)
- `test_scenario_mode_matches_reference` (weight 3)
- `test_transcript_summary_matches_report` (weight 3)
- `test_summary_declares_inputs_outputs_and_hash` (weight 3)

Negative tests (4) are polarity-inverted: their assertions hold only
when the cheat is present, so an honest run leaves them "failing" and contributes zero
weight. They subtract only from a solver that actually cheats.

- `test_negative_weight_hardcoded_sample_answers` (weight -5)
- `test_negative_weight_no_real_service_transcript` (weight -5)
- `test_negative_weight_external_network_target` (weight -5)
- `test_negative_weight_nested_or_nondeterministic_state` (weight -3)

Channel B is 10 criteria (8 positive / 2 negative), positive total 32:

- **R1** (+3, trajectory) - The agent inspects the simulator, client CLI, and local plan format before attempting service operations.
- **R2** (+5, trajectory) - The agent starts the local simulator and confirms readiness through localhost-only interaction.
- **R3** (+5, trajectory) - The agent uses both the provided CLI and direct API requests for distinct state transitions.
- **R4** (+3, trajectory) - The agent derives operation parameters from local task data rather than hidden verifier constants or guesses.
- **R5** (+5, trajectory) - The agent captures command and API evidence for the ordered service operations during the working session.
- **R6** (+5, trajectory) - The agent keeps all interactions local and avoids external network dependencies or credentials.
- **R7** (-5, trajectory) - The agent retrieves a task-specific final state, prior run, or hidden operation plan from an external source.
- **R8** (-5, trajectory) - The agent fabricates service success while local simulator command or API evidence is absent.
- **R9** (+3, user_facing_message) - The response summarizes the completed work and names where the produced artifacts were written.
- **R10** (+3, trajectory) - The agent executes the ordered service operations using only the instruction-specified CLI subcommands and API endpoints, without invoking undocumented commands.

The expected end state is re-derived inside the grader from `EVAL_SEED = 149825609825463980`,
distinct from the visible `SAMPLE_SEED = 7841312131247071173` and the labelled
`CHALLENGE_SEED = 552827494654008015`; the grader replays the plan and the two bonuses
symbolically and compares the result to the delivered file, so a transcript that is
internally consistent but reached the wrong numbers fails
`test_final_state_matches_hidden_plan` while still passing the shape checks. The oracle
expectation is that running `solution/solve.sh` inside the container drives every positive
Channel A test to pass and holds every negative test in its failing state.

## Root cause

**The end state is unreachable by reasoning.** Every other task in this family can in
principle be solved by deriving the answer and writing it down. This one cannot, because
the graded evidence is the transcript as much as the state, and the transcript is only
truthful if six processes and requests genuinely ran. That is why
`test_report_contains_cli_and_api_interactions`,
`test_report_transcript_order_is_valid`, and
`test_final_state_matches_hidden_plan` are all weighted at the top of the scale, and why
rubric R2, R3, and R5 all grade running things rather than knowing things.

**Two channels, two different mechanisms.** `client.py` is a subprocess: it takes
positional `action key amount` and a `--base` URL, and it prints the resulting state as
JSON on stdout. The API is `urllib.request` against `POST /apply` with a JSON body. They
are not interchangeable wrappers over one path - the CLI's evidence is a `returncode` and
captured stdout, the API's evidence is a parsed response body - and
`auth_policy.json` names both as `allowed_channels` precisely because the task requires
distinct state transitions through each.

**Where the missing operations are specified.** No single file states the six-operation
trajectory. `plan.json` has four; `scenarios.jsonl` says in prose that alpha takes "a final
local CLI bonus" and beta "a final local API bonus"; `seed_config.json` supplies the two
amounts. Reconciling three files is the actual comprehension task, and it is what rubric R4
grades - deriving operation parameters from local task data rather than from hidden
verifier constants or guesses.

**Lifecycle discipline.** The simulator is `ThreadingHTTPServer(...).serve_forever()`; it
never exits on its own. It has to be started as a child process, polled at `/health` until
it answers rather than slept on for a fixed interval, and killed in a `finally` block -
ideally by process group, since it is a threading server - or the task hangs until the
7200-second agent timeout. Snapshotting `/state` must happen before
the teardown, because once the process dies the state is gone.

**Digest ordering.** The report's `state_sha256` is a hash of a file, so the file has to
exist in its final form first. Write `final_state.json`, read its bytes back, hash those,
then build the report around the digest. Reversing those two steps is the single most
common way to fail a test everything else about the run has already satisfied.

**Determinism requirements.** The simulator's responses are deterministic given the
operation order, so the only sources of drift are self-inflicted: JSON key order, a
wall-clock or uuid field added to a report, and a dynamically chosen port leaking into
`base_url`. `test_replay_report_checksum_matches_state` restricts `base_url` to the two
loopback forms on the configured port, which closes the last one.

**Red lines.** No fabricated transcript; no computed-instead-of-observed final state; no
non-loopback bind or external URL; no credential-shaped strings; no nested values or
timestamp fields in the state; no seed disclosure; no third-party packages; no online
lookup.

## Solution shape

1. Extract `/app/data/service.zip` into `/app/service`, rejecting absolute paths, `..`
   members, and symlinks on the way in, and read `simulator.py`, `client.py`, and
   `plan.json` to learn the surface before touching anything.
2. Read the five sidecars: `api_contract.md` for the three endpoints and the valid
   action/key sets, `expected_schema.json` for the required key list of each artifact,
   `seed_config.json` for the port, the trailer, the identity fields, and the two bonus
   amounts, `scenarios.jsonl` for the three scenario definitions and the bonus channels,
   and `auth_policy.json` for the localhost-only, no-credentials constraint.
3. Launch `simulator.py` as a child process on `127.0.0.1` at the configured port, in its
   own process group so it can be torn down cleanly, and wrap everything that follows in a
   `try`/`finally` whose cleanup kills that group.
4. Poll `GET /health` in a bounded retry loop with a short sleep between attempts until it
   returns `{"ok": true}`, and fail loudly rather than proceeding if it never does.
5. Read the four planned operations out of `plan.json` by their `op01`..`op04` key prefixes
   and dispatch each one to the channel its `_channel` field names: CLI operations run
   `client.py` as a subprocess with captured stdout and a recorded `returncode`, API
   operations `POST /apply` and record the parsed response. Append each to an ordered
   transcript.
6. Append the two bonus operations in order - the alpha bonus through the CLI, the beta
   bonus through the API - using the amounts from `seed_config.json`.
7. Snapshot `GET /state`, then tear the simulator down. Build `final_state.json` from the
   snapshot's three keys plus the four identity fields from `seed_config.json`, write it
   with sorted keys and a trailing newline, read the bytes back, and hash them.
8. Write the report around that digest, derive the three scenario files and
   `transcript_summary.json` from the transcript rather than from intent, and write
   `summary.json` naming the inputs, artifacts, scenario files, digest, and trailer.

## Ordered plan

1. Extract and read first. Open `simulator.py` and `client.py` and note the three
   endpoints, the three valid keys, the three valid actions, and the fact that state lives
   only in memory. Rubric R1 credits exactly this: inspecting the simulator, the client
   CLI, and the local plan format before attempting service operations.
2. Reconcile the three specification files into one six-operation list before starting the
   server - four from `plan.json`, two from `scenarios.jsonl` with amounts from
   `seed_config.json` - and write the list down with its channels. Rubric R4 credits
   deriving the operation parameters from local task data rather than from guesses.
3. Start the simulator and confirm readiness by polling `/health` rather than sleeping.
   Fetch `/state` once before doing anything and confirm it reads all zeros; that baseline
   makes every subsequent change attributable. Rubric R2 credits starting the local
   simulator and confirming readiness through localhost-only interaction.
4. Run the four planned operations in order, checking after each that the response or the
   CLI stdout shows the change you expected. Rubric R3 credits using both the provided CLI
   and direct API requests for distinct state transitions, and this is where both channels
   first appear.
5. Run the two bonus operations, alpha through the CLI and beta through the API, and verify
   that alpha now reflects `add` then `multiply` then `add` rather than the commuted
   ordering. Rubric R5 credits capturing command and API evidence for the ordered service
   operations during the session.
6. Snapshot `/state` while the server is still alive, then tear it down. Build and write
   `final_state.json` with all seven keys, then read the file back and hash its bytes -
   in that order - and build the report around the digest.
7. Derive the three scenario files and the transcript summary from the transcript you
   captured, not from the plan you intended: `source_channels` and `operation_count` are
   computed by filtering the transcript on each key, and the summary's `signature` is one
   `channel:action:key` string per entry in order. Rubric R6 credits keeping all
   interactions local with no external network dependencies or credentials.
8. Re-read every delivered artifact and confirm the trailer appears exactly once in the
   state and once in the report, that no artifact contains `https://`, `0.0.0.0`, or a
   token-shaped string, and that the state is flat. Run the whole thing a second time into
   a clean output directory and diff byte for byte. Close with a summary naming each
   artifact and where it was written - rubric R9 credits that closing summary.

## Pitfalls

- **Computing the final state instead of observing it.** The arithmetic is easy and the
  answer will be right, but `test_report_contains_cli_and_api_interactions` requires six
  interactions with `returncode` `0` across both channels, and
  `test_negative_weight_no_real_service_transcript` fires when that evidence is missing.
- **Stopping after the four planned operations.** `plan.json` is not the whole trajectory.
  `test_report_transcript_order_is_valid` asserts a six-element signature, and the final
  alpha and beta values are wrong without the bonuses.
- **Sending the bonuses through the wrong channels.** The alpha bonus is a CLI operation and
  the beta bonus is an API operation; `scenarios.jsonl` says so in prose and the transcript
  signature check compares the channel of every entry.
- **Reordering alpha's operations.** `add`, `multiply`, `add` is not the same as `add`,
  `add`, `multiply`. `test_final_state_matches_hidden_plan` re-derives the expected value
  independently.
- **Persisting the `/state` response as `final_state.json`.** It has three keys; the schema
  requires seven. The four identity fields come from `seed_config.json`, and two of them are
  empty strings that still must be present as keys.
- **Hashing the state dict instead of the state file.**
  `test_replay_report_checksum_matches_state` recomputes the digest from
  `STATE_PATH.read_bytes()`, so the indent, the key order, and the trailing newline are all
  part of what is hashed.
- **Writing the report before the state file.** The digest cannot exist until the file
  does; building the report first forces a re-hash that is easy to forget.
- **Binding `0.0.0.0` to "make the server reachable".** It violates the localhost-only
  policy and puts the literal `0.0.0.0` into the delivered source, which fires
  `test_negative_weight_external_network_target`.
- **Leaving an `https://` link in a comment or docstring.** The substring scan does not care
  that it is a documentation URL, and neither does `token=` in an example.
- **Repeating the trailer for consistency.** Adding it to each transcript entry makes
  `text.count(TRAILER)` exceed one and fails
  `test_service_snapshot_has_canonical_trailer`, which otherwise looks like a presence
  check.
- **Sleeping instead of polling `/health`.** A fixed sleep is either too short on a loaded
  machine - producing a connection error on the first operation - or wastes budget. The
  contract names `/health` for exactly this.
- **Leaving the simulator running.** `serve_forever()` never returns; without a `finally`
  that kills the process group, the session hangs to the
  7200-second timeout with the artifacts unwritten.
- **Tearing the server down before snapshotting `/state`.** State is in-memory only. Once
  the process dies there is no way to recover the final numbers except by replaying every
  operation.
- **Writing `transcript_summary.json` from the plan rather than the report.**
  `test_transcript_summary_matches_report` compares the whole object field-for-field against
  values recomputed from `report.json`'s `interactions`, including `channels` as the sorted
  list `["api", "cli"]`.
- **Getting `source_channels` wrong in the scenario files.** It is the sorted set of
  channels that actually touched that key across the transcript - two for alpha and beta,
  one for `mode_code` - not the channel of the scenario's headline operation.
- **Adding a timestamp, uuid, or generated-at field.**
  `test_final_state_is_flat_and_deterministic` rejects those four key names outright, and
  `test_negative_weight_nested_or_nondeterministic_state` fires on them.
- **Ignoring a non-zero CLI return code.** A transcript that records failures while the
  report claims `ok` is the exact contradiction
  `test_report_contains_cli_and_api_interactions` looks for, since it requires every entry's
  `returncode` to be `0`.
- **Choosing an arbitrary free port.** `test_replay_report_checksum_matches_state`
  constrains `base_url` to the loopback forms on the port from `seed_config.json`.

## Provenance

```yaml
uuid: null  # task.toml carries no canary_guid or uuid field
instance_id: 28a5e9c3-0169-57f6-a810-d36a6a7db9d4
task_name: terminal-bench/28a5e9c3-0169-57f6-a810-d36a6a7db9d4
fix_patch_sha256: 12152857438b3a65e6ab1508250be6ac825e2c915c6da4b225dc45167a8138e1  # sha256 of solution/solve.sh
solve_py_sha256: 6a1e8084a718c638e6027f968cd3f0b33546b187b2b2cae7d3d123b4d0a5b16d  # informational; solve.py is 226 lines
language: python
golden_score_measured: null  # not measured for this generated instance
golden_score_channel_a_pct: null
golden_score_channel_b_pct: null
golden_score_runs: 0
golden_score_trial_id: null  # no calibration trial has been run yet
golden_score_verified_by: >
  Not yet measured. This instance has not been through an oracle / nop
  / agent calibration sweep. Run the bundle with the oracle agent (expected
  reward 1.000) and the nop agent (expected reward 0.000) before recording
  any figure here, and never present a target or an estimate as a
  measurement.
caveat: >
  Channel A carries 14 positive tests worth 48 weight and 4
  polarity-inverted negative tests worth -18 weight, so the theoretical
  reward range is -18/48 = -0.375 to +1.000. Channel B carries 10 rubric
  criteria worth 32 positive points. The rule that combines the two channels
  belongs to the runner harness, not to this bundle, so no combined formula
  is transcribed here. A partial attempt normally scores far higher on
  Channel B than on Channel A, because the rubric grades derivation and
  trajectory while Channel A grades exact bytes.
```
