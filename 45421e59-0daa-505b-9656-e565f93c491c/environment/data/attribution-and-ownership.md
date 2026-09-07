# Fault attribution and quarantine settlement

## The two side exports

The runtime probe export, `runtime_probe.csv`, carries the column headings
`module_tag`, `frame_index`, `samples` and `peak_ns`: a sample count and a peak
residency in nanoseconds for a module and a frame index.

The uplink export, `uplink_reports.jsonl`, carries one JSON object per line
under the keys `module_tag`, `frame_index`, `host_id`, `tenant_class` and
`bytes_uploaded`: a host identifier, a tenant class and an uploaded byte count
for a module and a frame index.

Both exports ship in the same three tiers as the reporter export, under the
`calibration_` and `sample_` name prefixes.

A fault frame for which the runtime probe export holds nothing is an
unprobed-frame rejection. A fault frame for which the uplink export holds
nothing is an unreported-dump rejection.

## Attribution tables

The host table, `host_zones.csv`, carries the column headings `host_id` and
`zone`: the deployment zone each host identifier belongs to. The tenant table,
`tenant_classes.csv`, carries the column headings `tenant_class` and
`tenant_segment`: the tenant segment each tenant class belongs to. The five
deployment zones and the four tenant segments are fixed for the fleet and are
the ones those two tables name.

## The severity ladder

The ladder table, `severity_ladder.csv`, carries the column headings
`component_key`, `tier_index` and `budget_ns`: for each component, one
residency budget in nanoseconds per tier index. Tier index zero is the lowest
tier of a component's ladder.

## Quarantine settlement

Each module holds an effective tier. A module's effective tier is at most its
declared tier index.

A component's containment envelope is the lowest tier budget standing in
service across the modules of that component that hold at least one accepted
fault frame, multiplied by the number of tiers in that component's ladder.

A module's allowance is the smaller of the budget of its own effective tier and
its component's containment envelope.

A fault frame is within budget when its peak residency is at most the allowance
of its module.

> The effective tiers of the fleet's modules take the single highest assignment
> under which no module holding an effective tier above zero counts more frames
> over budget than frames within budget. That assignment is unique and exists
> for every corpus.

A module whose effective tier stands at zero while its frames over budget
outnumber its frames within budget is quarantined.

Settlement is worked as passes over the module set, and the pass that finds no
module left to demote is counted with the rest.

## Conservation

The allocated byte count of a fault frame is the figure its reporter frame
carries. The uploaded byte count is the figure the uplink export carries. A
host that uploads a partial dump range records what it uploaded. The upload
residual of a corpus is the allocated byte total of its accepted frames less
their uploaded byte total.
