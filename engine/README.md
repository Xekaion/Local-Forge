# LocalForge engine

The bridge exposes one local API for the web studio:

- `GET /health`
- `POST /v1/generations`
- `GET /v1/generations/{id}`
- `GET /outputs/{id}.glb`

`LOCALFORGE_BACKEND=mock` verifies the full upload, queue, progress, preview, and
download path without a GPU. On the RTX 5090 machine, set
`LOCALFORGE_BACKEND=trellis2` and `TRELLIS2_ROOT` to the TRELLIS.2 checkout.

The TRELLIS adapter follows Microsoft's public minimal example and exports
PBR-ready GLB files. Run one worker process because the 4B pipeline owns most of
the GPU memory.
