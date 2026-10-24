"""LocalForge engine CLI client.

Stdlib-only bridge for scripts and agents: submit an image to the local
engine, follow the job, download the GLB and manifest, and cross-check
every published SHA-256 before reporting success.

Examples:
    python scripts/localforge_client.py health
    python scripts/localforge_client.py generate input.png --quality draft
    python scripts/localforge_client.py status <job_id>
    python scripts/localforge_client.py cancel <job_id>

Stdout carries exactly one JSON summary object (parse all of stdout);
progress goes to stderr.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import math
import mimetypes
import os
import struct
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

DEFAULT_API = os.getenv("LOCALFORGE_API", "http://127.0.0.1:8000")
TERMINAL_STATES = {"succeeded", "failed", "cancelled"}
SUBMIT_RETRIES = 5

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


class ClientError(RuntimeError):
    pass


def _headers(token: str | None) -> dict[str, str]:
    headers = {"User-Agent": "localforge-client/1.0"}
    if token:
        headers["X-LocalForge-Token"] = token
    return headers


def _request(
    url: str,
    *,
    token: str | None,
    method: str = "GET",
    data: bytes | None = None,
    content_type: str | None = None,
    timeout: float = 60.0,
) -> tuple[int, dict[str, str], bytes]:
    headers = _headers(token)
    if content_type:
        headers["Content-Type"] = content_type
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return (
                response.status,
                {k.lower(): v for k, v in response.headers.items()},
                response.read(),
            )
    except urllib.error.HTTPError as exc:
        body = exc.read()
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, body


def _json_or_error(status: int, body: bytes, context: str) -> dict[str, Any]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ClientError(f"{context}: HTTP {status}, non-JSON body") from exc
    if not isinstance(payload, dict):
        raise ClientError(f"{context}: expected a JSON object")
    if status >= 400:
        detail = payload.get("detail", payload)
        raise ClientError(f"{context}: HTTP {status}: {detail}")
    return payload


def _multipart(
    fields: dict[str, str],
    file_field: str,
    path: Path,
    image_bytes: bytes | None = None,
) -> tuple[bytes, str]:
    content_type = mimetypes.guess_type(path.name)[0]
    if content_type not in {"image/png", "image/jpeg", "image/webp"}:
        raise ClientError(
            f"unsupported image type for {path.name}; use PNG, JPG, or WEBP"
        )
    boundary = uuid.uuid4().hex
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode()
        )
    parts.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{file_field}"; '
            f'filename="{path.name}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode()
    )
    parts.append(path.read_bytes() if image_bytes is None else image_bytes)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def cmd_health(args: argparse.Namespace) -> dict[str, Any]:
    status, _, body = _request(f"{args.api}/health", token=args.token)
    return _json_or_error(status, body, "health")


def cmd_status(args: argparse.Namespace) -> dict[str, Any]:
    status, _, body = _request(
        f"{args.api}/v1/generations/{args.job_id}", token=args.token
    )
    return _json_or_error(status, body, "status")


def cmd_cancel(args: argparse.Namespace) -> dict[str, Any]:
    status, _, body = _request(
        f"{args.api}/v1/generations/{args.job_id}", token=args.token, method="DELETE"
    )
    return _json_or_error(status, body, "cancel")


def _submit(
    args: argparse.Namespace, image_path: Path, image_bytes: bytes
) -> dict[str, Any]:
    # No prompt field: the engine returns HTTP 501 for any non-empty prompt.
    fields = {
        "quality": args.quality,
        "remesh": "true" if args.remesh else "false",
        "texture": "true" if args.texture else "false",
    }
    body, content_type = _multipart(fields, "image", image_path, image_bytes)
    for attempt in range(SUBMIT_RETRIES):
        status, headers, response = _request(
            f"{args.api}/v1/generations",
            token=args.token,
            method="POST",
            data=body,
            content_type=content_type,
            timeout=120.0,
        )
        if status == 429:
            wait = float(headers.get("retry-after", "2"))
            log(f"queue full, retrying in {wait:.0f}s ({attempt + 1}/{SUBMIT_RETRIES})")
            time.sleep(wait)
            continue
        return _json_or_error(status, response, "submit")
    raise ClientError("queue stayed full; submit retries exhausted")


def _download_verified(
    args: argparse.Namespace, url_path: str, expected_sha256: str | None, context: str
) -> tuple[bytes, str]:
    status, headers, body = _request(
        f"{args.api}{url_path}", token=args.token, timeout=300.0
    )
    if status >= 400:
        _json_or_error(status, body, context)
    digest = hashlib.sha256(body).hexdigest()
    header_digest = headers.get("x-checksum-sha256")
    if header_digest and header_digest != digest:
        raise ClientError(f"{context}: X-Checksum-SHA256 header mismatch")
    if expected_sha256 and expected_sha256 != digest:
        raise ClientError(f"{context}: job checksum mismatch")
    return body, digest


def cmd_generate(args: argparse.Namespace) -> dict[str, Any]:
    image_path = Path(args.image).expanduser().resolve()
    if not image_path.is_file():
        raise ClientError(f"image not found: {image_path}")
    # A mock engine creates a generic pipeline fixture, never an image-derived
    # game asset. Check before submission so strict callers do not enqueue it.
    if args.require_real:
        health = cmd_health(args)
        if health.get("backend") != "trellis2":
            raise ClientError(
                "--require-real rejected a non-TRELLIS.2 engine before submit"
            )
    image_bytes = image_path.read_bytes()
    out_dir = Path(args.out).expanduser().resolve()

    job = _submit(args, image_path, image_bytes)
    job_id = job["id"]
    log(f"job {job_id} submitted (quality={args.quality})")

    deadline = time.monotonic() + args.timeout
    last_stage = None
    while job["status"] not in TERMINAL_STATES:
        if time.monotonic() > deadline:
            raise ClientError(
                f"timed out after {args.timeout:.0f}s; job {job_id} is still "
                f"'{job['status']}'. Check status or cancel it manually."
            )
        time.sleep(args.poll_interval)
        status, _, body = _request(
            f"{args.api}/v1/generations/{job_id}", token=args.token
        )
        job = _json_or_error(status, body, "poll")
        stage = f"{job.get('progress', '?')}% {job.get('stage', '')}".strip()
        if stage != last_stage:
            log(f"job {job_id}: {job['status']} {stage}")
            last_stage = stage

    if job["status"] != "succeeded":
        raise ClientError(
            f"job {job_id} ended as '{job['status']}': {job.get('error') or 'no detail'}"
        )

    for checksum in ("input_sha256", "output_sha256", "manifest_sha256"):
        if not _is_sha256(job.get(checksum)):
            raise ClientError(f"job: missing or invalid {checksum}")

    glb_bytes, glb_sha256 = _download_verified(
        args, job["model_url"], job.get("output_sha256"), "output"
    )
    manifest_bytes, manifest_sha256 = _download_verified(
        args, job["manifest_url"], job.get("manifest_sha256"), "manifest"
    )
    manifest = _json_or_error(200, manifest_bytes, "manifest")
    backend_name = validate_artifacts(
        glb_bytes, manifest, require_real=args.require_real, expected_job=job_id
    )
    if manifest.get("input", {}).get("sha256") != job.get("input_sha256"):
        raise ClientError("manifest input.sha256 does not match job canonical input")

    stem = image_path.stem or "asset"
    glb_path = out_dir / f"{stem}-{job_id[:8]}.glb"
    manifest_path = out_dir / f"{stem}-{job_id[:8]}.manifest.json"
    handoff_path = out_dir / f"{stem}-{job_id[:8]}.asset.json"
    is_mock = backend_name == "mock"
    handoff = {
        "schema_version": 1,
        "kind": "localforge-asset-handoff",
        "job_id": job_id,
        "backend": backend_name,
        "is_mock": is_mock,
        "asset_role": "pipeline_fixture" if is_mock else "unreviewed_generated_mesh",
        "rigging_ready": False,
        "quality_approved": False,
        "source_image": {
            "filename": image_path.name,
            "original_sha256": hashlib.sha256(image_bytes).hexdigest(),
            "canonical_sha256": manifest["input"]["sha256"],
        },
        "glb": {
            "filename": glb_path.name,
            "sha256": glb_sha256,
            "bytes": len(glb_bytes),
        },
        "manifest": {"filename": manifest_path.name, "sha256": manifest_sha256},
        "geometry_validation": manifest["output"]["validation"],
        "coordinates": {
            "format": "glTF 2.0",
            "up": "+Y",
            "handedness": "right",
            "unit": "metre",
            "authored_forward": "unknown",
            "physical_scale_verified": False,
            "transform_applied": False,
        },
        "review_required": [
            "Mock geometry is a generic test fixture, not image reconstruction."
            if is_mock
            else "Generated geometry requires visual and topology review.",
            "Verify physical scale and forward direction before rigging.",
            "No armature, skinning, animation, licence clearance or game-quality approval is implied.",
        ],
    }
    # The descriptor is the final commit marker. Consumers require it and verify
    # both siblings; interrupted or altered exports cannot pass `verify`.
    out_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write(glb_path, glb_bytes)
    _atomic_write(manifest_path, manifest_bytes)
    _atomic_write(handoff_path, (json.dumps(handoff, indent=2) + "\n").encode())
    log(f"saved {glb_path}")

    return {
        "job_id": job_id,
        "status": job["status"],
        "glb_path": str(glb_path),
        "manifest_path": str(manifest_path),
        "handoff_path": str(handoff_path),
        "glb_sha256": glb_sha256,
        "manifest_sha256": manifest_sha256,
        "output_bytes": len(glb_bytes),
        "backend": backend_name,
        "is_mock": is_mock,
        "asset_role": handoff["asset_role"],
        "rigging_ready": False,
    }


def _atomic_write(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def validate_artifacts(
    glb: bytes, manifest: dict[str, Any], *, require_real: bool, expected_job: str
) -> str:
    """Cross-check stored bytes; geometry counts remain the engine's validation."""
    if len(glb) < 20:
        raise ClientError("output: truncated GLB")
    magic, version, length = struct.unpack_from("<4sII", glb)
    if magic != b"glTF" or version != 2 or length != len(glb):
        raise ClientError("output: invalid GLB 2.0 header or length")
    if (
        not isinstance(expected_job, str)
        or not expected_job
        or manifest.get("schema_version") != "1.0"
        or manifest.get("job_id") != expected_job
    ):
        raise ClientError("manifest schema/job identity mismatch")
    backend = manifest.get("backend", {})
    if not isinstance(backend, dict) or backend.get("name") not in {"mock", "trellis2"}:
        raise ClientError("manifest: unsupported backend identity")
    name = backend["name"]
    if require_real and name != "trellis2":
        raise ClientError("--require-real rejected a mock result manifest")
    output = manifest.get("output", {})
    if (
        not isinstance(output, dict)
        or output.get("sha256") != hashlib.sha256(glb).hexdigest()
    ):
        raise ClientError("manifest output.sha256 does not match downloaded GLB")
    if output.get("bytes") != len(glb):
        raise ClientError("manifest output.bytes does not match downloaded GLB")
    validation = output.get("validation", {})
    if not isinstance(validation, dict) or validation.get("finite") is not True:
        raise ClientError("manifest: missing finite geometry validation")
    for key in ("geometry_count", "vertex_count", "face_count"):
        value = validation.get(key)
        if type(value) is not int or value < 1:
            raise ClientError(f"manifest: invalid geometry validation {key}")
    source = manifest.get("input", {})
    if not isinstance(source, dict) or not _is_sha256(source.get("sha256")):
        raise ClientError("manifest: missing canonical input checksum")
    return name


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _read_sibling(root: Path, record: Any) -> bytes:
    if not isinstance(record, dict) or not isinstance(record.get("filename"), str):
        raise ClientError("handoff: invalid sibling record")
    name = record["filename"]
    if not name or name in {".", ".."} or "/" in name or "\\" in name or ":" in name:
        raise ClientError("handoff: sibling filename must stay in export directory")
    path = root / name
    if path.resolve().parent != root.resolve():
        raise ClientError("handoff: sibling symlink escapes export directory")
    data = path.read_bytes()
    if (
        not _is_sha256(record.get("sha256"))
        or hashlib.sha256(data).hexdigest() != record["sha256"]
    ):
        raise ClientError(f"handoff: checksum mismatch for {name}")
    return data


def cmd_verify(args: argparse.Namespace) -> dict[str, Any]:
    path = Path(args.handoff).expanduser().resolve()
    descriptor = _json_or_error(200, path.read_bytes(), "handoff")
    if (
        descriptor.get("schema_version") != 1
        or descriptor.get("kind") != "localforge-asset-handoff"
    ):
        raise ClientError("handoff: unsupported schema or kind")
    glb = _read_sibling(path.parent, descriptor.get("glb"))
    manifest = _json_or_error(
        200, _read_sibling(path.parent, descriptor.get("manifest")), "manifest"
    )
    backend = validate_artifacts(
        glb,
        manifest,
        require_real=args.require_real,
        expected_job=descriptor.get("job_id"),
    )
    expected_role = (
        "pipeline_fixture" if backend == "mock" else "unreviewed_generated_mesh"
    )
    source = descriptor.get("source_image", {})
    if (
        descriptor.get("backend") != backend
        or descriptor.get("is_mock") is not (backend == "mock")
        or descriptor.get("asset_role") != expected_role
        or descriptor.get("rigging_ready") is not False
        or descriptor.get("quality_approved") is not False
        or descriptor.get("glb", {}).get("bytes") != len(glb)
        or not isinstance(source, dict)
        or source.get("canonical_sha256") != manifest["input"]["sha256"]
        or not _is_sha256(source.get("original_sha256"))
        or descriptor.get("geometry_validation") != manifest["output"]["validation"]
        or descriptor.get("coordinates")
        != {
            "format": "glTF 2.0",
            "up": "+Y",
            "handedness": "right",
            "unit": "metre",
            "authored_forward": "unknown",
            "physical_scale_verified": False,
            "transform_applied": False,
        }
    ):
        raise ClientError("handoff: provenance or unreviewed-asset identity mismatch")
    return {
        "integrity_verified": True,
        "handoff_path": str(path),
        "glb_path": str(path.parent / descriptor["glb"]["filename"]),
        "backend": backend,
        "is_mock": backend == "mock",
        "asset_role": expected_role,
        "rigging_ready": False,
        "quality_approved": False,
        "glb_sha256": descriptor["glb"]["sha256"],
    }


def _positive_seconds(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be finite and greater than zero")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default=DEFAULT_API, help="engine base URL")
    parser.add_argument(
        "--token",
        default=os.getenv("LOCALFORGE_API_TOKEN"),
        help="optional API token (default: LOCALFORGE_API_TOKEN env)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("health", help="engine health and backend info")

    generate = sub.add_parser("generate", help="image -> GLB, poll, download, verify")
    generate.add_argument("image", help="PNG/JPG/WEBP input image")
    generate.add_argument("--quality", choices=("draft", "studio"), default="draft")
    generate.add_argument("--out", default=".", help="output directory")
    generate.add_argument(
        "--require-real",
        action="store_true",
        help="reject mock engines before submit and recheck final manifest",
    )
    generate.add_argument(
        "--remesh", action=argparse.BooleanOptionalAction, default=True
    )
    generate.add_argument(
        "--texture", action=argparse.BooleanOptionalAction, default=True
    )
    generate.add_argument(
        "--timeout",
        type=_positive_seconds,
        default=3600.0,
        help="overall seconds to wait",
    )
    generate.add_argument("--poll-interval", type=_positive_seconds, default=2.0)

    verify = sub.add_parser(
        "verify", help="offline checksum/provenance check of an exported handoff"
    )
    verify.add_argument("handoff", help="exported .asset.json path")
    verify.add_argument(
        "--require-real", action="store_true", help="reject mock fixtures"
    )

    status = sub.add_parser("status", help="read one job")
    status.add_argument("job_id")

    cancel = sub.add_parser("cancel", help="request cooperative cancellation")
    cancel.add_argument("job_id")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.api = args.api.rstrip("/")
    handlers = {
        "health": cmd_health,
        "generate": cmd_generate,
        "status": cmd_status,
        "cancel": cmd_cancel,
        "verify": cmd_verify,
    }
    try:
        result = handlers[args.command](args)
    except ClientError as exc:
        log(f"error: {exc}")
        return 1
    except urllib.error.URLError as exc:
        log(
            f"error: cannot reach engine at {args.api} ({exc.reason}). "
            "Start it first (engine/start-engine.ps1 for the mock backend)."
        )
        return 2
    except (OSError, http.client.HTTPException) as exc:
        log(
            f"error: connection to engine failed mid-request ({exc!r}). "
            "A submitted job may still be running; check its status."
        )
        return 2
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
