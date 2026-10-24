from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pytest
import trimesh
from PIL import Image

MODULE = Path(__file__).resolve().parents[2] / "scripts" / "surface_asset.py"
spec = importlib.util.spec_from_file_location("localforge_surface_asset", MODULE)
surface = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = surface
spec.loader.exec_module(surface)


@pytest.fixture
def sources(tmp_path):
    root = tmp_path / "sources"
    root.mkdir()
    pixels = np.arange(128, dtype=np.uint16)[None, :].repeat(128, axis=0)
    normal = np.zeros((128, 128, 3), dtype=np.uint8)
    normal[:, :, :2], normal[:, :, 2] = 128, 255
    values = {
        "BaseColor": Image.new("RGB", (128, 128), (87, 74, 57)),
        "NormalGL": Image.fromarray(normal),
        "Roughness": Image.fromarray(pixels * 256 + 8192),
        "AO": Image.fromarray((pixels * 500).astype(np.uint16)),
    }
    maps = {}
    for kind, image in values.items():
        path = root / f"{kind}.png"
        image.save(path)
        maps[kind] = {
            "file": path.name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    manifest = {
        "license": "CC0-1.0",
        "license_url": "https://example.test/license",
        "assets": {
            "rough_wood": {
                "source_url": "https://example.test/wood",
                "dimensions_mm": [500, 500],
                "maps": maps,
            }
        },
    }
    path = root / "source_manifest.json"
    path.write_text(json.dumps(manifest))
    return path


def make(sources, tmp_path, **kwargs):
    output = tmp_path / "output"
    data = surface.generate_asset(
        sources, "rough_wood", output, resolution=128, cells=32, **kwargs
    )
    return output, data


def test_closed_metric_glb_and_real_scalar_maps(sources, tmp_path):
    output, data = make(sources, tmp_path)
    result = surface.verify_asset(output / "surface.asset.json")
    assert result["verified"] and not result["is_ai_generated"]
    assert result["vertices"] < 2000
    roughness = np.asarray(Image.open(output / "TimberPanel_Roughness.png"))
    ao = np.asarray(Image.open(output / "TimberPanel_AO.png"))
    assert 20 < roughness.min() < 50 and 140 < roughness.max() < 180
    assert ao.max() > 200 and ao.min() == 0
    assert data["geometry"]["watertight"] and data["geometry"]["winding_consistent"]
    assert data["coordinates"] == {"unit": "metre", "up": "+Y", "front": "+Z"}
    assert data["derivation"]["absolute_height_inferred"] is False
    assert data["geometry"]["volume_m3"] > 0


def test_uvs_keep_grain_on_long_axis_and_no_collapsed_end_faces(sources, tmp_path):
    output, _ = make(sources, tmp_path)
    mesh = next(
        iter(
            trimesh.load(
                output / "TimberPanel.glb", force="scene", process=False
            ).geometry.values()
        )
    )
    front = np.flatnonzero(mesh.face_normals[:, 2] > 0.99)
    indices = np.unique(mesh.faces[front])
    points, uv = mesh.vertices[indices], mesh.visual.uv[indices]
    assert np.allclose(uv[:, 1], (points[:, 0] + 0.45) / 0.5, atol=1e-6)
    assert np.allclose(uv[:, 0], (points[:, 1] + 0.08) / 0.5, atol=1e-6)
    assert surface.verify_asset(output / "surface.asset.json")["verified"]


def test_normal_integration_recovers_relative_sine_and_authored_limit():
    count = 128
    x = np.arange(count) / count
    target = np.sin(2 * np.pi * x)
    gradient = np.cos(2 * np.pi * x) * 0.4
    vectors = np.stack((-gradient, np.zeros(count), np.ones(count)), axis=-1)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    pixels = (
        np.rint((vectors + 1) * 127.5)
        .astype(np.uint8)[None, :, :]
        .repeat(count, axis=0)
    )
    height = surface.relative_height(pixels, (0.5, 0.5), 0.0006)
    assert np.corrcoef(height[count // 2], target)[0, 1] > 0.999
    assert np.ptp(height) <= 0.00060001
    assert np.ptp(surface.relative_height(pixels, (0.5, 0.5), 0)) == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"width": float("nan")},
        {"width": -1},
        {"height": 4},
        {"thickness": 0.001},
        {"bevel": 0.03},
        {"relief_mm": 6},
        {"resolution": 4096},
        {"cells": 1000},
    ],
)
def test_refuse_unbounded_authoring(sources, tmp_path, changes):
    parameters = {"resolution": 128, "cells": 32} | changes
    with pytest.raises(ValueError):
        surface.generate_asset(sources, "rough_wood", tmp_path / "output", **parameters)
    assert not (tmp_path / "output").exists()


def test_source_hash_tampering_is_rejected(sources, tmp_path):
    path = sources.parent / "BaseColor.png"
    path.write_bytes(path.read_bytes() + b"tampered")
    with pytest.raises(ValueError, match="Source hash mismatch"):
        make(sources, tmp_path)


def test_source_paths_cannot_escape_manifest(sources):
    data = json.loads(sources.read_text())
    data["assets"]["rough_wood"]["maps"]["AO"]["file"] = "../source_manifest.json"
    sources.write_text(json.dumps(data))
    with pytest.raises((ValueError, FileNotFoundError)):
        surface.load_sources(sources, "rough_wood")


def test_missing_source_license_is_rejected(sources):
    data = json.loads(sources.read_text())
    del data["license"]
    sources.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="license"):
        surface.load_sources(sources, "rough_wood")


def test_existing_asset_is_never_overwritten(sources, tmp_path):
    output, _ = make(sources, tmp_path)
    original = (output / "TimberPanel.glb").read_bytes()
    with pytest.raises(ValueError, match="never overwritten"):
        surface.generate_asset(sources, "rough_wood", output)
    assert original == (output / "TimberPanel.glb").read_bytes()


@pytest.mark.parametrize(
    "attack", ["bytes", "approval", "backend", "path", "bounds", "count"]
)
def test_offline_verifier_fails_closed(sources, tmp_path, attack):
    output, data = make(sources, tmp_path)
    path = output / "surface.asset.json"
    if attack == "bytes":
        glb = output / "TimberPanel.glb"
        glb.write_bytes(glb.read_bytes() + b"changed")
    elif attack == "approval":
        data["quality_approved"] = True
    elif attack == "backend":
        data["backend"] = "trellis2"
    elif attack == "path":
        data["files"]["../outside.glb"] = data["files"].pop("TimberPanel.glb")
    elif attack == "bounds":
        data["geometry"]["bounds_m"][0][0] = -1000
    else:
        data["geometry"]["vertices"] += 100
    path.write_text(json.dumps(data))
    with pytest.raises((ValueError, FileNotFoundError)):
        surface.verify_asset(path)
