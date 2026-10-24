# CPU timber surface authoring

This workflow makes a small, usable static timber module from existing licensed
PBR photographs without installing PyTorch, downloading weights or using paid
generation. It does not reconstruct an arbitrary object from one image. The
engine's `mock` backend and `localforge_client.py --require-real` boundary remain
unchanged.

The current tested target is a 0.9 × 0.16 × 0.04 metre plank with a 3mm bevel,
closed back and ends, 1,539 UV-seamed vertices and 2,458 triangles. A subtle
0.6mm peak-to-peak relative surface relief is authored from the normal map.
The module is intended for assembled rails, crate slats and bench boards.
It still needs placement, material grading and visual review inside its game.
This is not an AAA quality certification.

## Inputs and use

The source manifest follows the existing game material-library structure:

```json
{
  "license": "CC0-1.0",
  "license_url": "https://polyhaven.com/license",
  "assets": {
    "rough_wood": {
      "source_url": "https://polyhaven.com/a/rough_wood",
      "dimensions_mm": [500, 500],
      "maps": {
        "BaseColor": {"file": "wood/albedo.png", "sha256": "..."},
        "NormalGL": {"file": "wood/normal.png", "sha256": "..."},
        "Roughness": {"file": "wood/roughness.png", "sha256": "..."},
        "AO": {"file": "wood/ao.png", "sha256": "..."}
      }
    }
  }
}
```

All four images must be registered at the same resolution, remain within the
manifest folder and match their SHA-256 hashes. The manifest records a licence;
the tool does not independently grant rights or prove that an arbitrary
third-party manifest is truthful. The actual exercise reused the game's
already acquired [Rough Wood by Rob Tuytel](https://polyhaven.com/a/rough_wood).
The publisher identifies that material's width as 0.5m and provides it under
[CC0, permitting commercial asset reuse](https://polyhaven.com/license).
No additional texture download was needed.

Use a Python environment with the existing engine requirements, then run:

```powershell
python scripts/diagnose_runtime.py --workspace .
python scripts/surface_asset.py --source-manifest PATH/source_manifest.json --material rough_wood --output-dir outputs/timber-panel
python scripts/surface_asset.py --verify outputs/timber-panel/surface.asset.json
```

Generation requires a new output directory and publishes a complete directory
by rename. Existing assets are never overwritten. Optional `--width`, `--height`,
`--thickness`, `--bevel`, `--relief-mm`, `--resolution` and `--cells` parameters
stay within bounded static-surface limits. The default texture resolution is
1024, with 64 cells along the long front edge.

## Output and physical conventions

The single-primitive `TimberPanel.glb` is right-handed, uses metres, has +Y up
and +Z facing forward, and embeds a standard metallic/roughness PBR material.
The original photograph's fibre direction runs along the panel's long X axis.
Six planar UV islands preserve the measured source density and give the bevel,
back and ends non-collapsed UVs. For Unreal, transform position and normal by
`(X,Y,Z) -> (-Z,X,Y)` and multiply positions by 100. Recompute tangents from the
actual UV derivatives, preserving the bitangent sign; assuming tangent +X
would rotate the normal map incorrectly on the front.

Separate PNGs contain base colour, OpenGL normal, roughness, AO, ORM and a
16-bit relative-height field. Base colour is sRGB; scalar, height and normal
data are linear. ORM packs occlusion in R, roughness in G and zero metalness
in B. Scalar uint16 PNGs are explicitly divided by 257 before eight-bit
publication. Pillow's plain `convert('L')` would clip those values to white;
that error was caught using the actual source roughness and is covered by a
regression test.

Normal vectors are re-normalized after downsampling. Fewer than 0.1% backwards
normal outliers may be clamped and counted; a larger invalid population fails
generation. Relative relief uses periodic least-squares normal integration.
Its absolute millimetre range is a user-authored bound, not a measured depth
prediction. Source albedo is not automatically delighted. End faces use
axis-projected source timber, not reconstructed end grain. The tool therefore
does not substitute for a final hero-object sculpt or material pass.

`surface.asset.json` records source hashes/licence, dimensions, derivation,
counts, bounds, colour conventions and every output hash/byte length. Its
identity is `kind: localforge-surface-asset`, `backend: cpu_surface_authoring`,
`is_ai_generated: false`, `is_mock: false`, with approval flags false.
It is deliberately incompatible with the AI-generation handoff descriptor.
The offline verifier checks bytes, geometry counts/bounds, finite geometry,
non-collapsed UVs, closed positive-volume topology and ORM packing. Hashes
detect accidental changes; they do not authenticate an untrusted publisher.

## Actual native exercise, 2026-10-24

The fixed v4 output GLB SHA-256 is
`58d887a85fade115a7812ff1da48431e83f2fd23143b7834fd1d560801732d31`.
The asset was reloaded by trimesh and imported into Blender 4.2.0. Three actual
1200 × 720 Cycles CPU renders, 48 samples each, show the front, bevel/end and
grazing grain. PBR normal and roughness connections were checked in Blender.
The images were viewed individually. An earlier candidate had grain across
the short axis and an overbright review setup; both were corrected before the
fixed output. No mock mesh was used.

Reproduce native inspection with an existing Blender installation:

```powershell
blender --background --factory-startup --python scripts/render_surface_review.py -- outputs/timber-panel outputs/timber-review
```

The tracked [exercise receipt](validation/surface-authoring-2026-10-24.json)
binds the actual output and native PNG hashes. Local raw outputs stay under
ignored `work/`; a consuming game should preserve the adopted GLB, descriptor,
source licence and source hashes in its own asset inventory.

All 68 Python tests pass: 43 existing cases plus 21 surface and four preflight
cases. The new cases check normal integration against an analytic surface,
uint16 scalar conversion, real GLB closure/UVs/physical bounds, input limits,
source tampering, output tampering and false approval/backend identities.
Ruff and compilation also pass. The existing Starlette/httpx deprecation
warning remains. The web application was unchanged.

## Lower-memory inference remains a separate task

The actual workstation has an RTX 3070, 8GB VRAM and compute capability 8.6,
with no PyTorch or TRELLIS.2 checkout in the selected environment. The current
TRELLIS.2 adapter requires compute capability 12. The read-only preflight
preserves a 4GiB disk reserve plus a configurable planning allowance; the
8GiB allowance is not a measured checkpoint-download size.

Official [TripoSR documentation](https://github.com/VAST-AI-Research/TripoSR)
reports roughly 6GB VRAM for its defaults and MIT code/model licensing.
Official [Stable Fast 3D documentation](https://github.com/Stability-AI/stable-fast-3d)
also reports roughly 6GB, experimental Windows support and gated model access.
Its commercial use follows the
[Stability AI Community License](https://stability.ai/license).
These are researched candidates, not implemented LocalForge adapters.
Neither was installed, downloaded or run during this exercise. Total VRAM
alone does not prove a usable inference environment. Paid calls and actual AI
inference runs for this change are both zero.
