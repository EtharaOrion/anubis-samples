# Reconciliation invariants

The settlement is only accepted once the following are stated and understood. A residual
that is not zero is a finding to be reported, never an error to be silenced.

The declared record count of a capture is compared against the number of records actually
recovered from it. The declared segment count is compared against the number of segments
recovered. The declared roster size counts only the squads of the capture that appear in the
entrant registry, so comparing it against the number of distinct squads recovered exposes
every squad that played without an entry.

A squad that appears in a capture but not in the entrant registry is unregistered. Its
output and its upkeep are settled and reported in the season totals, and it is left out of
every conference report and every segment report because it belongs to none. The difference
between the season gross output and the sum of the conference gross outputs is therefore
the attribution residual.

A squad that appears in the entrant registry but in no capture is an absent entrant. It is
listed and it contributes nothing.

Gross output plus voided output equals all the output reported in a capture. Charged upkeep
plus overflow upkeep equals raw upkeep. Ticks inside a segment never move backwards, and any
place where they do is counted.

The kind ledger counts the recovered records of each event kind under the names deploy,
output, eliminate, disband, concede, transfer and close.
