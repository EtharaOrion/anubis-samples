A competitive turn-based tactics league has sealed its season and needs the ladder settled. Everything you need is present under `/app/data`. Read all of it before you start. The material is spread deliberately: no single document is sufficient, and the order in which the rules are written down is not the order in which they apply. Three sealed replay journals are shipped. One is a baseline capture whose stream guards are all disabled and which ships alongside a labelled event transcript; it documents the base envelope and nothing more. A second capture is a control population with its guards engaged, published alongside a short list of control figures. The third is the season itself, guards engaged, and it is the population you must settle.

Write one reusable program at `/app/out/derive_report.py`. It must take a data directory and an output directory as its two positional arguments and run as:

```
python3 /app/out/derive_report.py DATA_DIR OUT_DIR
```

It must write `/app/out/summary.json`, `/app/out/data_quality.json`, `/app/out/policy_audit.json`, `/app/out/region_North.json`, `/app/out/region_South.json`, `/app/out/region_West.json`, `/app/out/region_East.json`, `/app/out/segment_commercial.json` and `/app/out/segment_public.json`. Your program will be judged on captures you have never seen, produced by the same league software with different rosters, different guard settings and different published policy rosters, so it must implement the written contract in general rather than memorising the shipped numbers. The four conference reports are keyed by the conferences named in the division topology; the two segment reports are keyed by the entrant classes named in the licence table.

The end state is a settled season: every recovered event attributed to its effective owner, every squad scored, tiered and ranked, one finals bracket elected and one review docket chosen. Three quantities that govern the settlement are never stated anywhere in the material. Each is drawn from a roster that is published, and each is pinned to exactly one value by the control figures. Find them; do not guess them.

`/app/out/data_quality.json` carries the reconciliation between what the journals declare about themselves and what you actually recovered, together with the attribution and ledger residuals and the squads seen on the wire that the entrant registry never lists. A residual that is not zero is a finding, not a bug. Report it.

`/app/out/policy_audit.json` is your derivation record. It must show the candidate space you searched for each unstated quantity, a verdict for every candidate in that space, a falsifier naming the control figure that killed each rejected candidate, the attribution settlement pass count, the bracket candidate space together with the best satisfied-constraint count any candidate reaches, and a calibration comparison table with one row per published control figure carrying the published value, your reproduced value and whether the two agree.

When you are finished, say so in your own words. Your closing report must name the elected review docket, how its matches spread across the conferences, and its combined review index, and it must state the reconciliation findings together with the squad keys behind them.

Work in this way. Read every document under `/app/data` before writing any code. Decode the baseline capture against its labelled transcript first and confirm a byte-exact agreement before touching either guarded capture. Pin the three unstated quantities against the control population and record the falsifier for every candidate you reject. Settle attribution to a fixpoint and record the number of passes it took. Regenerate all nine reports from a single clean run of the finished program before you report completion.

Use only the Python standard library. Everything you need is under `/app/data`; that directory is read only input and must be left as you found it. At run time the delivered program may install nothing, reach no network, and read nothing outside the data directory handed to it and the output directory it is told to write; it must take both directories from its arguments and hard code neither, and it must write the same reports for the same input whatever the directories are called.
You have 21600 seconds to complete this task.
Do not cheat by using online solutions or hints specific to this task, and do not look up the answer online.
