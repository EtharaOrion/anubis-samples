# TRUTH.md - fb2f26ec-6595-5389-8b6d-36e52ebf086b

## Defect

Recover the seven lost scoring decisions of a batch inference scorer from an
archive that publishes only an opaque seal over each scoreboard, then deliver a
scorer that reproduces those seals and holds on batches nobody ever scored. This
hard task in Machine Learning is defined by the agent-visible instruction and the
files under `/app/data`. The missing author-side record is the reasoning contract
that connects those visible materials to the reference solution, the declared
artifacts, the deterministic tests, and the trajectory rubric.

Seven scoring decisions are missing from the shipped notes. The pages that replace
them name the decision each section governed and say nothing about how it was made.
Nothing in the container states any of the seven, and nobody recorded how many were
lost. They are recoverable one way only. Seven archived batches ship with a seal
over the scoreboard and the rows the gateway produced for them, and never the
scoreboard itself. The agent has to build a scorer whose seven decisions can be
varied, then search the combinations until the archive leaves one standing. The
seal is what carries the task. Because the archive publishes one opaque value
rather than the scoreboard's fields, a candidate reading either seals or it does
not, with nothing to say which of the seven is wrong. A candidate can only be
tested once every surviving section is already implemented exactly, and a slip
anywhere reports only that nothing seals. Of the 128 possible readings, exactly
one seals to all seven archived batches. One of the seven, keystone, inverts the
usual routes-smaller-than-items ratio and is what forces the routing-side
settlement to be recovered rather than left as a coincidence of the fixture
shape.

1. (focal) The solver must satisfy the contract pinned by `test_archived_batches_reproduce_their_seals` (weight 5). POSITIVE: the recovered scorer seals to every batch the archive sealed. A partial implementation that handles only the easiest visible example does not establish that archived batches reproduce their seals across the verifier's independently generated cases.
2. (focal) The solver must satisfy the contract pinned by `test_delivered_work_boards_match_the_notes` (weight 5). POSITIVE: every delivered scoreboard holds what the recovered settlements ask for. A reading that fits the six seals by coincidence rather than the true settlement fails on the held-out batches.
3. (focal) The solver must satisfy the contract pinned by `test_batch_across_nine_routes_matches` (weight 5). POSITIVE: a batch routing across nine classes scores exactly. The routing, abstain and empty-bucket settlements all bear on this case at once.
4. (drift) An apparently convenient implementation choice can violate `test_delivered_boards_agree_with_the_delivered_scorer` (weight 1). POSITIVE: each delivered scoreboard is what the delivered scorer writes for its batch. The reference behaviour follows the shipped contract rather than normalising, reordering, or simplifying observable behaviour merely because that alternative looks conventional.
5. (trap) The happy path does not prove the edge behaviour exercised by `test_batch_with_an_empty_bucket_matches` (weight 5). POSITIVE: a batch leaving one bucket with no surviving item scores exactly. The empty-bucket settlement only shows itself when a routed class ends with no surviving item, which the visible work batches need not exercise.

The reference implementation is Python in `solution/solve.py`, dispatched by
`solution/solve.sh`. The task declares 7 collected artifacts. Channel A is the
deterministic weighted suite in `tests/test_output.py`; Channel B is the process
and state-change rubric in `tests/rubric.json`. Counts and weights are read from
those files rather than restated here.

Out of bounds: reading anything under `/app/data` while `scorer.py` runs
(`test_negative_weight_reads_training_corpus_at_runtime`); keying behaviour on the
batch filename (`test_negative_weight_filename_dependent_behaviour`); replaying a
hardcoded scoreboard (`test_negative_weight_hardcoded_sample_answers`); emitting
different scoreboards across runs (`test_negative_weight_nondeterministic_output`);
rebinding `open` / `json.load` / `json.dump`
(`test_negative_weight_rebinds_verifier_dependencies`); importing an undeclared
sibling; installing third-party packages; and looking the format up online, since
Relay is invented for this task.

## Load-bearing work

Seven artifacts are collected, and every one is graded:

- `/app/out/scorer.py`
- `/app/out/board-gantry.json`
- `/app/out/board-hurdle.json`
- `/app/out/board-inglenook.json`
- `/app/out/board-jetty.json`
- `/app/out/board-kestrel.json`
- `/app/out/board-lintel.json`

- `/app/out/scorer.py`: the recovered scorer, runnable as
  `python3 /app/out/scorer.py <batch.json> <board.json>`, reading the batch from the
  first path, writing its scoreboard and rows as JSON to the second, and exiting 0.
  It carries the seven recovered settlements and seals to every archived batch.
- `/app/out/board-<name>.json` for gantry, hurdle, inglenook, jetty, kestrel and
  lintel: each holds exactly what the delivered scorer writes for the matching
  work batch and nothing else.

Every rule those artifacts must satisfy lives in `tests/test_output.py` and is
mirrored by the reference `solution/solve.py`. This section names the deliverables
so a reader knows the shape of the work, not the text to type.

Out of bounds by design: omitting a declared artifact; writing a semantically
similar result under a different path; modifying `/app/data`; importing an
undeclared sibling deliverable at runtime; depending on the batch filename rather
than its contents; or reaching the network for a task-specific answer.

## Verification

Grader entrypoint: `tests/test.sh`. It preflights the bijection between
`tests/test_weights.json` and the `test_*` functions in `tests/test_output.py`,
runs each weighted test by direct call, sums the weights of the passers into `raw`,
and writes `reward = clamp(raw / positive_total, -1.0, 1.0)` to
`/logs/verifier/reward.txt`, `reward.json`, and a CTRF-shaped `ctrf.json`. Channel B
is judged separately against `tests/rubric.json`. The rule that combines the two
channels belongs to the runner harness, not to this bundle.

Channel A declares 17 positive and 5 negative tests, positive weight total 73 and
negative weight total -21, so the theoretical reward range is -21/73 = -0.288 to
+1.000.

Positive tests (17):

- `test_scorer_and_boards_are_delivered` (weight 1)
- `test_rounding_helper_keeps_whole_numbers` (weight 1)
- `test_delivered_boards_agree_with_the_delivered_scorer` (weight 1)
- `test_archived_batches_reproduce_their_seals` (weight 5)
- `test_delivered_work_boards_match_the_notes` (weight 5)
- `test_batch_over_a_small_route_side_matches` (weight 5)
- `test_batch_with_a_wide_route_side_matches` (weight 5)
- `test_batch_with_blank_gates_matches` (weight 5)
- `test_batch_with_a_high_floor_matches` (weight 5)
- `test_batch_across_nine_routes_matches` (weight 5)
- `test_batch_over_the_longest_relation_matches` (weight 5)
- `test_batch_with_equal_bucket_means_matches` (weight 5)
- `test_batch_with_a_middling_floor_matches` (weight 5)
- `test_batch_with_the_widest_routing_matches` (weight 5)
- `test_batch_over_the_shortest_relation_matches` (weight 5)
- `test_batch_with_an_empty_bucket_matches` (weight 5)
- `test_batch_with_a_tied_top_row_matches` (weight 5)

Negative tests (5) are polarity-inverted: their assertions hold only when the cheat
is present, so an honest run leaves them "failing" and contributes zero weight.

- `test_negative_weight_hardcoded_sample_answers` (weight -5)
- `test_negative_weight_filename_dependent_behaviour` (weight -5)
- `test_negative_weight_reads_training_corpus_at_runtime` (weight -5)
- `test_negative_weight_nondeterministic_output` (weight -3)
- `test_negative_weight_rebinds_verifier_dependencies` (weight -3)

Channel B is `tests/rubric.json`: 14 criteria, 12 positive and 2 negative, of which
five are instruction-following. It grades the recovery trajectory, the delivered
state, and the final report, never a scoreboard byte, so it never overlaps the
deterministic tests. The archive corpus is reseeded on every run from the per-batch
seeds and the three tier seeds in `tests/test_output.py`, none of which reaches the
agent, so grading works under harbor's separate verifier mode where `/app/data` is
not propagated.

## Root cause

**Recovery under an opaque oracle.** The archive publishes a seal, a single FNV-1a
value over the canonical scoreboard-plus-rows, and never the scoreboard. A seal
answers one question, whether a candidate scoreboard is the archived one, and
nothing finer. So the seven lost settlements are pinned only jointly: a reading
either reproduces a seal or it does not, and a single wrong settlement anywhere
breaks the seal with nothing to say which one.

**The seven settlements interact.** Routing side (§5), abstain placement (§4),
empty-bucket emission (§6), whole-number division (§7), blank-mean order and
equal-mean tie-break (§9), and the confidence aggregate (§8) each change the
scoreboard, and several change it only on some batches. The abstain placement is
observable only where an item that survives the floor routes to a class that a
before-or-after ordering would have dropped; the empty-bucket rule shows only when
a routed class ends empty; the tie-break shows only when two buckets share a mean.
The archive is seeded so that its six batches, taken together, exercise every one
of the seven, which is why exactly one of the 128 readings seals to all six.

**Generalisation, not curve-fitting.** The verifier scores held-out batches nobody
archived. A reading chosen to fit the six seals by coincidence rather than the true
settlement scores those held-out batches wrong, so the seals must be reproduced by
the settlement the archive was actually taken under.

**Red lines.** No runtime read of `/app/data`; no filename-conditioned behaviour;
no rebinding of `open` / `json.load` / `json.dump`; no non-determinism across runs;
no hardcoded scoreboards; no third-party packages; no modification of `/app/data`;
no online lookup.

## Solution shape

1. Read `/app/data/scoring-spec.md` and `/app/data/archive-notes.md` end to end,
   then the shipped scorer, and implement every surviving section exactly.
2. Parameterise the seven lost settlements: routing side, abstain placement,
   empty-bucket emission, division, blank-mean order, equal-mean tie-break, and the
   confidence aggregate.
3. Compute the FNV-1a seal over the canonical scoreboard-plus-rows exactly as the
   archive notes describe.
4. Search the 128 readings, scoring each archived batch and comparing its seal to
   the archived value, until one reading seals to all six.
5. Confirm the surviving reading is unique, then freeze it into the scorer.
6. Deliver the scorer at `/app/out/scorer.py` and run it over the six work batches,
   writing each scoreboard to its named `/app/out/board-<name>.json`.
7. Report which settlement each lost section landed on and the archived batch that
   pinned it.

## Ordered plan

1. Read the surviving notes and the archive notes first and list the seven lost
   settlements before writing code. Rubric R1 and R2 credit running the shipped
   scorer against a seal and grounding each settlement in the section or batch
   behind it.
2. Implement every surviving section exactly, then implement the seal. A slip in a
   surviving section makes every candidate reading fail to seal, so this comes
   first.
3. Parameterise the seven settlements and build a small search over the 128
   readings that scores each archived batch and checks its seal. Rubric R3 credits
   varying the settlements together and finding the one combination that seals.
4. Confirm the recovered scorer seals to all six archived batches before writing
   any deliverable. Rubric R4 credits this confirmation step.
5. Freeze the reading into `/app/out/scorer.py`, keeping it standard-library only,
   opening nothing under `/app/data`, deterministic across runs, and indifferent to
   the batch filename. Rubric R8, R9, R10 and R11 credit these instruction
   constraints.
6. Run the scorer over gantry, hurdle, inglenook, jetty, kestrel and lintel and
   write the six scoreboards to their exact paths (rubric R8).
7. Close with a report naming each settlement and the archived batch that pinned it
   (rubric R12).

## Pitfalls

- **Testing a candidate before the surviving sections are exact.** Every reading
  fails to seal while any surviving section is wrong, which reads as "no reading
  works" rather than "one section is off." Implement the surviving sections first
  (fails `test_archived_batches_reproduce_their_seals`).
- **Fitting the six seals without generalising.** A reading tuned to the six sealed
  batches by coincidence fails the held-out batches
  (`test_batch_*` held-out checks).
- **Missing the empty-bucket case.** The visible work batches need not leave a
  routed class empty, so a scorer that omits empty buckets can look right and fail
  `test_batch_with_an_empty_bucket_matches`.
- **Getting the abstain placement backwards.** Before-versus-after routing keeps the
  same items but changes which join identifiers survive, so it changes the seal
  (`test_archived_batches_reproduce_their_seals`).
- **Floor division versus half-to-even.** The two agree on many means and differ on
  the rest, so a wrong choice seals on some batches and not others.
- **Reading `/app/data` at runtime, keying on filename, non-determinism, or
  rebinding stdlib names.** Each fires its dedicated negative test.
- **Modifying the shipped files.** The instruction requires `/app/data` to be left
  as found.

## Provenance

```yaml
uuid: null  # task.toml carries no uuid/guid field
instance_id: fb2f26ec-6595-5389-8b6d-36e52ebf086b
task_name: terminal-bench/fb2f26ec-6595-5389-8b6d-36e52ebf086b
fix_patch_sha256: bd4c51709e32b11d8a3d4f07a1cabae6995a00514dee40297536d83def6e8542  # sha256 of solution/solve.sh
solve_py_sha256: f8c79168ba8d40175f7e2234f8dae9b0b69d536a4c8440c35573cd0b4c0e9658  # informational; solve.py is 131 lines
language: python
status: authored_reference
golden_score_measured: null  # not measured under Docker harbor for this instance yet
golden_score_channel_a_pct: null
golden_score_channel_b_pct: null
golden_score_runs: 0
golden_score_trial_id: null
golden_score_verified_by: >
  Local checks confirm the reference scorer seals to all six archived batches
  and matches the independent reference scoring on every archived, work and
  held-out batch, that the shipped broken scorer seals to none of the six, and
  that exactly one of the 128 possible settlement readings seals to the whole
  archive. These are local measurements, not Docker harbor runs; run the bundle
  with the oracle agent (expected reward 1.000) and the nop agent (expected
  reward 0.000) before recording any figure here.
caveat: >
  Channel A carries 17 positive tests worth 73 weight and 5 polarity-inverted
  negative tests worth -21 weight, so the theoretical reward range is
  -21/73 = -0.288 to +1.000. Channel B carries 14 rubric criteria, five of them
  instruction-following. The rule that combines the two channels belongs to the
  runner harness, not to this bundle, so no combined formula is transcribed here.
```
