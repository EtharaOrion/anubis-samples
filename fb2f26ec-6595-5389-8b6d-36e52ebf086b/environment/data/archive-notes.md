# What the Relay scoring archive holds

The gateway scored batches for years under one set of scoring settlements. When
the service was retired, part of the scoring notes went missing and the engineer
who kept the rest in her head moved on.

What survives is the batch schema, the surviving notes, the scorer as it was last
left, and the archive.

The archive holds batches that were scored while the lost sections were still in
force. It does not hold the scoreboards those batches produced. What it holds
beside each one is a seal, a single value the gateway computed over the scoreboard
and the rows together, so that a later reading of the scorer could be checked
against the batch without the scoreboard itself surviving. A seal tells you
whether a scoreboard and its rows are the ones it was taken from. It tells you
nothing else, and two scoreboards that differ anywhere at all seal differently.

Every seal in the archive was taken under the same settlements, so they agree with
each other and with the surviving sections.

More batches sit beside them with no seal. Nobody ever scored those.

The scorer shipped beside these notes still runs and still writes a scoreboard. Its
scoreboards do not seal to what the archive holds, so some of the settlements it
carries are the wrong ones. Nobody wrote down which, or how many.
