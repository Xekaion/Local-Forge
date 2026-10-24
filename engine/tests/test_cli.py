from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from conftest import image_bytes

CLI_PATH = Path(__file__).resolve().parents[2] / "scripts" / "localforge_client.py"
SPEC = importlib.util.spec_from_file_location("localforge_client", CLI_PATH)
assert SPEC and SPEC.loader
cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cli)


@pytest.fixture
def cli_engine(client, monkeypatch):
    requests: list[tuple[str, str]] = []

    def request(url: str, **kwargs: Any):
        path = urlsplit(url).path
        method = kwargs.get("method", "GET")
        requests.append((method, path))
        headers = {}
        if kwargs.get("content_type"):
            headers["Content-Type"] = kwargs["content_type"]
        response = client.request(
            method, path, content=kwargs.get("data"), headers=headers
        )
        return response.status_code, dict(response.headers), response.content

    monkeypatch.setattr(cli, "_request", request)
    return requests


def generate(tmp_path: Path, *flags: str) -> dict[str, Any]:
    image = tmp_path / "owned-dog.png"
    image.write_bytes(image_bytes())
    args = cli.build_parser().parse_args(
        [
            "generate",
            str(image),
            "--out",
            str(tmp_path / "export"),
            "--poll-interval",
            "0.01",
            *flags,
        ]
    )
    return cli.cmd_generate(args)


def verify(handoff: str, *flags: str):
    args = cli.build_parser().parse_args(["verify", handoff, *flags])
    return cli.cmd_verify(args)


def test_generate_and_offline_verify_explicit_mock_fixture(cli_engine, tmp_path: Path):
    result = generate(tmp_path)
    assert result["status"] == "succeeded"
    assert result["is_mock"] is True
    assert result["asset_role"] == "pipeline_fixture"
    assert result["rigging_ready"] is False
    checked = verify(result["handoff_path"])
    assert checked["integrity_verified"] is True
    assert checked["glb_sha256"] == result["glb_sha256"]
    assert checked["quality_approved"] is False
    assert ("POST", "/v1/generations") in cli_engine


def test_require_real_rejects_before_submit_without_export(cli_engine, tmp_path: Path):
    with pytest.raises(cli.ClientError, match="before submit"):
        generate(tmp_path, "--require-real")
    assert cli_engine == [("GET", "/health")]
    assert not (tmp_path / "export").exists()


def test_require_real_rechecks_manifest_if_server_changes_backend(
    cli_engine, monkeypatch, tmp_path: Path
):
    monkeypatch.setattr(cli, "cmd_health", lambda _: {"backend": "trellis2"})
    with pytest.raises(cli.ClientError, match="mock result manifest"):
        generate(tmp_path, "--require-real")
    assert not (tmp_path / "export").exists()


def test_verify_require_real_rejects_mock(cli_engine, tmp_path: Path):
    result = generate(tmp_path)
    with pytest.raises(cli.ClientError, match="mock result manifest"):
        verify(result["handoff_path"], "--require-real")


@pytest.mark.parametrize("part", ["glb_path", "manifest_path"])
def test_verify_rejects_tampered_sibling(cli_engine, tmp_path: Path, part: str):
    result = generate(tmp_path)
    path = Path(result[part])
    path.write_bytes(path.read_bytes() + b"tampered")
    with pytest.raises(cli.ClientError, match="checksum mismatch"):
        verify(result["handoff_path"])


@pytest.mark.parametrize(
    "field,value",
    [
        ("is_mock", False),
        ("backend", "trellis2"),
        ("asset_role", "game_ready"),
        ("rigging_ready", True),
        ("quality_approved", True),
    ],
)
def test_verify_rejects_promoting_fixture_to_approved_asset(
    cli_engine, tmp_path: Path, field: str, value: Any
):
    result = generate(tmp_path)
    path = Path(result["handoff_path"])
    payload = json.loads(path.read_text())
    payload[field] = value
    path.write_text(json.dumps(payload))
    with pytest.raises(cli.ClientError, match="identity mismatch"):
        verify(str(path))


@pytest.mark.parametrize("name", ["../outside.glb", "..\\outside.glb", "C:outside.glb"])
def test_verify_rejects_handoff_path_escape(cli_engine, tmp_path: Path, name: str):
    result = generate(tmp_path)
    path = Path(result["handoff_path"])
    payload = json.loads(path.read_text())
    payload["glb"]["filename"] = name
    path.write_text(json.dumps(payload))
    with pytest.raises(cli.ClientError, match="export directory"):
        verify(str(path))


def test_validation_rejects_missing_digest_and_wrong_job(cli_engine, tmp_path: Path):
    result = generate(tmp_path)
    glb = Path(result["glb_path"]).read_bytes()
    manifest = json.loads(Path(result["manifest_path"]).read_text())
    with pytest.raises(cli.ClientError, match="identity mismatch"):
        cli.validate_artifacts(
            glb, manifest, require_real=False, expected_job="other-job"
        )
    broken = copy.deepcopy(manifest)
    del broken["output"]["sha256"]
    with pytest.raises(cli.ClientError, match="output.sha256"):
        cli.validate_artifacts(
            glb, broken, require_real=False, expected_job=manifest["job_id"]
        )


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf"])
def test_invalid_poll_interval_rejected(value: str):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["generate", "dog.png", "--poll-interval", value])


def test_non_object_json_is_actionable_error():
    with pytest.raises(cli.ClientError, match="JSON object"):
        cli._json_or_error(200, b"[]", "manifest")
