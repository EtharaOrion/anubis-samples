# Replay guards

The guard register in the envelope is a bit field. A set bit means the named guard was
active when the capture was sealed.

| bit | guard |
| --- | --- |
| 0 | keystream |
| 1 | chain |
| 2 | delta2 |
| 3 | driftkey |
| 4 | segperm |
| 5 | backref |
| 6 | endian |
| 7 | countbias |
| 8 | shadow |
| 9 | rotate |
| 10 | alias |
| 11 | escape |

Three quantities govern how the guards resolve. None of them is written down anywhere in
this material. Each is drawn from the roster published beside it here.

Engine tag roster: `orion-fold`, `wren-cadence`, `tessel-drift`, `halcyon-mark`, `vector-plume`, `umbral-stack`, `quill-lattice`, `nimbus-gable`

Window depth roster: `4`, `6`, `8`, `10`

Sentinel roster: `0x1A`, `0x1D`, `0x20`, `0x7F`

The keystream guard masks a record blob by exclusive or against the SHA-256 digest of the
engine tag, the match identifier, the effective segment salt and the ordinal of the record
inside its segment, written as decimal text and joined by single colons, truncated to the
width of the blob.

The rotate guard turns a record blob left. The turn is the sum of the effective segment
salt and the ordinal of the record inside the whole capture, taken modulo the true width of
the blob. The turn moves bytes without changing their sum, so the payload checksum agrees
either way.

The escape guard lets a record carry directive pairs ahead of its body. Every pair is the
sentinel byte followed by a directive byte whose high nibble is the operation and whose low
nibble is the operand. Operation 1 redirects the attribution of the record to the roster
dictionary entry at the operand position. Operation 2 advances the attribution of the
record by the operand, counted forward around the roster dictionary from the position the
record already holds. A record carries at most two pairs. No event kind ever collides with a
sentinel value.

The back reference guard replaces the attribution field with a position inside a rolling
list of recently seen squads. The list is as deep as the window depth, holds each squad at
most once, and is ordered from the least recently seen entry to the most recently seen one.
Positions count from the least recently seen end. The list is refreshed after every record
using the attribution key as written on the wire, before any escape directive has been
weighed, and a squad already in the list moves to the most recent end rather than being
added twice.

The alias guard replaces the attribution field with a position in the roster dictionary.
