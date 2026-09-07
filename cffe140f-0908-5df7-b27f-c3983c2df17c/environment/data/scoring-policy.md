# Scoring policy

Output reported by a unit counts toward the gross output of its squad in that match unless
it is withdrawn.

Output reported by a unit at or after the tick at which that unit was eliminated is
withdrawn. Output reported by a squad at or after the tick at which that squad conceded the
match is withdrawn, and that applies to every unit of the squad. Output reported before the
concession stands. Withdrawn output is reported as voided output and never counts toward
gross output.

The match score of a squad is its gross output multiplied by the ladder weight of its
division, less its charged upkeep in that match multiplied by the levy rate of its licence
class. Overflow upkeep is never levied. All of this arithmetic is exact integer
arithmetic; nothing is ever rounded.

A squad that appears in a capture without appearing in the entrant registry carries a
ladder weight of one and a levy rate of one.

The season score of a squad is the sum of its match scores. A season score may be negative.
