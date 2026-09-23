# Code enumerations

These are the closed enumerations the artifacts use. No other value is legal.

## Placement codes

Used in selected placement, conservation adjusted placement and rejected placement.

- P_OK a bay was selected
- P_NO_FIT no bay fits any admissible orientation
- P_NO_ORIENTATION no bay fits and only the base orientation was available
- P_CONSERVATION_BLOCK a bay fits geometrically but none passes conservation
- P_BAY_EXCEPTION reserved for a venue exception that removes the only fitting bay

## Crate codes

Used in crate assignment.

- C_PRIMARY the object received its locally optimal crate
- C_DISPLACED the object was pushed off its locally optimal crate by shared capacity
- C_NO_CRATE no fitting crate class had remaining availability

## Dependency kinds

Used in dependency edges and cycle resolution audit, in ascending removal precedence so
a bay size edge is removed before a conservation edge and a conservation edge before an
explicit rule.

- D_RULE an explicit dependency rule
- D_CONSERVATION a within bay edge from a more sensitive to a less sensitive class
- D_BAYSIZE a within bay edge from a larger to a smaller handling volume

## Schedule codes

Used in the install window table and the unschedulable report.

- S_OK scheduled within its window
- S_NO_PLACEMENT never placed
- S_NO_CRATE placed but received no crate
- S_NO_WINDOW placed and crated but the window intersection is empty
- S_SHORT_WINDOW placed and crated with a non empty window that is too few days for the setup
