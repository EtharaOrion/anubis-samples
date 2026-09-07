# Stream continuity

Four guards make a capture readable only from its first record forward.

The segment permutation guard stores segments away from the order in which they happened.
The storage slot at position i holds the segment whose logical position is the capture
nonce plus i times a step, taken modulo the number of segments in the capture. The step is
the smallest integer that is at least two more than the nonce shifted right by eight bits
and reduced modulo the number of segments, and that shares no factor with the number of
segments. The slot word inside a segment envelope repeats the storage slot and says nothing
about logical position.

The delta guard stores the tick field as a second order difference. Within a segment the
first difference is the gap from the previous tick, the second difference is the gap from
the previous first difference, and both run from zero at the head of each segment. Ticks
inside a segment never move backwards.

The chain guard masks the value field by exclusive or against the tick of the record that
was decoded immediately before it. The chain runs across segment boundaries in logical
order for the whole capture, and the very first record of a capture is masked against the
capture nonce.

The drift key guard makes the effective segment salt differ from the salt written in the
segment envelope. The effective salt is the written salt combined by exclusive or with the
running sum, modulo two to the power thirty two, of the value fields of every record in
every logically earlier segment of the same capture.

All word arithmetic in this document is unsigned and modulo two to the power thirty two.
