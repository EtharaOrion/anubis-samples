# fb2f26ec-6595-5389-8b6d-36e52ebf086b

## Task Metadata

- **Difficulty:** `hard`
- **Category:** `Machine Learning` (internal taxonomy: `ML` / `Inference`)
- **Tags:** ["python", "inference", "batch-scoring", "aggregation", "recovery", "relay", "stdlib"]
- **Expert time:** 11 hours (660.0 min)
- **Junior time:** 44 hours (2640.0 min)
- **Agent timeout:** 6 hours (21600s)

## Difficulty explanation

The scoring notes for the Relay batch scorer survive only in part. Seven scoring
decisions were lost, and the pages that replace them name the decision each
section governed while saying nothing about how it was settled. Nothing in the
container states any of the seven, and nobody recorded how many were lost.

They are recoverable one way only. Six archived batches ship with a seal over the
scoreboard and the rows the gateway produced for them, and never the scoreboard
itself. The seal is a 64-bit FNV-1a hash over the canonical scoreboard-plus-rows,
so a candidate reading either seals or it does not, with nothing to say which of
the seven decisions is wrong. The agent has to build a scorer whose seven
decisions can be varied, then search the combinations until the archive leaves one
standing.

The search space is 2^7 = 128 combinations. Exactly one of them seals to all
seven archived batches (the six original ones plus keystone, which inverts the
usual routes-smaller-than-items ratio and pins the routing side directly), and
it is the reference reading. A candidate can only be tested once every surviving
section is already implemented exactly, and a slip anywhere reports only that
nothing seals. The seven lost decisions are:

1. which of the two relations the scorer drives the routing from (§5)
2. whether the abstain floor runs before routing or after (§4)
3. whether a bucket that ends with no surviving item still reports (§6)
4. how the reported mean becomes a whole number (§7)
5. where a bucket with a blank mean sorts (§9)
6. how two buckets holding the same mean order between themselves (§9)
7. whether the confidence is the sum of winning logits or the sum of margins (§8)

The scorer shipped under `/app/data` runs but carries the wrong reading on every
one of the seven, so it seals to none of the seven archived batches. The recovered
scorer must also generalise: the verifier scores held-out batches nobody archived,
so a reading that fits the seven seals by coincidence rather than the true settlement
still fails.

## Solution explanation

The reference scorer in `solution/solve.py` carries the recovered reading:
smaller relation drives, the floor abstains before routing, empty buckets still
report, the mean rounds half to even, blank means sort first, equal means order by
the larger join identifier, and the confidence is the sum of item margins. The
oracle dispatcher `solution/solve.sh` installs it at `/app/out/scorer.py`, confirms
it seals to all seven archived batches, then writes the six work scoreboards.

The six declared work scoreboards plus the scorer are the seven collected
artifacts. No scoreboards ship with the bundle beyond the seven sealed archive
batches (which ship a seal, not a scoreboard); the verifier reseeds its own
archive, work and held-out corpus and grades against an independent scoring.

## Verification explanation

`tests/test_output.py` carries an independent scoring of the catalogue (never
imported from `solution/`), the FNV seal, a deterministic seeded corpus builder,
and a sandboxed runner that executes the delivered scorer as an unprivileged user
under an audit hook that blocks any read of `/app/data`. `tests/test.sh` runs each
weighted test, sums positive weights on pass, and computes
`reward = raw / positive_total` clamped to `[-1, 1]`.

Channel A declares 17 positive tests (weight total 73) and 5 negative tests
(weight total -21). The load-bearing tests are the seal reproduction over the
archive and the exact match over the six work batches and twelve held-out batches,
each weight 5. Three low-weight tests confirm delivery, whole-number means, and
that each delivered scoreboard is what the delivered scorer writes.

The five negative tests fire, with inverted polarity, when the scorer replays a
hardcoded answer, keys on the batch filename, reads `/app/data` at runtime, emits
different scoreboards across runs, or rebinds the stdlib names grading relies on.

Process, not scoreboard bytes, is graded by `tests/rubric.json` (14 criteria, five
of them instruction-following). No rubric criterion reads an observable a
deterministic test reads.

## Measured scores

No Docker harbor run for this rebuilt task has been recorded here yet; treat the
figures below as **UNVERIFIED ESTIMATES** from a local mirror of the reward math
until a real harbor run replaces them.

- reference `solution/solve.py`: seals to all seven archived batches and matches
  the reference scoring on every work and held-out batch (expected reward `1.000`)
- shipped `/app/data/scorer.py` (the broken starting point): seals to zero of the
  seven archived batches (the task is genuinely unsolved by the starting point)
- of the 128 possible settlement combinations, exactly one seals to all seven
  archived batches, so the seven seals pin the reading uniquely (the added
  keystone batch inverts the routes/items ratio and pins the routing side)

## Additional Notes

- `environment/data/` ships the two written documents (scoring spec, archive
  notes), the broken scorer, seven sealed archive batches with their seals, and
  six unsealed work batches: 23 flat files.
- `SAMPLE_SEED`, `CHALLENGE_SEED`, `EVAL_SEED` and the per-batch seeds live only in
  `tests/test_output.py`. The verifier rebuilds the corpus into a temporary
  directory on every run so grading works under harbor's separate-mode verifier
  container even though `/app/data/` is not propagated.
- Replace the Measured scores anchors only after a real oracle/nop harbor run.
