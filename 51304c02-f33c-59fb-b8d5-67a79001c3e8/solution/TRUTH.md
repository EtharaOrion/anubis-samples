# TRUTH.md - 51304c02-f33c-59fb-b8d5-67a79001c3e8

## Defect

The source archive at `/app/data/tool.zip` holds a stdlib-only command-line tool that
installs as `task-ctl` and is broken in every one of its four subcommands.
The task is not merely to repair it: the repaired tool must then be *used* to produce the
configuration it exists to generate. Hand-writing `/app/out/config.json` and shipping a
working CLI beside it scores worse than shipping a slightly rough CLI that actually
generated the file, because the report artifact has to carry a real command transcript
and the tests cross-check the transcript against the config. The policy values the
configuration must reach — `max_records`, `retry_limit`, `timeout_ms`, and the canonical
trailer — are drawn from generator seed `0000000000016384` and published only in
`/app/data/defaults.json`, so nothing below can be recalled from training.

1. (focal) `cmd_init` reads `args.config`, but the `init` subparser declares only
   `--schema` and `--out`. Every `init` invocation therefore dies with `AttributeError`
   before it reads a byte. The fix is `args.schema`, and it is the first thing that has to
   land because nothing downstream can run until `init` produces a file.
2. (focal) `cmd_init` emits a nested document. It writes
   `"settings": {"checksum": ...}`, and the contract in `/app/data/config_schema.json`
   sets `scalar_only` and lists the nine required keys at the top level. A nested value is
   not merely stylistically wrong — the tool's own `validate` subcommand rejects it, and
   `test_config_is_flat_json_object` reads the delivered artifact directly. `checksum`
   belongs at the top level, and `mode` is missing from the initial document entirely.
3. (focal) `cmd_set` corrupts both halves of every assignment. It writes
   `data[args.key.upper()] = args.value`, so `--key max_records --value 100` produces the
   key `MAX_RECORDS` holding the *string* `"100"`. The contract wants the key verbatim and
   the value coerced to its natural scalar type — decimal strings to `int`, `true` and
   `false` to `bool`, everything else left as text. A configuration whose numbers are
   strings passes a naive eyeball check and fails
   `test_config_matches_hidden_seed_policy` on every numeric key at once.
4. (focal) `cmd_report` produces a transcript that carries no information:
   `{"ok": True, "config": data, "commands": "unknown"}`. `commands` has to be a list of
   the CLI invocations that actually ran, each with its `argv`, its `returncode`, and its
   captured `stdout`, and the report additionally needs the tool identity, the config
   path, the sha256 digest of the config bytes as delivered, and the trailer.
   `test_report_records_real_cli_commands` and `test_replay`-style cross-checks read this
   structure; a string where a list belongs fails them all.
5. (drift) The two schema files disagree about the shape of `required_keys` and the
   disagreement is deliberate. The tool's own `schema.json` inside the archive stores it
   as a single comma-separated string, which is what `cmd_validate` splits on; the sidecar
   `/app/data/config_schema.json` stores it as a JSON array, along with the `scalar_only`
   flag. The experienced instinct is to "fix the inconsistency" by rewriting the tool's
   schema into a list and changing `cmd_validate` to iterate it. That produces a tool that
   no longer round-trips against the file it ships with. The sidecar is *context* for what
   a valid configuration looks like; the archive's `schema.json` is the file the CLI
   consumes. `test_config_round_trips_through_cli_validator` runs the repaired validator
   against the repaired tool's own schema.
6. (trap) `sub.add_subparsers(dest="command")` is declared without `required=True`. On
   Python 3.12 a bare invocation therefore reaches `args.func` and raises `AttributeError`
   instead of printing usage and exiting 2. This is invisible during a happy-path session
   because every command the agent types has a subcommand on it, and it is exactly the
   kind of argument-parsing root cause rubric R2 asks to be repaired rather than routed
   around.
7. (trap) `write_json` serialises with a bare `json.dumps(data)` — no `sort_keys`, no
   `indent`. Two runs that set the same keys in a different order produce byte-different
   files. `test_report_is_deterministic_and_truthful` re-reads the artifacts, and
   `test_negative_weight_nested_or_dynamic_config` fires on configurations that are not
   stable flat scalars.

The reference is authored in Python (Python 3.12, standard library only, a
298-line repair-and-drive driver) with a Bash oracle dispatcher
(`solve.sh`) that extracts the archive, rewrites `cli.py`, then drives the repaired CLI
through `init`, one `set` per policy key, `validate`, and `export-report` as real
subprocesses before packaging the tool and writing the sidecar artifacts. A deterministic
channel observes the delivered configuration, transcript, and archive (Channel A,
`tests/test_output.py`, weighted through `tests/test_weights.json`); a judged rubric
channel grades the repair-and-drive trajectory (Channel B, `tests/rubric.json`). Counts
and weights are not mirrored into the prose above — read them from those files, or from
the lists below, which are generated from them.

Out of bounds: hand-writing `config.json` or the profiles instead of generating them by
running the repaired CLI (`test_report_records_real_cli_commands`,
`test_cli_source_was_repaired_not_bypassed`); writing a plausible-looking command
transcript that no subprocess ever produced
(`test_negative_weight_fake_cli_transcript`); leaving nested objects or non-scalar values
anywhere in the delivered configuration
(`test_negative_weight_nested_or_dynamic_config`); embedding the sample profile documents
as literals rather than deriving them from the defaults, samples, and overrides
(`test_negative_weight_hardcoded_sample_answers`); referencing an external host, an API
endpoint, or anything that reads like a credential
(`test_negative_weight_network_or_secret_material`); installing third-party packages; and
looking the tool up online — it is generated for this instance and exists nowhere else.

## Load-bearing work

7 artifacts are collected, and every one of them is graded:

- `/app/out/config.json`
- `/app/out/report.json`
- `/app/out/tool.zip`
- `/app/out/profile_dev.json`
- `/app/out/profile_prod.json`
- `/app/out/validation_report.json`
- `/app/out/summary.json`

- `/app/out/config.json` (new) — a flat JSON object with all nine required keys at the
  top level and no nested values anywhere. It must be produced by running the repaired
  CLI, and it must reach the seeded policy: `max_records` = 145,
  `retry_limit` = 3, `timeout_ms` = 1771, and
  `trailer` = `harbor:cli-config:TASK:0000000000016384`, alongside the profile, producer, container, checksum, and
  mode the schema and defaults supply.
- `/app/out/report.json` (new) — the repair-and-run record. `commands` is a list of the
  invocations that actually ran, each carrying `argv`, `returncode`, and `stdout`; the
  document also names the tool, the config path, the sha256 of the config bytes, and the
  trailer.
- `/app/out/tool.zip` (new) — the repaired tool itself. Built from a sorted file list with
  fixed member timestamps so two builds of the same tree are byte-identical.
- `/app/out/profile_dev.json` and `/app/out/profile_prod.json` (new) — the two environment
  profiles, derived rather than transcribed. Each starts from the seeded policy, then adds
  the sample's `max_records_delta`, the environment's `retry_limit_delta` and
  `timeout_ms_delta` from `/app/data/env_overrides.json`, takes `mode` from the override,
  and suffixes the trailer with the profile name.
- `/app/out/validation_report.json` (new) — the tool's own validation output, covering
  every required artifact and both profiles, including whether the main configuration
  matches the policy and whether every document is scalar-flat.
- `/app/out/summary.json` (new) — the roll-up naming the input files consumed, the
  profiles produced, the artifacts written, and the canonical trailer.

Every rule those artifacts must satisfy lives in `tests/test_output.py` and is mirrored by
the reference `solution/solve.py`. This section names the deliverables so a reader knows
the shape of the work, not the text to type.

Out of bounds by design: producing the configuration by any path other than the repaired
CLI; a transcript whose entries do not correspond to processes that ran; profiles copied
from `/app/data/sample_config_*.json` without applying the deltas; artifacts that are not
world-readable regular files; and any output whose bytes change between two runs.

## Verification

Grader entrypoint: `tests/test.sh`. It does not use pytest. It preflights the bijection
between `tests/test_weights.json` and the `test_*` functions in `tests/test_output.py`,
writes a floor reward of `0.0`, runs each weighted test by direct function call, sums the
weights of the passers into `raw`, and writes
`reward = clamp(raw / positive_total, -1.0, 1.0)` to `/logs/verifier/reward.txt`,
`reward.json`, and a CTRF-shaped `ctrf.json`. Channel B is judged separately against
`tests/rubric.json`. The rule that combines the two channels is set by the runner harness,
not by files in this bundle — do not transcribe a formula here.

Channel A declares 14 positive and 4 negative tests, positive weight total
48 and negative weight total -16, so the
theoretical reward range is -16/48 = -0.333 to +1.000.

Positive tests (14):

- `test_artifacts_exist` (weight 1)
- `test_config_is_flat_json_object` (weight 3)
- `test_config_matches_hidden_seed_policy` (weight 5)
- `test_report_records_real_cli_commands` (weight 5)
- `test_report_is_deterministic_and_truthful` (weight 3)
- `test_outputs_are_world_readable_regular_files` (weight 1)
- `test_no_external_network_endpoints_or_secrets` (weight 3)
- `test_cli_source_was_repaired_not_bypassed` (weight 5)
- `test_config_round_trips_through_cli_validator` (weight 5)
- `test_canonical_trailer_present` (weight 3)
- `test_profile_dev_matches_embedded_reference` (weight 3)
- `test_profile_prod_matches_embedded_reference` (weight 3)
- `test_validation_report_covers_all_artifacts_and_profiles` (weight 5)
- `test_summary_lists_inputs_profiles_and_artifacts` (weight 3)

Negative tests (4) are polarity-inverted: their assertions hold
only when the cheat is present, so an honest run leaves them "failing" and contributes
zero weight. They subtract only from a solver that actually cheats.

- `test_negative_weight_hardcoded_sample_answers` (weight -5)
- `test_negative_weight_nested_or_dynamic_config` (weight -5)
- `test_negative_weight_fake_cli_transcript` (weight -3)
- `test_negative_weight_network_or_secret_material` (weight -3)

Channel B is 10 criteria (8 positive / 2 negative), positive total 30:

- **R1** (+3, trajectory) — The agent inspects the local CLI source, schema file, and failing command behaviour before editing.
- **R2** (+5, trajectory) — The agent repairs root causes in argument parsing, flat-key validation, and deterministic serialization rather than only writing final JSON by hand.
- **R3** (+3, trajectory) — The agent uses only local files and standard-library Python capabilities while configuring the CLI.
- **R4** (+5, trajectory) — The agent runs the repaired CLI to create and validate the delivered configuration artifact.
- **R5** (+3, trajectory) — The agent records a report whose command outcomes are supported by actual terminal evidence.
- **R6** (+5, trajectory) — The agent uses the repaired CLI commands to set each required flat scalar configuration value during the working session.
- **R7** (-5, trajectory) — The agent retrieves a task-specific repaired CLI, prior submission, or hidden policy from an external source.
- **R8** (-5, trajectory) — The agent claims validation or command success while the trajectory contains no matching local command output.
- **R9** (+3, user_facing_message) — The response summarizes the completed work and names where the produced artifacts were written.
- **R10** (+3, trajectory) — The agent creates the configuration artifact at the exact path the instruction names and refrains from writing auxiliary or renamed copies.

The policy the configuration must reach is seeded from `EVAL_SEED = 4268352045810440771`,
distinct from the visible `SAMPLE_SEED = 6984012359403174948` and the labelled
`CHALLENGE_SEED = 7271637421635044894`. `test_config_round_trips_through_cli_validator`
unpacks the delivered `tool.zip` into a temporary directory and runs the shipped
validator there, so the repair has to survive being moved off the machine it was built on.
The oracle expectation is that running `solution/solve.sh` inside the container drives
every positive Channel A test to pass and holds every negative test in its failing state.

## Root cause

**Why "repair" and "use" are one task, not two.** The task is built to defeat the
shortcut of writing the answer directly. A configuration produced by hand can be made
correct in a minute; a configuration produced by a repaired CLI requires that the CLI
actually work, and the report artifact is the receipt. That is why
`test_report_records_real_cli_commands` and `test_cli_source_was_repaired_not_bypassed`
are both weighted at the top of the scale, and why rubric R4 and R6 credit *running* the
repaired tool to create and validate the artifact rather than describing having done so.

**Flatness is a contract, not a preference.** `/app/data/config_schema.json` declares
`scalar_only`, `/app/data/validation_rules.json` bounds artifact size, and the tool's own
`validate` refuses any value that is a `dict` or a `list`. Three independent files agree,
which is the signal that the constraint is load-bearing. `cmd_init`'s nested `settings`
object and any convenience grouping an agent adds later both violate it.

**Type coercion at the CLI boundary.** Every argument arrives as a string. A configuration
in which `max_records` is `"100"` rather than `100` is a different document, compares
unequal to the policy, and serialises differently. The coercion rule is narrow and
deliberate: decimal strings become integers, the exact tokens `true` and `false` become
booleans, everything else stays text. Widening it — accepting floats, parsing JSON
fragments, stripping quotes — reintroduces nested values through the back door.

**How the profiles are derived.** Neither profile is a file that can be copied. Each is
the seeded policy plus three deltas from two different sidecars: `max_records` takes the
`max_records_delta` from `/app/data/sample_config_<name>.json`, while `retry_limit` and
`timeout_ms` take their deltas from `/app/data/env_overrides.json`, which also supplies
`mode`. The trailer is the canonical trailer with `:<profile>` appended. An agent that
copies the sample files and edits them by eye lands close enough to look right and fails
`test_profile_dev_matches_embedded_reference` and
`test_profile_prod_matches_embedded_reference`.

**Determinism requirements.** Three surfaces move between runs unless they are pinned:
JSON key order, zip member order and timestamps, and any wall-clock or random value that
finds its way into a report. `/app/data/validation_rules.json` names the forbidden keys
explicitly — `timestamp`, `generated_at`, `uuid`, `random_seed` — because those are the
four an agent reaches for by reflex.

**Red lines.** No hand-written configuration; no fabricated transcript; no nested or
non-scalar values; no external hosts or credential-shaped strings; no third-party
packages; no hardcoded profile answers; no non-determinism between runs; no online lookup.

## Solution shape

1. Extract `/app/data/tool.zip` into `/app/tool`, refusing absolute paths, `..` members,
   and symlinks on the way in. Read `README.md` for the four commands the tool must
   support, `cli.py` for the code, and `schema.json` for the file the CLI itself consumes.
2. Read the four sidecars in `/app/data` and decide what each one is for:
   `config_schema.json` fixes the required key set and the scalar-only rule,
   `defaults.json` carries the seeded `base_config` policy and the canonical trailer,
   `env_overrides.json` carries the per-environment deltas and modes, and
   `validation_rules.json` fixes the artifact list, the forbidden keys, and the size bound.
3. Rewrite `cli.py`: make the subparser required; have `init` read `--schema` and emit a
   flat document with `profile`, `producer`, `container`, `checksum`, and `mode`; have
   `set` preserve the key verbatim, coerce the value, assert flatness, and persist; have
   `validate` split the archive schema's comma-separated `required_keys`, assert flatness,
   and print `ok`; have `export-report` accept the accumulated transcript and the config
   digest and write the structured report.
4. Make `write_json` deterministic — `sort_keys=True`, a fixed indent, a trailing newline
   — and make it create parent directories, so the same command sequence always produces
   the same bytes.
5. Drive the repaired CLI as real subprocesses: `init` once, then one `set` per policy key
   drawn from `defaults.json`'s `base_config`, then `validate`, capturing `argv`,
   `returncode`, and `stdout` from each. Abort if any invocation returns non-zero rather
   than continuing and reporting success.
6. Compute the sha256 of the config bytes as they now sit on disk, then call
   `export-report` with the accumulated transcript and that digest so the report is
   written *by the tool*, not beside it.
7. Package the repaired tool into `/app/out/tool.zip` from a sorted file list with fixed
   member timestamps and fixed permission bits.
8. Derive both profiles from defaults, samples, and overrides, then write
   `validation_report.json` covering artifact presence, policy match, and scalar-flatness
   for the main config and both profiles, and `summary.json` naming the inputs consumed,
   the profiles produced, the artifacts written, and the trailer.

## Ordered plan

1. Extract and read before editing. Run each of the four documented commands once and
   record how each one fails; `init` fails first and hardest, and until it is fixed the
   other three have no input to work on. Rubric R1 credits inspecting the CLI source, the
   schema, and the failing command behaviour before making changes.
2. Separate the contract from the code. Write down the nine required keys, the
   scalar-only rule, the seeded policy values, and the two environment override sets
   before touching `cli.py`, so the repair is driven by the contract rather than by
   whichever traceback is loudest.
3. Repair `init` and `set` first and verify by running them: `init` must produce a flat
   five-key document, and a single `set` must add a correctly-typed scalar under the
   verbatim key. Check the file on disk after each command rather than trusting the exit
   code. Rubric R2 credits repairing argument parsing, flat-key validation, and
   deterministic serialization at the root rather than writing the final JSON by hand.
4. Repair `validate` and confirm it round-trips against the archive's own `schema.json`.
   Deliberately feed it a nested document and confirm it refuses; deliberately delete a
   required key and confirm it names the missing key. Rubric R3 credits staying within
   local files and standard-library capability.
5. Repair `export-report` last, once there is a real transcript to hand it. Its input is
   the list of invocations already run plus the digest of the config those invocations
   produced, which forces the driving loop to exist before the report can be written.
6. Drive the full sequence end to end as subprocesses and capture the output of every one.
   This is the step rubric R6 grades — using the repaired CLI commands to set each required
   flat scalar during the working session — and rubric R5 grades the report's outcomes
   being backed by actual terminal evidence.
7. Package the tool, derive the two profiles from the deltas, and write the validation
   report and summary from what the run observed. Unpack `tool.zip` into a scratch
   directory and run `validate` from there to prove the delivered archive is the working
   tool.
8. Run the whole pipeline a second time into a clean output directory and diff every
   artifact byte for byte. Close with a summary naming where each artifact was written —
   rubric R9 credits exactly that closing summary.

## Pitfalls

- **Hand-writing `config.json` because it is faster.** It is faster, and it is the single
  most expensive decision available here. `test_report_records_real_cli_commands`,
  `test_cli_source_was_repaired_not_bypassed`, and rubric R4 all read the evidence that
  the tool produced the file.
- **Fabricating the transcript.** Writing plausible `argv` / `returncode` / `stdout`
  entries for commands that never ran fires
  `test_negative_weight_fake_cli_transcript` and is graded again by rubric R8 as a claim
  with no matching local command output.
- **Leaving `settings` nested, or adding a nested group later.** Every convenience
  grouping violates `scalar_only`. `test_config_is_flat_json_object` and
  `test_negative_weight_nested_or_dynamic_config` both read the delivered document.
- **Uppercasing keys, or leaving values as strings.** The two halves of the `cmd_set` bug
  are independent and each one alone is enough to fail
  `test_config_matches_hidden_seed_policy`. A config whose `timeout_ms` is
  `"1771"` rather than 1771 is wrong.
- **"Harmonising" the two schema files.** The archive's `schema.json` uses a
  comma-separated string on purpose and `cmd_validate` splits it. Rewriting it to match
  the sidecar's array breaks
  `test_config_round_trips_through_cli_validator` when the delivered `tool.zip` is
  unpacked and run elsewhere.
- **Copying the sample profiles.** `/app/data/sample_config_dev.json` and
  `sample_config_prod.json` carry *deltas*, not values. The profiles are the seeded policy
  plus the sample delta plus the environment delta, which is what
  `test_profile_dev_matches_embedded_reference` and
  `test_profile_prod_matches_embedded_reference` check.
- **Applying the deltas from the wrong file.** `max_records_delta` comes from the sample;
  `retry_limit_delta` and `timeout_ms_delta` come from the overrides. Swapping the sources
  produces two profiles that are individually plausible and both wrong.
- **Forgetting the profile trailer suffix.** The profile trailer is the canonical trailer
  with `:dev` or `:prod` appended, not the bare trailer.
  `test_canonical_trailer_present` and the profile comparisons read it.
- **Leaving the subparser optional.** A bare invocation should exit 2 with usage, not
  raise `AttributeError`. It never comes up during a normal session, which is precisely
  why it survives into the delivered tool.
- **Non-deterministic serialization.** `json.dumps` without `sort_keys` reflects insertion
  order, so the same policy set in a different sequence yields different bytes.
  `test_report_is_deterministic_and_truthful` runs the comparison.
- **A non-deterministic `tool.zip`.** `zipfile` stamps each member with the current time
  and `rglob` does not guarantee an order. Fix both, or the archive differs on the second
  build of an identical tree.
- **Adding a timestamp, uuid, or seed to a report.** `/app/data/validation_rules.json`
  names those four keys as forbidden, and they are the reflex additions to any
  "generated" document.
- **Mentioning a host, URL, token, or key anywhere in the artifacts.** The workflow is
  entirely local; `test_no_external_network_endpoints_or_secrets` and
  `test_negative_weight_network_or_secret_material` scan the delivered files, and a
  commented-out example endpoint counts.
- **Continuing after a non-zero return code.** A driving loop that ignores failures
  produces a transcript full of `returncode` values that contradict a report claiming
  success — the exact contradiction
  `test_report_is_deterministic_and_truthful` is built to find.

## Provenance

```yaml
uuid: null  # task.toml carries no canary_guid or uuid field
instance_id: 51304c02-f33c-59fb-b8d5-67a79001c3e8
task_name: terminal-bench/51304c02-f33c-59fb-b8d5-67a79001c3e8
fix_patch_sha256: 5b94fe7beb94fae0bdc95f907b2afb3b3d2f8acb948d68014d413b1ed4ae27e6  # sha256 of solution/solve.sh
solve_py_sha256: 9a20d8f218a310cefc889dc6cc75133604bd529c608adedce2462183ca481962  # informational; solve.py is 298 lines
language: python
golden_score_measured: null  # not measured for this generated instance
golden_score_channel_a_pct: null
golden_score_channel_b_pct: null
golden_score_runs: 0
golden_score_trial_id: null  # no calibration trial has been run yet
golden_score_verified_by: >
  Not yet measured. This instance has not been through an oracle / nop / agent
  calibration sweep. Run the bundle with the oracle agent (expected reward
  1.000) and the nop agent (expected reward 0.000) before recording any
  figure here, and never present a target or an estimate as a measurement.
caveat: >
  Channel A carries 14 positive tests worth 48 weight and 4
  polarity-inverted negative tests worth -16 weight, so the theoretical
  reward range is -16/48 = -0.333 to +1.000. Channel B carries 10 rubric
  criteria worth 30 positive points. The rule that combines the two channels
  belongs to the runner harness, not to this bundle, so no combined formula
  is transcribed here. A partial attempt normally scores far higher on
  Channel B than on Channel A, because the rubric grades derivation and
  trajectory while Channel A grades exact bytes.
```
