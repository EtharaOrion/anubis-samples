# c4a9a9b2-0818-5b05-9829-3a208c74638b

## Task Metadata

- **Difficulty:** `medium`
- **Category:** `Security`
- **Tags:** ["python", "cli", "configuration", "security", "admission-control", "hardening", "derivation"]
- **Expert time:** 8 hours (480.0 min)
- **Junior time:** 24 hours (1440.0 min)
- **Agent timeout:** 2 hours (7200s)

## Difficulty explanation

This task is a container-image admission-control hardening configuration-repair
exercise built around an offline hardening-baseline compiler. The agent receives a
stdlib-only Python command in `/app/data/tool.zip` (`task-ctl`) whose
derivation logic is faulty in ten independent, interacting ways: it rounds
half-up instead of half-even, floors on the rate-limit granularity step instead
of rounding, reads the first row of every table instead of the authoritative
row for the active enforcement class, folds the hardening controls while
ignoring the carry rule, counts every voter toward quorum instead of only the
healthy adopted voters, takes the maximum granted UID for the privilege drop
instead of the minimum, applies the profile scale after quantization instead of
before, emits a nested `admission` object instead of a flat configuration, and
leaves the subcommand optional. The authoritative key list also disagrees in
shape between the two schema surfaces.

Every numeric rule and every constant lives across the heterogeneous data files
in `environment/data`, each densely populated with superseded, draft, shadow,
and revoked decoy rows. The single active enforcement class is
`((max_records // 37) + 3 * retry_limit) % 5`, and only the row whose class
matches and whose status is `ADOPTED` may be bound; every other row is a trap. A
solver that copies a plausible-looking row, keeps floating-point arithmetic,
ignores the carry rule in the fold, or trusts the shipped sample profiles
instead of re-deriving them fails the graded numeric equalities.

## Solution explanation

The reference solution extracts and repairs the CLI so that it rounds half-even with integer
remainders only, rounds on the granularity step, binds the adopted row for the
active class, folds the hardening controls honouring the carry subtraction,
counts only healthy adopted voters toward quorum, drops to the minimum granted
UID subject to the floor, scales the profile before quantization, requires a
subcommand, and enforces a flat single-object configuration. It then drives the
repaired tool to initialize, set each spine key, validate, and export a truthful
command report, before packaging the repaired tool and writing every derivation
sidecar. The canary and enforce profiles are re-derived from the documented
per-mille TTL scale and the signed quorum and risk deltas rather than read from
the context samples.

## Verification explanation

`tests/test_output.py` declares 33 tests (29 positive, four negative). Positive
tests cover exact equality of the derived configuration against the integer
reference, the active-class selection, the rate-limit and burst chain, the
scope reconciliation and minimum privilege-drop UID, the carry-aware hardening
fold, the risk and quorum thresholds, the layered precedence resolution honouring
the pin flag, the TLS floor / seccomp count / retention days, the canary and
enforce profile derivations and their signed delta report, the fold trace and
ledgers, validation-report and summary coverage, reproducibility stability,
artifact existence and permissions, the canonical trailer,
and evidence that the CLI was genuinely repaired rather than bypassed. The four
negative tests target hardcoded answer constants, nested or dynamic configuration
shapes, fabricated command transcripts, and network or secret material.

The rubric grades the reasoning trajectory and response, covering diagnosis of
the ten defects, active-class discrimination against decoys, cross-layer
dependency reasoning, and offline discipline, and is deliberately disjoint from
the artifact and numeric-state checks owned by the pytest suite.

## Additional Notes

- Every graded value is derivable from the agent-visible files alone; the
  verifier reads no answer file and re-derives nothing from a hidden seed.
- The task environment contains a flat `environment/data` directory of heterogeneous
  input files plus `tool.zip`, with no input subdirectories, and the solution
  emits 24 output artifacts under `/app/out`.
