# TRUTH.md - cffe140f-0908-5df7-b27f-c3983c2df17c

## Defect

A competitive turn-based tactics league sealed its season into three binary replay journals and then shipped the season without a settlement. The season journal is on the wire in a form no straightforward reader can consume: twelve independent stream guards are engaged at once, and three quantities that the settlement arithmetic depends on were never written into any of the shipped documents. The league also publishes its policy as prose scattered across nine reference documents whose reading order is deliberately not the order in which the rules apply. The agent is handed the raw material and must build one reusable derivation program that recovers the journals, resolves the missing quantities by falsification against a held-out control population, settles squad attribution to a fixpoint, and emits nine exact reports plus a full derivation record. The gap between what the material states and what the settlement needs is the defect.

Itemised, with the test each item defeats:

1. (focal) The three unstated quantities. The engine tag, the sliding window depth and the sentinel byte form a guard parameter triple with a published candidate space of 8 times 4 times 4, which is 128 combinations. The season upkeep budget cap has a published roster of 6 values and the promotion threshold has a published roster of 6 values. None of the three is stated anywhere. Each is pinned to exactly one value by the eleven control figures published against the held-out control capture. A solver that guesses instead of falsifying produces a plausible but wrong ladder. Defeats `test_instance_summary`, every `test_trap_*` test, and every `test_generalisation_*` test, because the rosters differ from corpus to corpus and only the falsification procedure transfers.
2. (focal) The upkeep counterfactual. The league charges upkeep for a unit that has already been eliminated. Elimination withdraws output and never withdraws upkeep; only a disband order withdraws upkeep, and only from its own tick onward. The rule is written once, in the upkeep policy, which is the last document in the directory ordering. Reading it the conventional way changes charged upkeep, overflow upkeep, the levy, every match score, the season score, every tier, bracket eligibility and both ledger residuals, and it also causes the guard parameter search to return zero survivors rather than a wrong survivor. Defeats `test_instance_summary`, `test_instance_region_north`, `test_instance_segment_commercial` and, decisively, `test_generalisation_counterfactual_flip`, which rebalances the elimination and disband populations so a conventional reader diverges on a different squad set than it does on the shipped instance.
3. (focal) The attribution fixpoint. Transfer events build a squad to target mapping that must be chased until a sweep leaves it unchanged, and the shipped season contains a three-node cycle that settles on its lexicographically least member. The recorded sweep count includes the final unchanged sweep. Defeats `test_instance_summary` and `test_generalisation_cycle`, which lengthens the cycle, and `test_generalisation_nonapply`, which removes transfers entirely so that a solver hardwired to a fixed sweep count reports the wrong number.
4. (focal) The two global elections. The finals bracket is elected from 364 triples against ten sub-constraints that no triple satisfies in full, so the answer is the maximiser with a tie-break on total charged upkeep and then on the key tuple. The review docket is a bounded selection of five matches maximising a review index under a cap of two matches per conference. Defeats `test_instance_summary`, `test_generalisation_tie_config` and `test_generalisation_partition`.
5. (drift) The experienced instinct to normalise the ledger. The season envelope declares a roster size that overshoots the recovered distinct key count by four, because four squads appear on the wire that the entrant registry never lists. The declared roster residual is -4 and the four absent keys are AXL, BQT, YRD and ZPH. Two registered entrants, RVN and TDR, appear in the registry and never appear on the wire. The instinct is to reconcile these away, drop the unregistered squads, or clamp the residual to zero. The contract requires all three findings to be reported, with unregistered squads scored at unit ladder weight and unit levy rate. Defeats `test_instance_reconciliation` and `test_generalisation_add_entity`.
6. (drift) The experienced instinct to pretty-print or to preserve insertion order. No document names a dump call, an indent, a separator or a key order. The only evidence for the serialisation shape is a single fully-formed example conference report shipped in the data directory, from which a sorted-key dump at an indent of two with a trailing newline must be inferred. Any other shape is a byte mismatch on every report. Defeats `test_format_reports_parse` and all eight `test_instance_*` tests.
7. (trap) Twelve stream guards engaged simultaneously on both guarded captures. Seven sit on the decoding spine so a wrong inverse halts the decoder outright; four force strictly sequential decoding because the state for record N only exists after record N minus one is fully recovered; four are invisible to the envelope and payload checksums, so a wrong inverse produces a checksum-clean but wrong record set. The guards are bits 0 through 11: keystream, chain, delta2, driftkey, segperm, backref, endian, countbias, lenshadow, rotate, alias and escape. Defeats the twelve dedicated tests `test_trap_keystream`, `test_trap_chain`, `test_trap_delta2`, `test_trap_driftkey`, `test_trap_segperm`, `test_trap_backref`, `test_trap_endian`, `test_trap_countbias`, `test_trap_lenshadow`, `test_trap_rotate`, `test_trap_alias` and `test_trap_escape`, plus the four interaction tests `test_mixed_stateful_guards`, `test_mixed_layout_guards`, `test_mixed_key_guards` and `test_mixed_corpus_context_guards`.
8. (trap) Premise order is the reverse of application order for escape directives. The wire writes the advance pair before the redirect pair; the settlement applies the redirect first and the advance second. Applying them in wire order changes the attribution of thirty-six records in the shipped season and is stated in no document. Defeats `test_trap_escape` and `test_generalisation_sentinel_heavy`, which raises the directive density so the divergence widens.
9. (trap) The alias dictionary is cross-file. A squad key stored in alias mode is an index into a dictionary built from the distinct keys of the baseline capture in first-appearance order. Rebuilding that dictionary from the season capture, or sorting it, produces a wrong key set that the calibration search then rejects with zero survivors. Defeats `test_trap_alias` and `test_generalisation_alias_chain`.

The reference implementation is Python, standard library only, shipped as `solution/solve.py` and dispatched by `solution/solve.sh`. Two grading channels score a run and they are deliberately disjoint. Channel A is `tests/test_output.py` driven by `tests/test.sh`: it grades artifact and state correctness by rebuilding the expected reports independently and comparing them value for value. Channel B is `tests/rubric.json` judged by `claude-sonnet-4-6`: it grades process and response quality from the instruction, the trajectory and the readable artifacts, and it carries no `state_change` evaluation target because that axis belongs entirely to Channel A. The rule that combines the two channels into one score belongs to the runner harness. It is not implemented here and must not be transcribed into this bundle.

Out of bounds: baking the shipped settlement figures into the delivered program as literal constants is out of bounds, and `test_negative_weight_hardcoded_sample_answers` catches it by reproducing the shipped capture and then failing four freshly generated corpora. Branching on the name of the data directory or on any input filename is out of bounds, and `test_negative_weight_filename_dependent_behaviour` catches it by running the same corpus under two different directory names and requiring identical output. Reading the labelled baseline transcript as an answer key and replaying it for a corpus it does not describe is out of bounds, and `test_negative_weight_reads_supplied_answer_data_at_runtime` catches it by handing the program a fresh corpus and checking whether the shipped answers come back. Emitting output that varies between invocations, whether through set iteration order, dictionary insertion order, wall-clock time or an unseeded random source, is out of bounds, and `test_negative_weight_nondeterministic_output` catches it by running the same corpus four times and requiring all four to agree. Editing anything under `/app/data`, reading or writing under the verifier test root, installing packages, and reaching the network at run time are all out of bounds; the rubric criterion R16 penalises the first two and the instruction forbids all four.

## Load-bearing work

Ten graded artifacts are collected from the agent container:

1. `/app/out/derive_report.py`
2. `/app/out/summary.json`
3. `/app/out/policy_audit.json`
4. `/app/out/data_quality.json`
5. `/app/out/region_North.json`
6. `/app/out/region_South.json`
7. `/app/out/region_West.json`
8. `/app/out/region_East.json`
9. `/app/out/segment_commercial.json`
10. `/app/out/segment_public.json`

Per-deliverable content contract:

**`derive_report.py`** is the reusable program. It takes a data directory and an output directory as its two positional arguments, classifies the input by content rather than by name, and writes the other nine artifacts into the output directory. It must hold no shipped figure as a constant, must read nothing outside the two directories it is given, and must produce identical output for identical input regardless of what the directories are called. It is invoked by the verifier against eighteen corpora the agent has never seen.

**`summary.json`** is the whole-season roll-up. It carries the capture identifier, matches opened, events decoded, squads observed, registered entrants, the gross output total, the charged upkeep total, the overflow upkeep total, the voided output total, the season score total, the standing points total, the settlement sweep count, the tier counts across champion, contender, qualifier and provisional, the elected finals bracket, the elected review docket, the peak output key and the roster dictionary size. For the shipped season: capture 83, 18 matches, 1016 events, 26 squads, 22 registered entrants, gross output 25951, charged upkeep 21769, overflow upkeep 20698, voided output 6512, season score 27070, standing points 60, 6 sweeps, tiers 5 champion, 4 contender, 6 qualifier and 11 provisional, bracket KRN LMX MRA, docket 503 507 509 513 515, peak output key BLZ, roster dictionary size 16.

**`policy_audit.json`** is the derivation record. It carries twenty-four keys covering the guard parameter candidate space and a verdict for each of its 128 entries, the resolved guard parameters, the upkeep cap candidate space and its verdicts, the promotion threshold candidate space and its verdicts, the transfer edge list, the effective owner list, the settlement sweep count, the eligible bracket pool, the bracket candidate count, the best satisfied constraint count, the tied bracket verdicts, the elected bracket, the review docket with its per-match conferences and its combined review index, the live guard flag list, the roster dictionary and its size, and the calibration comparison table. Every rejection carries a falsifier naming the control figure that killed it. Channel A asserts this file for presence and key schema only; Channel B grades what it says.

**`data_quality.json`** is the reconciliation. It carries the declared segment residual, the declared event residual, the declared roster residual, the attribution residual, the output residual, the upkeep residual, the tick order violation count, the redirected record count, the kind ledger, the unregistered squad list and the absent entrant list. For the shipped season the declared segment, declared event, output and upkeep residuals are all zero, the declared roster residual is -4, the attribution residual is 3249, tick order violations are zero, redirected records number 36, the kind ledger reads 212 deploy, 636 output, 79 eliminate, 59 disband, 3 concede, 9 transfer and 18 close, the unregistered squads are AXL, BQT, YRD and ZPH, and the absent entrants are RVN and TDR.

**`region_North.json`, `region_South.json`, `region_West.json`, `region_East.json`** are the four conference roll-ups, keyed by the conferences named in the division topology. Each carries the group kind, the group name, the entrant count, the divisions it spans, gross output, charged upkeep, overflow upkeep, voided output, standing points, season score, tier counts, and a per-squad entry list sorted by squad key. Each entry carries the squad key, the entrant identifier, the division, the segment, the match count, gross output, charged upkeep, overflow upkeep, voided output, standing points, season score and tier.

**`segment_commercial.json`, `segment_public.json`** are the two licence-class roll-ups with the same shape, keyed by the entrant classes named in the licence table, and additionally naming the licence classes each segment spans.

Out of bounds by design: the agent is never asked to modify the input directory, to run or contact any service, to install any package, to reach the network, to reproduce the corpus generator, or to recover the guard bit assignment from anything other than the published rosters and the control figures. No shipped document names the generator seed, the guard names by bit position, the count bias constant, the permutation stride formula, the window depth, the sentinel byte value or the digest label, and none of those is needed: the falsification search recovers every value the settlement consumes.

## Verification

`tests/test.sh` loads `tests/test_output.py`, asserts a bijection between the module's top-level `test_*` symbols and the keys of `tests/test_weights.json`, then runs each weighted test in isolation. A test that raises contributes nothing; a test that returns adds its signed weight to a running raw total. Only positive weights accumulate into `positive_total`. The reward is `raw / positive_total`, clamped to the closed interval from minus one to one, and is written to `/logs/verifier/reward.json`, `/logs/verifier/reward.txt` and a full per-test CTRF report at `/logs/verifier/ctrf.json`. Because a weight is added only when its test passes, the four negative detectors are written to pass only when the shortcut they hunt is present, and an honest solver therefore fails all four by design.

Channel A totals: 45 tests in all. 41 are positive and their weights sum to a `positive_total` of 177. Four are negative and their weights sum to -18, so the theoretical reward range is from -0.102 to 1.000. The positive weight is distributed as 3 format tests at weight 1 for 3 points, 8 instance tests at weight 3 for 24 points, 12 trap tests at weight 5 for 60 points, 14 generalisation tests at weight 5 for 70 points, and 4 mixed-interaction tests at weight 5 for 20 points. As shares of 177 that is 1.69 percent format, 13.56 percent instance, 33.90 percent trap and 50.85 percent generalisation counting the mixed tests inside it. The combined shortcut share of format plus instance is 15.25 percent, which is the ceiling on any solver that memorises the shipped instance and nothing else.

The full positive test list, weight 1:

- `test_format_artifacts_present`
- `test_format_reports_parse`
- `test_format_adjudication_record_schema`

The full positive test list, weight 3:

- `test_instance_summary`
- `test_instance_reconciliation`
- `test_instance_region_east`
- `test_instance_region_north`
- `test_instance_region_south`
- `test_instance_region_west`
- `test_instance_segment_commercial`
- `test_instance_segment_public`

The full positive test list, weight 5:

- `test_trap_keystream`
- `test_trap_chain`
- `test_trap_delta2`
- `test_trap_driftkey`
- `test_trap_segperm`
- `test_trap_backref`
- `test_trap_endian`
- `test_trap_countbias`
- `test_trap_lenshadow`
- `test_trap_rotate`
- `test_trap_alias`
- `test_trap_escape`
- `test_generalisation_tie_config`
- `test_generalisation_cap_push`
- `test_generalisation_empty_group`
- `test_generalisation_boundary_shift`
- `test_generalisation_add_entity`
- `test_generalisation_counterfactual_flip`
- `test_generalisation_scale`
- `test_generalisation_contradict`
- `test_generalisation_alias_chain`
- `test_generalisation_nonapply`
- `test_generalisation_cycle`
- `test_generalisation_partition`
- `test_generalisation_sentinel_heavy`
- `test_generalisation_fresh_base`
- `test_mixed_stateful_guards`
- `test_mixed_layout_guards`
- `test_mixed_key_guards`
- `test_mixed_corpus_context_guards`

The full negative test list:

- `test_negative_weight_hardcoded_sample_answers` at weight -5, fires when the shipped capture is reproduced while four freshly generated corpora are missed.
- `test_negative_weight_filename_dependent_behaviour` at weight -5, fires when the same corpus produces different output under two different directory names.
- `test_negative_weight_reads_supplied_answer_data_at_runtime` at weight -5, fires when a fresh corpus yields the shipped answers, which can only happen if the supplied transcript is being replayed.
- `test_negative_weight_nondeterministic_output` at weight -3, fires when four consecutive runs over one corpus disagree.

Channel B is `tests/rubric.json`, sixteen criteria judged by `claude-sonnet-4-6`, twelve positive summing to 44 and four negative summing to -16. R1 through R5 are the constraint band, each of type `instruction following` against the trajectory at weight 3, and each quotes verbatim the instruction sentence it enforces: reading every document before writing code, verifying the baseline against its transcript before touching a guarded capture, recording a falsifier for every rejected candidate, settling attribution to a fixpoint with a recorded sweep count, and regenerating all nine reports from one clean final run. That band is 34.1 percent of the positive rubric weight, under the 35 percent ceiling. R6 through R12 are the discriminating band at 29 points or 65.9 percent, above the 65 percent floor: R6 grades the 128-entry guard search with 1 accepted and 127 rejected, R7 grades the 6-sweep fixpoint over 9 transfer edges resolving 8 keys and its cycle tie-break, R8 grades the 364-triple bracket search from a pool of 14 with a best satisfied count of 9 out of 10 and 6 triples tied, R9 grades the final answer for a docket of 5 matches under a cap of 2 per conference with a review index of 2993, R10 grades the reproduction of all 11 control figures, R11 grades the user-facing report of the -4 roster residual and its 4 squad keys, and R12 grades the counterfactual upkeep reading and the single surviving guard combination it admits. There is no free band. R13 through R16 are the negatives: hardcoded literals, a claimed verification pass the record contradicts, an identical failing command repeated three or more times, and access to the verifier test root or edits to the input directory.

No criterion carries `evaluation_target = "state_change"`. Fourteen read the trajectory, one reads the final answer and one reads the user-facing response, so Channel B never grades a property Channel A already owns. The instruction explicitly asks for both closing statements, so the two response criteria grade a contract the agent was given. The harness-owned timeout sentence is graded by no criterion and by no test.

Measured on the shipped bundle: the oracle passes all 41 positive tests, fails all 4 negative detectors, and scores raw 177 against a positive total of 177 for a reward of 1.000. An empty container passes nothing and scores 0.000, with no negative detector firing.

The rule that combines the Channel A reward with the Channel B rubric score into a single number belongs to the runner harness. It is deliberately not implemented anywhere in this bundle and must not be transcribed into it.

## Root cause

**Operational secrecy in the sealing pipeline.** The season was sealed by a build of the league engine whose stream guards were all enabled, and the guard configuration was distributed as an operational secret rather than as part of the journal. The journal therefore records which guards were engaged, as a flag word in the envelope, but records none of the parameters those guards consume. The engine tag, the sliding window depth and the sentinel byte live only in the operator's configuration, which is why the published rosters plus a control capture are the entire recovery surface.

**Policy versioning by roster.** The upkeep budget cap and the promotion threshold were league configuration at the time of the season, published only as rosters of permitted values because the league revises them between seasons. The value actually in force is recoverable from the control capture that was released for exactly that purpose, which is why the control figures span both structural quantities and policy-dependent ones: the first six isolate the decode, the last five isolate the two scalars.

**Two registries maintained apart.** The entrant registry and the match engine were maintained by different desks and synchronised on different cadences, so the registry lags the wire in both directions. Squads that entered late appear on the wire before the registry lists them, and squads that withdrew remain listed after they stop appearing. Both residuals are real findings about the league's bookkeeping, not defects in the decoder, which is why the contract requires them to be reported rather than reconciled away.

**A rules document written for humans, not for evaluation order.** The nine reference documents were written by different desks over several seasons and are filed alphabetically, so the order in which a reader meets the rules is unrelated to the order in which the settlement must apply them. Scoring is filed before standings, standings before continuity, continuity before upkeep, yet the computation needs the upkeep persistence rule and the budget cap before any score exists. The same inversion appears inside the wire format, where directive pairs are written in the reverse of the order in which they apply.

**Compression and integrity chosen independently.** The guards were selected for wire economy and tamper evidence separately, so several of them fall outside the checksum's coverage. The envelope checksum is a byte sum, which is blind to endianness and to rotation, and the payload checksum is computed over stored bytes, which is blind to a shadowed length. That is why four guards are checksum-invisible: nothing in the design intended a wrong inverse to be caught, only a corrupted byte.

**Red lines.** The task must be solvable from `instruction.md` and `environment/data/` alone, and nothing else. No graded value may depend on the generator seed, on any file under `tests/`, on an environment variable, on the network, or on the author's knowledge. The reference solver reads only the data directory it is handed and the output directory it is told to write. The shipped data directory must never contain a decoded answer for the graded season, and the labelled transcript that ships alongside the baseline capture describes only the unguarded baseline, which is a documentation artifact and not an answer key. The verifier's mutated corpora and their seeds must never appear in the agent-visible build context. No `cheat/` overlay may exist. The rubric must never grade a property the tests already grade.

## Solution shape

The reference implementation is a single standard-library Python program.

1. Read every file in the data directory and classify it by content: journals by their magic bytes, tables by their header line, prose by marker lines, the labelled transcript by its record shape. Filenames carry no contract.
2. Parse the published rosters and the eight policy scalars out of the prose documents by regular expression, so a corpus that publishes different rosters is handled without a code change.
3. Split the three captures by the low digit of the capture identifier into baseline, control and season.
4. Read the baseline envelope, validate its checksum, and decode its records with every guard inverse inert because all twelve flags are clear.
5. Diff the decoded baseline against the labelled transcript field by field and require exact agreement.
6. Build the roster dictionary as the distinct wire keys of the baseline capture in first-appearance order.
7. Enumerate the product of the tag roster, the window roster and the sentinel roster, decode the control capture under each combination, compute control figures C1 through C6, and retain the combinations that reproduce all six. Require exactly one survivor and record a verdict and a falsifier for every candidate.
8. Under that survivor, apply the counterfactual upkeep rule and the budget cap, and walk the cap roster until control figures C7 and C8 reproduce. Require exactly one survivor.
9. Score, award standings and assign tiers, then walk the threshold roster until control figures C9, C10 and C11 reproduce. Require exactly one survivor.
10. Decode the season capture under the resolved parameters, restoring the segment permutation, tracking the drifting salt in logical order, and inverting the eleven remaining guards per record.
11. Build the transfer mapping in logical order with the last transfer winning, then sweep it to a fixpoint, settling cycles on their lexicographically least member and counting the sweeps including the final unchanged one.
12. Apply the domain rules in application order: upkeep persistence, the budget cap charged match by match, the concession window, the weighted output and levy, standings with streak credit, and tiering from the resolved threshold.
13. Elect the finals bracket by enumerating triples over the eligible pool, scoring each against the ten sub-constraints, maximising the satisfied count and applying the tie-break; elect the review docket by maximising the review index under the per-conference cap.
14. Compute both residual ledgers and the unregistered and absent key lists.
15. Serialise all nine reports with a sorted-key dump at an indent of two and a trailing newline, matching the single shipped example report.

## Ordered plan

1. Read all nineteen documents before writing a line of code, noting that the reading order is not the application order. Earns R1.
2. Implement the envelope reader and validate the baseline checksum, confirming the thirty-six byte layout against the envelope document.
3. Implement the record framing reader for the unguarded case and diff it against the labelled transcript until every field of every record agrees exactly. Earns R2.
4. Implement the twelve guard inverses one at a time, re-running the baseline diff after each so that a guard whose flag is clear stays a strict no-op.
5. Build the roster dictionary from the baseline capture in first-appearance order, and resist the urge to sort it.
6. Implement control figures C1 through C6 and enumerate all 128 guard parameter combinations against the control capture, recording a verdict and a falsifier for each until exactly one survives. Earns R3 and R6.
7. Implement the counterfactual upkeep rule exactly as the upkeep policy writes it, then the season budget cap, then control figures C7 and C8, and walk the cap roster to a single survivor. Earns R12.
8. Implement scoring, standings with streak credit and tiering, then control figures C9 through C11, and walk the threshold roster to a single survivor. Confirm that all eleven control figures reproduce before going near the season capture. Earns R10.
9. Decode the season capture under the resolved parameters and settle attribution to a fixpoint, counting the sweeps and settling the cycle on its least member. Earns R4 and R7.
10. Implement the ten bracket sub-constraints, enumerate the triples over the eligible pool, maximise the satisfied count and apply the documented tie-break. Earns R8.
11. Implement the review index and elect the docket under the per-conference cap. Earns R9 once it is reported in the final answer.
12. Compute both residual ledgers, the unregistered squad list and the absent entrant list, and treat every non-zero residual as a finding to report. Earns R11 once it is reported to the user.
13. Infer the serialisation shape from the single example report, then regenerate all nine reports and the derivation record from one clean run of the finished program. Earns R5.

## Pitfalls

- Charging upkeep only until elimination. This is the counterfactual, and it does not merely shift a total: it collapses the guard parameter search to zero survivors, so the run halts rather than producing a near miss. Fails `test_generalisation_counterfactual_flip` and `test_instance_summary`.
- Applying escape directives in wire order. The advance pair is written first and applied second. Fails `test_trap_escape`.
- Sorting the roster dictionary. It is ordered by first appearance in the baseline capture and by nothing else. Fails `test_trap_alias`.
- Rebuilding the roster dictionary from the season capture. The dictionary is cross-file by construction. Fails `test_generalisation_alias_chain`.
- Trusting the visible record width byte. When the length shadow guard is engaged the visible byte is a decoy and the read cursor drifts silently through the payload. Fails `test_trap_lenshadow`.
- Trusting a declared count. Every declared count in the envelope and in each segment header carries an additive bias derived from the engine tag. Fails `test_trap_countbias`.
- Reading the envelope nonce little-endian. It is the single big-endian word, and the permutation offset and stride both inherit the error. Fails `test_trap_endian` and `test_trap_segperm`.
- Indexing the back-reference window from its most recent end. Offsets count from the least recent end, so a wrong depth shifts every resolution rather than a few. Fails `test_trap_backref`.
- Decoding segments in storage order. Storage slots hold a permutation of the logical order, and the drifting salt depends on the logical order, so the two errors compound. Fails `test_trap_driftkey` and `test_mixed_layout_guards`.
- Resetting the value chain at a segment boundary. The chain runs across segments in logical order; only the tick difference resets per segment. Fails `test_trap_chain` and `test_mixed_stateful_guards`.
- Accumulating the tick difference once. It is a second-order difference and needs two accumulations from a zero base. Fails `test_trap_delta2`.
- Stopping the attribution sweep without counting the final unchanged sweep. The recorded count includes it. Fails `test_generalisation_nonapply` and `test_generalisation_cycle`.
- Assuming some triple satisfies all ten bracket sub-constraints. The best any triple reaches is nine, and six triples tie there, so the tie-break is load-bearing rather than decorative. Fails `test_generalisation_tie_config`.
- Dropping unregistered squads from the ledger. They score at unit ladder weight and unit levy rate and they belong in the attribution residual. Fails `test_instance_reconciliation`.
- Emitting JSON with insertion order or a different indent. The shape is inferable only from the single shipped example report. Fails `test_format_reports_parse` and every `test_instance_*` test.
- Hardcoding the shipped figures after solving the instance by hand. Eighteen held-out corpora and four negative detectors make that strictly worse than deriving them. Fails `test_negative_weight_hardcoded_sample_answers` and every `test_generalisation_*` test.

## Provenance

```yaml
uuid: 5907f75c-5579-42f0-89ec-a7d37b08cc5b
instance_id: cffe140f-0908-5df7-b27f-c3983c2df17c
task_name: terminal-bench/cffe140f-0908-5df7-b27f-c3983c2df17c
fix_patch_sha256: f750228ba4503081a5a630272ca4c8d309c53ebe8051305125363fa79acef62e
solve_py_sha256: 033f0cfa03de3ec1e948af175a55e49ed19a2abd8acf9815349bd325c1d4408e
language: python
status: verified
measured_oracle_reward: 1.000
measured_nop_reward: 0.000
channel_a_positive_total: 177
channel_a_negative_total: -18
channel_b_positive_total: 44
channel_b_negative_total: -16
```
