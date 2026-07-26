# LocalForge

LocalForge is a local-first image-to-3D studio built for an RTX 5090 workstation.
The web app uploads an image to a small local engine, follows the generation
job, previews the returned GLB, and downloads the asset.

To continue this project on another RTX 5090 PC or ChatGPT/Codex account, start
with [HANDOFF.md](HANDOFF.md).

## What works now

- Image upload with local preview and validation
- Local engine health detection
- Async generation jobs and progress stages
- Interactive GLB preview and download
- Mock backend for end-to-end testing without a GPU
- TRELLIS.2 adapter based on Microsoft's public image-to-3D example
- Canonical image normalization, a bounded single-worker queue, cancellation,
  and SQLite job persistence
- GLB reload validation, SHA-256 checksums, and a downloadable provenance
  manifest
- Optional API token, local-origin/host restrictions, and terminal-job TTL
  cleanup
- Strict TypeScript/ESLint checks and same-origin/nosniff/frame/referrer/
  permissions headers on hosted web responses

## Run the tested preview

In one PowerShell window:

```powershell
npm ci
npm run dev
```

In another:

```powershell
python -m venv engine\.venv
engine\.venv\Scripts\python.exe -m pip install -r engine\requirements.txt
$env:LOCALFORGE_BACKEND = "mock"
engine\.venv\Scripts\python.exe -m uvicorn app:app --app-dir engine --host 127.0.0.1 --port 8000
```

Open the local address printed by the web server. The mock backend creates a
small preview GLB to verify the complete product flow; it is not an AI result.

## Runtime safety

The engine does not trust the uploaded filename or MIME declaration alone.
PNG/JPEG/WEBP input is decoded, verified, EXIF-oriented, stripped of metadata,
bounded by byte/pixel/dimension limits, and re-encoded as a canonical PNG. A
single-admission semaphore bounds concurrent image decoding, then a bounded
queue feeds exactly one generation worker so multiple requests cannot run the
4B pipeline concurrently. An OS-released lock also prevents a second process
from opening another worker against the same runtime root.

Completed output is reloaded with `trimesh` and checked for finite, bounded,
non-empty mesh geometry before it is published. The API then exposes the GLB
with SHA-256 response headers and a JSON manifest containing input/output
checksums, generation settings, validation counts, and runtime provenance.
The browser fetches both artifacts with authentication when configured,
recomputes their SHA-256 values, cross-checks the job/header/manifest records,
and gives `model-viewer` only the verified in-memory GLB blob.
Jobs and idempotency keys are stored in SQLite under `engine/runtime`; completed
jobs and their files expire after a configurable TTL.

Cancellation is cooperative. A queued job cancels immediately, while a running
TRELLIS.2 job stops only at safe stage boundaries because the upstream pipeline
does not expose a safe per-denoising-step abort.

See [engine/README.md](engine/README.md) for the API, all
`LOCALFORGE_*` settings, authentication headers, persistence, and cleanup
behavior.

## Quality gates

The same CPU-safe checks can run on a computer without an NVIDIA GPU:

```powershell
npm ci
npm run lint
npm run typecheck
npm test

python -m venv engine\.venv
engine\.venv\Scripts\python.exe -m pip install `
  -r engine\requirements.txt -r engine\requirements-dev.txt
engine\.venv\Scripts\python.exe -m compileall -q -f `
  -x "(\.venv|runtime|__pycache__)" engine
engine\.venv\Scripts\python.exe -m ruff check engine
engine\.venv\Scripts\python.exe -m ruff format --check engine
engine\.venv\Scripts\python.exe -m pip_audit -r engine\requirements.txt
engine\.venv\Scripts\python.exe -m pytest -q engine\tests
```

GitHub Actions runs these checks with Node 22 and Python 3.10. The npm lockfile
reports zero findings from a full `npm audit`; CI also audits the directly
pinned Python runtime requirements and their resolved dependency set with
`pip-audit`, which reports no known vulnerabilities as of the handoff date.
Python transitive packages are resolved at install time, so this is a dated
audit snapshot rather than a byte-for-byte cross-platform lock. A separate
CodeQL workflow analyzes JavaScript/TypeScript and Python. Workflow actions are
pinned to reviewed full commit SHAs, while Dependabot tracks npm, pip, and
GitHub Actions updates. These checks do **not** install CUDA, download the 4B
checkpoint, or prove RTX 5090 inference; those checks remain on the target
workstation.

The workflow layout follows GitHub's official
[Node.js CI](https://docs.github.com/en/actions/tutorials/build-and-test-code/nodejs),
[Python CI](https://docs.github.com/en/actions/tutorials/build-and-test-code/python),
[CodeQL advanced setup](https://docs.github.com/en/code-security/how-tos/find-and-fix-code-vulnerabilities/configure-code-scanning/configuring-advanced-setup-for-code-scanning),
and [Dependabot configuration](https://docs.github.com/en/code-security/concepts/supply-chain-security/about-the-dependabot-yml-file)
guidance.

The current dependency snapshot is Next.js 16.2.12, React 19.2.8,
`@cloudflare/vite-plugin` 1.47.0, Vite 8.1.5, and Wrangler 4.114.0. Exact
transitive versions remain locked by `package-lock.json`; review that lockfile
and CI when Dependabot proposes an update.

## Connect TRELLIS.2

Follow [docs/RTX5090-TRELLIS2.md](docs/RTX5090-TRELLIS2.md) on the computer that
actually contains the RTX 5090. Run a single engine worker because the model
occupies most of the 32GB VRAM.
