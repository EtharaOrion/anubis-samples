# Ladder brief

The tactics league seals one binary journal for every capture it keeps. A capture holds a
run of match segments; a segment holds the ordered record stream of a single match. The
league settles a season from the sealed material alone.

Three captures accompany this brief. Their roles are told apart by the capture identifier
carried in the envelope, never by the name of the file that holds them.

Event kinds carried in a record body:

| kind | meaning | value field |
| --- | --- | --- |
| 1 | a unit is deployed | upkeep rate charged for each tick the unit stays on the field |
| 2 | a unit reports output | points reported |
| 3 | a unit is eliminated | unused |
| 4 | a unit is disbanded | unused |
| 5 | a squad concedes the match | unused |
| 6 | a squad hands its entry to another squad | position of the receiving squad in the roster dictionary |
| 8 | the match closes | unused |

A record carries the attribution key of the squad it belongs to, the unit ordinal inside
that squad, the tick at which the event happened and a value whose reading depends on the
kind above. Unit ordinals are scoped to the squad and to the match.

The season report is settled for four conferences and for two entrant segments. The
conference of a squad follows from its division, and the segment of a squad follows from
its licence class.
