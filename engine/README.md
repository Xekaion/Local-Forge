# LocalForge engine

The FastAPI bridge presents one local contract to the web studio and serializes
all 3D generation through one worker. The `mock` backend exercises the complete
CPU path; `trellis2` loads the RTX 5090 pipeline.

## Run

Windows mock engine:

```powershell
python -m venv engine\.venv
engine\.venv\Scripts\python.exe -m pip install -r engine\requirements.txt
$env:LOCALFORGE_BACKEND = "mock"
engine\.venv\Scripts\python.exe -m uvicorn app:app `
  --app-dir engine --host 127.0.0.1 --port 8000
```

The included `engine/start-engine.ps1` runs the same virtual environment and
defaults to `mock` when `LOCALFORGE_BACKEND` is unset.

RTX 5090/TRELLIS.2:

```bash
export LOCALFORGE_BACKEND=trellis2
export TRELLIS2_ROOT=~/src/TRELLIS.2
python -m uvicorn app:app --app-dir engine \
  --host 127.0.0.1 --port 8000
```

Use one Uvicorn process. The application already owns one bounded worker queue;
multiple Uvicorn workers would each load a pipeline and defeat GPU
serialization. An OS-level `engine.lock` prevents a second process from using
the same runtime root, but a process pointed at a different root would have a
different lock and could still load the GPU again.

## API

| Method | Path | Behavior |
| --- | --- | --- |
| `GET` | `/health` | Engine version, limits, queue, persistence, and security state |
| `POST` | `/v1/generations` | Normalize an image and enqueue one generation |
| `GET` | `/v1/generations/{id}` | Read the persisted public job state |
| `DELETE` | `/v1/generations/{id}` | Request cooperative cancellation |
| `GET` | `/v1/generations/{id}/output` | Download and re-check the completed GLB |
| `GET` | `/v1/generations/{id}/manifest` | Download the provenance/integrity JSON |

`POST /v1/generations` accepts multipart fields `image`, `prompt`, `quality`,
`remesh`, and `texture`. `quality` is either `draft` or `studio`. Text-only
generation is not implemented and returns HTTP 501.

Clients may send `Idempotency-Key` using 1–128 characters from
`A-Z`, `a-z`, `0-9`, `.`, `_`, `:`, and `-`. Repeating the same key and request
returns the original job with `idempotency_replayed: true`; reusing the key for
different content returns HTTP 409. A full queue returns HTTP 429 with
`Retry-After: 2`.

Public job states are `queued`, `running`, `succeeded`, `failed`, and
`cancelled`. Successful jobs can include:

- `input_sha256`
- `output_sha256`
- `output_bytes`
- `model_url`
- `manifest_url`
- `created_at` and `updated_at`

Both file endpoints return `ETag`, `X-Checksum-SHA256`, and RFC-style
`Content-Digest` headers. The GLB endpoint hashes the file again and rejects a
post-generation mismatch instead of serving altered content.

## Input and output integrity

Input is accepted only when the declared PNG/JPEG/WEBP content type matches the
decoded format. Pillow verifies the full file, rejects animated images and
decompression-bomb limits, applies EXIF orientation, enforces byte/pixel/edge
limits, converts to RGB or RGBA, and re-encodes a canonical PNG. This removes
EXIF, ICC, comments, and other source metadata. `input_sha256` is the checksum
of that canonical PNG, not the original upload bytes.
Only one request at a time may perform this memory-heavy decode and
normalization step. Queue capacity is rechecked after acquiring that admission
slot, before the upload is read into application memory.

A generated file is first written as a private `.building.glb`. Before
publication, `trimesh` reloads it as a scene and requires:

- at least one non-empty triangular mesh;
- finite vertices, valid integer face indices, and finite non-zero bounds;
- at most 10,000,000 vertices and 20,000,000 faces.

Only then is the file atomically promoted, hashed, and paired with a JSON
manifest. The manifest records schema/engine/backend identity, generation
options, canonical input properties and checksum, output bytes/checksum,
geometry validation counts and bounds, Python/platform/package provenance, and
the active integrity limits. A TRELLIS.2 manifest adds available PyTorch, CUDA,
GPU, and compute-capability provenance after the model runner is loaded.
The input record uses `exif_orientation_normalized: true` to describe the
canonicalization step.

## Queue, persistence, cancellation, and TTL

The queue capacity counts `queued + running` jobs. A single background worker
claims jobs in creation order, so only one model call is active per application
process.

State and idempotency keys persist in
`LOCALFORGE_RUNTIME_ROOT/jobs.sqlite3`; canonical inputs, outputs, manifests,
and the OS-released `engine.lock` live under that same root. Writes use SQLite
WAL/full synchronization or atomic file replacement. On restart:

- queued jobs are restored to the queue;
- a previously running job becomes `failed` with stage `interrupted`;
- a previously running job with cancellation requested becomes `cancelled`.

`DELETE` cancels a queued job immediately. A running job records a cancellation
request and checks it at safe stage boundaries. TRELLIS.2 currently offers no
safe per-denoising-step abort, so a request made during its long pipeline call
does not interrupt CUDA mid-kernel; it is honored at the next boundary and no
partial GLB is published.

Only terminal jobs (`succeeded`, `failed`, `cancelled`) expire. The cleanup
thread removes their database row and managed input/output/manifest files after
the configured TTL. Active jobs are never TTL-deleted. Path containment checks
prevent deletion outside the managed runtime roots.

## Authentication and network boundary

When `LOCALFORGE_API_TOKEN` is unset, authentication is disabled for local
development. When set, every endpoint—including `/health` and file downloads—
requires either:

```text
X-LocalForge-Token: <token>
```

or:

```text
Authorization: Bearer <token>
```

The web UI can send the same value through
`NEXT_PUBLIC_LOCALFORGE_API_TOKEN`. Any `NEXT_PUBLIC_*` value is embedded in the
browser bundle, so it is suitable only as a local shared guard—not as an
internet-facing secret. An externally reachable deployment needs a server-side
authentication proxy and TLS; do not expose this development engine directly.
Interactive Swagger/ReDoc pages and the OpenAPI JSON route are disabled.

The defaults accept only `localhost`, `127.0.0.1`, and `testserver` hosts and
browser origins on local HTTP(S) ports. If the Windows UI calls an engine bound
inside WSL or another host, update trusted hosts and CORS deliberately rather
than using an unrestricted wildcard.

Unsafe requests (`POST`, `PUT`, `PATCH`, `DELETE`) that carry an `Origin`
header are rejected before body parsing unless that origin matches the same
allowlist. This prevents an unrelated web page from submitting a `no-cors`
generation job to localhost. Origin-less local CLI requests remain supported.

## Environment variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `LOCALFORGE_RUNTIME_ROOT` | `engine/runtime` | SQLite, inputs, outputs, manifests |
| `LOCALFORGE_BACKEND` | `mock` | `mock` or `trellis2` |
| `LOCALFORGE_MAX_IMAGE_BYTES` | `20971520` | Maximum upload bytes (20 MiB) |
| `LOCALFORGE_MAX_IMAGE_PIXELS` | `40000000` | Maximum decoded pixels |
| `LOCALFORGE_MAX_IMAGE_DIMENSION` | `8192` | Maximum width or height |
| `LOCALFORGE_QUEUE_CAPACITY` | `8` | Maximum queued plus running jobs |
| `LOCALFORGE_API_TOKEN` | unset | Optional token protecting every endpoint |
| `LOCALFORGE_CORS_ORIGINS` | empty | Comma-separated exact browser origins |
| `LOCALFORGE_CORS_ORIGIN_REGEX` | local HTTP(S) regex | Additional origin pattern |
| `LOCALFORGE_TRUSTED_HOSTS` | `localhost,127.0.0.1,testserver` | Comma-separated Host allowlist |
| `LOCALFORGE_JOB_TTL_SECONDS` | `604800` | Terminal job/file retention (7 days) |
| `LOCALFORGE_CLEANUP_INTERVAL_SECONDS` | `3600` | Expiry scan interval |
| `LOCALFORGE_MOCK_STEP_DELAY` | `0.15` | Mock progress delay for development/tests |
| `TRELLIS2_ROOT` | unset | Required TRELLIS.2 checkout for `trellis2` |

`ATTN_BACKEND`, `SPARSE_ATTN_BACKEND`, and
`PYTORCH_CUDA_ALLOC_CONF` are given safe defaults by the TRELLIS adapter. The
CUDA build variables are documented in
[`docs/RTX5090-TRELLIS2.md`](../docs/RTX5090-TRELLIS2.md).

## CPU tests

```powershell
engine\.venv\Scripts\python.exe -m pip install `
  -r engine\requirements.txt -r engine\requirements-dev.txt
engine\.venv\Scripts\python.exe -m compileall -q -f `
  -x "(\.venv|runtime|__pycache__)" engine
engine\.venv\Scripts\python.exe -m ruff check engine
engine\.venv\Scripts\python.exe -m ruff format --check engine
engine\.venv\Scripts\python.exe -m pip_audit -r engine\requirements.txt
engine\.venv\Scripts\python.exe -m pytest -q engine\tests
```

These tests cover the mock API and integrity/persistence contract without
loading CUDA. Actual TRELLIS.2 inference, `sm_120` extensions, VRAM use, and
output quality still require the RTX 5090 workstation.
