# cffe140f-0908-5df7-b27f-c3983c2df17c

## Task Metadata

- **Difficulty:** `expert`
- **Category:** `Games` (internal taxonomy: `Software` / `Algorithms`, save-file / replay flavour)
- **Tags:** `replay-journal`, `save-file-recovery`, `constraint-satisfaction`, `fixpoint`, `reconciliation`
- **Expert time:** 5 hours (300 min)
- **Junior time:** 15 hours (900 min)
- **Agent timeout:** 6 hours (21600s)
- **Verifier timeout:** 1 hour (3600s)
- **Inputs:** 19 flat files under `environment/data`
- **Outputs:** 10 artifacts under `/app/out`
- **Internet:** allowed
- **Judge:** `gpt-5.6-sol`

### Constraint positions in the shipped material

Fourteen constraints must hold simultaneously and each is independently verifiable.

Sustained cross-record tracking, five of them, all located in the last third of the shipped material when the documents are read in the order the directory lists them:

1. Upkeep persistence past elimination. Stated once, in the upkeep policy, the last document in the ordering.
2. Season upkeep budget cap charged match by match in logical order, with the excess spilling to overflow. Also in the upkeep policy.
3. Streak credit accruing across consecutive match wins. In the standings policy.
4. The concession window that voids later output of a conceding squad inside the same match. In the scoring policy.
5. The transfer fixpoint that resolves a squad to its effective owner. In the roster continuity note.

Structural and ordering, four of them:

6. Segment logical ordering and per-segment tick monotonicity.
7. Escape directive precedence: a redirect is applied before an advance, while the wire writes the advance pair first.
8. Roster dictionary ordering by first appearance in the baseline capture.
9. Report serialisation shape, which is the single strict whole-output format constraint and is stated nowhere in prose. It is inferable only from the one fully-formed example report shipped in the data directory.

Global coupling, three of them:

10. Finals bracket election over the eligible owner pool against ten sub-constraints.
11. Review docket election maximising a review index under a per-conference cap.
12. Promotion tiering from a threshold that is itself an unknown.

Envelope and framing, two of them:

13. Envelope layout and checksum validation.
14. Record framing width recovery.

## Difficulty explanation

The task is a search, and it resists transcription at three separate levels.

At the wire level, twelve independent stream guards are engaged simultaneously on the two guarded captures. Each guard is a triple of a flag bit, a forward transform applied when the journal was written, and a single exact inverse. Seven of the twelve sit on the decoding spine, meaning a wrong inverse halts the decoder rather than quietly shifting a number, and four of them force strictly sequential decoding because the state needed for record N is only available after record N minus one has been fully recovered.

At the policy level, three quantities that govern the settlement appear nowhere in the material. Each is drawn from a roster that is published. The guard parameter triple of engine tag, sliding window depth and sentinel byte has a candidate space of 8 times 4 times 4, which is 128 combinations; the upkeep budget cap has a roster of 6; the promotion threshold has a roster of 6. The control capture and its eleven published control figures pin each of the three to exactly one value and reject every other combination with a named falsifier.

At the domain level, the settlement is a fixpoint followed by two global elections with tie-breaks. Attribution chases transfer edges until a sweep changes the mapping no further, and the shipped season contains a three-node cycle that settles on its lexicographically least member. The finals bracket is chosen from 364 triples against ten sub-constraints that no triple satisfies in full, so the elected triple is the one maximising the satisfied count with a documented tie-break. The review docket is a bounded selection maximising a review index under a per-conference cap.

Premise order is deliberately different from application order. The wire writes the advance directive pair before the redirect pair; the settlement applies the redirect first. The documents present scoring before standings before continuity before upkeep; the computation needs upkeep persistence and the budget cap before any score exists. No document states a precedence order, a serialisation call, a field list, a partition membership, or a numbered procedure.

### The counterfactual

Exactly one domain rule contradicts the default a competent reader would assume, and it is stated once, in the upkeep policy:

> An eliminated unit remains upkeep bearing for the remainder of the match. Elimination withdraws the unit's output but never its upkeep. Only a disband order withdraws upkeep, and it does so from its own tick onward.

The conventional reading charges upkeep from deploy until the earliest of elimination, disband and match close. The league charges it from deploy until the earlier of disband and match close, ignoring elimination entirely. The difference propagates into charged upkeep, overflow upkeep, the levy, every match score, the season score, the tier assignment, the bracket eligibility test and both reconciliation residuals. Adopting the conventional reading changes the guard parameter search itself: the control figures then admit zero surviving candidates, so a solver that assumes the default halts with an underdetermined parameter set rather than producing a plausible wrong answer. Graded outputs that differ under the two readings: `summary.json`, `data_quality.json`, `policy_audit.json`, all four conference reports and both segment reports.

### Trap portfolio

| Bit | Flag | Scope | Forward | Inverse | Tests | Weight |
| --- | --- | --- | --- | --- | --- | --- |
| 0 | `keystream` | record, spine | Blob bytes are exclusive-ored with a repeating digest keyed on the engine tag, the match id, the effective salt and the record ordinal | Regenerate the same digest stream and exclusive-or it back | `test_trap_keystream` | 5 |
| 1 | `chain` | cross-record, spine | The stored value word is exclusive-ored with the tick of the previously decoded record, the first record using the envelope nonce | Exclusive-or with the previously decoded tick, carried in logical order | `test_trap_chain` | 5 |
| 2 | `delta2` | cross-record, spine | Ticks are stored as a second-order difference that resets at each segment boundary | Accumulate twice per segment from a zero base | `test_trap_delta2` | 5 |
| 3 | `driftkey` | block, spine | The effective salt of a segment is the written salt exclusive-ored with the running sum of decoded values of all logically earlier segments | Maintain that running sum in logical order and recover the effective salt before decoding the block | `test_trap_driftkey` | 5 |
| 4 | `segperm` | block, ordering, cursor drift | Storage slot i holds the logical segment at offset plus i times a stride derived from the nonce | Regenerate the offset and the stride from the nonce and restore logical order | `test_trap_segperm` | 5 |
| 5 | `backref` | cross-record, spine | A repeated squad key is stored as an offset into the window of the last W distinct keys ordered least recently seen first | Maintain the same window at the same depth and index from its least recent end | `test_trap_backref` | 5 |
| 6 | `endian` | metadata, checksum invisible | The envelope nonce is written big-endian while every other envelope word stays little-endian | Read the nonce big-endian before any nonce-derived quantity is computed | `test_trap_endian` | 5 |
| 7 | `countbias` | metadata | Every declared count in the envelope and in each segment header is stored with a bias derived from the engine tag added to it | Subtract that bias from each declared count before use | `test_trap_countbias` | 5 |
| 8 | `lenshadow` | record, checksum invisible, cursor drift | The visible width byte is a decoy and the true width is a shadow byte exclusive-ored with the low byte of the effective salt | Recover the true width from the shadow byte and advance the cursor by it | `test_trap_lenshadow` | 5 |
| 9 | `rotate` | record, checksum invisible | The blob is rotated left by an amount derived from the effective salt and the record ordinal, modulo the true width | Rotate right by the same amount | `test_trap_rotate` | 5 |
| 10 | `alias` | cross-file, corpus level | A squad key is stored as an index into a dictionary built from the distinct keys of another capture in first-appearance order | Rebuild that dictionary from the baseline capture and index into it | `test_trap_alias` | 5 |
| 11 | `escape` | record, sparse sentinel | Directive pairs of a sentinel byte and a directive byte are prefixed to the blob | Strip leading pairs while the leading byte equals the sentinel and apply the directives in application order | `test_trap_escape` | 5 |

Composition check against the recipe minimums: cross-record or stateful traps are bits 1, 2, 3 and 5, which is four against a minimum of three. Block-level traps are bits 3 and 4, which is two against a minimum of two. Metadata or header traps are bits 6 and 7, which is two against a minimum of two. Checksum invisible, ordering invisible or cursor invisible traps are bits 4, 6, 8 and 9, which is four against a minimum of two. Cross-file traps are bit 10, which is one against a minimum of one. Traps forcing strictly sequential decoding are bits 1, 2, 3 and 5, which is four against a minimum of three. Corpus-level traps are bit 10. Cursor-drift traps are bits 4 and 8. Ordering-not-value traps are bit 4. Seven of the twelve sit on the decoding spine, above the required half. No two traps share an inverse: the twelve inverses are digest regeneration, chained exclusive-or, double accumulation, running-sum key recovery, permutation restoration, window indexing, endianness selection, bias subtraction, width recovery, rotation, dictionary lookup and sentinel stripping.

### Tiering

Three journals ship. The baseline capture has all twelve guard flags cleared and is accompanied by a labelled event transcript listing every record it holds, so the base envelope, the field layout and the event vocabulary are fully documented and nothing else is. The control capture and the season capture both carry all twelve guard flags set, which is 100 percent of the trap surface against the required floor of 85 percent. The control capture is the calibration slice: it is a held-out population with eleven published control figures and no transcript, and it is the only thing that pins the three unstated quantities. The season capture is the graded population and publishes nothing about itself beyond its declared counts, which are themselves biased.

## Solution explanation

The oracle is a single stdlib program. It classifies the shipped files entirely by content rather than by name, so renaming any input changes nothing: journals are found by magic bytes, tables by their header line, and prose by marker lines. It parses the published rosters and the eight policy scalars out of the prose documents, so a corpus that publishes different rosters is handled without change.

It then decodes the unguarded baseline capture and rebuilds the roster dictionary from it in first-appearance order. It enumerates the 128 guard parameter combinations, decodes the control capture under each, computes the six structural control figures, and keeps the combinations that reproduce all six. Exactly one survives. Against that survivor it walks the upkeep cap roster until the charged and overflow control figures reproduce, then the promotion threshold roster until the three tier control figures reproduce.

With the three quantities fixed it decodes the season capture, resolves attribution to a fixpoint, applies the counterfactual upkeep rule, charges the budget cap in logical match order, scores every match, awards standing points with streak credit, assigns tiers, elects the bracket and the docket, and emits the nine reports plus the derivation record. Serialisation is a sorted-key dump at an indent of two with a trailing newline, which the solver must infer from the single example report because no document describes it.

### Calibration uniqueness

The eleven control figures are published as C1 through C11: matches opened 5, records recovered 238, distinct squads 16, gross output 6228, voided output 1703, raw upkeep 8166, charged upkeep 7939, overflow upkeep 227, champion entrants 1, qualifier entrants 1, provisional entrants 14.

C1 through C6 depend only on decoding and are independent of the two policy scalars, so they isolate the guard parameter triple. Exhaustive enumeration of all 128 combinations was run and confirmed exactly one survivor: engine tag `tessel-drift`, window depth 6, sentinel byte 0x1D. C7 and C8 then isolate the cap at 860 against a roster of 6, rejecting the other 5 with charged-upkeep falsifiers of 300, 3278, 5769, 7230 and 8159 against the published 7939. C9 through C11 isolate the threshold at 700 against a roster of 6, rejecting the other 5 with tier-count falsifiers.

The cap roster and the threshold roster are derived at corpus generation time from the control data itself, by scanning the achievable range and retaining six values with pairwise distinct control signatures. That construction guarantees the uniqueness property for every generated corpus, including all fourteen held-out mutations, rather than only for the shipped one. All fourteen were built and resolved; each yields exactly one surviving guard triple, one cap and one threshold.

## Verification explanation

Forty-five graded tests, of which forty-one are positive with a combined positive weight of 177 and four are negative shortcut detectors.

### Shortcut ceiling

| Tier | Tests | Weight | Share | Ceiling |
| --- | --- | --- | --- | --- |
| FORMAT | 3 | 3 | 1.69% | at most 3% |
| INSTANCE | 8 | 24 | 13.56% | at most 25% |
| TRAP | 12 | 60 | 33.90% | at least 30% |
| GENERALISATION | 18 | 90 | 50.85% | at least 42% |
| SHORTCUT_TOTAL | 11 | 27 | 15.25% | at most 28% |

The generalisation tier holds fourteen mutated corpora plus four mixed-interaction cases, all built inside the verifier at grading time from seeds and mutation names that appear nowhere in the shipped environment. The fourteen mutations are `tie_config`, `cap_push`, `empty_group`, `boundary_shift`, `add_entity`, `counterfactual_flip`, `scale`, `contradict`, `alias_chain`, `nonapply`, `cycle`, `partition`, `sentinel_heavy` and `fresh_base`. The `nonapply` corpus exercises rules that do no work in the shipped instance: it settles attribution in a single pass and leaves the streak and concession paths idle. The `counterfactual_flip` corpus rebalances the elimination and disband populations so that a solver reading upkeep the conventional way diverges on a different set of squads than it does on the shipped instance.

Each of the twelve traps has a dedicated weight-5 test that builds a corpus with that guard bit set, together with the companion bits it structurally requires, and demands an exact match on all eight graded reports. Grading is serial in the sense that a report only matches if every earlier stage of the chain was right: an envelope misread changes the segment count, a segment misread changes the record set, a record misread changes the ledger, the ledger changes the scores, the scores change the tiers, and the tiers change the bracket.

### Rubric weight budget

Sixteen criteria, twelve positive with a combined positive weight of 44.

| Band | Criteria | Weight | Share | Requirement |
| --- | --- | --- | --- | --- |
| CONSTRAINT | R1 to R5 | 15 | 34.1% | at most 35% |
| DISCRIMINATING | R6 to R12 | 29 | 65.9% | at least 65% |
| FREE | none | 0 | 0% | exactly 0% |
| NEGATIVE | R13 to R16 | -16 | n/a | exactly four |

The five constraint criteria are all of type `instruction following` against the trajectory, and each quotes verbatim the instruction sentence it enforces. All five are ordering, procedural or tool-use claims; none of them grades a number. Every positive criterion scored 3 or 5 names an exact count copied from the reference run: 128 candidates with 1 accepted and 127 rejected, 6 settlement sweeps over 9 transfer edges resolving 8 keys, 364 bracket triples from a pool of 14 with a best satisfied count of 9 out of 10 and 6 triples tied, a docket of 5 matches under a cap of 2 per conference with a review index of 2993, 11 control rows, a declared roster residual of -4 and 4 unregistered squads.

Evaluation targets obey the separation of concerns the format spec requires. No criterion carries `evaluation_target = "state_change"`, because that axis is artifact and state correctness and `tests/test_output.py` already owns it in full. Fourteen criteria read the trajectory, one reads the final answer, and one reads the user-facing response. Five carry `type = "instruction following"`, above the floor of three. The harness-owned timeout sentence is graded by no criterion.

### Observable partition

The tests own artifact and state correctness: they compare the eight report files value for value against an independently derived expectation. The rubric owns process and response quality: how the agent searched, what it falsified, what it settled, and what it told the user. The partition is disjoint by construction.

`policy_audit.json` is the derivation record. The tests assert it for presence and key schema only, never for any value. Nothing the rubric asserts about candidate spaces, verdicts, falsifiers, sweep counts, cycle resolution or the calibration table is checked by any test, and no value a test compares is restated by any criterion. R1 through R8, R10 and R12 through R16 read the trajectory, which the tests cannot see at all; R9 reads the final answer and R11 reads the user-facing response, neither of which any test inspects. The instruction asks for both of those closing statements explicitly, so the two response criteria grade a contract the agent was actually given.

### Probes

- P1, is it a search rather than a transcription: yes. Three quantities are absent from the material and must be recovered by falsification against a held-out population.
- P2, are there at least two interacting load-bearing structures: yes. A constraint satisfaction over 128 combinations, a fixpoint over transfer edges, and a global optimisation with a documented tie-break over 364 triples, all coupled through the upkeep ledger.
- P3, do at least ten constraints hold simultaneously: yes, fourteen, itemised above.
- P4, is there exactly one counterfactual: yes, the upkeep persistence rule, and it moves nine of the ten artifacts.
- P6, is premise order distinct from application order: yes, the directive pairs are written in the reverse of the order in which they apply.
- P7, is the calibration slice sufficient and unique: yes, verified by exhaustive enumeration on the shipped corpus and on all fourteen mutations.
- P8, does a partial solver stay under the blind ceiling: yes. Clearing any single guard bit and repairing the envelope checksum halts the decoder or diverges from the reference on ten of the twelve bits, and the remaining two are pinned by the calibration search instead.
- P9, is every graded value derivable from the instruction plus the data directory alone: yes. The oracle reads nothing else and takes no arguments beyond the two directories.
- P10, does the format constraint appear only once and only by example: yes, the serialisation shape exists solely in the one example report.
- P11, can the tests be passed by memorising the shipped instance: no. The instance tier is 13.56 percent of the weight and the four negative detectors fire on hardcoding, on filename-dependent behaviour, on reading supplied answer data and on nondeterminism.

## Measured scores

- oracle reward: 1.000
- nop reward: 0.000

## Additional Notes

### Gate results

The oracle run passes all forty-one positive tests and fails all four negative detectors, which is the intended polarity, giving a raw score of 177 against a positive total of 177. An empty output directory passes nothing and scores 0.

### Not measured here

The rubric score is produced by the external judge at evaluation time and is not computed by this bundle. The bundle only guarantees that the rubric is well formed, that its observable partition is disjoint from the tests, and that every count it asserts was copied from the reference run recorded in `TRUTH.md`.

Data files avoid the `sample_`, `field_`, `eval_` and `challenge_` prefixes so the static solution audit stays clean.
