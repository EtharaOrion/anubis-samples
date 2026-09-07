# Global invariants

Every invariant below holds for the shipped corpora and for every corpus the
fleet produces. They hold jointly, and a derivation that breaks one of them has
gone wrong somewhere upstream.

## Wire closure

- Every dump check total equals the total the reporter rules define over that
  dump's fault frames, and every frame check total equals the total those rules
  define over that frame's columns.
- Every dump holds at least one fault frame, and a dump's fault frame count
  never exceeds the number of lines that follow its header.
- A dump that sets the split-base bit carries at least one trailer line.
- Every fault signature recovered from the export is sixteen lowercase
  hexadecimal characters.
- The recovered dump base of every dump is a non-negative whole number of ticks.

## Identity closure

- Every module tag the runtime probe export and the uplink export carry is a
  canonical tag.
- Every canonical tag the registry produces carries a terminal registry row on
  the build index it was resolved for.
- Every component key the registry produces carries a severity ladder and a
  support window.

## Ledger closure

- A fault frame reaches the fault ledger exactly when its module resolves onto
  a still-carried module, its capture tick falls inside its component's support
  window, the runtime probe export holds its residency facts, and the uplink
  export holds its report.
- No two ledger rows share a capture tick.
- The ledger is strictly increasing in capture tick.
- The accepted frame count added to the rejected frame count equals the number
  of fault frames the export carries. A fault frame that breaks more than one
  acceptance condition is counted once against each condition it breaks, and
  once in the rejected total.

## Aggregate closure

- Every deployment zone byte total added together equals the ledger byte total,
  and every tenant segment byte total added together equals the ledger byte
  total.
- The accepted counts of the five deployment zones add to the accepted count of
  the ledger, and so do the accepted counts of the four tenant segments.
- Every module holding at least one accepted fault frame appears once in the
  per-module report, and no other module appears there.
- A deployment zone or a tenant segment holding no accepted fault frame still
  carries a report, with its counts and totals standing at zero and its
  collections empty.
- The allocated byte total less the uploaded byte total equals the upload
  residual.

## Serialisation closure

Every report the triage set carries is written the way the shipped example zone
report is written, and the ledger is written the way the shipped sample ledger
answer is written. One shape governs all of them.
