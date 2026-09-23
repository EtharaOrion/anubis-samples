# Artifact contract

The program writes its own source at `plan.py` and twenty eight derived files, all
into the output directory it is handed. Each file is checked whole and byte for
byte, so the serialization rules count for as much as the arithmetic. The JSONL row
key sets are listed here; keys are serialized in sorted order regardless of the
order listed.

## JSONL files and their row keys

`canonical_sources.jsonl` with `source_id`, `raw_amount`, `raw_unit`,
`canonical_tokens`, `rounding_tie`, in canonical source order.

`eligibility_ledger.jsonl` with `source_id`, `eligible`, `code`, in canonical
source order.

`demand_score_ledger.jsonl` with `source_id`, `quality_bp`, `fresh_bp`,
`priority_factor`, `lang_balance_bp`, `demand_score`, eligible sources only.

`phase_membership.jsonl` with `phase`, `source_id`.

`proportional_allocation.jsonl` with `phase`, `source_id`, `target_tokens`,
`share_num`, `share_den`, for every member source per phase in phase then source
order.

`protected_floor_ledger.jsonl` with `phase`, `source_id`, `safety_floor`,
`language_floor`, `floor`.

`capacity_binding_trace.jsonl` with `phase`, `source_id`, `before`, `after`,
`displaced`, `kind`, one row per reduction in the order the reductions happen.
The `kind` field is the string `above_floor` when the reduction cut a source that
still sat above its floor, and the string `floor_cut` when the reduction cut into
the floor itself.

`precedence_closure.jsonl` with `phase`, `requires`, in sorted phase then sorted
requires order.

`cycle_report.jsonl` with `cycle_nodes`, a sorted list of phase names.

`cycle_break_decisions.jsonl` with `edge_id`, `from`, `to`, `hardness`.

`precedence_overturn_trace.jsonl` with `phase`, `prereq_phase`, `source_id`.

`carry_state_ledger.jsonl` with `phase`, `credit_in`, `debt_in`, `credit_out`,
`debt_out`, in topological phase order.

`pre_reconciliation_ledger.jsonl` with `phase`, `source_id`, `tokens`, the stage 6
allocation before reconciliation.

`global_reconciliation_trace.jsonl` with `source_id`, `phase`, `before`, `after`,
`clawed`.

`final_allocation.jsonl` with `phase`, `source_id`, `tokens`, the reconciled spine
in phase then source order.

`language_allocation_summary.jsonl` with `language`, `tokens`, `min_required`,
`max_allowed`.

`safety_allocation_summary.jsonl` with `stratum`, `tokens`, `floor`.

`source_allocation_summary.jsonl` with `source_id`, `tokens`, `canonical_tokens`.

`rational_example_weights.jsonl` with `example_id`, `source_id`, `w_num`, `w_den`.

`fixed_point_weights.jsonl` with `example_id`, `source_id`, `materialized_tokens`,
one row per example in canonical source order and ascending example id.

`rounding_residual_ledger.jsonl` with `example_id`, `source_id`, `base_tokens`,
`residual_unit`.

`microbatch_manifest.jsonl` with `batch_id`, `phase`, `members`, `seq_tokens`. The
`batch_id` is the string `BATCH` followed by the batch number zero padded to four
digits, counting up from one across the whole run, so the first batch is `BATCH0001`.

`phase_token_totals.jsonl` with `phase`, `allocated`, `cap`, `budget`.

`invariant_report.jsonl` with `invariant`, `holds`. It carries exactly four rows in
this order, each `invariant` naming the property and `holds` the boolean result:
`phase_within_cap` (every phase allocation stays within its effective cap),
`source_within_supply` (no source total exceeds its canonical token supply),
`materialized_sums_match` (each non empty source materializes exactly its token
total across its examples) and `batches_respect_seq_cap` (every batch is within the
sequence cap or holds a single example).

`exception_trace.jsonl` with `code`, `ref`, one row per quiet branch activation in
the order the stages activate them.

## JSON files

`objective_tuple.json` with `objective` a list of five integers and `order` a list
of the five component names.

`integrity_manifest.json` mapping every derived file name and `plan.py` to its
lowercase hex sha256 digest.

`final_state.json` with `chain`, `total_tokens`, `global_clawbacks`,
`capacity_displacements`, `cycle_edges_removed`, `precedence_overturns`,
`microbatches`, `examples_placed`, `quiet_branches`, `objective`, and
`invariants_hold`.
