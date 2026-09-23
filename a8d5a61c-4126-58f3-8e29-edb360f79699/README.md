# a8d5a61c-4126-58f3-8e29-edb360f79699

Maintainer notes for the task bundle. Never shown to the agent.

## Task Metadata

- Difficulty: medium
- Category: Optimization
- Tags: allocation, precedence, rounding, determinism, integrity
- Expert time: 420 minutes
- Junior time: 1320 minutes
- Agent timeout: 7200 seconds
- Verifier timeout: 3600 seconds
- Inputs: 22 flat files under environment/data/
- Outputs: 29 declared artifacts, all absolute under /app/out/

## Difficulty explanation

The difficulty is a stateful two pass allocation compiler, all of it written down. A
training mixture certificate is derived from a twenty two file manifest snapshot
through a fourteen stage serial chain, and every artifact hangs off one spine, the
per source per phase integer token allocation. The weight five tests read that spine
or a rollup of it through a different facet, so one wrong canonical count, one wrong
rounding, one wrong displacement or one missed reconciliation cascades into many
downstream figures.

The chain has a serial two pass shape with two convergence points. A source that
competes in two phases is the locally correct and globally wrong stage: each phase allocates it a
share that is right where it is made, and only the global reconciliation exposes that
the two shares together exceed the token supply the source actually holds. The
proportional shares and the protected floors are a convergence point for the capacity
binding, and the precedence closure and the surviving cycle order are a convergence
point for the carry state and the reconciliation. Records resolve only across
sources, with quality, freshness and the six policy documents split across separate
files.

The mainline needs edge rules. On the shipped snapshot capacity binds in a phase and
is repaired by ranked displacement rather than a clip, the curriculum carries a cycle
that is broken by the hardness ranking, and a shared source is clawed back during
global reconciliation. A run that only implements the smooth proportional path
produces a plan that disagrees with the certificate. A pervasive round half even
quantization applies at every named stage, so a solver that truncates or rounds once
at the end diverges across many rows.

The deliverable is a program, rerun by the verifier on three reseeded snapshots and
seven hand built fixtures the agent never saw, carrying legal states the shipped
snapshot does not contain, so a run that only fits today's numbers earns the
structural weight and none of the hard core.

Designed ceiling: twelve weight one tests and twenty weight three tests give an easy
total of seventy two against a hard total of one hundred ninety five, so the weight
layout caps a structurally clean but numerically partial attempt at 72 / 267 which is
0.270. That is an upper bound rather than a forecast. The authored partial solver
measures 0.165, which is the realistic structural floor.

## Solution explanation

The reference compiler lives at solution/solve.py. It is an independent
implementation written from the specification documents, sharing no derivation code
with the verifier, so the oracle run proves the task is solvable rather than scoring
its own copy. It canonicalizes raw units into
integer tokens under the round half even rule, projects eligibility, scores demand
through four quantized steps, derives per phase proportional shares, applies protected
floors, binds capacity by ranked displacement, closes the curriculum precedence and
breaks its cycle, advances the carry state across phases in topological order,
reconciles sources that two phases over allocated, materializes per example integer
weights, builds deterministic microbatches, and emits twenty eight documents before
hashing them into the manifest and folding the manifest into the chain.

solution/solve.sh runs the program against /app/data, publishes it at its declared
artifact path afterwards, then self checks by recomputing every manifest digest from
disk, refolding the chain, confirming the shipped snapshot actually bound capacity,
carried a cycle and needed reconciliation, and rerunning the delivered program into a
scratch directory to confirm byte identical output.

## Verification explanation

Channel A is seventy five tests in tests/test_output.py, twelve at weight one, twenty
at weight three, thirty nine at weight five, and four negative probes. The positive
total is 267. The verifier owns its corpora: test_output.py rebuilds the shipped
sample tier from SAMPLE_SEED and generates three further reseeded tiers plus seven
hand built fixtures at grade time in temporary directories, so nothing under tests/
ever reads /app/data, which does not exist in the verifier container. The delivered
program is only ever executed as a subprocess with the isolated interpreter flag from
a foreign working directory, never imported. The verifier image installs util-linux
and runs that subprocess under setpriv as a non-root user, so hostile agent code
cannot read the answer key under tests/ or write the root-owned reward directory even
from a backgrounded child that outlives the run.

Most weight five tests require the program to be rerun on a snapshot the solver never
saw: the reseeded tiers drive the whole artifact set and the fixtures each stage a
legal state the shipped snapshot never shows, a zero quality denominator, a single
example source, an empty example group, protected floors above a cap, an allocation
that meets a cap exactly, a two node cycle and a language with no eligible source. The
four negative probes are polarity inverted, so honest work makes them fail and no
penalty applies, and each keys on the cheat rather than on a symptom an honest partial
attempt would also show.

Channel B is fifteen rubric criteria, every one about conduct rather than output:
demonstrating in one command that the deliverables were written at their paths in the
required record shape, showing a byte identical rerun, keeping the data read only,
honest reporting that agrees with the files, running a forced floor over cap fixture
and naming the displacement rule, recomputing a rounding tie canonical count, breaking
a two node cycle, refolding the digest chain, running a second reseeded snapshot, and
several default deny and named naive path probes. Output space hygiene is graded by a
default deny negative that fires when an extra file is left in the output directory,
so the clean output requirement in the instruction is covered without adding an easy
positive that a tidy agent would meet. R6 and R7 stay at score five on purpose: they
are the mandated proofs that the numeric output is correct, and because every outcome
figure is graded only in Channel A the conduct score outweighs no outcome criterion.
No criterion asserts a produced figure, so there is no observable a test and a
criterion both read; the hygiene negative reads the output directory state, which no
test enumerates.

## Additional Notes

The fourteen generated input files are reproduced byte for byte by
`python3 tests/test_output.py environment` run from the bundle root, which is how the
shipped bytes were produced. The eight specification documents under environment/data/
are authored by hand and are not regenerated. Seeds: SAMPLE_SEED reproduces the
shipped snapshot; the reseeded rerun tiers and the fixtures never leave tests/.
