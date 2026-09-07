# Journal envelope

Every capture opens with a thirty six byte envelope.

| offset | width | field |
| --- | --- | --- |
| 0 | 4 | the ASCII marker RJNL |
| 4 | 1 | envelope version |
| 5 | 1 | reserved, always zero |
| 6 | 2 | guard register, unsigned, little endian |
| 8 | 4 | capture nonce, unsigned |
| 12 | 4 | declared segment count, unsigned little endian |
| 16 | 4 | declared record count, unsigned little endian |
| 20 | 4 | season base tick, unsigned little endian |
| 24 | 4 | capture identifier, unsigned little endian |
| 28 | 4 | declared roster size, unsigned little endian |
| 32 | 4 | envelope checksum, unsigned little endian |

The envelope checksum is the sum of the first thirty two bytes of the envelope taken
modulo two to the power thirty two. Because it sums bytes rather than words it agrees with
the envelope whichever way round the multi byte fields were laid down.

The capture identifier ends in 1 for the reference capture that carries the base envelope,
in 2 for the control slice, and in 3 for the sealed season.

Each segment that follows the envelope opens with a twenty eight byte segment envelope:
the ASCII marker SEG0, the storage slot of the segment as an unsigned little endian word,
the match identifier as an unsigned little endian word, the declared record count of the
segment as an unsigned little endian word, the segment salt as an unsigned little endian
word, the payload width in bytes as an unsigned little endian word, and the payload
checksum as an unsigned little endian word. The payload checksum is the sum of the payload
bytes modulo two to the power thirty two.

The payload holds the declared number of record frames and then trailing filler. The width
of the filler is three bytes more than the segment salt taken modulo five, and its content
carries no meaning. Nothing marks where the records stop; only the declared count does.

A record frame opens with a visible width byte and a shadow width byte and then carries a
record blob of the true width. When the shadow guard is idle the visible byte holds the
true width and the shadow byte is zero.

A decoded record blob holds, in order, the event kind as one byte, the attribution mode as
one byte, the attribution field, the unit ordinal as an unsigned little endian half word,
the tick field as an unsigned little endian word and the value field as an unsigned little
endian word. Attribution mode 0 carries three ASCII bytes naming the squad. Attribution
mode 1 carries a single byte holding a back reference. Attribution mode 2 carries a single
byte holding a roster dictionary position.

Guards that touch the envelope:

The endian guard lays the capture nonce down most significant byte first while every other
envelope word stays least significant byte first.

The count bias guard stores every declared count of a capture, in the envelope and in each
segment envelope alike, offset upward by a constant. The constant is the sum of the byte
values of the engine tag taken modulo eleven.

The shadow guard makes the visible width byte a decoy. The true width of the blob is the
shadow byte combined by exclusive or with the low byte of the effective segment salt.
