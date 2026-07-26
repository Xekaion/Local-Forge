from __future__ import annotations

from pathlib import Path

import pytest
import trimesh
from integrity import IntegrityError, validate_glb
from store import RuntimeLock


def test_glb_validator_rejects_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "corrupt.glb"
    path.write_bytes(b"not a glb")
    with pytest.raises(IntegrityError):
        validate_glb(path)


def test_glb_validator_reports_mesh_geometry(tmp_path: Path) -> None:
    path = tmp_path / "box.glb"
    mesh = trimesh.creation.box()
    path.write_bytes(trimesh.Scene(mesh).export(file_type="glb"))
    result = validate_glb(path)
    assert result["geometry_count"] == 1
    assert result["vertex_count"] == 8
    assert result["face_count"] == 12
    assert result["finite"] is True


def test_glb_validator_rejects_all_degenerate_faces(tmp_path: Path) -> None:
    path = tmp_path / "degenerate.glb"
    mesh = trimesh.Trimesh(
        vertices=[[0, 0, 0], [1, 0, 0], [0, 1, 0]],
        faces=[[0, 0, 0]],
        process=False,
    )
    path.write_bytes(trimesh.Scene(mesh).export(file_type="glb"))
    with pytest.raises(IntegrityError, match="퇴화"):
        validate_glb(path)


def test_runtime_lock_allows_only_one_worker(tmp_path: Path) -> None:
    first = RuntimeLock(tmp_path / "engine.lock")
    second = RuntimeLock(tmp_path / "engine.lock")
    assert first.acquire() is True
    try:
        assert second.acquire() is False
    finally:
        first.release()
    assert second.acquire() is True
    second.release()
