# TRUTH.md - 45421e59-0daa-505b-9656-e565f93c491c

## Defect

Nothing in this bundle is broken. What is withheld is the derivation. The agent
receives one incident window of a service fleet as three wire exports, five
reference tables and six rule documents, and must reconstruct a per-frame fault
ledger plus twelve roll-ups from them. Every rule the graded corpora exercise
is written out in `environment/data/`; what no shipped file states is the order
the rules bite in, which rule wins where two could apply, or the values of the
nine wire parameters the crash reporter holds back. Those are the work.

1. (focal) The crash reporter holds back nine wire parameters. Their candidate
   domains are published in `calibration-controls.md`; their values are not.
   Eighty one candidate values sit across the nine domains, and exactly one
   assignment reproduces the eleven control figures the fleet published for the
   held-out control slice. A solver that guesses instead of searching fails
   every generalisation test, beginning with
   `test_generalises_to_a_fresh_crash_corpus`.
2. (focal) The parameters are not independently pinnable in an arbitrary order.
   The declared-count multiplier has to fall before frames split from trailers,
   the rotation offset before resident spans can be accumulated in logical
   order, resident spans before the running drift can be removed from the
   unwind column, the entry clock modulus before the chained entry stamps
   unwind, and the coarse unit before an allocated byte total means anything.
   The staging order is nowhere stated; it follows only from the data
   dependencies between the rules. A solver that searches in the wrong order
   finds no unique survivor and fails
   `test_shipped_reconciliation_record_matches_the_reference`.
3. (focal) The quarantine settlement is a fixpoint, not a pass. A component's
   containment envelope is set by the lowest tier budget still in service
   across that component's modules, so demoting one module tightens the
   allowance of its siblings, which can demote them in turn. The shipped corpus
   settles after six sweeps. `attribution-and-ownership.md` states that a
   unique stable assignment exists and never says that iteration is needed. A
   single forward pass scores 0.036 and fails
   `test_shipped_incident_and_module_rollups_match_the_reference` along with
   every generalisation test.
4. (focal) Twelve wire traps sit on the incident corpus, each with a flag bit,
   a forward transform and a one-sentence inverse. Four of them force strictly
   sequential decoding: the running unwind drift and the signature ring advance
   over the export as a whole, so no dump decodes in isolation. Each has a
   dedicated weight-5 test that fails if and only if its inverse is missing.
5. (drift) The experienced instinct is that a support window is half open, so
   that a frame landing exactly on the opening tick is inside it. This fleet
   runs the opposite convention: `clock-and-sequencing.md` states that both
   endpoints stand outside the window. Three frames of the shipped corpus sit
   exactly on a boundary. The conventional reading moves seven of the thirteen
   graded artifacts and accepts 153 frames against 151, failing
   `test_shipped_ledger_matches_the_reference`.
6. (drift) The second experienced instinct is that the labelled sample tier
   teaches the format. It teaches only the plain column layout: every flag word
   in the sample tier is clear. A solver that generalises from the sample
   implements no inverse at all and scores 0.080.
7. (trap) The rotated fault signature and the rotated frame lines both survive
   the shipped check totals: a decoder that verifies the totals and stops there
   still returns wrong signatures and wrong ordering.
   `test_trap_rotated_fault_signature_inverse` and
   `test_trap_rotated_frame_lines_inverse` catch each.
8. (trap) The biased declared count drifts the frame cursor rather than any
   value. Ignoring it makes trailer lines look like fault frames, which is
   invisible until a dump check total is recomputed.
   `test_trap_biased_declared_count_inverse` catches it.

The reference implementation is Python 3.12, standard library only, at
`solution/solve.py`. Grading runs on two channels: Channel A is the weighted
pytest suite in `tests/test_output.py`, Channel B is the LLM-judged rubric in
`tests/rubric.json`. The rule that combines the two channels belongs to the
runner harness and is deliberately absent from this bundle.

Out of bounds: baking the shipped corpus totals or a corpus seed into the
delivered program, caught by `test_negative_weight_hardcoded_sample_answers`;
branching on the input directory name, caught by
`test_negative_weight_filename_dependent_behaviour`; reaching for the labelled
sample answer files at run time, caught by
`test_negative_weight_reads_supplied_answer_data_at_runtime`; and emitting a
triage set that changes between identical runs, caught by
`test_negative_weight_nondeterministic_output`.

## Load-bearing work

Fourteen artifacts are graded, all under `/app/out`:

- `transform.py`, the reusable triage program
- `normalized.jsonl`, the fault ledger
- `summary.json`, the incident roll-up
- `product_summary.json`, the per-module roll-up
- `region_central.json`, `region_east.json`, `region_north.json`,
  `region_south.json`, `region_west.json`, the five deployment zone reports
- `segment_consumer.json`, `segment_corporate.json`, `segment_enterprise.json`,
  `segment_smb.json`, the four tenant segment reports
- `data_quality.json`, the reconciliation and derivation record

Per-deliverable contract:

- `transform.py` takes an input directory and an output directory as its two
  arguments and falls back to `/app/data` and `/app/out` when given none. It
  uses the standard library alone, writes nothing outside its output directory,
  and derives every figure from the directory it was handed.
- `normalized.jsonl` carries one JSON object per accepted fault frame, sorted
  by capture tick, each object written as one compact line with its keys in
  sorted order.
- `summary.json` and `product_summary.json` carry the incident totals and the
  per-module settlement outcome, including each module's declared tier, its
  effective tier, its allowance and whether it is quarantined.
- The nine partition reports carry the accepted count, the byte totals, the
  resident span total, the within-budget count and the module list of their
  partition. A partition holding no accepted frame still ships a report, with
  its counts at zero and its collections empty.
- `data_quality.json` carries the acceptance and rejection tallies, the byte
  conservation, and the derivation record: the enumerated candidate space, the
  verdict on every candidate, the control figure that falsified each rejection,
  the settlement pass count, and the comparison against every published control
  figure.

Out of bounds by design: the agent never sees `EVAL_SEED`, the held-out mutated
corpora, this file, or anything under `tests/`. Every graded answer follows from
`instruction.md` plus `environment/data/` alone.

## Verification

`tests/test.sh` runs each weighted test function, sums the weights of the ones
that pass, and divides by the total positive weight, exactly as the standardized
reward formula requires. It asserts the bijection between `test_weights.json`
and the `test_*` symbols of `test_output.py` before running anything.

Channel A carries 35 tests: 31 positive summing to **137** and four negative
summing to -18. The reward floor is -18/137 and the reward range is
approximately [-0.131, 1.000].

Positive tests, by tier:

- FORMAT, weight 2 total: `test_triage_set_is_complete` (1),
  `test_triage_program_carries_its_promised_fallback_locations` (1).
- INSTANCE, weight 15 total: `test_shipped_ledger_matches_the_reference` (3),
  `test_shipped_incident_and_module_rollups_match_the_reference` (3),
  `test_shipped_zone_reports_match_the_reference` (3),
  `test_shipped_tenant_segment_reports_match_the_reference` (3),
  `test_shipped_reconciliation_record_matches_the_reference` (3).
- TRAP, weight 60 total, one weight-5 test per trap card:
  `test_trap_second_order_cost_inverse`, `test_trap_chained_entry_inverse`,
  `test_trap_running_unwind_drift_inverse`,
  `test_trap_rotated_frame_lines_inverse`,
  `test_trap_biased_declared_count_inverse`,
  `test_trap_alternate_radix_base_inverse`,
  `test_trap_registry_alias_column_inverse`,
  `test_trap_split_dump_base_inverse`, `test_trap_shadow_frame_index_inverse`,
  `test_trap_rotated_fault_signature_inverse`,
  `test_trap_coarse_allocation_escape_inverse`,
  `test_trap_ring_back_reference_inverse`.
- GENERALISATION, weight 60 total, one weight-5 test per held-out mutated
  corpus: `test_generalises_to_a_fresh_crash_corpus`,
  `test_generalises_when_budget_ties_move`,
  `test_generalises_when_peaks_sit_on_the_budget`,
  `test_generalises_when_rule_presentation_order_inverts`,
  `test_generalises_when_a_deployment_zone_empties`,
  `test_generalises_when_the_registry_carries_a_cycle`,
  `test_generalises_when_the_support_boundary_shifts`,
  `test_generalises_when_a_new_module_enters`,
  `test_generalises_when_a_dormant_rule_fires`,
  `test_generalises_when_the_partition_count_changes`,
  `test_generalises_when_the_corpus_scales`,
  `test_generalises_when_the_corpus_contradicts_the_control_slice`.

Negative tests: `test_negative_weight_hardcoded_sample_answers` (-5),
`test_negative_weight_filename_dependent_behaviour` (-5),
`test_negative_weight_reads_supplied_answer_data_at_runtime` (-5),
`test_negative_weight_nondeterministic_output` (-3). Each fires only when its
specific shortcut is taken, so an honest solver scores zero on all four.

Channel B carries 16 rubric criteria: twelve positive summing to 44 and four
negative summing to -16. R1 through R6 are the instruction-following
constraints, the five positive ones holding 11 of the positive weight and the
sixth carrying the negative scope rule on the shipped input directory; R7
through R11 are the discriminating criteria and hold 23; nothing in the rubric
is satisfied by default behaviour. R12 through R14 are the remaining
negatives, and R15 and R16 are the two conduct criteria, holding 10 between
them.

**Observable partition (RB-8).** The two channels grade disjoint observables.

Facts `tests/test_output.py` asserts: the presence of every declared
deliverable; the two fallback locations named in the delivered program; the
byte content of `normalized.jsonl`, `summary.json`, `product_summary.json`, the
five zone reports and the four tenant segment reports; and, inside
`data_quality.json`, the values of `accepted_count`, `frame_count`,
`rejections` and `reconciliation`, plus the presence and types alone of
`calibration_comparison`, `candidate_search`, `candidate_search.evaluations`,
`candidate_search.parameters`, `settlement`, `settlement.passes` and
`settlement.quarantined_modules`.

Facts `tests/rubric.json` asserts: that the parameter search preceded any
figure computed for the incident corpus; that a verdict was recorded for every
candidate before a value was selected; that the shipped check totals were
checked before a decoded field was trusted; that the derivation was re-run from
an empty output directory; that the arithmetic was carried in integers; that
the derivation record accounts for all eighty one candidate values; that it
leaves one survivor per domain and marks seventy two as rejected; that it names
a falsifying control figure for every rejection; that it shows the settlement
stabilising after exactly six passes; that it carries one comparison row per
published control figure; that the shipped input directory was left as it
was found; that each wire-decoding inverse was round-tripped against
constructed inputs before delivery; and that the program was exercised on an
input distinct from the shipped corpus.

The last two are conduct criteria: they judge how the agent worked rather than
what it produced, so neither traces to a sentence in `instruction.md` and
neither reads an observable any test asserts.

The two lists are disjoint: no value a test asserts appears in a criterion, and
no value a criterion grades is asserted by a test.

## Root cause

**Withholding versus unreachability.** Every rule the graded corpora exercise is
written out in `environment/data/`. What is withheld is procedure: precedence,
staging order, arithmetic order, serialisation directives and the values of the
nine wire parameters. The control slice closes the gap: exactly one assignment
of the nine reproduces its eleven published figures, so every withheld value is
recoverable from agent-visible files alone.

**Sequential state.** The running resident span total and the signature ring
advance over the export as a whole and over every fault frame it carries,
whatever flags that frame's own dump sets. A decoder that treats dumps as
independent units gets the right answer for the first dump and drifts
thereafter. This is the invariant that defeats decoding a single example and
generalising from it.

**Cursor integrity.** The declared count decides where fault frames end and
trailer lines begin. The count bias and the split base both act on that
boundary, so a cursor error corrupts the dump check total, the base, and every
frame after it.

**Derived identity.** A module tag is not an identity. Identity is the terminal
registry row covering that tag on the frame's own build index, reached through
however many alias hops the registry defines. A rename moves frames between
module groups, between components and between severity tiers, so it changes the
settlement and therefore the graded roll-ups.

**Monotone convergence.** Demotion only ever lowers a module's allowance, and an
allowance only ever lowers a component's envelope, so the settlement is monotone
and terminates. Uniqueness is what `attribution-and-ownership.md` asserts; the
pass count is what the derivation record has to report.

**Red lines.** No graded answer may depend on a value that lives only in
`tests/`, in an environment variable, or in the network. No `cheat/` directory
exists or may be added. Anti-cheat pressure lives entirely in the four
negative-weight tests and the four negative rubric criteria.

## Solution shape

1. Read the six rule documents and separate the three corpora by name prefix.
2. Read the eleven published control figures and the nine candidate domains.
3. Derive the staging order of the parameter search from the data dependencies
   between the wire rules.
4. Settle the count multiplier against CTRL-A over its nine candidates.
5. Settle the rotation offset against CTRL-C over its eight candidates.
6. Settle the entry clock modulus against CTRL-D over its thirteen candidates.
7. Settle the drift period against CTRL-E over its five candidates.
8. Settle the coarse unit against CTRL-F over its five candidates.
9. Settle the signature offset with the ring width against CTRL-G over their
   thirty two combinations.
10. Settle the alternate radix with the base shift width against CTRL-B over
    their hundred combinations, then confirm CTRL-H, CTRL-I, CTRL-J and CTRL-K.
11. Decode the incident export with all twelve inverses applied in dependency
    order, carrying the running span total and the signature ring across dumps.
12. Resolve each frame's module through the registry at that frame's build
    index and apply the support window convention.
13. Join the runtime probe and uplink exports on the canonical tag and the
    authoritative frame index, recording a rejection reason for every frame that
    fails an acceptance condition.
14. Settle the quarantine assignment by sweeping the module set until no module
    is left to demote, counting the sweeps.
15. Write the fourteen deliverables, including the reconciliation and the
    derivation record.

## Ordered plan

1. Inventory `/app/data` and read all six rule documents before writing code,
   changing none of them. Earns R1 by establishing that calibration comes
   first, and avoids R6 by leaving the shipped inputs untouched.
2. Reproduce the sample tier answers from the plain column layout, confirming
   the written shape of the ledger and the reports against
   `example_zone_report.json`. Earns R3 by exercising the check totals.
3. Build the decoder with every flag inverse, verifying dump and frame check
   totals as it goes. Earns R3.
4. Stage the parameter search against the control slice, recording a verdict for
   every candidate value in each domain before selecting that domain's survivor.
   Earns R2, R7, R8 and R9.
5. Reproduce all eleven control figures with the settled parameters and record
   the comparison. Earns R11, and avoids R12 by computing the reproduced column
   rather than copying the published one.
6. Derive the incident corpus, settle the quarantine assignment, and record the
   sweep count. Earns R10.
7. Re-run the whole derivation from an empty output directory once the parameter
   set is settled. Earns R4.
8. Keep every clock, byte and residency figure in Python integers throughout.
   Earns R5.

## Pitfalls

- Decoding each dump in isolation. The running span total and the signature ring
  cross dump boundaries; this fails `test_trap_running_unwind_drift_inverse` and
  `test_trap_ring_back_reference_inverse`.
- Trusting the declared count. The count bias moves the frame and trailer
  boundary; this fails `test_trap_biased_declared_count_inverse`.
- Verifying the check totals and stopping. The rotated signature and the rotated
  frame lines both pass them; this fails
  `test_trap_rotated_fault_signature_inverse` and
  `test_trap_rotated_frame_lines_inverse`.
- Reading the frame index column. Under the shadow bit it is a dump-local
  placeholder; this fails `test_trap_shadow_frame_index_inverse` and breaks the
  join to both side exports.
- Taking the module column as an identity. Under the alias bit it is a registry
  alias; this fails `test_trap_registry_alias_column_inverse`.
- Treating the support window as half open. This fails
  `test_shipped_ledger_matches_the_reference` and
  `test_generalises_when_the_support_boundary_shifts`.
- Settling the quarantine in one sweep. This fails
  `test_shipped_incident_and_module_rollups_match_the_reference` and every
  generalisation test.
- Generalising from the labelled sample tier. Every flag word there is clear, so
  this implements no inverse and fails all twelve trap tests.
- Fitting the parameters to the shipped tallies rather than searching. This
  passes the shipped corpus and fails all twelve generalisation tests.
- Dropping a partition report because its partition is empty. Every zone and
  every tenant segment ships a report; this fails
  `test_generalises_when_a_deployment_zone_empties`.

## Provenance

```yaml
uuid: 04f4916e-b05d-51c2-9639-be973e3f3e16
instance_id: 45421e59-0daa-505b-9656-e565f93c491c
task_name: terminal-bench/45421e59-0daa-505b-9656-e565f93c491c
fix_patch_sha256: d71a83304abd634ad1f2d0093ea5f292a19a51ed83aaefd25c6eb0e1187d89c7
solve_py_sha256: 53c3d4a069ad4ad053062b3a0b258211d154aabfd0bcb12b3aeb2cb47d5745e9
language: python
status: ready
root_seed: 6142398075219483361
measured_oracle_reward: 1.000
measured_nop_reward: 0.000
measured_trap_blind_reward: 0.080
verification_notes: >-
  Oracle, nop and every S13 probe were measured locally with the shipped
  reward formula against the shipped grader. Harbor Docker runs, the live
  judge run and the eight-trial measurement have not been performed and no
  figure is claimed for them.
caveats: >-
  The single-wrong-parameter probe scores 0.526 because the reference recovers
  the parameter set from the control slice at run time and therefore repairs a
  planted parameter fault on the one corpus that ships a control slice. Every
  held-out corpus still fails.
```
