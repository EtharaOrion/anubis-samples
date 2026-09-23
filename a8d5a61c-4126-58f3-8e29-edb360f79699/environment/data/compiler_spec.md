# TrainMix allocation compiler specification

This is the master rule set. It defines how the training mixture certificate is
derived from the shipped records through a fourteen stage chain. Every number is an
integer or an exact rational. No floating point value is produced, compared or
serialized anywhere. Read this together with `scoring_rules.md`,
`allocation_rules.md`, `weight_rules.md`, `serialization_rules.md`,
`artifact_contract.md` and `reason_codes.md`. The small illustration is in
`worked_example.md`.

## Objects and identifiers

Sources are `S01`, `S02`, and so on, in the order they first appear in
`sources.csv`. A source carries a language, a domain, a source class, a safety
stratum, a priority class, a raw amount and a raw unit. Languages are the five keys
in `language_policy.json`. Safety strata are the keys in `safety_policy.json`.
Phases are `PH1`, `PH2`, `PH3`, iterated in that order. Examples are `X0001`,
`X0002`, and so on, from `examples.csv`, each tied to one source.

## The pervasive quantization rule

Define the fixed point scale as `scale` in `unit_rules.json`, which is 10000. A
quantization of a value `p` over a positive denominator `d` is
`round_half_even(p, d)`: take the integer quotient and remainder of `p` divided by
`d`, round up when twice the remainder exceeds `d`, and on an exact half round to
the even quotient. This quantization is applied at every stage named below, not
only once at the end. A derivation that keeps exact fractions through several
stages and rounds a single time will diverge from a conforming one. A denominator
of zero never yields a rounded value; the stage that meets it follows the explicit
rule for that case in this document.

## Stage 1, manifest canonicalization

For each source read `raw_amount` and `raw_unit` from `sources.csv`. Convert to
canonical tokens with the rational for that unit in `unit_rules.json`: the
canonical token count is `round_half_even(raw_amount * num, den)` for the unit's
`num` and `den`. Record whether the conversion landed on an exact half. The `tok`
unit is one to one.

## Stage 2, eligibility projection

For each source decide eligibility. Test the predicates in this order and let the
first failing test name the reason code. First the quality ratio `q_num / q_den`
from `quality.csv` is at least `min_quality` in `quality_policy.json`, compared by
cross multiplication, and a `q_den` of zero fails this test. Then the language is a
key in `language_policy.json`. Then the safety stratum is a key in
`safety_policy.json`. Then the domain is not listed in `blocked_domains` in
`quality_policy.json`. Then the canonical token count is above zero. A source that
passes all of these is `E_OK`; only an eligible source is scored and allocated.

## Stage 3, demand scoring

See `scoring_rules.md`. The demand score of an eligible source is an integer built
by four quantized steps in a fixed order.

## Stage 4, phase membership and proportional allocation

Each domain belongs to a fixed set of phases: `maint` to PH1; `manual` to PH1 and
PH2; `incident` to PH2; `qa` to PH2 and PH3; `translated` to PH1 and PH3;
`synthetic` to PH3; `reg` to PH2. A source is a member of every phase its domain
maps to, which is why some sources compete in two phases. Within a phase the phase
budget in `capacity.csv` is split across the member sources with a positive demand
score by the largest remainder method in `allocation_rules.md`.

## Stage 5, protected floors

Each member source has a floor equal to the larger of its safety stratum floor in
`safety_policy.json` and its language floor in `language_policy.json`.

## Stage 6, capacity binding redistribution

This is the binding rule and it is not a simple clip. When the floored allocations
of a phase exceed that phase's effective capacity, displacement removes tokens from
lower precedence strata first, as defined in `allocation_rules.md`. If the floors
alone still exceed capacity the floors themselves are cut in the same order.

## Stage 7, curriculum precedence closure

Build the phase dependency graph from `curriculum.json` and take its transitive
closure.

## Stage 8, cycle break and precedence overturn

Curriculum cycles are legal input. Resolve them by the ranking in
`allocation_rules.md`, removing exactly one edge and recording the decision. A
source whose prerequisite phase received nothing while its dependent phase received
a positive allocation is a precedence overturn and is recorded.

## Stage 9, phase carry state

Process phases in the topological order of the surviving edges. Unused effective
capacity becomes credit carried into the next phase; a deferred floor becomes debt
carried into the next phase. The details are in `allocation_rules.md`.

## Stage 10, global reconciliation

A source that competes in two phases can be allocated locally correct amounts that,
summed, exceed the canonical token supply the source actually holds. Reconciliation
repairs the full ledger by clawing the excess back from the later phase in
topological order first. A per phase result that looked correct where it was made
can become globally wrong here.

## Stage 11 through 14

Example weight derivation, integer weight materialization, microbatch construction
and certificate emission are defined in `weight_rules.md`, `serialization_rules.md`
and `artifact_contract.md`. The reconciled per source and per phase token matrix is
the spine every later artifact hangs from.
