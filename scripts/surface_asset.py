"""Author a metric timber panel from verified PBR photographs on the CPU.

This is a narrow surface-authoring tool, not AI reconstruction. It never calls
the engine, downloads a checkpoint, or treats the mock backend as production.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
import trimesh
from PIL import Image
from scipy.ndimage import gaussian_filter
from trimesh.visual.material import PBRMaterial
from trimesh.visual.texture import TextureVisuals


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_sources(manifest_path: Path, material: str) -> tuple[dict, dict]:
    manifest_path = manifest_path.resolve(strict=True)
    root = manifest_path.parent
    data = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if not data.get("license") or not data.get("license_url"):
        raise ValueError("A source license and license_url must be recorded.")
    asset = data["assets"][material]
    dimensions = np.asarray(asset["dimensions_mm"], dtype=float) / 1000
    if (
        dimensions.shape != (2,)
        or not np.all(np.isfinite(dimensions))
        or np.any(dimensions <= 0)
    ):
        raise ValueError("The source needs two positive measured dimensions_mm.")
    maps = {}
    for kind in ("BaseColor", "NormalGL", "Roughness", "AO"):
        record = asset["maps"][kind]
        path = (root / record["file"]).resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError("Source maps must remain inside the manifest directory.")
        if sha256(path) != record["sha256"]:
            raise ValueError(f"Source hash mismatch: {kind}")
        with Image.open(path) as image:
            if (
                image.width > 8192
                or image.height > 8192
                or image.width * image.height > 33_554_432
            ):
                raise ValueError("Source image exceeds the bounded surface workflow.")
            if kind in ("Roughness", "AO") and image.mode.startswith("I;16"):
                # Pillow's convert('L') clips uint16 to 255 instead of scaling.
                # That silently turns calibrated roughness into constant white.
                pixels = np.rint(np.asarray(image, dtype=np.float64) / 257).astype(
                    np.uint8
                )
                maps[kind] = Image.fromarray(pixels)
            else:
                maps[kind] = image.convert(
                    "RGB" if kind in ("BaseColor", "NormalGL") else "L"
                )
    sizes = {image.size for image in maps.values()}
    if len(sizes) != 1:
        raise ValueError("PBR maps must share the same registered resolution.")
    return maps, {
        "manifest_sha256": sha256(manifest_path),
        "material": material,
        "license": data["license"],
        "license_url": data["license_url"],
        "source_url": asset["source_url"],
        "authors": asset.get("authors", {}),
        "dimensions_m": dimensions.tolist(),
        "maps": {key: asset["maps"][key] for key in maps},
    }


def relative_height(
    normal: np.ndarray, dimensions: tuple[float, float], relief_m: float
) -> np.ndarray:
    """Periodic least-squares normal integration, then explicit artistic scale.

    OpenGL normal Y points up, whereas image rows point down. Input normals do
    not establish absolute height: relief_m is a user-authored peak-to-peak cap.
    """
    values = normal.astype(np.float64) / 127.5 - 1
    denominator = np.maximum(values[:, :, 2], 0.15)
    gradient_x = -values[:, :, 0] / denominator
    gradient_y = values[:, :, 1] / denominator
    rows, columns = gradient_x.shape
    kx = 2 * math.pi * np.fft.fftfreq(columns, d=dimensions[0] / columns)[None, :]
    ky = 2 * math.pi * np.fft.fftfreq(rows, d=dimensions[1] / rows)[:, None]
    divisor = kx * kx + ky * ky
    divisor[0, 0] = 1
    spectrum = (
        -1j * kx * np.fft.fft2(gradient_x) - 1j * ky * np.fft.fft2(gradient_y)
    ) / divisor
    spectrum[0, 0] = 0
    height = gaussian_filter(np.fft.ifft2(spectrum).real, sigma=1.25, mode="wrap")
    low, high = np.quantile(height, [0.005, 0.995])
    if high - low < 1e-12 or relief_m == 0:
        return np.zeros_like(height, dtype=np.float32)
    height = (np.clip(height, low, high) - (low + high) / 2) / (high - low)
    return (height * relief_m).astype(np.float32)


def panel_mesh(
    width: float,
    height: float,
    thickness: float,
    bevel: float,
    relief: np.ndarray,
    dimensions: tuple[float, float],
    cells: int,
) -> tuple[trimesh.Trimesh, int]:
    """Closed panel; +Y up, +Z front, metres, physical source UV scale."""
    nx = cells + 1
    ny = max(9, round(cells * height / width) + 1)
    x = np.linspace(-width / 2 + bevel, width / 2 - bevel, nx)
    y = np.linspace(-height / 2 + bevel, height / 2 - bevel, ny)
    xx, yy = np.meshgrid(x, y)
    # This timber source's fibres run in texture V. Keep them along the panel's
    # long X axis; cross-cut grain would look structurally implausible on a rail.
    uv = np.stack(
        ((yy + height / 2) / dimensions[0], (xx + width / 2) / dimensions[1]), axis=-1
    )
    # Image rows are down; glTF V and the panel's up direction are positive up.
    px = np.mod(uv[:, :, 0], 1) * relief.shape[1]
    py = np.mod(1 - uv[:, :, 1], 1) * relief.shape[0]
    from scipy.ndimage import map_coordinates

    sampled = map_coordinates(
        relief, [py.ravel(), px.ravel()], order=1, mode="wrap"
    ).reshape(ny, nx)
    edge = np.minimum.reduce([xx - x[0], x[-1] - xx, yy - y[0], y[-1] - yy])
    sampled *= np.clip(edge / max(bevel, 0.001), 0, 1)
    vertices = (
        np.stack((xx, yy, thickness / 2 + sampled), axis=-1).reshape(-1, 3).tolist()
    )
    uvs = uv.reshape(-1, 2).tolist()
    faces = []
    for row in range(ny - 1):
        for column in range(nx - 1):
            a = row * nx + column
            faces.extend([[a, a + 1, a + nx + 1], [a, a + nx + 1, a + nx]])
    front_count = len(faces)
    boundary = list(range(nx)) + [row * nx + nx - 1 for row in range(1, ny)]
    boundary += list(range(ny * nx - 2, (ny - 1) * nx - 1, -1))
    boundary += [row * nx for row in range(ny - 2, 0, -1)]
    previous = boundary
    for z, expand in (
        (thickness / 2 - bevel, True),
        (-thickness / 2 + bevel, True),
        (-thickness / 2, False),
    ):
        current = []
        for source in boundary:
            vx, vy, _ = vertices[source]
            if expand:
                vx *= width / (width - 2 * bevel)
                vy *= height / (height - 2 * bevel)
            current.append(len(vertices))
            vertices.append([vx, vy, z])
            uvs.append(
                [(vx + width / 2) / dimensions[0], (vy + height / 2) / dimensions[1]]
            )
        for index, a in enumerate(previous):
            next_index = (index + 1) % len(previous)
            b, c, d = previous[next_index], current[next_index], current[index]
            faces.extend([[a, d, c], [a, c, b]])
        previous = current
    center = len(vertices)
    vertices.append([0, 0, -thickness / 2])
    uvs.append([width / 2 / dimensions[0], height / 2 / dimensions[1]])
    for index, a in enumerate(previous):
        faces.append([a, center, previous[(index + 1) % len(previous)]])
    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
    mesh.visual = TextureVisuals(uv=np.asarray(uvs))
    return mesh, front_count


def project_uv_seams(
    mesh: trimesh.Trimesh, dimensions: tuple[float, float]
) -> trimesh.Trimesh:
    """Separate six planar islands; side UVs never collapse to a zero-width line."""
    vertices, uvs, faces = [], [], []
    minimum = mesh.bounds[0]
    lookup = {}
    for face_index, face in enumerate(mesh.faces):
        normal = mesh.face_normals[face_index]
        axis = int(np.argmax(np.abs(normal)))
        side = 1 if normal[axis] >= 0 else -1
        output_face = []
        for vertex_index in face:
            key = (int(vertex_index), axis, side)
            if key not in lookup:
                lookup[key] = len(vertices)
                point = mesh.vertices[vertex_index]
                vertices.append(point)
                if axis == 2:
                    uv = [
                        (point[1] - minimum[1]) / dimensions[0],
                        (point[0] - minimum[0]) / dimensions[1],
                    ]
                elif axis == 0:
                    uv = [-side * point[2] / dimensions[0], point[1] / dimensions[1]]
                else:
                    uv = [
                        -side * point[2] / dimensions[0],
                        (point[0] - minimum[0]) / dimensions[1],
                    ]
                uvs.append(uv)
            output_face.append(lookup[key])
        faces.append(output_face)
    result = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
    result.visual = TextureVisuals(uv=np.asarray(uvs))
    return result


def generate_asset(
    source_manifest: Path,
    material: str,
    output_dir: Path,
    *,
    width: float = 0.9,
    height: float = 0.16,
    thickness: float = 0.04,
    bevel: float = 0.003,
    relief_mm: float = 0.6,
    resolution: int = 1024,
    cells: int = 64,
) -> dict[str, Any]:
    numeric = (width, height, thickness, bevel, relief_mm)
    if not all(math.isfinite(value) for value in numeric):
        raise ValueError("Dimensions must be finite.")
    if not (0.05 <= width <= 3 and 0.05 <= height <= 3 and 0.005 <= thickness <= 0.2):
        raise ValueError(
            "Use a static surface panel between 5cm and 3m, thickness 5-200mm."
        )
    if not (
        0 < bevel < min(width, height, thickness) / 2
        and 0 <= relief_mm <= min(5, thickness * 200)
    ):
        raise ValueError(
            "Bevel or authored relief exceeds the panel's physical bounds."
        )
    if resolution not in (128, 256, 512, 1024, 2048) or not 16 <= cells <= 256:
        raise ValueError(
            "Use a bounded texture resolution and 16-256 front-grid cells."
        )
    output_dir = output_dir.resolve()
    if output_dir.exists():
        raise ValueError(
            "Output must be a new directory; existing assets are never overwritten."
        )
    maps, source = load_sources(source_manifest, material)
    maps = {
        kind: image.resize((resolution, resolution), Image.Resampling.LANCZOS)
        for kind, image in maps.items()
    }
    # Re-normalize filtered normals, rather than shrinking vector lengths at mip scale.
    normals = np.asarray(maps["NormalGL"], dtype=np.float64) / 127.5 - 1
    corrected_normal_pixels = int(np.count_nonzero(normals[:, :, 2] < 0.05))
    if corrected_normal_pixels > resolution * resolution * 0.001:
        raise ValueError(
            "More than 0.1% of NormalGL pixels face behind the tangent surface."
        )
    normals[:, :, 2] = np.maximum(normals[:, :, 2], 0.05)
    normals /= np.maximum(np.linalg.norm(normals, axis=2, keepdims=True), 1e-12)
    maps["NormalGL"] = Image.fromarray(
        np.rint((normals + 1) * 127.5).clip(0, 255).astype(np.uint8)
    )
    dimensions = tuple(source["dimensions_m"])
    relief = relative_height(np.asarray(maps["NormalGL"]), dimensions, relief_mm / 1000)
    mesh, _ = panel_mesh(width, height, thickness, bevel, relief, dimensions, cells)
    if not mesh.is_watertight or not mesh.is_winding_consistent or mesh.volume <= 0:
        raise ValueError("Generated panel failed closed-volume validation.")
    if not np.isfinite(mesh.vertices).all() or mesh.area_faces.min() <= 1e-14:
        raise ValueError("Generated panel contains invalid or degenerate geometry.")
    orm = np.zeros((resolution, resolution, 3), dtype=np.uint8)
    orm[:, :, 0] = np.asarray(maps["AO"])
    orm[:, :, 1] = np.asarray(maps["Roughness"])
    # Metalness is exactly zero. AO is also supplied as a separately named map.
    maps["ORM"] = Image.fromarray(orm)
    scaled = (
        np.zeros_like(relief, dtype=np.uint16)
        if relief_mm == 0
        else np.rint((relief / (relief_mm / 1000) + 0.5) * 65535)
        .clip(0, 65535)
        .astype(np.uint16)
    )
    maps["RelativeHeight"] = Image.fromarray(scaled)
    pbr = PBRMaterial(
        name="VerifiedTimber",
        baseColorTexture=maps["BaseColor"],
        normalTexture=maps["NormalGL"],
        metallicRoughnessTexture=maps["ORM"],
        occlusionTexture=maps["AO"],
        metallicFactor=0.0,
        roughnessFactor=1.0,
        doubleSided=False,
    )
    export_mesh = project_uv_seams(mesh, dimensions)
    export_mesh.visual.material = pbr
    scene = trimesh.Scene()
    scene.add_geometry(export_mesh, geom_name="TimberPanel", node_name="TimberPanel")
    scene.metadata.update(
        {"authoring": "LocalForge CPU surface authoring", "is_ai_generated": False}
    )
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".localforge-surface-", dir=output_dir.parent
    ) as temporary:
        staging = Path(temporary) / "asset"
        staging.mkdir()
        for kind, image in maps.items():
            image.save(staging / f"TimberPanel_{kind}.png")
        glb_path = staging / "TimberPanel.glb"
        glb_path.write_bytes(scene.export(file_type="glb"))
        reloaded = trimesh.load(glb_path, force="scene", process=False)
        if len(reloaded.geometry) != 1 or not np.allclose(
            reloaded.bounds, mesh.bounds, atol=1e-6
        ):
            raise ValueError("Exported GLB failed reload bounds/material validation.")
        descriptor = {
            "schema_version": 1,
            "kind": "localforge-surface-asset",
            "backend": "cpu_surface_authoring",
            "is_mock": False,
            "is_ai_generated": False,
            "inference_runs": 0,
            "asset_role": "authored_static_surface",
            "rigging_ready": False,
            "quality_approved": False,
            "production_approved": False,
            "coordinates": {"unit": "metre", "up": "+Y", "front": "+Z"},
            "dimensions_m": {
                "width": width,
                "height": height,
                "thickness": thickness,
                "bevel": bevel,
            },
            "source": source,
            "derivation": {
                "method": "periodic least-squares normal integration",
                "absolute_height_inferred": False,
                "authored_relief_peak_to_peak_mm": relief_mm,
                "source_albedo_delighted": False,
                "uv_scale": "source physical dimensions",
                "fibre_alignment": "texture V along panel long X axis",
                "cut_edge_material": "axis-projected source timber; no end-grain reconstruction",
            },
            "geometry": {
                "vertices": len(export_mesh.vertices),
                "topological_vertices": len(mesh.vertices),
                "triangles": len(mesh.faces),
                "watertight": bool(mesh.is_watertight),
                "winding_consistent": bool(mesh.is_winding_consistent),
                "bounds_m": mesh.bounds.tolist(),
                "volume_m3": mesh.volume,
            },
            "textures": {
                "resolution": resolution,
                "normal_convention": "OpenGL +Y",
                "corrected_backward_normal_pixels": corrected_normal_pixels,
                "orm_channels": {"R": "occlusion", "G": "roughness", "B": "metallic=0"},
                "base_color_space": "sRGB",
                "other_maps_color_space": "linear",
            },
            "limitations": [
                "Static timber surfaces only; no unseen-object reconstruction.",
                "Cut edges use axis-projected timber; end-grain is not reconstructed.",
                "Height scale is authored; source normals cannot prove absolute dimensions.",
                "Visual approval and engine integration remain separate.",
            ],
            "paid_calls": 0,
            "network_calls": 0,
            "files": {
                path.name: {"sha256": sha256(path), "bytes": path.stat().st_size}
                for path in sorted(staging.iterdir())
            },
        }
        (staging / "surface.asset.json").write_text(
            json.dumps(descriptor, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        # A new-directory rename publishes all outputs together, descriptor included.
        os.rename(staging, output_dir)
    return descriptor


def verify_asset(descriptor_path: Path) -> dict[str, Any]:
    descriptor_path = descriptor_path.resolve(strict=True)
    root = descriptor_path.parent
    data = json.loads(descriptor_path.read_text(encoding="utf-8"))
    expected = {
        "schema_version": 1,
        "kind": "localforge-surface-asset",
        "backend": "cpu_surface_authoring",
        "is_mock": False,
        "is_ai_generated": False,
        "inference_runs": 0,
        "asset_role": "authored_static_surface",
        "rigging_ready": False,
        "quality_approved": False,
        "production_approved": False,
        "paid_calls": 0,
        "network_calls": 0,
    }
    if any(data.get(key) != value for key, value in expected.items()):
        raise ValueError(
            "Surface descriptor must retain its unapproved CPU-authoring identity."
        )
    if not data.get("source", {}).get("license") or not data["source"].get(
        "license_url"
    ):
        raise ValueError("Source license identity is missing.")
    for name, record in data["files"].items():
        path = (root / name).resolve(strict=True)
        if (
            Path(name).name != name
            or not path.is_relative_to(root)
            or not path.is_file()
        ):
            raise ValueError("Artifact path escape.")
        if path.stat().st_size != record["bytes"] or sha256(path) != record["sha256"]:
            raise ValueError(f"Artifact hash or byte-length mismatch: {name}")
    required = {"TimberPanel.glb"} | {
        f"TimberPanel_{kind}.png"
        for kind in (
            "BaseColor",
            "NormalGL",
            "Roughness",
            "AO",
            "ORM",
            "RelativeHeight",
        )
    }
    if set(data["files"]) != required:
        raise ValueError("Unexpected or missing surface artifacts.")
    scene = trimesh.load(root / "TimberPanel.glb", force="scene", process=False)
    if len(scene.geometry) != 1:
        raise ValueError("Expected a single-primitive authored surface.")
    mesh = next(iter(scene.geometry.values()))
    if (
        len(mesh.vertices) != data["geometry"]["vertices"]
        or len(mesh.faces) != data["geometry"]["triangles"]
    ):
        raise ValueError("Geometry counts differ from the handoff.")
    if (
        not np.isfinite(mesh.vertices).all()
        or not np.isfinite(mesh.vertex_normals).all()
    ):
        raise ValueError("Non-finite geometry.")
    if not np.allclose(mesh.bounds, data["geometry"]["bounds_m"], atol=1e-6):
        raise ValueError("Physical bounds differ from the handoff.")
    uv = mesh.visual.uv[mesh.faces]
    first, second = uv[:, 1] - uv[:, 0], uv[:, 2] - uv[:, 0]
    determinant = first[:, 0] * second[:, 1] - first[:, 1] * second[:, 0]
    if not np.isfinite(uv).all() or np.any(np.abs(determinant) < 1e-12):
        raise ValueError("Invalid or collapsed per-triangle UVs.")
    topology = mesh.copy()
    topology.merge_vertices(merge_tex=True, merge_norm=True)
    if (
        not topology.is_watertight
        or not topology.is_winding_consistent
        or topology.volume <= 0
    ):
        raise ValueError(
            "The UV-seamed GLB does not reconstruct a closed positive-volume panel."
        )
    orm = np.asarray(Image.open(root / "TimberPanel_ORM.png"))
    roughness = np.asarray(Image.open(root / "TimberPanel_Roughness.png"))
    ao = np.asarray(Image.open(root / "TimberPanel_AO.png"))
    if (
        not np.array_equal(orm[:, :, 0], ao)
        or not np.array_equal(orm[:, :, 1], roughness)
        or np.any(orm[:, :, 2])
    ):
        raise ValueError("ORM channel packing differs from the supplied scalar maps.")
    return {
        "verified": True,
        "glb_path": str(root / "TimberPanel.glb"),
        "glb_sha256": data["files"]["TimberPanel.glb"]["sha256"],
        "vertices": len(mesh.vertices),
        "triangles": len(mesh.faces),
        "is_ai_generated": False,
        "quality_approved": False,
        "backend": "cpu_surface_authoring",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--verify",
        type=Path,
        help="Verify a previously published surface.asset.json offline.",
    )
    parser.add_argument("--source-manifest", type=Path)
    parser.add_argument("--material", default="rough_wood")
    parser.add_argument("--output-dir", type=Path)
    for name, default in (
        ("width", 0.9),
        ("height", 0.16),
        ("thickness", 0.04),
        ("bevel", 0.003),
        ("relief-mm", 0.6),
    ):
        parser.add_argument(f"--{name}", type=float, default=default)
    parser.add_argument("--resolution", type=int, default=1024)
    parser.add_argument("--cells", type=int, default=64)
    arguments = vars(parser.parse_args())
    verify = arguments.pop("verify")
    if verify:
        print(json.dumps(verify_asset(verify)))
        return
    if arguments["source_manifest"] is None or arguments["output_dir"] is None:
        parser.error("generation requires --source-manifest and --output-dir")
    descriptor = generate_asset(**arguments)
    print(
        json.dumps(
            {"output_dir": str(arguments["output_dir"].resolve()), "asset": descriptor},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
