# Example weight, materialization and microbatch rules

## Stage 11, rational example weights

The token total of a source is the sum of its final allocation across every phase.
Order the examples of a source by ascending example id and let the group
denominator be the sum of their token lengths. The rational weight of an example is
the total times the example token length over the group denominator, written as a
fully reduced fraction with a positive denominator. A source whose group
denominator is zero is an empty group and contributes no rational weight row. A
source with no examples contributes no row. `rational_example_weights.jsonl` holds
one row per contributing example with `w_num` and `w_den`, in canonical source
order and ascending example id within a source.

## Stage 12, integer weight materialization

Split the token total of a source across its examples in proportion to token length
by the largest remainder method, breaking a remainder tie by ascending example id.
An empty group, a source with no examples or a source with a zero token total
materializes zero for each of its examples. `fixed_point_weights.jsonl` records the
materialized token count for every example, and `rounding_residual_ledger.jsonl`
records each example that received a leftover unit above its base quotient, with the
base token count and the single residual unit.

## Stage 13, microbatch construction

For each phase in phase order take the member sources with a positive final
allocation in ascending source id, and within each source the examples with a
positive materialized token count in ascending example id. Pack these examples into
batches in that fixed order. Start a new batch when adding the next example would
push the running sequence token total above `max_seq_tokens` in `batch_rules.json`,
or when the next example would make more than `adjacency_limit` consecutive members
come from the same source. A single example whose materialized token count exceeds
the sequence cap stands as its own batch. Each row in `microbatch_manifest.jsonl`
carries a `batch_id` counting up from one across the whole run, the phase, the list
of member example ids in packing order, and the sequence token total of the batch.

## Summaries and objective

`language_allocation_summary.jsonl`, `safety_allocation_summary.jsonl`,
`source_allocation_summary.jsonl` and `phase_token_totals.jsonl` roll the final
allocation up by language, by safety stratum, by source and by phase. A language
minimum required is `round_half_even(min_share_bp * total_tokens, scale)` and the
maximum allowed uses `max_share_bp`, where the total tokens is the sum of the final
allocation. The objective tuple lists in order the total allocated tokens, the
count of global claw backs, the count of removed cycle edges, the count of capacity
displacement events and the count of microbatches.
