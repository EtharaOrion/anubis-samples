# Demand scoring rules

The demand score of an eligible source is an integer. It is built in four quantized
steps, in this exact order, each step rounding half to even over the scale in
`scoring_weights.json`, which is 10000.

First the quality factor in parts of the scale is `round_half_even(q_num * scale,
q_den)` from `quality.csv`.

Second the freshness factor in parts of the scale is `round_half_even(half_life *
scale, half_life + age_days)`, where `half_life` is `half_life_days` in
`scoring_weights.json` and `age_days` comes from `freshness.csv`.

Third the priority factor is the integer weight for the source priority class in
the `priority_weights` map in `scoring_weights.json`. That map assigns a larger
weight to a smaller priority class number.

Fourth the language balance factor in parts of the scale is the `balance_bp` field
for the source language in `language_policy.json`.

The score is then, in order: `s1 = round_half_even(canonical_tokens * quality_bp,
scale)`, then `s2 = round_half_even(s1 * fresh_bp, scale)`, then `s3 = s2 *
priority_factor` with no rounding at that multiply, then `demand_score =
round_half_even(s3 * balance_bp, scale)`. Carrying the exact product through and
rounding once gives a different integer.

The `demand_score_ledger.jsonl` records `quality_bp`, `fresh_bp`,
`priority_factor`, `lang_balance_bp` and `demand_score` for every eligible source,
in canonical source order, and holds no row for an ineligible source.
