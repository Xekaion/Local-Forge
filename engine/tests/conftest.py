from __future__ import annotations

import io
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from PIL import Image

ENGINE_ROOT = Path(__file__).resolve().parents[1]
if str(ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENGINE_ROOT))

# Importing app creates Uvicorn's module-level application. Point even that
# unused instance at an isolated temporary runtime so tests never touch the
# developer's engine/runtime database or artifacts.
MODULE_RUNTIME = tempfile.TemporaryDirectory(prefix="localforge-test-module-")
os.environ["LOCALFORGE_RUNTIME_ROOT"] = MODULE_RUNTIME.name

from app import Settings, create_app


def image_bytes(
    image_format: str = "PNG",
    *,
    size: tuple[int, int] = (32, 24),
    exif_orientation: int | None = None,
) -> bytes:
    image = Image.new("RGB", size, (73, 146, 219))
    output = io.BytesIO()
    kwargs: dict[str, Any] = {}
    if exif_orientation is not None:
        exif = Image.Exif()
        exif[274] = exif_orientation
        kwargs["exif"] = exif
        kwargs["comment"] = b"private test metadata"
    image.save(output, format=image_format, **kwargs)
    return output.getvalue()


def upload(
    client: TestClient,
    payload: bytes,
    content_type: str = "image/png",
    *,
    headers: dict[str, str] | None = None,
    quality: str = "studio",
    texture: bool = True,
):
    extension = {
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/webp": "webp",
    }.get(content_type, "bin")
    return client.post(
        "/v1/generations",
        files={"image": (f"sample.{extension}", payload, content_type)},
        data={
            "quality": quality,
            "remesh": "true",
            "texture": str(texture).lower(),
        },
        headers=headers,
    )


def wait_for_status(
    client: TestClient,
    job_id: str,
    expected: set[str],
    timeout: float = 8.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = client.get(f"/v1/generations/{job_id}")
        assert response.status_code == 200
        latest = response.json()
        if latest["status"] in expected:
            return latest
        time.sleep(0.02)
    pytest.fail(f"job did not reach {expected}; latest={latest}")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        runtime_root=tmp_path / "runtime",
        mock_step_delay=0.02,
        cleanup_interval_seconds=60,
    )


@pytest.fixture
def app(settings: Settings):
    return create_app(settings)


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client
