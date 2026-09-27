# TRUTH.md - a8d5a61c-4126-58f3-8e29-edb360f79699

Ground truth for maintainers. Never enters the agent image (environment/Dockerfile
copies only environment/data) or the verifier image. Facts here are read back from
the files on disk.

## Spine

The spine is the per source per phase integer token allocation `A[phase, source]` in
final_allocation.jsonl. Every graded artifact is a projection or rollup of it:
eligibility gates it, the demand score sizes it, the proportional share proposes it,
the protected floors lift it, capacity binding displaces it, the curriculum precedence
and cycle break order the phases that carry state into it, and the global
reconciliation repairs it. The summaries, example weights, microbatches, objective
tuple, manifest and folded chain all reduce from it. A one token error in `A`
propagates into many downstream figures.

## Shipped sample facts (from a clean oracle run)

- Corpus: 22 files in environment/data/ (14 generated CSV/JSON, 8 hand authored
  specification documents).
- Artifacts: 29 declared (plan.py plus 28 derived).
- Sources: 14, all eligible on the shipped snapshot.
- Quiet branches on the shipped snapshot: 18 exception rows. B_UNIT_ROUND_TIE 10,
  B_FLOOR_BIND 3, B_CAP_BIND 1, B_CYCLE_BREAK 1, B_GLOBAL_CLAWBACK 3.
- Capacity displacement events: 2. Cycle edges removed: 1. Global clawback rows: 4.
- Microbatches: 20. Examples placed: 34.
- Total allocated tokens: 17998. Objective tuple: [17998, 4, 1, 2, 20].
- Invariants hold: true.

## Structural shapes present

- Serial 14 stage chain over the spine.
- Convergence 1: capacity binding needs both the proportional shares and the protected
  floors.
- Convergence 2: the carry state and the global reconciliation need both the surviving
  precedence order after the cycle break and the phase allocations.
- Cross source: quality num/den in quality.csv, freshness in freshness.csv, examples
  in examples.csv, and the six policy documents resolve a source only across files.
- Locally correct, globally wrong: a source that competes in two phases is allocated a
  locally sensible share in each, and only the global reconciliation exposes that the
  two shares together exceed its canonical token supply. This case has no per
  layer synthetic fixture.

## Trap levers

- Completeness (notice you must emit): capacity_binding_trace, cycle_report,
  cycle_break_decisions, precedence_overturn_trace, global_reconciliation_trace,
  rounding_residual_ledger, invariant_report, exception_trace and the folded
  final_state are all owed outputs a careless read misses.
- Pervasive rule: round half even at scale 10000 applied at every named stage. A
  solver that truncates, uses float, or rounds once at the end diverges across many
  rows.
- Edge rule mainline: the shipped answer needs ranked displacement (not a clip), a
  hardness ranked cycle break, and a global reconciliation claw back.
- Robustness tax (legal but unshipped states, all in the reseeded and fixture tiers,
  none in the visible corpus): zero quality denominator, single example source, empty
  example group, protected floors above a cap, allocation equal to a cap, two node
  cycle, language with no eligible source.
- Conjunction depth spread: weight one structural (k=1), weight three mid derivations
  (k around 2 to 4), weight five whole document outcomes and reseeded reruns (k around
  6 to 10).

## Channel A

- 75 test functions. positive_total = 267. easy_total (weight 1 and 3) = 72,
  hard_total (weight 5) = 195. Partial ceiling = 72 / 267 = 0.270.
- Weight counts: 12 at weight 1, 20 at weight 3, 39 at weight 5, 4 negative.
- Negative tests (polarity inverted, contribute 0 for an honest solver):
  test_negative_weight_hardcoded_sample_answers (-5),
  test_negative_weight_reads_training_corpus_at_runtime (-5),
  test_negative_weight_filename_dependent_behaviour (-5),
  test_negative_weight_nondeterministic_output (-3).

## Channel B

- 15 rubric criteria. R1 to R5 are instruction following backed by real instruction
  clauses on conduct, state and message observables no test grades. R6 to R14 are the
  hard executed evidence block. positive_total of the rubric is 30, negatives sum to
  -25 across seven negatives (R3, R5, R10, R11, R12, R13, R15). R6 and R7 prove the
  numeric output is correct.
- R1 grades one demonstration act: a single command whose printed output both lists
  the deliverables at their paths and shows one record in the required shape, so it is
  a single observable rather than a compound criterion. The record shape correctness
  itself is graded only in Channel A, so R1 stays conduct only.
- R15 grades output space hygiene as a default deny negative on the output directory
  state, firing when a file beyond the twenty nine declared deliverables is left. It
  backs the clean output clause in instruction.md that no test enumerates, so it adds
  no test to rubric mirror and no easy positive; an honest agent that cleans up never
  trips it, so it can only lower a sloppy run and never raises the score.
- R6 and R7 are held at score five deliberately. They are the mandated numeric
  correctness proofs (run the adversarial floor over cap fixture and print the
  divergence; recompute a rounding tie canonical count and print it beside the shipped
  value). Every outcome figure is graded in Channel A only, so these conduct scores
  outweigh no outcome criterion; lowering them would shrink the positive denominator
  and raise the rubric percentage for a time boxed agent that fails them, so they are
  confirmed at five.
- Default deny ace guards: R5 and R10. Named naive path negatives: R11, R12, R13.

## Measured gates

- Oracle 1.000000, empty 0.000000 on the grader logic in tests/test_outputs.py. A
  structurally clean but numerically partial attempt stays far below the passing
  range, well under the 72 / 267 = 0.270 easy ceiling.
- The reference oracle in solution/solve.py is an independent implementation written
  from the specification documents. It shares no derivation code with the verifier
  (line overlap under 5 percent), so its agreement with the grader is real evidence
  the task is solvable rather than a copy scoring itself.
- Determinism: two subprocess runs on identical bytes are byte identical.
- Mutant bank (four load bearing rules): perturbing the protected floor, unit
  conversion, freshness factor or priority factor each drops the score below 0.5.
- Trigger ablation bank (eleven rules): removing any one moves the score off 1.000.
- Grading executes the delivered program only as an unprivileged subprocess: the
  verifier image installs util-linux and drops to a non-root user with setpriv, so
  agent code cannot read the answer key under /tests or write the root-owned reward
  directory.

## Solvability

Every graded value is reachable from instruction.md plus environment/data/. The
specification documents carry the exact stage order, arithmetic, the quantization
rule, tie breaks, precedences, boundary conventions and serialization, and
artifact_contract.md fixes every required output literal the grader insists on: the
four invariant identifiers and their order, the BATCH0001 style batch id format, and
the above_floor and floor_cut binding kind tokens. No graded value depends on
anything outside the agent files.
