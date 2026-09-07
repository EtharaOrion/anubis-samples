# Reporter clock and sequencing

## The clock

The reporter clock runs at 1000 ticks per millisecond, so a build index covers
900000000 ticks. Entry and unwind stamps are counted on a clock that wraps at
the entry clock modulus, a power of two whose candidate domain the control
slice publishes.

## Capture tick

A fault frame's capture tick is its dump's base increased by the distance from
the dump's first fault frame's entry stamp to its own entry stamp, that
distance taken on the wrapping clock.

A fault frame's build index is its capture tick divided by the ticks in a build
index, with the remainder discarded.

## Support window membership

`support_windows.csv` carries one row per component, under the column headings
`component_key`, `open_tick` and `close_tick`: an opening tick and a closing
tick for each component.

> A fault frame belongs to its component's support window when its capture tick
> is strictly greater than the opening tick and strictly less than the closing
> tick.

This is the boundary convention the fleet runs, and it holds for every
component and every corpus. A fault frame landing exactly on an opening tick
sits outside its window, and so does a fault frame landing exactly on a closing
tick. Both endpoints stand outside the window they bound.

## Unwind order

A fault frame's unwind stamp is at most its entry stamp. The gap between them
is the unwind delay the reporter needed and carries no further meaning here.

## Resident spans

Resident spans are whole tick counts. The resident span total of a corpus is
the sum of the resident spans of the frames the corpus accepts.
