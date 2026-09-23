# Allocation, binding, precedence and reconciliation rules

## Stage 4 proportional share

Within a phase let the demand sum be the total of the demand scores of the member
sources with a positive demand score. The proportional target of such a source is
`round_half_even(demand_score * budget, demand_sum)`, where the budget is the phase
`budget_tokens` in `capacity.csv`. The share numerator is `demand_score * budget`
and the share denominator is the demand sum; both are recorded so the quantization
is checkable. A member with a zero demand score, or a phase whose demand sum is
zero, has a target of zero. Targets are per source and independent; they are not
rescaled to sum to the budget, because the capacity binding in stage 6 is what
reconciles the phase to its capacity.

## Largest remainder split

To split an integer total across a set of keys in proportion to a positive weight
per key, give each key the integer quotient of the total times its weight over the
weight sum, then hand the leftover out one unit at a time to keys in descending
remainder order, breaking a remainder tie by ascending key. A zero weight sum or a
zero total gives every key zero. This split is used in stage 12 to materialize a
source token total across its examples in proportion to example token length, with
the example id as the tie break key.

## Protected floors

The floor of a member source in a phase is the larger of the safety stratum
`floor_tokens` in `safety_policy.json` and the language `floor_tokens` in
`language_policy.json`. The floored allocation of a member source is the larger of
its proportional target and its floor.

## Effective capacity and binding displacement

The effective capacity of a phase is its `cap_tokens` in `capacity.csv` plus the
credit carried in minus the debt carried in. When the sum of floored allocations in
a phase exceeds its effective capacity, the excess is removed by displacement, not
by scaling everyone down. Order the member sources by ascending stratum
displacement rank in `priority_order.json`, then by priority class with the larger
class number first, then by ascending source id. Walking that order, reduce each
source from its floored allocation down to but not below its floor, taking as much
of the remaining excess as the room allows, until the excess is gone. If excess
remains after every source sits at its floor, the floors themselves are cut in the
same order, each down toward zero, until the effective capacity holds. Every reduced
source is written to `capacity_binding_trace.jsonl` with its before amount, after
amount, the displaced quantity and whether the cut was above the floor or into the
floor.

## Precedence closure, cycles and overturn

Take the transitive closure of the edges in `curriculum.json`. A phase that is
reachable from itself sits on a cycle. Among the edges whose endpoints are both
cycle phases, rank by hardness with soft below hard, then by the from phase, then
by the to phase, then by the edge id, and remove the single lowest ranked edge.
Record the cycle phases in `cycle_report.jsonl` and the removed edge in
`cycle_break_decisions.jsonl`. A member source that also belongs to a phase it
requires through the closure, where the required phase allocated it nothing while
the requiring phase allocated it a positive amount, is a precedence overturn and is
written to `precedence_overturn_trace.jsonl`.

## Carry state

Process the phases in the topological order of the edges that survive the cycle
break, breaking ties by phase name. The credit carried into the first phase is its
`initial_credit` in `carry_state.csv` and the debt carried in is its
`initial_debt`. After a phase is allocated, its unused effective capacity is the
credit carried into the next phase and its deferred floor total is the debt carried
into the next phase; both add to whatever that next phase already carries.
`carry_state_ledger.jsonl` records the credit in, debt in, credit out and debt out
for every phase.

## Global reconciliation

The canonical token supply of a source is its stage 1 canonical token count. When a
source competes in two phases the sum of its phase allocations can exceed that
supply. For each source whose summed allocation exceeds its supply, remove the
excess by clawing back from its phases in descending topological order, the later
phase first, until the sum equals the supply. Each claw back is written to
`global_reconciliation_trace.jsonl`. The reconciled matrix is the final allocation.
