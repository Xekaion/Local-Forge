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

## Run the tested preview

In one PowerShell window:

```powershell
npm install
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

## Connect TRELLIS.2

Follow [docs/RTX5090-TRELLIS2.md](docs/RTX5090-TRELLIS2.md) on the computer that
actually contains the RTX 5090. Run a single engine worker because the model
occupies most of the 32GB VRAM.
