# Crash reporter dump export

The crash reporter writes one line-oriented pipe-delimited export per corpus:
`fault_frames.psv` for the incident corpus, `calibration_fault_frames.psv` for
the control slice and `sample_fault_frames.psv` for the sample tier. A line
whose first character is `#` is a comment. Every other line belongs to a dump.

## Dump shape

A dump opens with a header line whose first column is `@`. The header columns,
in the order written, are the dump identifier, the module column, the declared
count, the base field, the dump flag word in lowercase hexadecimal, the dump
key, and the dump check total in four lowercase hexadecimal digits.

Every line after a header and before the next header belongs to that header's
dump. The declared count states how many of those lines are fault frames. The
lines a dump holds beyond its fault frames are trailer lines. Trailer lines
carry the same nine-column shape as fault frames.

## Fault frame columns

A fault frame carries nine columns: the frame index column, the entry column,
the unwind column, the resident span column, the allocation column, the fault
signature column, the frame flag word in lowercase hexadecimal, the symbol
column, and the frame check total.

The symbol column carries a symbol kind, a colon, and an integer.

The fault signature of a frame is sixteen lowercase hexadecimal characters.

## Check totals

A frame check total is the sum of the character codes of that frame's eight
preceding columns joined by the pipe delimiter, reduced modulo 65536, written
as four lowercase hexadecimal digits. A dump check total is the sum of the
frame check totals of that dump's fault frames, reduced modulo 65536.

## Dump flag word

| Bit | Rule |
|---|---|
| `0x001` | The resident span column of the dump carries the second-order differences of the dump's resident spans, taken over the dump's fault frames in logical order and started at the dump's first fault frame. |
| `0x002` | The entry column of every fault frame after the dump's first carries that frame's entry stamp added to the previous frame's entry stamp, reduced modulo the entry clock modulus. |
| `0x004` | The unwind column of every fault frame carries that frame's unwind stamp increased by the running total of every resident span that precedes it in the export, reduced modulo the drift period. |
| `0x008` | The dump's fault frame lines are written rotated to the left by the dump key added to the rotation offset, reduced modulo the dump's fault frame count. |
| `0x010` | The declared count carries a bias of the dump's one-based ordinal in the export multiplied by the count multiplier, reduced modulo nine. |
| `0x020` | The numeral in the base field is written in the alternate radix, using the digits `0` to `9` followed by `a` to `z`. |
| `0x100` | The module column carries a registry alias. |
| `0x800` | The base field carries the high part of the dump base. The low part is the frame index column of the dump's first trailer line, and the dump base is the high part shifted left by the base shift width with the low part occupying the shifted-in positions. |

## Frame flag word

| Bit | Rule |
|---|---|
| `0x040` | The frame index column carries a dump-local placeholder and the authoritative frame index of the frame is the integer the symbol column carries after its final colon. |
| `0x080` | The fault signature column is written rotated to the left by the frame's authoritative frame index added to the signature offset, reduced modulo sixteen. The frame check total covers the column as written. |
| `0x200` | The allocation column opens with a tilde and carries the frame's allocated byte count divided by the coarse unit. |
| `0x400` | The fault signature column opens with a caret and carries a distance. The signature is the one standing in the slot that the distance reaches counting back from the current write position of a ring of the ring width, into which every fault signature recovered so far in the export has been written in turn. |

## Withheld wire parameters

The two flag tables above name nine parameters whose values the crash reporter
holds back: the entry clock modulus, the drift period, the rotation offset, the
count multiplier, the alternate radix, the base shift width, the signature
offset, the coarse unit and the ring width. The sequencing rules name the entry
clock modulus again. The candidate domain of each of the nine is published with
the control slice.

## Running state

The running resident span total that bit `0x004` names, and the ring that bit
`0x400` names, both advance over the export as a whole and over every fault
frame it carries, whatever flags that frame's own dump sets.
