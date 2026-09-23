# Reason and branch codes

## Eligibility codes

A source carries exactly one eligibility code, the first predicate it fails in the
stage 2 order, or `E_OK` when it passes them all.

`E_OK` the source is eligible.
`E_LOW_QUALITY` the quality ratio is below the minimum, or its denominator is zero.
`E_LANG_EXCLUDED` the language is not a key in the language policy.
`E_STRATUM_UNKNOWN` the safety stratum is not a key in the safety policy.
`E_DOMAIN_BLOCKED` the domain is listed as blocked in the quality policy.
`E_EMPTY_TOKENS` the canonical token count is not above zero.

## Quiet branch codes

The exception trace records each quiet branch activation with a code and a
reference, in the order the stages produce them. A run over the shipped snapshot
activates several of these.

`B_UNIT_ROUND_TIE` a stage 1 unit conversion landed on an exact half; the reference
is the source id.
`B_FLOOR_BIND` a phase raised at least one source above its proportional target to
meet a floor; the reference is the phase.
`B_CAP_BIND` a phase invoked capacity binding displacement; the reference is the
phase.
`B_FLOOR_OVER_CAP` a phase had to cut floors themselves because they alone exceeded
capacity; the reference is the phase.
`B_PRECEDENCE_OVERTURN` a source outranked its own prerequisite position; the
reference is the phase.
`B_CYCLE_BREAK` a curriculum cycle edge was removed; the reference is the edge id.
`B_CARRY_DEBT` a phase carried a deferred floor as debt; the reference is the phase.
`B_GLOBAL_CLAWBACK` a source was clawed back to its supply during reconciliation;
the reference is the source id.
`B_EMPTY_GROUP` a source had examples whose token lengths summed to zero and
contributed no weight rows; the reference is the source id.
