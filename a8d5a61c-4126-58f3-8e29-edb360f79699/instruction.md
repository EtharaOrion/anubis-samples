We are standing up the data mixture compiler for a fine tune of a small multilingual
industrial safety model. Before each run the training team wants a reproducible
allocation certificate rather than an opaque sampler, so given the source manifests,
the policy constraints, the curriculum dependencies and the token budgets, we need
the exact weighted training plan, the phase allocations and the batch manifests
derived deterministically, without doing any model optimisation. We want the compiler
rebuilt properly, and we want a program we can hand next week's manifests to rather
than a one off answer.

Everything the job depends on sits in `/app/data`. That directory holds the source,
quality, freshness, example, capacity, carry state, policy and curriculum records,
and the written specification that says how the plan is derived. The specification is
spread over several documents in that same directory. Read them before you write
anything. They carry the whole rule set, the order the stages run in, the exact
integer and rational arithmetic, the quantization rule that applies at every stage,
the tie breaks, the precedence that can overturn a locally sensible allocation, and
the exact shape and key names of every file you have to produce. Nothing about this
job is decided anywhere else, and none of the shipped files is decoration.

What we want at the end is a program plus the twenty eight files it derives from the
shipped manifests. The program goes at `/app/out/plan.py`. We will run it as
`python3 /app/out/plan.py <DATA_DIR> <OUT_DIR>` against next week's manifests and the
week after, so it has to read whichever data directory it is handed and write into
whichever output directory it is handed. It must not pin the shipped data path inside
its own source, it must not behave differently because a directory was given another
name, and two runs over the same input bytes have to produce the same output bytes.
Solve this using only the Python standard library and deliver a single program file.
Treat everything under the data directory as read only and leave it exactly as you
found it. Write each derived record as one JSON object per line with keys in sorted
order where the specification asks for a lines file, and as a sorted key indented
object where it asks for a single object.

The twenty eight derived files all sit under `/app/out` as well:

```
/app/out/canonical_sources.jsonl
/app/out/eligibility_ledger.jsonl
/app/out/demand_score_ledger.jsonl
/app/out/phase_membership.jsonl
/app/out/proportional_allocation.jsonl
/app/out/protected_floor_ledger.jsonl
/app/out/capacity_binding_trace.jsonl
/app/out/precedence_closure.jsonl
/app/out/cycle_report.jsonl
/app/out/cycle_break_decisions.jsonl
/app/out/precedence_overturn_trace.jsonl
/app/out/carry_state_ledger.jsonl
/app/out/pre_reconciliation_ledger.jsonl
/app/out/global_reconciliation_trace.jsonl
/app/out/final_allocation.jsonl
/app/out/language_allocation_summary.jsonl
/app/out/safety_allocation_summary.jsonl
/app/out/source_allocation_summary.jsonl
/app/out/rational_example_weights.jsonl
/app/out/fixed_point_weights.jsonl
/app/out/rounding_residual_ledger.jsonl
/app/out/microbatch_manifest.jsonl
/app/out/phase_token_totals.jsonl
/app/out/invariant_report.jsonl
/app/out/exception_trace.jsonl
/app/out/objective_tuple.json
/app/out/integrity_manifest.json
/app/out/final_state.json
```

Each one is checked whole and byte for byte, so the serialization rules in the
specification count for as much as the arithmetic does.

One thing worth knowing up front, because it has caught people here before. The
manifest set you can run locally is one snapshot out of many. Next week's has a
different shape, and the specification writes down states this one happens not to
contain at all, among them a source group that contributes nothing, a source with a
single example, a language with no eligible source, a quality denominator that comes
out zero, an allocation that meets a cap exactly, protected floors that alone exceed
a cap, and a two node curriculum cycle. Every one of those is legal and the
specification says what each one does. A run that balances on the shipped snapshot is
a start and a long way from the finish. Read for the rule, build for the rule, then
convince yourself the rule holds rather than checking only that today's numbers add
up.

Write the code yourself from the specification in the data directory. Please do not go
looking online for a ready made answer to this. It is our own format and anything that
turns up will be a different problem wearing similar words.

When you finish, the output directory must hold exactly the deliverables named here
and nothing else, so remove any scratch file, backup, note or log you created there.
Close with a short message stating the concrete numbers you computed, the total
allocated tokens, how many global claw backs and cycle edge removals happened and the
final objective tuple, and they must agree with the files you wrote. Say plainly what
you checked by running it and what you checked only by reading. An honest gap is far
more useful to us than a confident claim that turns out wrong next week.

You have 7200 seconds to complete this task.
