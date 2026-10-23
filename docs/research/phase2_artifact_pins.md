# Phase 2 external pins (addendum, append-only)

This file records values that the Phase 2 registration (`phase2_preregistration.md`, plan hash `a3a0ff0672445ea7`, code fingerprint
`c39cd20e12619f0b`) deliberately leaves as runtime pins. It does not amend the registration: no arm, statistic, threshold or rule is
changed, the plan hash and the code fingerprint are unaffected, and the registration file is untouched.

| Pin | Value | Supplied by | Used as |
|---|---|---|---|
| `phase1_results.json` SHA-256 | `09f6b33df62fb02dc83b9e87ff4f82d09aebb094eab94d23814e1c3acab2762d` | project owner, computed on the research host (`/home/botops/mba-research`) | `--expect-phase1-sha256` for `phase2 download` and `phase2 run` |
| `phase1_report.md` SHA-256 | `82a72c974e254b06a0a6062bc1ef080998ac29ec73fd99813c48003c390c14fa` | project owner | recorded for provenance only; no Phase 2 code reads this file |

The registration states that the runner reads Phase 1 figures from `phase1_results.json`, pinned by SHA-256. The value was always an
external runtime input and is not part of the plan hash. Recording it here fixes which artifact was reviewed.

Not verified by the author: these hashes were computed on the owner's host; the artifact is not available in the authoring environment. The
read-only check in the checkpoint report (artifact's own `plan_hash` must read `6f29fad5f4cd6e33`) is how the match to the Phase 1 run is confirmed.
