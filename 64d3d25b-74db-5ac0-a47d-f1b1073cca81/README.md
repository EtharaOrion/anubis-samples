# 64d3d25b-74db-5ac0-a47d-f1b1073cca81

Maintainer notes for the task bundle. Never shown to the agent.

## Task Metadata

- Difficulty: hard
- Category: Data Processing
- Tags: derivation, precedence, determinism, geometry, integrity
- Expert time: 480 minutes
- Junior time: 1440 minutes
- Agent timeout: 7200 seconds
- Verifier timeout: 3600 seconds
- Inputs: 27 flat files under environment/data/
- Outputs: 25 declared artifacts, all absolute under /app/out/

## Difficulty explanation

The difficulty is a stateful derivation graph, all of it written down. A travelling
exhibition installation dossier is derived from a twenty seven file snapshot through a
fourteen stage serial chain, and every artifact hangs off one spine, the per object
installation state. The weight five tests read that spine or a rollup of it through a
different facet, so one wrong unit conversion, one wrong boundary reading, one missed
displacement or one wrong cycle break cascades into dozens of downstream figures.

The chain has a serial-derivation shape. The conservation pass is the locally
correct and globally wrong stage, since a geometry only placement is valid where it is
made and only the conservation pass reveals a bay is too bright. The dependency graph
and its cycle resolution are a convergence point, since the resolved order needs both
the placement and the edge set and each needs the earlier state. Records resolve only
across sources, with a stale duplicate master row for one object discarded by the first
occurrence rule while its version is read from a separate version map, and object fields
split over six files, and the crate assignment consumes a shared stock in installation
order so a later object sees only what earlier installs left.

Two levers keep a partial reader out of the top band. The shipped snapshot is live for
both repairs, with sixteen dependency cycle edges removed and nine crate displacements,
so an agent that stops at the naive per object plan fails visibly rather than silently.
And the deliverable is a program, rerun by the verifier on twenty reseeded snapshots and
seven hand built fixtures the agent never saw, carrying legal states the shipped snapshot
does not hold, so a run that only fits today's numbers earns the structural weight and
none of the hard core.

The structural ceiling is the weight layout: twelve
weight one tests and twenty weight three tests give an easy total of seventy two against
a hard total of two hundred, so a structurally clean but numerically partial attempt
tops out at 72 / 272 which is 0.265, an upper bound rather than a forecast.

## Solution explanation

The reference planner lives at solution/solve.py. It reads the snapshot onto canonical
keys and converts units, derives clearance and handling envelopes and orientation
admissibility, selects a geometry placement then overturns it under conservation,
assigns shared crates in installation order under the contention rule, derives transport
legs, builds the dependency graph and resolves its cycles with the deterministic edge
removal, orders the install, computes crew, rounded setup and reconciled windows, prices
the penalties and insurance, and emits twenty two derived documents before hashing them
together with the program into the manifest and folding the manifest into the chain.

solution/solve.sh runs the program against /app/data, publishes it at its declared
artifact path afterwards, then self checks by recomputing every manifest digest from
disk, refolding the chain, confirming the cycle resolution converged and that the graph
was not already acyclic, confirming a displacement happened, and rerunning the delivered
program into a scratch directory to confirm byte identical output.

## Verification explanation

Channel A is seventy six tests in tests/test_output.py, twelve at weight one, twenty at
weight three, forty at weight five, and four negative probes. The positive total is 272.
The verifier owns its corpora: test_output.py rebuilds the shipped sample tier from its
seed and generates twenty further reseeded tiers plus seven hand built fixtures at grade
time in temporary directories, so nothing under tests/ ever reads /app/data, which does
not exist in the verifier container. The delivered program is only ever executed as a
subprocess with a timeout, never imported. Every derived artifact is graded exactly once
per tier, the visible tier through per artifact tests that confirm both the delivered
artifact and a rerun of the delivered program on the sample, and each reseeded tier
through one whole document conjunction test, so no observable is paid for twice.

Most weight five tests require the program to be rerun on a snapshot the solver never saw:
the reseeded tiers drive the whole artifact set and the fixtures each stage a legal state
the shipped snapshot never shows, a zero width scarcity sentinel, a single legal
orientation, a footprint exactly on the exclusive depth boundary, a crate class emptied
before an object reaches it, a bright bay placement overturned by conservation, a cycle
completed only by a conservation edge, and a setup exactly halfway between quanta. The four
negative probes are polarity inverted, so honest work makes them fail and no penalty
applies, and each keys on the cheat rather than on a symptom an honest partial attempt
would also show. The filename probe checks that each directory name is stable across
repeated runs before it fires, so a nondeterministic program is caught by the determinism
probe alone and never penalised twice for one fault.

Channel B is sixteen rubric criteria on conduct, reporting, ordering and state changes rather
than produced values. Five instruction following constraints lead it: no writes under the data
directory, the closing figures agreeing with the delivered files, a clean output directory,
reading the specification before writing the program, and a report that separates run confirmed
results from read confirmed results. The rest are executable evidence positives for inspecting
deliverables, rerunning byte-identically, checking a boundary case, recomputing integrity
information and inspecting crate contention, plus factuality negatives covering unsupported
execution claims and incorrect placement, crate and setup reports. No criterion asserts a
produced figure, so there is no observable a test and a criterion both read.

## Additional Notes

To regenerate the twenty two generated input files, run
`python3 tests/test_output.py environment` from the bundle root; it writes them byte for
byte. The five specification documents under environment/data/ are maintained by hand. The
reseeded rerun tiers and the fixtures never leave tests/.
