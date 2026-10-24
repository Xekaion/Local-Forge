# Verified handoff to rigging tools

`scripts/localforge_client.py` is a stdlib-only client for the existing engine
API. This change ports the user's local CLI prototype into the remote project
and adds an explicit fixture boundary and an offline handoff check. It does not
add a model backend or alter the engine's generation model.

## Generate and verify

Start the engine normally, then run:

```powershell
python scripts/localforge_client.py health
python scripts/localforge_client.py generate dog-reference.png --out outputs/dog --require-real
python scripts/localforge_client.py verify outputs/dog/dog-reference-JOB.asset.json --require-real
```

Replace `JOB` with the generated eight-character job prefix; the complete
`handoff_path` is returned in stdout. Stdout contains one JSON object and
progress goes to stderr. The optional `--api` and `--token` parameters precede
the command. Prefer `LOCALFORGE_API_TOKEN` over placing a token in shell history.
`status JOB_ID` and `cancel JOB_ID` address an existing job. A timeout leaves
the submitted job running, as the error explains.

`--require-real` checks `/health` before any generation submission. It accepts
only the configured `trellis2` backend, and also requires `trellis2` in the
completed, checksum-verified engine manifest. It does not bypass the engine's
hardware or dependency checks. A server can report TRELLIS.2 yet fail while
loading; this produces a failed job rather than a replacement mock result.

For a CPU pipeline exercise, omit the flag deliberately. A mock result is a
generic deterministic mesh used to test upload, queue, preview and export. It
does **not** reconstruct the supplied image and must not replace a game model.

## Files and contract

Each successful generation exports three adjacent files:

| File | Purpose |
| --- | --- |
| `NAME-JOB.glb` | Original downloaded GLB bytes, unchanged |
| `NAME-JOB.manifest.json` | Original engine provenance JSON bytes, unchanged |
| `NAME-JOB.asset.json` | LocalForge handoff descriptor, written last |

The descriptor uses `schema_version: 1` and
`kind: "localforge-asset-handoff"`; the upstream engine manifest separately
uses its existing `schema_version: "1.0"`. The descriptor records:

- GLB and manifest filenames and SHA-256, output length, job ID and backend.
- Original uploaded image SHA-256 and the engine's canonical PNG SHA-256.
  These can differ because the engine strips metadata and normalizes encoding.
- The engine's finite geometry counts/bounds, preserved without claiming a
  second topology inspection in this stdlib client.
- `is_mock`, `asset_role`, `rigging_ready: false` and
  `quality_approved: false`. Real generation has role
  `unreviewed_generated_mesh`; mock has role `pipeline_fixture`.
- glTF's right-handed `+Y` up and metre convention. The authored forward axis
  and real physical scale remain unverified. No coordinate transform is applied.

The verifier requires local sibling filenames, checks the stored SHA-256 values
and GLB 2.0 header/length, cross-checks engine schema/job/input/output identity,
and rejects inconsistent fixture or approval fields. It also rejects path
escapes and symlinks resolving outside the export folder. Atomic per-file writes
and a descriptor written last make interrupted exports fail verification.
Hashes detect accidental changes; they are not digital signatures or proof that
an untrusted server actually ran an AI model.

A rigging importer such as Xekaion3D can run `verify ... --require-real` before
accepting a LocalForge export, then use the returned `glb_path`. It must still
establish forward direction, actual size, landmarks and skinning. A GLB authored
directly by the game is a separate source asset and should not be labelled a
LocalForge or AI generation. No skeleton, animation, legal clearance, or
production-quality judgement follows from a successful integrity check.

## Local exercise on 2026-10-24

The measured workstation has an RTX 3070 with 8GB VRAM. The existing Python
environment has the CPU engine dependencies but no PyTorch, TRELLIS.2 checkout,
or model checkpoint. The repository's actual TRELLIS.2 adapter is still the
documented RTX 5090 path. No attempt was made to download multi-gigabyte models,
change that hardware policy, or spend generation credits.

An isolated Uvicorn process on an automatically assigned localhost port accepted
the existing game-rendered `RareDogPortrait.png`, normalized it, queued a mock
job, returned the GLB and manifest, and passed offline handoff verification.
Both strict submission and strict offline verification rejected that fixture.
The process used a separate runtime directory and was stopped after the test;
existing development servers and runtime data were untouched.

The input portrait contains an animal, harness, cropped ears/body, UI overlays
and background scenery. It is useful for exercising file handling, not a clean
reference for production reconstruction. A later real inference trial should
use an unobscured full-body reference with a known licence and neutral stance.

The produced fixture has 2 geometries, 2,692 vertices and 5,376 faces. It was not
used by the game and is not a dog reconstruction. The local evidence summary
is [recorded separately](validation/game-handoff-2026-10-24.json).

Validation: 43 Python tests passed (23 existing engine tests plus 20 client
cases), Python compilation, Ruff lint and formatting passed. The test suite
covers mock rejection before submission, changed-backend rejection after job
completion, artifact tampering, missing checksum/job identity, path escape,
false approval flags and invalid polling arguments. Existing Starlette/httpx
test-client deprecation warning remains. No web files changed, so the web build
was not repeated for this CLI change. This is not evidence of actual GPU
inference or generated asset quality.
