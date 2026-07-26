from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from conftest import image_bytes, upload, wait_for_status
from fastapi.testclient import TestClient
from PIL import Image

from app import Settings, create_app


def test_health_reports_limits_queue_persistence_and_security(
    client: TestClient,
) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "online"
    assert body["schema_version"] == "1.0"
    assert body["queue"]["single_worker"] is True
    assert body["queue"]["capacity"] == 8
    assert body["limits"]["max_image_pixels"] == 40_000_000
    assert body["limits"]["normalization_concurrency"] == 1
    assert body["persistence"]["type"] == "sqlite"
    assert body["security"]["api_token_required"] is False
    assert "test-secret" not in json.dumps(body)


def test_interactive_api_docs_and_schema_are_not_exposed(client: TestClient) -> None:
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_cors_and_trusted_host_boundaries(client: TestClient) -> None:
    allowed = client.options(
        "/health",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "http://localhost:3000"

    denied_origin = client.options(
        "/health",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert denied_origin.status_code == 400
    assert "access-control-allow-origin" not in denied_origin.headers

    denied_host = client.get("/health", headers={"Host": "evil.example"})
    assert denied_host.status_code == 400

    denied_post = client.post(
        "/v1/generations",
        content=b"body-must-not-be-parsed",
        headers={
            "Content-Type": "multipart/form-data; boundary=invalid",
            "Origin": "https://evil.example",
        },
    )
    assert denied_post.status_code == 403
    assert not client.app.state.manager.store.queued()


def test_nonfinite_float_settings_are_rejected(monkeypatch) -> None:
    for value in ("nan", "inf", "-inf"):
        monkeypatch.setenv("LOCALFORGE_CLEANUP_INTERVAL_SECONDS", value)
        with pytest.raises(RuntimeError):
            Settings.from_env()
    monkeypatch.delenv("LOCALFORGE_CLEANUP_INTERVAL_SECONDS")


def test_real_image_is_decoded_exif_normalized_and_metadata_removed(
    client: TestClient,
) -> None:
    jpeg = image_bytes("JPEG", size=(18, 10), exif_orientation=6)
    response = upload(client, jpeg, "image/jpeg")
    assert response.status_code == 202
    job = response.json()

    manager = client.app.state.manager
    record = manager.store.get(job["id"])
    assert record is not None
    safe_path = Path(record["input_path"])
    safe_bytes = safe_path.read_bytes()
    assert hashlib.sha256(safe_bytes).hexdigest() == job["input_sha256"]
    with Image.open(safe_path) as safe:
        assert safe.format == "PNG"
        assert safe.size == (10, 18)
        assert not safe.getexif()
        assert "comment" not in safe.info


def test_disguised_corrupt_and_oversized_images_are_rejected(
    tmp_path: Path,
) -> None:
    settings = Settings(
        runtime_root=tmp_path / "runtime",
        max_image_pixels=100,
        max_image_dimension=10,
        mock_step_delay=0.01,
    )
    with TestClient(create_app(settings)) as client:
        disguised = upload(
            client,
            image_bytes("PNG", size=(5, 5)),
            "image/jpeg",
        )
        assert disguised.status_code == 415

        corrupt = upload(client, b"\x89PNG\r\n\x1a\nnot-a-real-image")
        assert corrupt.status_code == 422

        too_large = upload(
            client,
            image_bytes("PNG", size=(11, 9)),
            "image/png",
        )
        assert too_large.status_code == 413

        unsupported = upload(client, b"GIF89a", "image/gif")
        assert unsupported.status_code == 415


def test_upload_byte_limit_is_enforced_before_decode(tmp_path: Path) -> None:
    settings = Settings(
        runtime_root=tmp_path / "runtime",
        max_image_bytes=16,
        mock_step_delay=0.01,
    )
    with TestClient(create_app(settings)) as client:
        response = upload(client, image_bytes())
        assert response.status_code == 413
        assert not client.app.state.manager.store.queued()


def test_nonempty_prompt_is_not_silently_ignored(client: TestClient) -> None:
    response = client.post(
        "/v1/generations",
        files={"image": ("sample.png", image_bytes(), "image/png")},
        data={"prompt": "make it metallic"},
    )
    assert response.status_code == 501
    assert not client.app.state.manager.store.queued()


def test_trellis_rejects_unsupported_texture_false_without_loading_gpu(
    tmp_path: Path,
) -> None:
    settings = Settings(
        runtime_root=tmp_path / "runtime",
        backend="trellis2",
        mock_step_delay=0.01,
    )
    with TestClient(create_app(settings)) as client:
        response = upload(client, image_bytes(), texture=False)
        assert response.status_code == 422
        assert not client.app.state.manager.store.queued()


def test_generation_lifecycle_manifest_and_checksum(
    client: TestClient,
    tmp_path: Path,
) -> None:
    created = upload(client, image_bytes())
    assert created.status_code == 202
    initial = created.json()
    assert initial["status"] == "queued"
    assert len(initial["input_sha256"]) == 64
    assert initial["created_at"].endswith("Z")

    finished = wait_for_status(client, initial["id"], {"succeeded"})
    assert finished["progress"] == 100
    assert finished["output_bytes"] > 0
    assert len(finished["output_sha256"]) == 64
    assert finished["model_url"].endswith("/output")
    assert finished["manifest_url"].endswith("/manifest")

    output = client.get(finished["model_url"])
    assert output.status_code == 200
    assert output.headers["content-type"].startswith("model/gltf-binary")
    assert output.headers["etag"] == f'"{finished["output_sha256"]}"'
    assert output.headers["x-checksum-sha256"] == finished["output_sha256"]
    assert output.headers["content-digest"].startswith("sha-256=:")
    assert hashlib.sha256(output.content).hexdigest() == finished["output_sha256"]

    partial = client.get(
        finished["model_url"],
        headers={"Range": "bytes=0-9"},
    )
    assert partial.status_code == 206
    assert len(partial.content) == 10
    assert "content-digest" not in partial.headers
    assert partial.headers["x-checksum-sha256"] == finished["output_sha256"]

    manifest_response = client.get(finished["manifest_url"])
    assert manifest_response.status_code == 200
    manifest = manifest_response.json()
    assert manifest["schema_version"] == "1.0"
    assert manifest["job_id"] == initial["id"]
    assert manifest["input"]["sha256"] == initial["input_sha256"]
    assert manifest["input"]["metadata_stripped"] is True
    assert manifest["output"]["sha256"] == finished["output_sha256"]
    assert manifest["output"]["bytes"] == finished["output_bytes"]
    assert manifest["output"]["validation"]["geometry_count"] >= 1
    assert manifest["output"]["validation"]["vertex_count"] > 0
    assert manifest["output"]["validation"]["face_count"] > 0
    assert manifest["output"]["validation"]["finite"] is True
    assert str(tmp_path) not in json.dumps(manifest)

    manifest_checksum = hashlib.sha256(manifest_response.content).hexdigest()
    assert finished["manifest_sha256"] == manifest_checksum
    assert manifest_response.headers["etag"] == f'"{manifest_checksum}"'
    assert manifest_response.headers["x-checksum-sha256"] == manifest_checksum


def test_output_tampering_is_detected(client: TestClient) -> None:
    created = upload(client, image_bytes()).json()
    finished = wait_for_status(client, created["id"], {"succeeded"})
    record = client.app.state.manager.store.get(created["id"])
    assert record is not None
    path = Path(record["output_path"])
    path.write_bytes(path.read_bytes() + b"tampered")
    response = client.get(finished["model_url"])
    assert response.status_code == 409
    assert "checksum" in response.json()["detail"]

    manifest_path = Path(record["manifest_path"])
    manifest_path.write_bytes(manifest_path.read_bytes() + b" ")
    manifest_response = client.get(finished["manifest_url"])
    assert manifest_response.status_code == 409
    assert "checksum" in manifest_response.json()["detail"]


def test_persisted_input_tampering_fails_before_generation(
    tmp_path: Path,
) -> None:
    settings = Settings(
        runtime_root=tmp_path / "runtime",
        queue_capacity=2,
        mock_step_delay=0.15,
    )
    with TestClient(create_app(settings)) as client:
        first = upload(client, image_bytes()).json()
        wait_for_status(client, first["id"], {"running"})
        second = upload(client, image_bytes(size=(20, 20))).json()
        record = client.app.state.manager.store.get(second["id"])
        assert record is not None
        input_path = Path(record["input_path"])
        input_path.write_bytes(input_path.read_bytes() + b"tampered")

        failed = wait_for_status(client, second["id"], {"failed"})
        assert "checksum" in failed["error"]
        assert failed.get("model_url") is None


def test_idempotency_replays_and_rejects_key_reuse(
    client: TestClient,
) -> None:
    payload = image_bytes()
    headers = {"Idempotency-Key": "upload:test-001"}
    first = upload(client, payload, headers=headers)
    assert first.status_code == 202
    second = upload(client, payload, headers=headers)
    assert second.status_code == 202
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["idempotency_replayed"] is True

    conflict = upload(
        client,
        payload,
        headers=headers,
        quality="draft",
    )
    assert conflict.status_code == 409


def test_token_authentication_accepts_custom_and_bearer_headers(
    tmp_path: Path,
) -> None:
    settings = Settings(
        runtime_root=tmp_path / "runtime",
        api_token="test-secret",
        mock_step_delay=0.01,
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").status_code == 401
        assert (
            client.get(
                "/health",
                headers={"X-LocalForge-Token": "test-secret"},
            ).status_code
            == 200
        )
        assert (
            client.get(
                "/health",
                headers={"Authorization": "Bearer test-secret"},
            ).status_code
            == 200
        )
        body = client.get(
            "/health",
            headers={"X-LocalForge-Token": "test-secret"},
        ).json()
        assert body["security"]["api_token_required"] is True
        assert "test-secret" not in json.dumps(body)


def test_running_and_queued_jobs_can_be_cancelled(tmp_path: Path) -> None:
    settings = Settings(
        runtime_root=tmp_path / "runtime",
        queue_capacity=2,
        mock_step_delay=0.3,
    )
    with TestClient(create_app(settings)) as client:
        first = upload(client, image_bytes()).json()
        wait_for_status(client, first["id"], {"running"})
        second_response = upload(client, image_bytes(size=(20, 20)))
        assert second_response.status_code == 202
        second = second_response.json()

        queued_cancel = client.delete(f"/v1/generations/{second['id']}")
        assert queued_cancel.status_code == 200
        assert queued_cancel.json()["status"] == "cancelled"

        replacement = upload(client, image_bytes(size=(18, 18)))
        assert replacement.status_code == 202
        replacement_cancel = client.delete(
            f"/v1/generations/{replacement.json()['id']}"
        )
        assert replacement_cancel.status_code == 200
        assert replacement_cancel.json()["status"] == "cancelled"

        running_cancel = client.delete(f"/v1/generations/{first['id']}")
        assert running_cancel.status_code == 200
        cancelled = wait_for_status(client, first["id"], {"cancelled"})
        assert cancelled["stage"] == "취소됨"


def test_queue_capacity_returns_429(tmp_path: Path) -> None:
    settings = Settings(
        runtime_root=tmp_path / "runtime",
        queue_capacity=1,
        mock_step_delay=0.5,
    )
    with TestClient(create_app(settings)) as client:
        first = upload(client, image_bytes())
        assert first.status_code == 202
        wait_for_status(client, first.json()["id"], {"running"})
        # Admission control must reject before attempting an expensive decode.
        full = upload(client, b"not-an-image")
        assert full.status_code == 429
        assert full.headers["retry-after"] == "2"


def test_worker_survives_transient_claim_failure(tmp_path: Path) -> None:
    application = create_app(
        Settings(
            runtime_root=tmp_path / "runtime",
            mock_step_delay=0.01,
        )
    )
    manager = application.state.manager
    original_claim = manager.store.claim
    calls = 0

    def flaky_claim(job_id: str) -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("transient test failure")
        return original_claim(job_id)

    manager.store.claim = flaky_claim
    with TestClient(application) as client:
        created = upload(client, image_bytes()).json()
        finished = wait_for_status(client, created["id"], {"succeeded"})
        assert finished["output_sha256"]
        health = client.get("/health").json()
        assert health["queue"]["worker_alive"] is True
        assert calls >= 2


def test_restart_marks_running_interrupted_and_resumes_queued(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    slow = Settings(
        runtime_root=runtime,
        queue_capacity=2,
        mock_step_delay=0.5,
    )
    with TestClient(create_app(slow)) as client:
        running = upload(client, image_bytes()).json()
        wait_for_status(client, running["id"], {"running"})
        queued = upload(client, image_bytes(size=(20, 20))).json()
        assert queued["status"] == "queued"

    recovered = Settings(
        runtime_root=runtime,
        queue_capacity=2,
        mock_step_delay=0.01,
    )
    with TestClient(create_app(recovered)) as client:
        interrupted = client.get(f"/v1/generations/{running['id']}").json()
        assert interrupted["status"] == "failed"
        assert interrupted["stage"] == "interrupted"
        assert "재시작" in interrupted["error"]
        resumed = wait_for_status(client, queued["id"], {"succeeded"})
        assert resumed["output_sha256"]
        health = client.get("/health").json()
        assert health["persistence"]["running_jobs_recovered_as_interrupted"] == 1


def test_ttl_cleanup_removes_artifacts_and_job(
    client: TestClient,
) -> None:
    created = upload(client, image_bytes()).json()
    wait_for_status(client, created["id"], {"succeeded"})
    manager = client.app.state.manager
    record = manager.store.get(created["id"])
    assert record is not None
    paths = [
        Path(record["input_path"]),
        Path(record["output_path"]),
        Path(record["manifest_path"]),
    ]
    assert all(path.is_file() for path in paths)

    old = (
        (datetime.now(timezone.utc) - timedelta(days=30))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    with sqlite3.connect(manager.store.path) as connection:
        connection.execute(
            "UPDATE jobs SET updated_at = ? WHERE id = ?",
            (old, created["id"]),
        )
        connection.commit()

    assert manager.cleanup_expired() == 1
    assert manager.store.get(created["id"]) is None
    assert all(not path.exists() for path in paths)
