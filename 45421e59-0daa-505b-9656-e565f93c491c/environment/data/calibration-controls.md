# Sample tier and control slice

Three corpora ship beside these notes.

## Sample tier

The files whose names open with `sample_` carry a small corpus the crash
reporter wrote with every flag word clear, so no wire rule beyond the plain
column layout is in force there. They are `sample_fault_frames.psv`,
`sample_runtime_probe.csv` and `sample_uplink_reports.jsonl`.

That corpus ships with its answers. `sample_ledger_answer.jsonl` holds its
fault ledger exactly as a ledger is written, and `sample_reports_answer.json`
holds each of its reports under the report's own name.
`example_zone_report.json` holds one of those reports as a standalone file,
written exactly as a report file is written.

The sample tier teaches the plain column layout and the written shape of the
triage set. It carries no information about the withheld wire parameters.

## Control slice

The files whose names open with `calibration_` carry a held-out corpus the
crash reporter wrote with its flag words live, in the same proportions as the
incident corpus. They are `calibration_fault_frames.psv`,
`calibration_runtime_probe.csv` and `calibration_uplink_reports.jsonl`. No
answers ship for it. What ships instead is `control-slice-figures.json`,
holding eleven control figures the fleet published for that corpus.

| Control | Figure |
|---|---|
| `CTRL-A` | The number of fault frames the control slice export carries. |
| `CTRL-B` | The sum of the recovered dump bases of its dumps. |
| `CTRL-C` | The sum of the recovered resident spans, in ticks. |
| `CTRL-D` | The sum of the recovered entry stamps of its fault frames. |
| `CTRL-E` | The sum of the recovered unwind stamps of its fault frames. |
| `CTRL-F` | The sum of the recovered allocated byte counts of its fault frames. |
| `CTRL-G` | The sum of its recovered fault signatures read as hexadecimal integers, reduced modulo 1000000007. |
| `CTRL-H` | The sum of the authoritative frame indices of its fault frames. |
| `CTRL-I` | The accepted frame count of the control slice corpus. |
| `CTRL-J` | The number of quarantined modules of the control slice corpus. |
| `CTRL-K` | The upload residual of the control slice corpus, in bytes. |

Exactly one assignment of the nine withheld wire parameters reproduces all
eleven published figures from the control slice files.

## Candidate domains

| Parameter | Candidate domain | Size |
|---|---|---|
| entry clock modulus | the powers of two from two to the twenty-second up to two to the thirty-fourth, inclusive | 13 |
| drift period | 48, 96, 192, 384, 768 | 5 |
| rotation offset | the whole numbers from 0 up to 7 | 8 |
| count multiplier | the whole numbers from 0 up to 8 | 9 |
| alternate radix | the whole numbers from 11 up to 35 | 25 |
| signature offset | the whole numbers from 0 up to 7 | 8 |
| coarse unit | 64, 128, 256, 512, 1024 | 5 |
| ring width | 4, 8, 12, 16 | 4 |
| base shift width | 8, 10, 12, 16 | 4 |

The nine parameters hold the same values for the control slice, for the
incident corpus, and for every corpus the fleet produces. The sample tier
leaves them unexercised.

## Incident corpus

The unprefixed wire files carry the incident corpus, with its flag words live
and no answers of any kind. They are `fault_frames.psv`, `runtime_probe.csv`
and `uplink_reports.jsonl`. The five reference tables beside them,
`build_registry.csv`, `severity_ladder.csv`, `support_windows.csv`,
`host_zones.csv` and `tenant_classes.csv`, govern all three corpora alike.
