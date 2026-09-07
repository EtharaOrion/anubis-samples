# Upkeep policy

A deployed unit charges its squad an upkeep rate for every tick it remains on the field.
The rate is the value field of the deployment record. The charge runs from the tick of the
deployment up to, and not counting, the tick at which the unit leaves the field.

An eliminated unit remains upkeep bearing for the remainder of the match. Elimination
withdraws the output of the unit but never its upkeep. Only a disband order withdraws
upkeep, and only from its own tick onward. A unit that is neither disbanded nor withdrawn
by the close of the match bears upkeep until the closing tick.

Upkeep raised in this way is the raw upkeep of the squad in that match.

Every squad carries one budget for the whole season. Raw upkeep is charged against that
budget match by match, taken in the logical order of the matches. Upkeep that still fits
inside the budget is charged upkeep. Upkeep that no longer fits is overflow upkeep. Once
the budget is exhausted every further tick of upkeep is overflow. Overflow upkeep is
reported but never levied.

Budget cap roster: `20`, `220`, `440`, `640`, `860`, `1060`

The season budget is one of the figures on that roster. Which one it is appears nowhere in
this material.
