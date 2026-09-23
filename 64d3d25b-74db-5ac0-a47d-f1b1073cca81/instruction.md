We are assembling a travelling museum exhibition out of objects borrowed from
several institutions, and the administrative snapshot we work from is a mess.
Object measurements arrive in different units, one loan record is duplicated, crate
stock is shared and runs out, some galleries are too bright for the more fragile
pieces, and a handful of install dependencies quietly contradict each other. Plans
that look right object by object keep turning out wrong once the whole exhibition is
on the table. We want the installation dossier rebuilt properly from the snapshot,
and we want a program we can hand next season's snapshot to rather than a one off
answer.

Everything the job depends on sits in `/app/data`. That directory holds the loan,
object, gallery, crate, conservation, dependency and policy records, and the written
specification that says how the dossier is derived. The specification is spread over
several documents in that same directory. Read them before you write anything. They
carry the whole rule set, the order the stages run in, the exact integer and rational
arithmetic, the unit scale factors, the inclusive and exclusive fit boundaries, the
shared crate rule that can overturn a choice that was right for one object, the
deterministic cycle break, the pervasive rounding rule, and the exact shape and key
names of every file you have to produce. Nothing about this job is decided anywhere
else, and none of the shipped files is decoration.

What we want at the end is a program plus the twenty four files it derives from the
shipped snapshot. The program goes at `/app/out/plan.py`. We will run it as
`python3 /app/out/plan.py <DATA_DIR> <OUT_DIR>` against next season's snapshot and the
one after, so it has to read whichever data directory it is handed and write into
whichever output directory it is handed. It must not pin the shipped data path inside
its own source, it must not behave differently because a directory was given another
name, and two runs over the same input bytes have to produce the same output bytes.
Solve this using only the Python standard library and deliver a single program file.
Treat everything under the data directory as read only and leave it exactly as you
found it. Write each derived record as one JSON object per line with keys in sorted
order where the specification asks for a lines file, and as a sorted key indented
object where it asks for a single object.

The twenty four derived files all sit under `/app/out` as well:

```
/app/out/canonical_objects.jsonl
/app/out/normalized_geometry.jsonl
/app/out/admissibility_matrix.jsonl
/app/out/bay_candidate_matrix.jsonl
/app/out/selected_placement.jsonl
/app/out/rejected_placement.jsonl
/app/out/conservation_adjusted.jsonl
/app/out/crate_candidate_matrix.jsonl
/app/out/crate_assignment.jsonl
/app/out/crate_capacity_ledger.jsonl
/app/out/crate_contention_report.jsonl
/app/out/transport_leg_manifest.jsonl
/app/out/dependency_edges.jsonl
/app/out/cycle_resolution_audit.jsonl
/app/out/install_order.jsonl
/app/out/crew_requirement_ledger.jsonl
/app/out/install_window_table.jsonl
/app/out/unschedulable_report.jsonl
/app/out/gallery_occupancy_map.jsonl
/app/out/deterministic_audit.jsonl
/app/out/penalty_summary.json
/app/out/objective_tuple.json
/app/out/integrity_manifest.json
/app/out/final_state.json
```

Each one is checked whole and byte for byte, so the serialization rules in the
specification count for as much as the arithmetic does.

One thing worth knowing up front, because it has caught people here before. The
snapshot you can run locally is one exhibition out of many. Next season's has a
different shape, and the specification writes down states this one happens not to hold
at all, among them an object that asks for nothing usable, a footprint that lands
exactly on a bay boundary, a value whose scarcity denominator comes out zero, a crate
class that empties before an object reaches it, and an install window too short for
the setup once the rounding is applied. Every one of those is legal and the
specification says what each one does. A run that balances on the shipped snapshot is
a start and a long way from the finish. Read for the rule, build for the rule, then
convince yourself the rule holds rather than checking only that today's numbers add
up.

Write the code yourself from the specification in the data directory. Please do not
go looking online for a ready made answer to this. It is our own format and anything
that turns up will be a different problem wearing similar words.

When you finish, the output directory must hold exactly the deliverables named here
and nothing else, so remove any scratch file, backup, note or log you created there.
Close with a short message stating the concrete numbers you computed, the scheduled
install count, how many crate displacements the shared capacity rule forced and the
final objective tuple, and they must agree with the files you wrote. Say plainly what
you checked by running it and what you checked only by reading. An honest gap is far
more useful to us than a confident claim that turns out wrong next season.

You have 7200 seconds to complete this task.
