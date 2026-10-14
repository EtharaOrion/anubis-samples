<p align="center"><img src="images/hero.svg" alt="Anubis" width="720"></p>

# Anubis: samples

<p align="center"><b>The samples lane: 3 of 30 Harbor task bundles.</b></p>

<p align="center"><a href="#summary"><img src="https://img.shields.io/badge/bundles-3_of_30-845EF7" alt="bundles: 3 of 30"></a> <a href="#evidence-status"><img src="https://img.shields.io/badge/sealed-0-845EF7" alt="sealed: 0"></a> <a href="#difficulty-tiers"><img src="https://img.shields.io/badge/difficulty-measured-845EF7" alt="difficulty: measured"></a></p>

<p align="center"><sub><a href="#abstract">Abstract</a> | <a href="#summary">Summary</a> | <a href="#difficulty-tiers">Difficulty tiers</a> | <a href="#evidence-status">Evidence status</a> | <a href="#coverage">Coverage</a> | <a href="#dataset-structure">Dataset structure</a> | <a href="#trajectory-structure">Trajectory structure</a> | <a href="#verification-and-quality-assurance">Verification and quality assurance</a> | <a href="#reproduction">Reproduction</a> | <a href="#cite">Cite</a> | <a href="#license">License</a> | <a href="#contact">Contact</a></sub></p>

## Abstract

The curated lane of this corpus: at most 30 Harbor task bundles, every anchor seated first and every other bundle in seal order. Bundles past that ceiling live in `delivery/`.

Each bundle is one Harbor task in a directory named by its content-derived uuid5, so the name doubles as an integrity check. This page is rendered from the resident bundles on every reconcile; a value a bundle does not carry reads `absent`, and the page never fills one in.

## Summary

| Property | Value |
| --- | --- |
| Bundles | 3 of 30 |
| Sealed | 0 of 3 |
| Registrations | unregistered (3) |
| Audit verdicts | none (3) |
| Harbor schema | 1.4 (3) |

## Difficulty tiers

Difficulty is measured by the signed pilot alone and never declared in a bundle, so this page states no tier a bundle claims.

## Evidence status

The verdict is the audit outcome recorded at the sealed digest, and `none` means no verdict has landed for that digest yet. Only an authoring run removes a bundle from this lane, so removal waits for the next one.

| uuid | Verdict | Sealed digest | Stream |
| --- | --- | --- | --- |
| 64d3d25b-74db-5ac0-a47d-f1b1073cca81 | none | absent | none |
| a8d5a61c-4126-58f3-8e29-edb360f79699 | none | absent | none |
| c4a9a9b2-0818-5b05-9829-3a208c74638b | none | absent | none |

## Coverage

| Keyword | Bundles |
| --- | --- |
| python | 3 |
| derivation | 2 |
| stdlib | 2 |
| admission-control | 1 |
| allocation | 1 |
| cli | 1 |
| configuration | 1 |
| constraint-compiler | 1 |
| determinism | 1 |
| hardening | 1 |
| precedence | 1 |
| security | 1 |
| state-machine | 1 |

## Dataset structure

| Bundle | Task | Keywords | Registration | Sealed at | Tier |
| --- | --- | --- | --- | --- | --- |
| [`64d3d25b-74db-5ac0-a47d-f1b1073cca81`](64d3d25b-74db-5ac0-a47d-f1b1073cca81/) | `terminal-bench/64d3d25b-74db-5ac0-a47d-f1b1073cca81` | derivation, precedence, python, state-machine, stdlib | unregistered | unsealed | - |
| [`a8d5a61c-4126-58f3-8e29-edb360f79699`](a8d5a61c-4126-58f3-8e29-edb360f79699/) | `terminal-bench/a8d5a61c-4126-58f3-8e29-edb360f79699` | allocation, constraint-compiler, determinism, python, stdlib | unregistered | unsealed | - |
| [`c4a9a9b2-0818-5b05-9829-3a208c74638b`](c4a9a9b2-0818-5b05-9829-3a208c74638b/) | `terminal-bench/c4a9a9b2-0818-5b05-9829-3a208c74638b` | admission-control, cli, configuration, derivation, hardening, python, security | unregistered | unsealed | - |

```mermaid
%%{init: {"theme":"base","themeVariables":{"primaryColor":"#131a33","primaryTextColor":"#e8ecff","primaryBorderColor":"#845EF7","lineColor":"#22d3ee","fontFamily":"monospace"}}}%%
flowchart LR
    bundle["&lt;uuid&gt;/"]
    bundle --> n0["task.toml<br/>runner manifest"]
    bundle --> n1["instruction.md<br/>objective the agent reads"]
    bundle --> n2["environment/<br/>agent sandbox image"]
    bundle --> n3["tests/<br/>verifier, hidden from the agent"]
    bundle --> n4["solution/<br/>private oracle"]
    bundle --> n5["trajectories/<br/>private rollout evidence"]
    bundle --> n6["README.md"]
```

```mermaid
%%{init: {"theme":"base","themeVariables":{"primaryColor":"#131a33","primaryTextColor":"#e8ecff","primaryBorderColor":"#845EF7","lineColor":"#22d3ee","fontFamily":"monospace"}}}%%
flowchart LR
    staging["staging/"]
    staging --> samples["samples/<br/>3 of 30"]
    samples --> delivery["delivery/<br/>0 of 10000"]
    delivery --> stock["stock/<br/>0 of 10000"]
```

## Trajectory structure

3 of 3 resident bundles carry a `trajectories/` subtree of private rollout evidence. The signed pilot reads it; this page never restates a rollout.

## Verification and quality assurance

Each bundle's signed provenance carrier holds its screening roots, screening instant, and absolute expiry. Placement moves that carrier into the private derivation set, which this page never reads, so reconcile lists no root it cannot verify here.

## Reproduction

Each bundle directory is a complete Harbor task: `task.toml` is the runner manifest, `instruction.md` the objective, `environment/` the sandbox image, and `tests/` the verifier. Point a Harbor runner at one bundle directory to reproduce it; `solution/` and `trajectories/` stay private.

## Cite

Anubis 2026. Reference implementation of Ethara Terminal Tasks bundle
authoring under the governance contracts.

## License

Anubis is released under the [MIT License](LICENSE).

## Contact

Project owner: Ethara Research. See `.github/CODEOWNERS` for routing rules.
