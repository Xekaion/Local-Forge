from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import os
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import trimesh
from PIL import Image, ImageOps, UnidentifiedImageError

CONTENT_TYPE_FORMATS: dict[str, set[str]] = {
    "image/png": {"PNG"},
    "image/jpeg": {"JPEG"},
    "image/webp": {"WEBP"},
}


class IntegrityError(ValueError):
    def __init__(self, detail: str, status_code: int = 422) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


@dataclass(frozen=True)
class NormalizedImage:
    data: bytes
    sha256: str
    source_format: str
    width: int
    height: int
    mode: str


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def content_digest_from_hex(checksum: str) -> str:
    raw = bytes.fromhex(checksum)
    return f"sha-256=:{base64.b64encode(raw).decode('ascii')}:"


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    data = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    atomic_write_bytes(path, data)


def normalize_uploaded_image(
    data: bytes,
    declared_content_type: str,
    *,
    max_pixels: int,
    max_dimension: int,
) -> NormalizedImage:
    content_type = declared_content_type.split(";", 1)[0].strip().lower()
    allowed_formats = CONTENT_TYPE_FORMATS.get(content_type)
    if allowed_formats is None:
        raise IntegrityError("PNG, JPEG, WEBP 이미지만 지원합니다.", 415)

    Image.MAX_IMAGE_PIXELS = max_pixels
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as probe:
                source_format = (probe.format or "").upper()
                if source_format not in allowed_formats:
                    raise IntegrityError(
                        "파일 내용과 선언된 이미지 형식이 일치하지 않습니다.",
                        415,
                    )
                if getattr(probe, "n_frames", 1) != 1:
                    raise IntegrityError("애니메이션 이미지는 지원하지 않습니다.")
                probe.verify()

            with Image.open(io.BytesIO(data)) as source:
                source.load()
                normalized = ImageOps.exif_transpose(source)
                width, height = normalized.size
                if width <= 0 or height <= 0:
                    raise IntegrityError("이미지 치수가 올바르지 않습니다.")
                if width > max_dimension or height > max_dimension:
                    raise IntegrityError(
                        f"이미지 한 변은 {max_dimension}px 이하여야 합니다.",
                        413,
                    )
                if width * height > max_pixels:
                    raise IntegrityError(
                        f"이미지는 {max_pixels:,} 픽셀 이하여야 합니다.",
                        413,
                    )

                has_alpha = normalized.mode in {"RGBA", "LA"} or (
                    normalized.mode == "P" and "transparency" in normalized.info
                )
                clean = normalized.convert("RGBA" if has_alpha else "RGB")
                output = io.BytesIO()
                # Re-encoding to PNG deliberately drops EXIF, ICC, comments,
                # and other source metadata while preserving alpha.
                clean.save(output, format="PNG", optimize=False, compress_level=6)
                canonical = output.getvalue()
    except IntegrityError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise IntegrityError("이미지 픽셀 수 제한을 초과했습니다.", 413) from exc
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise IntegrityError("손상되었거나 해석할 수 없는 이미지입니다.") from exc

    return NormalizedImage(
        data=canonical,
        sha256=sha256_bytes(canonical),
        source_format=source_format,
        width=width,
        height=height,
        mode=clean.mode,
    )


def validate_glb(
    path: Path,
    *,
    max_vertices: int = 10_000_000,
    max_faces: int = 20_000_000,
) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size == 0:
        raise IntegrityError("생성된 GLB 파일이 비어 있습니다.", 500)

    try:
        loaded = trimesh.load(path, file_type="glb", force="scene", process=False)
    except Exception as exc:
        raise IntegrityError("생성된 GLB를 다시 읽을 수 없습니다.", 500) from exc

    scene = loaded if isinstance(loaded, trimesh.Scene) else trimesh.Scene(loaded)
    meshes = [
        geometry
        for geometry in scene.geometry.values()
        if isinstance(geometry, trimesh.Trimesh)
    ]
    if not meshes:
        raise IntegrityError("GLB에 유효한 메시 지오메트리가 없습니다.", 500)

    vertices_total = 0
    faces_total = 0
    for mesh in meshes:
        vertices = np.asarray(mesh.vertices)
        faces = np.asarray(mesh.faces)
        if vertices.ndim != 2 or vertices.shape[1] != 3 or len(vertices) == 0:
            raise IntegrityError("GLB에 잘못된 vertex 배열이 있습니다.", 500)
        if faces.ndim != 2 or faces.shape[1] != 3 or len(faces) == 0:
            raise IntegrityError("GLB에 잘못된 face 배열이 있습니다.", 500)
        if not np.isfinite(vertices).all():
            raise IntegrityError("GLB vertex에 NaN 또는 Infinity가 있습니다.", 500)
        if not np.issubdtype(faces.dtype, np.integer):
            raise IntegrityError("GLB face index 형식이 올바르지 않습니다.", 500)
        if int(faces.min()) < 0 or int(faces.max()) >= len(vertices):
            raise IntegrityError("GLB face index가 vertex 범위를 벗어났습니다.", 500)
        triangles = vertices[faces]
        twice_area = np.linalg.norm(
            np.cross(
                triangles[:, 1] - triangles[:, 0],
                triangles[:, 2] - triangles[:, 0],
            ),
            axis=1,
        )
        if not np.any(twice_area > 1e-12):
            raise IntegrityError("GLB 메시의 모든 face가 퇴화했습니다.", 500)

        bounds = np.asarray(mesh.bounds, dtype=np.float64)
        if bounds.shape != (2, 3) or not np.isfinite(bounds).all():
            raise IntegrityError("GLB bounds가 유효하지 않습니다.", 500)
        vertices_total += len(vertices)
        faces_total += len(faces)
        if vertices_total > max_vertices or faces_total > max_faces:
            raise IntegrityError("GLB 지오메트리 안전 제한을 초과했습니다.", 500)

    for node_name in scene.graph.nodes_geometry:
        transform, _ = scene.graph[node_name]
        matrix = np.asarray(transform, dtype=np.float64)
        if matrix.shape != (4, 4) or not np.isfinite(matrix).all():
            raise IntegrityError("GLB scene transform이 유효하지 않습니다.", 500)

    scene_bounds = np.asarray(scene.bounds, dtype=np.float64)
    if scene_bounds.shape != (2, 3) or not np.isfinite(scene_bounds).all():
        raise IntegrityError("GLB scene bounds가 유효하지 않습니다.", 500)
    lower, upper = scene_bounds
    extents = upper - lower
    if not np.isfinite(extents).all() or math.isclose(
        float(np.linalg.norm(extents)), 0.0, abs_tol=1e-12
    ):
        raise IntegrityError("GLB bounds의 크기가 0이거나 유효하지 않습니다.", 500)

    return {
        "geometry_count": len(meshes),
        "vertex_count": vertices_total,
        "face_count": faces_total,
        "bounds": [lower.tolist(), upper.tolist()],
        "finite": True,
    }
