# Module identity and the build registry

The build registry, `build_registry.csv`, carries one row per tag per effective
interval, under these column headings:

| Column | Holds |
|---|---|
| `module_tag` | the tag as a reporter dump header writes it |
| `canonical_tag` | the tag this row hands the resolution on to |
| `component_key` | the component the terminal row belongs to |
| `tier_index` | the declared severity tier of the terminal row |
| `effective_from_build` | the first build index the row covers |
| `effective_to_build` | the last build index the row covers |
| `status` | whether the terminal row is still carried |

## Effective intervals

A registry row covers a tag on a build index when that index falls at or after
the row's first build index and at or before its last build index. Both
endpoints belong to the interval.

A tag is covered by exactly one row on any build index the registry describes.
A tag covered by no row on a fault frame's build index leaves that frame's
module unresolved.

## Canonical form

A row whose canonical tag repeats its own module tag is terminal. A row whose
canonical tag differs from its module tag hands the resolution on to that
canonical tag, which is looked up again on the same build index. A chain that
arrives twice at the same tag leaves the frame's module unresolved.

A terminal row supplies the frame's canonical module tag, its component key,
and its declared severity tier.

## Status

A terminal row whose status reads `supported` is still carried. A terminal row
whose status carries any other value marks the module as retired for the build
indices that row covers, and a frame resolving onto such a row is a
retired-build rejection rather than an unresolved-module rejection.

## Renames and retirements are consequential

A tag may resolve onto different canonical tags on different build indices, and
two tags may resolve onto the same canonical tag. Modules are counted, settled
and reported under their canonical tag alone, so a rename moves frames between
module groups, between components, and between severity tiers.
