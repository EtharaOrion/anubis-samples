# 45421e59-0daa-505b-9656-e565f93c491c

## Task Metadata

- **Difficulty:** `medium`
- **Category:** `Debugging` (internal taxonomy: `Software` / `Systems`)
- **Tags:** ["debugging", "crash-triage", "fault-attribution", "calibration", "fixpoint", "stdlib"]
- **Expert time:** 5.0 hours (300 min)
- **Junior time:** 15.0 hours (900 min)
- **Agent timeout:** 6.0 hours (21600s)
- **Corpus seeds:** root `0x553E2E816E801AE1`, sample `0x6BD848BD6903F9E0`, control slice `0x0042409CB9DE4ACA`, incident `0x723418053D8DF788`, held-out `0x41CB4F838AF016C0`

## Difficulty explanation

The agent is handed one incident window of a service fleet as three
heterogeneous wire exports plus five reference tables and six rule documents,
and must reconstruct a per-frame fault ledger and twelve roll-ups from it. The
work is a search, not a transcription, over two interacting load-bearing
structures.

**Structure one, an unstated parameter set (S2c).** The crash reporter holds
back nine wire parameters. Their candidate domains are published; their values
are not. Eighty one candidate values sit across the nine domains. Exactly one
assignment reproduces the eleven control figures the fleet published for a
held-out corpus. The parameters are not independently pinnable in an arbitrary
order: the declared-count multiplier has to fall before frames split from
trailers, the rotation offset before resident spans can be accumulated in
logical order, resident spans before the running drift can be removed from the
unwind column, the entry clock modulus before the chained entry stamps unwind,
and the coarse unit before an allocated byte total means anything. The staging
order is nowhere stated; it is derivable only from the data dependencies
between the rules.

**Structure two, a mutually recursive quarantine settlement (S2a).** Every
module holds an effective severity tier. A component's containment envelope is
set by the lowest tier budget still in service across that component's modules,
so demoting one module tightens the allowance of all of its siblings, which can
demote them in turn. The shipped corpus settles after six sweeps. The
specification states only that a unique stable assignment exists; a single
forward pass produces silently wrong values on the whole graded set (measured
below at 0.036).

The output of the first structure feeds the second: the recovered parameters
decide which frames decode to which peak residencies, which decides the
settlement.

**Twelve wire traps carried by the incident corpus.** Every one has a flag bit,
a forward transform and a one-sentence inverse; four force strictly sequential
decoding; two survive the shipped check totals; one drifts the frame cursor;
one changes ordering rather than values; one needs cross-file context. The
sample tier ships with every flag clear and its answers labelled, so the
tempting shortcut, generalising from the labelled sample, is exactly the trap:
that solver implements no inverse at all and scores 0.080.

**One counterfactual (S2c).** A fault frame is in support only when its capture
tick lies strictly inside its component's support window: both endpoints stand
outside. Every conventional reading disagrees. Three frames of the shipped
corpus sit exactly on a boundary, and the graded set moves under each
conventional reading: seven of thirteen artifacts differ under the half-open
`[open, close)` reading, six under `(open, close]`, and nine under the fully
inclusive reading.

**Serial horizon.** Fifteen stages, two convergence points (parameter
settlement, quarantine settlement), each consuming the exact output of the one
before. Truncation profile measured at 0.015 / 0.007 / 0.036 / 1.000.

### Where the constraints sit (S2e)

The six rule documents are read in name order: `attribution-and-ownership.md`,
`calibration-controls.md`, `clock-and-sequencing.md`, `identity-and-builds.md`,
`invariants-and-closure.md`, `wire-and-dumps.md`. Application order is the
reverse end of that list first: the wire rules, then sequencing, then identity,
then attribution, then the invariants, with calibration threaded through the
front. Four of the five sustained cross-record constraints (second-order
resident spans, the chained entry column, the running unwind drift and the
rolling signature ring) are stated in `wire-and-dumps.md`, the last file in
reading order; the fifth, quarantine settlement, sits in
`attribution-and-ownership.md`. The one whole-output format constraint lives in
a single place, the serialisation paragraph closing `invariants-and-closure.md`,
and is inferable only from the shipped example zone report.

## Solution explanation

The reference reads the incident corpus and the control slice, stages the
parameter search (172 candidate evaluations, exactly one survivor at every
stage), decodes the reporter export with all twelve inverses applied in
dependency order, resolves each frame's module through the registry's effective
intervals and alias chains at that frame's build index, applies the support
window convention, joins the runtime probe and uplink exports on the canonical
tag and the authoritative frame index, settles the quarantine assignment to its
unique stable state, and writes fourteen deliverables. It carries the settled
parameters forward so that corpora shipping no control slice are derived from
the same contract.

## Verification explanation

`tests/test_output.py` declares 35 tests: 31 positive summing to 137 and four
negative summing to -18. It rebuilds every corpus it grades from a declared
seed, so the verifier never reads the agent image's data tree. Positive weight
is partitioned into four tiers:

| Tier | Weight | Share of positive total |
|---|---:|---:|
| FORMAT | 2 | 1.46% |
| INSTANCE | 15 | 10.95% |
| TRAP | 60 | 43.80% |
| GENERALISATION | 60 | 43.80% |

`SHORTCUT_TOTAL` (FORMAT + INSTANCE) is 17 of 137, **12.41%**. Every INSTANCE
test has a GENERALISATION counterpart at higher weight. The twelve TRAP tests
each run a corpus carrying exactly one trap and nothing else. The twelve
GENERALISATION tests each run a held-out mutated corpus with every trap live,
covering: a fresh corpus; moved budget ties; peaks sitting on the budget;
inverted registry row order; an emptied deployment zone; a registry alias cycle;
a support boundary shifted by one tick; an added module; a dormant rule firing;
a changed zone partition count; an order-of-magnitude larger corpus; and a
corpus whose windows contradict the shipped tallies.

Rubric weight budget: positive total 44, DISCRIMINATING 23 (**52.27%**),
CONSTRAINT 11 (**25.00%**), CONDUCT 10 (**22.73%**), FREE 0. The six
instruction-following constraints
lead the file as R1 through R6, five of them positive and the sixth the negative
scope rule holding the agent off the shipped input directory; the five
discriminating criteria follow as R7 through R11, three negatives follow them as
R12 through R14, and the two conduct criteria close the file as R15 and R16.
Every criterion carries
`evaluation_target` of `trajectory` or `final_answer`. The rubric emission
check refuses any bundle whose rubric carries `state_change`,
so the derivation-record criteria are graded from the trajectory and from the
delivered record the judge reads under `artifacts/` rather than through a
`state_change` target. Every discriminating criterion names an exact figure
taken from the reference run, and each of those figures follows from the shipped
materials alone. The observable partition is recorded in `TRUTH.md`: no value
the rubric grades is asserted by a test, and no value a test asserts appears in
a criterion.

## Measured scores

oracle: 1.000
nop: 0.000

Both measured by running `tests/test_weights.json` against
`tests/test_output.py` with the standardized `test.sh` reward formula, on the
shipped bundle: the oracle run installs `solution/solve.py` at
`/app/out/transform.py` and runs it over `environment/data`; the nop run grades
an empty output directory. No negative-weight test fires on either. These are
harness-equivalent local runs of the shipped reward function; they have not been
re-measured through `harbor run --env docker`, and no Docker-based figure is
claimed here.

### Probes (S13), all measured against the shipped grader

| Probe | Variant graded | Result |
|---|---|---|
| P1 transcription | every documented rule implemented, the nine parameters left unrecovered | **0.153** |
| P2 self-consistent-wrong | correct decode, quarantine settled in one sweep | **0.036** |
| P3 stage corruption | identity stage disabled 0.015; support window stage disabled 0.015; settlement stage 0.036; a single parameter off by one 0.526 | see note |
| P4 truncation | 25% 0.015, 50% 0.007, 75% 0.036, 100% 1.000 | serial |
| P7 constraint independence | each of the twelve trap inverses removed in turn | **12 of 12 isolated**: exactly the one bound trap test fails, the other eleven pass |
| P8 counterfactual | conventional half-open reading | **7 of 13** graded artifacts differ; accepted 153 against 151. The other two conventional readings also diverge: `(open, close]` moves 6 artifacts and accepts 152, fully inclusive moves 9 and accepts 154 |
| P9 trap-blind | base transform only, no wire inverse implemented | **0.080** |
| P10 trap presence | shipped incident corpus | 27 of 27 dumps carry at least one trap (**100%**); all twelve flags present; **0** sample dumps set any flag |
| P11 solvability | staged recovery from the shipped control slice alone | unique survivor at every stage in **172** evaluations; recovered set equals the reporter's. A confirmation sweep varied each parameter alone across its whole domain: of the 81 candidate values, exactly the 9 settled ones reproduce all eleven control figures |

**Note on P3.** Corrupting a stage of the derivation chain collapses the FORMAT,
INSTANCE and GENERALISATION tiers to 2 of 77 weight in every case measured. The
0.526 figure for a single wrong parameter is the isolated TRAP tier plus the
shipped-corpus INSTANCE tier, which survives because the reference recovers the
parameter set from the control slice at run time and therefore repairs the
planted fault on the one corpus that ships a control slice; every held-out
corpus fails. S10 mandates one dedicated weight-5 test per trap that fails if
and only if that trap's inverse is missing, and P7 requires those tests to be
independent, so eleven of the twelve necessarily survive a fault in the twelfth.
The serial-chain gate is carried by the INSTANCE and GENERALISATION tiers, which
score 2 of 77 under every stage corruption measured.

P5 (cheap-model ceiling), P6 as an executed judge run, and the S15 eight-trial
Opus 5 measurement need a live judge and Harbor runs and **have not been run**.
No number is claimed for them.

### Rubric acceptance grading (S11, by hand)

**(a) Most plausible complete-but-wrong solution.** Implements every documented
wire rule and the settlement, picks the nine parameters by inspection and
plausibility, settles the quarantine in one sweep, and emits a derivation record
naming its selected values with no enumeration, no falsifiers and no control
comparison. R1 0, R2 0, R3 3, R4 0, R5 1, R6 0, R7 0, R8 0, R9 0, R10 0, R11 0.
Total **4 of 34 = 11.76%**; no negative fires, because it leaves the shipped
input directory alone and publishes no comparison table to copy into.

**(b) Strong solution, correct on the shipped corpus and wrong on the held-out
mutations.** Fits the parameters against the labelled sample and the shipped
tallies rather than by search, reaches the correct settlement, copies the
published control figures into its comparison table, and reports no
enumeration. R1 0, R2 0, R3 3, R4 1, R5 1, R6 0, R7 0, R8 0, R9 0, R10 5, R11 5,
for a positive subtotal of 15 of 34, and **10 of 34 = 29.41%** once R12 fires on
the copied comparison table.

### Trap portfolio (twelve cards)

| # | Flag | Scope | Forward | Inverse | Bound test | Weight |
|---|---|---|---|---|---|---:|
| T1 | `0x001` | dump, cross-record | resident spans written as second-order differences within the dump | accumulate twice from the dump's first frame | `test_trap_second_order_cost_inverse` | 5 |
| T2 | `0x002` | dump, cross-record | entry stamp added to the previous frame's, on the wrapping clock | subtract the previously decoded stamp, on the wrapping clock | `test_trap_chained_entry_inverse` | 5 |
| T3 | `0x004` | dump, cross-record | unwind stamp raised by the running resident span total, reduced by the drift period | recompute the running total while decoding and subtract | `test_trap_running_unwind_drift_inverse` | 5 |
| T4 | `0x008` | dump, ordering | dump frame lines rotated left by the dump key plus the rotation offset | rotate the lines back by the same amount | `test_trap_rotated_frame_lines_inverse` | 5 |
| T5 | `0x010` | header, cursor | declared count carries an ordinal-driven bias | subtract the bias before splitting frames from trailers | `test_trap_biased_declared_count_inverse` | 5 |
| T6 | `0x020` | header | base numeral written in the alternate radix | read the numeral in that radix | `test_trap_alternate_radix_base_inverse` | 5 |
| T7 | `0x040` | frame | frame index column holds a dump-local placeholder | take the authoritative index from the symbol column's suffix | `test_trap_shadow_frame_index_inverse` | 5 |
| T8 | `0x080` | frame, checksum-invisible | fault signature rotated before the check total is taken | verify the total on the wire text, then rotate back | `test_trap_rotated_fault_signature_inverse` | 5 |
| T9 | `0x100` | cross-file | module column carries a registry alias | resolve it through the registry at the frame's own build index | `test_trap_registry_alias_column_inverse` | 5 |
| T10 | `0x200` | frame | allocation column escaped into a coarse unit | multiply the escaped figure by the coarse unit | `test_trap_coarse_allocation_escape_inverse` | 5 |
| T11 | `0x400` | frame, cross-record | signature written as a distance into a rolling ring | read the ring slot that distance reaches from the write position | `test_trap_ring_back_reference_inverse` | 5 |
| T12 | `0x800` | header | base split between the header and the dump's first trailer | rejoin by the base shift width | `test_trap_split_dump_base_inverse` | 5 |

Composition: four cross-record or stateful (T1, T2, T3, T11); four dump level
(T1, T2, T3, T4); three metadata or header (T5, T6, T12); three checksum,
ordering or cursor invisible (T4, T5, T8); one cross-file (T9). Four force
strictly sequential decoding (T1, T2, T3, T11). Two are invisible to the shipped
check totals (T4, T8). No two share an inverse.

### Parameter domains and the staged search

| Parameter | Domain size | Bound control | Combinations evaluated |
|---|---:|---|---:|
| count multiplier | 9 | CTRL-A | 9 |
| rotation offset | 8 | CTRL-C | 8 |
| entry clock modulus | 13 | CTRL-D | 13 |
| drift period | 5 | CTRL-E | 5 |
| coarse unit | 5 | CTRL-F | 5 |
| signature offset with ring width | 8 and 4 | CTRL-G | 32 |
| alternate radix with base shift width | 25 and 4 | CTRL-B | 100 |

Eighty one candidate values, 172 combination evaluations, exactly one survivor
at every stage. CTRL-H, CTRL-I, CTRL-J and CTRL-K are confirmation figures.

## Additional Notes

- `environment/data/` is flat and holds 24 files: six rule documents, the
  control figures, two labelled sample answer files plus one standalone example
  report, and the wire and reference tables for the three corpora.
- The incident corpus carries 222 fault frames across 27 dumps; 151 are
  accepted, 71 rejected across all five rejection reasons, the upload residual
  is 10409 bytes and the quarantine settles after 6 sweeps over the module set,
  leaving 6 modules quarantined.
- `outside_support` fires 39 times, `retired_build` 10 times,
  `unresolved_module` 10 times, `unprobed_frame` 8 times and `unreported_dump`
  7 times in the shipped corpus. These per-reason counts sum to 74 rather than 71
  because a frame breaking multiple conditions is counted once per reason yet only
  once in the 71 distinct rejected frames. The registry alias cycle rule fires only in the
  held-out `cycle` corpus, where `unresolved_module` rises from 10 to 19, and the
  zero-count report rule only in the `empty_group` and `partition_count`
  corpora. Both non-applying rules are exercised by a generalisation corpus.
- No `cheat/` directory exists and none may be added; anti-cheat pressure lives
  entirely in the four negative-weight tests and the four negative rubric
  criteria.
- Regenerating the wire files: `python3 tests/test_output.py environment`
  rewrites the fifteen generated files under `environment/data/` from the
  declared seeds. The six rule documents, the two sample answer files and the
  example zone report are authored, not generated, and are left untouched.
