from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
import os
import platform
import queue
import re
import threading
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from importlib import metadata
from pathlib import Path
from typing import Annotated, Any, Literal

import numpy as np
import trimesh
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse
from PIL import Image
from starlette.concurrency import run_in_threadpool
from starlette.types import ASGIApp, Receive, Scope, Send

if __package__:
    from .integrity import (
        IntegrityError,
        NormalizedImage,
        atomic_write_bytes,
        atomic_write_json,
        content_digest_from_hex,
        normalize_uploaded_image,
        sha256_file,
        validate_glb,
    )
    from .store import JobStore, RuntimeLock, utc_now
else:
    from integrity import (
        IntegrityError,
        NormalizedImage,
        atomic_write_bytes,
        atomic_write_json,
        content_digest_from_hex,
        normalize_uploaded_image,
        sha256_file,
        validate_glb,
    )
    from store import JobStore, RuntimeLock, utc_now


ENGINE_VERSION = "0.2.0"
SCHEMA_VERSION = "1.0"
ROOT = Path(__file__).resolve().parent
IDEMPOTENCY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if not math.isfinite(value) or value < minimum:
        raise RuntimeError(f"{name} must be at least {minimum}")
    return value


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number") from exc
    if not math.isfinite(value) or value < minimum:
        raise RuntimeError(f"{name} must be at least {minimum}")
    return value


def _env_list(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.getenv(name)
    if raw is None:
        return default
    return tuple(item.strip() for item in raw.split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    runtime_root: Path
    backend: str = "mock"
    max_image_bytes: int = 20 * 1024 * 1024
    max_image_pixels: int = 40_000_000
    max_image_dimension: int = 8192
    queue_capacity: int = 8
    api_token: str | None = None
    cors_origins: tuple[str, ...] = ()
    cors_origin_regex: str | None = r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"
    trusted_hosts: tuple[str, ...] = ("localhost", "127.0.0.1", "testserver")
    job_ttl_seconds: int = 7 * 24 * 60 * 60
    cleanup_interval_seconds: float = 60 * 60
    mock_step_delay: float = 0.15

    @classmethod
    def from_env(cls) -> Settings:
        runtime = (
            Path(os.getenv("LOCALFORGE_RUNTIME_ROOT", str(ROOT / "runtime")))
            .expanduser()
            .resolve()
        )
        origin_regex = os.getenv(
            "LOCALFORGE_CORS_ORIGIN_REGEX",
            r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
        ).strip()
        token = os.getenv("LOCALFORGE_API_TOKEN")
        return cls(
            runtime_root=runtime,
            backend=os.getenv("LOCALFORGE_BACKEND", "mock").strip().lower(),
            max_image_bytes=_env_int("LOCALFORGE_MAX_IMAGE_BYTES", 20 * 1024 * 1024),
            max_image_pixels=_env_int("LOCALFORGE_MAX_IMAGE_PIXELS", 40_000_000),
            max_image_dimension=_env_int("LOCALFORGE_MAX_IMAGE_DIMENSION", 8192),
            queue_capacity=_env_int("LOCALFORGE_QUEUE_CAPACITY", 8),
            api_token=token if token else None,
            cors_origins=_env_list("LOCALFORGE_CORS_ORIGINS", ()),
            cors_origin_regex=origin_regex or None,
            trusted_hosts=_env_list(
                "LOCALFORGE_TRUSTED_HOSTS",
                ("localhost", "127.0.0.1", "testserver"),
            ),
            job_ttl_seconds=_env_int("LOCALFORGE_JOB_TTL_SECONDS", 7 * 24 * 60 * 60),
            cleanup_interval_seconds=_env_float(
                "LOCALFORGE_CLEANUP_INTERVAL_SECONDS", 60 * 60, 0.05
            ),
            mock_step_delay=_env_float("LOCALFORGE_MOCK_STEP_DELAY", 0.15),
        )


class QueueCapacityError(RuntimeError):
    pass


class IdempotencyConflictError(RuntimeError):
    pass


class JobCancelled(RuntimeError):
    pass


class EngineStopping(RuntimeError):
    pass


class OriginGuardMiddleware:
    """Reject cross-origin unsafe requests before multipart body parsing."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        allowed_origins: tuple[str, ...],
        allowed_origin_regex: str | None,
    ) -> None:
        self.app = app
        self.allowed_origins = frozenset(allowed_origins)
        self.allowed_origin_pattern = (
            re.compile(allowed_origin_regex) if allowed_origin_regex else None
        )

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] == "http" and scope["method"] in {
            "POST",
            "PUT",
            "PATCH",
            "DELETE",
        }:
            headers = dict(scope["headers"])
            origin_bytes = headers.get(b"origin")
            if origin_bytes:
                origin = origin_bytes.decode("latin-1")
                exact_match = origin in self.allowed_origins
                regex_match = bool(
                    self.allowed_origin_pattern
                    and self.allowed_origin_pattern.fullmatch(origin)
                )
                if not exact_match and not regex_match:
                    response = JSONResponse(
                        status_code=403,
                        content={"detail": "허용되지 않은 Origin입니다."},
                    )
                    await response(scope, receive, send)
                    return
        await self.app(scope, receive, send)


class BoundedFIFO:
    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self._items: deque[str] = deque()
        self._condition = threading.Condition()

    def put_nowait(self, item: str) -> None:
        with self._condition:
            if len(self._items) >= self.capacity:
                raise queue.Full
            self._items.append(item)
            self._condition.notify()

    def get(self, timeout: float) -> str:
        deadline = time.monotonic() + timeout
        with self._condition:
            while not self._items:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise queue.Empty
                self._condition.wait(remaining)
            return self._items.popleft()

    def remove(self, item: str) -> bool:
        with self._condition:
            try:
                self._items.remove(item)
            except ValueError:
                return False
            self._condition.notify()
            return True

    def clear(self) -> None:
        with self._condition:
            self._items.clear()
            self._condition.notify_all()


def create_preview_glb(image_path: Path, output_path: Path) -> None:
    with Image.open(image_path) as source:
        image = source.convert("RGB")
        sample = image.resize((32, 32))
        average = np.asarray(sample, dtype=np.float32).mean(axis=(0, 1))

    mesh = trimesh.creation.icosphere(subdivisions=4, radius=0.72)
    vertices = mesh.vertices
    vertex_range = float(np.ptp(vertices[:, 2]))
    height = (vertices[:, 2] - vertices[:, 2].min()) / vertex_range
    colors = np.zeros((len(vertices), 4), dtype=np.uint8)
    colors[:, :3] = np.clip(
        average[None, :] * (0.62 + height[:, None] * 0.55), 0, 255
    ).astype(np.uint8)
    colors[:, 3] = 255
    mesh.visual.vertex_colors = colors

    base = trimesh.creation.cylinder(radius=0.52, height=0.08, sections=64)
    base.apply_translation((0, 0, -0.82))
    base.visual.face_colors = [36, 40, 36, 255]

    scene = trimesh.Scene()
    scene.add_geometry(mesh, node_name="preview-form")
    scene.add_geometry(base, node_name="preview-base")
    output_path.write_bytes(scene.export(file_type="glb"))


class GenerationManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.input_root = settings.runtime_root / "inputs"
        self.output_root = settings.runtime_root / "outputs"
        self.manifest_root = settings.runtime_root / "manifests"
        for directory in (
            self.input_root,
            self.output_root,
            self.manifest_root,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        self.store = JobStore(settings.runtime_root / "jobs.sqlite3")
        self.runtime_lock = RuntimeLock(settings.runtime_root / "engine.lock")
        self.queue = BoundedFIFO(settings.queue_capacity)
        self.stop_event = threading.Event()
        self.submission_lock = threading.Lock()
        self.lifecycle_lock = threading.Lock()
        self.worker: threading.Thread | None = None
        self.cleaner: threading.Thread | None = None
        self.trellis_runner: Any = None
        self.trellis_lock = threading.Lock()
        self.recovered_interrupted = 0
        self.last_cleanup_error: str | None = None
        self.last_worker_error: str | None = None

    def start(self) -> None:
        with self.lifecycle_lock:
            if self.worker and self.worker.is_alive():
                return
            if not self.runtime_lock.acquire():
                raise RuntimeError(
                    "같은 runtime에서 다른 LocalForge worker가 실행 중입니다."
                )
            self.stop_event.clear()
            try:
                self.cleanup_expired()
                self.queue.clear()
                for partial in self.output_root.glob(".*.building.glb"):
                    if partial.is_file():
                        partial.unlink(missing_ok=True)
                self.recovered_interrupted = self.store.recover_interrupted()
                queued = self.store.queued()
                for index, job in enumerate(queued):
                    if index < self.settings.queue_capacity:
                        self.queue.put_nowait(job["id"])
                    else:
                        self.store.update(
                            job["id"],
                            status="failed",
                            stage="interrupted",
                            error="현재 큐 용량으로 복구할 수 없습니다.",
                        )
                self.worker = threading.Thread(
                    target=self._worker_loop,
                    name="localforge-generation-worker",
                    daemon=True,
                )
                self.cleaner = threading.Thread(
                    target=self._cleanup_loop,
                    name="localforge-ttl-cleaner",
                    daemon=True,
                )
                self.worker.start()
                self.cleaner.start()
            except Exception:
                self.stop_event.set()
                if self.worker and self.worker.is_alive():
                    self.worker.join(timeout=2)
                if self.cleaner and self.cleaner.is_alive():
                    self.cleaner.join(timeout=2)
                self.runtime_lock.release()
                raise

    def stop(self) -> None:
        self.stop_event.set()
        worker = self.worker
        cleaner = self.cleaner
        if worker:
            worker.join(timeout=5)
        if cleaner:
            cleaner.join(timeout=2)
        if worker is None or not worker.is_alive():
            self.runtime_lock.release()

    def _worker_loop(self) -> None:
        try:
            while not self.stop_event.is_set():
                try:
                    job_id = self.queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                claimed = False
                try:
                    claimed = self.store.claim(job_id)
                    if claimed:
                        self._process(job_id)
                    self.last_worker_error = None
                # Keep the only generation worker alive through transient
                # persistence or unexpected adapter-boundary failures.
                except Exception as exc:  # noqa: BLE001
                    self.last_worker_error = exc.__class__.__name__
                    if claimed:
                        try:
                            self.store.complete_failure_or_cancellation(
                                job_id,
                                error=f"worker boundary: {exc.__class__.__name__}",
                            )
                        except Exception as persistence_exc:  # noqa: BLE001
                            self.last_worker_error = (
                                f"{exc.__class__.__name__}/"
                                f"{persistence_exc.__class__.__name__}"
                            )
                    else:
                        try:
                            self.queue.put_nowait(job_id)
                        except queue.Full:
                            pass
                    self.stop_event.wait(0.25)
        finally:
            cleaner = self.cleaner
            if self.stop_event.is_set() and (cleaner is None or not cleaner.is_alive()):
                self.runtime_lock.release()

    def _cleanup_loop(self) -> None:
        while not self.stop_event.wait(self.settings.cleanup_interval_seconds):
            self.last_cleanup_error = None
            try:
                self.cleanup_expired()
            # Cleanup must survive transient filesystem/SQLite failures and
            # retry on the next interval.
            except Exception as exc:  # noqa: BLE001
                self.last_cleanup_error = exc.__class__.__name__

    def _request_fingerprint(
        self,
        image: NormalizedImage,
        *,
        prompt: str,
        quality: str,
        remesh: bool,
        texture: bool,
    ) -> str:
        canonical = json.dumps(
            {
                "input_sha256": image.sha256,
                "prompt": prompt.strip(),
                "quality": quality,
                "remesh": remesh,
                "texture": texture,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def submit(
        self,
        image: NormalizedImage,
        *,
        prompt: str,
        quality: str,
        remesh: bool,
        texture: bool,
        idempotency_key: str | None,
    ) -> tuple[dict[str, Any], bool]:
        fingerprint = self._request_fingerprint(
            image,
            prompt=prompt,
            quality=quality,
            remesh=remesh,
            texture=texture,
        )
        with self.submission_lock:
            if idempotency_key:
                existing = self.store.get_by_idempotency(idempotency_key)
                if existing:
                    if existing["request_fingerprint"] != fingerprint:
                        raise IdempotencyConflictError
                    return self.store.public(existing), True

            if self.store.count_active() >= self.settings.queue_capacity:
                raise QueueCapacityError

            job_id = uuid.uuid4().hex
            now = utc_now()
            input_path = self.input_root / f"{job_id}.png"
            atomic_write_bytes(input_path, image.data)
            job = {
                "id": job_id,
                "status": "queued",
                "progress": 4,
                "stage": "대기열 등록",
                "model_url": None,
                "manifest_url": None,
                "error": None,
                "input_sha256": image.sha256,
                "output_sha256": None,
                "manifest_sha256": None,
                "output_bytes": None,
                "input_path": str(input_path),
                "output_path": None,
                "manifest_path": None,
                "input_bytes": len(image.data),
                "image_width": image.width,
                "image_height": image.height,
                "source_format": image.source_format,
                "quality": quality,
                "remesh": int(remesh),
                "texture": int(texture),
                "request_fingerprint": fingerprint,
                "idempotency_key": idempotency_key,
                "cancel_requested": 0,
                "created_at": now,
                "updated_at": now,
            }
            try:
                created = self.store.create(job)
                self.queue.put_nowait(job_id)
            except queue.Full as exc:
                self.store.update(
                    job_id,
                    status="failed",
                    stage="실패",
                    error="대기열 등록 중 용량이 소진되었습니다.",
                )
                self.store.delete(job_id)
                input_path.unlink(missing_ok=True)
                raise QueueCapacityError from exc
            except Exception:
                input_path.unlink(missing_ok=True)
                raise
            return self.store.public(created), False

    def cancel(self, job_id: str) -> dict[str, Any] | None:
        with self.submission_lock:
            job = self.store.request_cancel(job_id)
            if job and job["status"] == "cancelled":
                self.queue.remove(job_id)
            return job

    def _get_trellis_runner(self) -> Any:
        with self.trellis_lock:
            if self.trellis_runner is None:
                if __package__:
                    from .trellis_backend import TrellisBackend
                else:
                    from trellis_backend import TrellisBackend

                self.trellis_runner = TrellisBackend()
        return self.trellis_runner

    def _checkpoint(self, job_id: str) -> None:
        if self.stop_event.is_set():
            raise EngineStopping
        if self.store.should_cancel(job_id):
            raise JobCancelled

    def _verify_persisted_input(
        self,
        job_id: str,
        path: Path,
        expected_sha256: str,
    ) -> Path:
        resolved = path.resolve()
        expected_path = (self.input_root / f"{job_id}.png").resolve()
        if resolved != expected_path or not resolved.is_file():
            raise IntegrityError("보관된 입력 이미지 경로가 유효하지 않습니다.", 500)
        if sha256_file(resolved) != expected_sha256:
            raise IntegrityError(
                "보관된 입력 이미지 checksum이 일치하지 않습니다.", 500
            )
        return resolved

    def _mock_generate(
        self,
        job_id: str,
        input_path: Path,
        build_path: Path,
    ) -> None:
        steps = (
            (24, "형상 특징 추출"),
            (48, "메시 초안 생성"),
            (70, "표면 정리"),
        )
        for progress, stage in steps:
            self._checkpoint(job_id)
            if self.stop_event.wait(self.settings.mock_step_delay):
                raise EngineStopping
            self._checkpoint(job_id)
            self.store.update(job_id, progress=progress, stage=stage)
        self._checkpoint(job_id)
        create_preview_glb(input_path, build_path)

    def _backend_info(self) -> dict[str, Any]:
        if self.settings.backend == "trellis2":
            info: dict[str, Any] = {
                "name": "trellis2",
                "model": "microsoft/TRELLIS.2-4B",
            }
            runner = self.trellis_runner
            if runner is not None and hasattr(runner, "provenance"):
                info.update(runner.provenance())
            return info
        return {"name": "mock", "model": "LocalForge deterministic preview"}

    @staticmethod
    def _package_version(name: str) -> str:
        try:
            return metadata.version(name)
        except metadata.PackageNotFoundError:
            return "unknown"

    def _manifest(
        self,
        job: dict[str, Any],
        *,
        output_sha256: str,
        output_bytes: int,
        validation: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "job_id": job["id"],
            "generated_at": utc_now(),
            "engine": {
                "name": "LocalForge Engine",
                "version": ENGINE_VERSION,
            },
            "backend": self._backend_info(),
            "generation": {
                "quality": job["quality"],
                "remesh": bool(job["remesh"]),
                "texture": bool(job["texture"]),
            },
            "input": {
                "sha256": job["input_sha256"],
                "bytes": job["input_bytes"],
                "source_format": job["source_format"],
                "canonical_format": "PNG",
                "width": job["image_width"],
                "height": job["image_height"],
                "metadata_stripped": True,
                "exif_orientation_normalized": True,
            },
            "output": {
                "sha256": output_sha256,
                "bytes": output_bytes,
                "media_type": "model/gltf-binary",
                "validation": validation,
            },
            "environment": {
                "python": platform.python_version(),
                "platform": platform.system(),
                "architecture": platform.machine(),
                "packages": {
                    "numpy": self._package_version("numpy"),
                    "pillow": self._package_version("pillow"),
                    "trimesh": self._package_version("trimesh"),
                },
            },
            "integrity_policy": {
                "max_image_bytes": self.settings.max_image_bytes,
                "max_image_pixels": self.settings.max_image_pixels,
                "max_image_dimension": self.settings.max_image_dimension,
                "output_reloaded": True,
                "finite_geometry_required": True,
            },
        }

    def _process(self, job_id: str) -> None:
        job = self.store.get(job_id)
        if job is None:
            return
        input_path = Path(job["input_path"])
        output_path = self.output_root / f"{job_id}.glb"
        build_path = self.output_root / f".{job_id}.building.glb"
        manifest_path = self.manifest_root / f"{job_id}.json"
        for path in (build_path, output_path, manifest_path):
            path.unlink(missing_ok=True)

        try:
            self._checkpoint(job_id)
            input_path = self._verify_persisted_input(
                job_id,
                input_path,
                job["input_sha256"],
            )
            if self.settings.backend == "mock":
                self._mock_generate(job_id, input_path, build_path)
            elif self.settings.backend == "trellis2":
                self.store.update(
                    job_id,
                    progress=16,
                    stage="TRELLIS.2 형상 생성",
                )
                runner = self._get_trellis_runner()
                runner.generate(
                    image_path=input_path,
                    output_path=build_path,
                    quality=job["quality"],
                    remesh=bool(job["remesh"]),
                    texture=bool(job["texture"]),
                    cancel_check=lambda: self._checkpoint(job_id),
                )
            else:
                raise RuntimeError(
                    f"지원하지 않는 백엔드입니다: {self.settings.backend}"
                )

            self._checkpoint(job_id)
            self.store.update(job_id, progress=90, stage="GLB 무결성 검증")
            validation = validate_glb(build_path)
            self._checkpoint(job_id)
            os.replace(build_path, output_path)
            output_sha256 = sha256_file(output_path)
            output_bytes = output_path.stat().st_size
            manifest = self._manifest(
                job,
                output_sha256=output_sha256,
                output_bytes=output_bytes,
                validation=validation,
            )
            atomic_write_json(manifest_path, manifest)
            manifest_sha256 = sha256_file(manifest_path)
            self._checkpoint(job_id)
            completed = self.store.complete_success(
                job_id,
                model_url=f"/v1/generations/{job_id}/output",
                manifest_url=f"/v1/generations/{job_id}/manifest",
                output_path=str(output_path),
                manifest_path=str(manifest_path),
                output_sha256=output_sha256,
                manifest_sha256=manifest_sha256,
                output_bytes=output_bytes,
            )
            if not completed:
                raise JobCancelled
        except EngineStopping:
            for path in (build_path, output_path, manifest_path):
                path.unlink(missing_ok=True)
            # Keep "running" persisted so the next engine start records a
            # deterministic interrupted failure instead of silently retrying it.
        except JobCancelled:
            for path in (build_path, output_path, manifest_path):
                path.unlink(missing_ok=True)
            self.store.complete_failure_or_cancellation(job_id, error=None)
        # This is the job boundary: adapters can raise library-specific errors.
        except Exception as exc:  # noqa: BLE001
            for path in (build_path, output_path, manifest_path):
                path.unlink(missing_ok=True)
            message = str(exc)
            sensitive_paths = (
                str(self.settings.runtime_root),
                str(Path.home()),
                os.getenv("TRELLIS2_ROOT", ""),
            )
            for sensitive_path in sensitive_paths:
                if sensitive_path:
                    message = message.replace(sensitive_path, "<private-path>")
            self.store.complete_failure_or_cancellation(
                job_id,
                error=message or exc.__class__.__name__,
            )

    def cleanup_expired(self) -> int:
        cutoff = (
            (
                datetime.now(timezone.utc)
                - timedelta(seconds=self.settings.job_ttl_seconds)
            )
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        expired = self.store.expired_terminal(cutoff)
        cleaned = 0
        for job in expired:
            try:
                self._safe_unlink(job.get("input_path"), self.input_root)
                self._safe_unlink(job.get("output_path"), self.output_root)
                self._safe_unlink(job.get("manifest_path"), self.manifest_root)
                self._safe_unlink(
                    str(self.output_root / f"{job['id']}.glb"),
                    self.output_root,
                )
                self._safe_unlink(
                    str(self.manifest_root / f"{job['id']}.json"),
                    self.manifest_root,
                )
            except OSError as exc:
                self.last_cleanup_error = exc.__class__.__name__
                continue
            self.store.delete(job["id"])
            cleaned += 1
        return cleaned

    @staticmethod
    def _safe_unlink(value: str | None, root: Path) -> None:
        if not value:
            return
        path = Path(value).resolve()
        try:
            path.relative_to(root.resolve())
        except ValueError:
            return
        if path.is_file():
            path.unlink(missing_ok=True)


async def _read_upload(upload: UploadFile, limit: int) -> bytes:
    contents = bytearray()
    while True:
        remaining = limit + 1 - len(contents)
        chunk = await upload.read(min(1024 * 1024, remaining))
        if not chunk:
            break
        contents.extend(chunk)
        if len(contents) > limit:
            raise HTTPException(
                status_code=413,
                detail=f"이미지는 {limit:,}바이트 이하여야 합니다.",
            )
    if not contents:
        raise HTTPException(status_code=422, detail="이미지 파일이 비어 있습니다.")
    return bytes(contents)


def _verified_file(
    path_value: str | None,
    root: Path,
    *,
    not_ready_detail: str,
) -> Path:
    if not path_value:
        raise HTTPException(status_code=409, detail=not_ready_detail)
    path = Path(path_value).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise HTTPException(
            status_code=500, detail="저장 경로가 안전하지 않습니다."
        ) from exc
    if not path.is_file():
        raise HTTPException(
            status_code=410, detail="보관 파일이 만료되었거나 없습니다."
        )
    return path


def _checksum_headers(
    checksum: str,
    *,
    include_content_digest: bool = True,
) -> dict[str, str]:
    headers = {
        "ETag": f'"{checksum}"',
        "X-Checksum-SHA256": checksum,
        "Cache-Control": "private, no-cache",
    }
    if include_content_digest:
        headers["Content-Digest"] = content_digest_from_hex(checksum)
    return headers


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings or Settings.from_env()
    manager = GenerationManager(config)
    normalization_semaphore = asyncio.Semaphore(1)

    async def authenticate(request: Request) -> None:
        expected = config.api_token
        if expected is None:
            return
        supplied = request.headers.get("X-LocalForge-Token", "")
        authorization = request.headers.get("Authorization", "")
        if not supplied and authorization.lower().startswith("bearer "):
            supplied = authorization[7:].strip()
        if not supplied or not hmac.compare_digest(supplied, expected):
            raise HTTPException(
                status_code=401,
                detail="유효한 LocalForge API 토큰이 필요합니다.",
                headers={"WWW-Authenticate": "Bearer"},
            )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        manager.start()
        try:
            yield
        finally:
            manager.stop()

    application = FastAPI(
        title="LocalForge Engine",
        version=ENGINE_VERSION,
        lifespan=lifespan,
        dependencies=[Depends(authenticate)],
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    application.state.manager = manager
    application.state.settings = config
    application.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=list(config.trusted_hosts) or ["localhost"],
    )
    application.add_middleware(
        OriginGuardMiddleware,
        allowed_origins=config.cors_origins,
        allowed_origin_regex=config.cors_origin_regex,
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(config.cors_origins),
        allow_origin_regex=config.cors_origin_regex,
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "Idempotency-Key",
            "X-LocalForge-Token",
        ],
        expose_headers=[
            "Content-Digest",
            "ETag",
            "X-Checksum-SHA256",
        ],
    )

    @application.get("/health")
    def health() -> dict[str, Any]:
        counts = manager.store.status_counts()
        model = "TRELLIS.2-4B" if config.backend == "trellis2" else "BRIDGE / MOCK"
        return {
            "status": "online",
            "schema_version": SCHEMA_VERSION,
            "engine_version": ENGINE_VERSION,
            "model": model,
            "backend": config.backend,
            "gpu_required": config.backend == "trellis2",
            "limits": {
                "max_image_bytes": config.max_image_bytes,
                "max_image_pixels": config.max_image_pixels,
                "max_image_dimension": config.max_image_dimension,
                "normalization_concurrency": 1,
            },
            "queue": {
                "capacity": config.queue_capacity,
                "active": counts["queued"] + counts["running"],
                "queued": counts["queued"],
                "running": counts["running"],
                "single_worker": True,
                "cross_process_runtime_lock": True,
                "worker_alive": bool(manager.worker and manager.worker.is_alive()),
                "last_worker_error": manager.last_worker_error,
            },
            "persistence": {
                "type": "sqlite",
                "running_jobs_recovered_as_interrupted": (
                    manager.recovered_interrupted
                ),
                "terminal_job_ttl_seconds": config.job_ttl_seconds,
                "last_cleanup_error": manager.last_cleanup_error,
            },
            "security": {
                "api_token_required": config.api_token is not None,
                "cors_origins": list(config.cors_origins),
                "cors_origin_regex": config.cors_origin_regex,
                "trusted_hosts": list(config.trusted_hosts),
            },
        }

    @application.post("/v1/generations", status_code=202)
    async def create_generation(
        image: Annotated[UploadFile | None, File()] = None,
        prompt: Annotated[str, Form(max_length=2000)] = "",
        quality: Annotated[Literal["draft", "studio"], Form()] = "studio",
        remesh: Annotated[bool, Form()] = True,
        texture: Annotated[bool, Form()] = True,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        if prompt.strip():
            raise HTTPException(
                status_code=501,
                detail="프롬프트 조건화는 아직 연결되지 않았습니다.",
            )
        if image is None:
            raise HTTPException(status_code=400, detail="입력 이미지가 필요합니다.")
        if idempotency_key and not IDEMPOTENCY_PATTERN.fullmatch(idempotency_key):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Idempotency-Key는 영문, 숫자, . _ : - 문자로 1~128자여야 합니다."
                ),
            )
        if config.backend == "trellis2" and not texture:
            raise HTTPException(
                status_code=422,
                detail="현재 TRELLIS.2 백엔드는 texture=true만 지원합니다.",
            )
        if manager.store.count_active() >= config.queue_capacity:
            existing = (
                manager.store.get_by_idempotency(idempotency_key)
                if idempotency_key
                else None
            )
            if existing is None:
                raise HTTPException(
                    status_code=429,
                    detail="생성 대기열이 가득 찼습니다. 잠시 후 다시 시도하세요.",
                    headers={"Retry-After": "2"},
                )

        declared_type = image.content_type or ""
        async with normalization_semaphore:
            if manager.store.count_active() >= config.queue_capacity:
                existing = (
                    manager.store.get_by_idempotency(idempotency_key)
                    if idempotency_key
                    else None
                )
                if existing is None:
                    raise HTTPException(
                        status_code=429,
                        detail="생성 대기열이 가득 찼습니다. 잠시 후 다시 시도하세요.",
                        headers={"Retry-After": "2"},
                    )
            contents = await _read_upload(image, config.max_image_bytes)
            try:
                normalized = await run_in_threadpool(
                    normalize_uploaded_image,
                    contents,
                    declared_type,
                    max_pixels=config.max_image_pixels,
                    max_dimension=config.max_image_dimension,
                )
            except IntegrityError as exc:
                raise HTTPException(
                    status_code=exc.status_code,
                    detail=exc.detail,
                ) from exc

        try:
            job, replayed = await run_in_threadpool(
                manager.submit,
                normalized,
                prompt=prompt,
                quality=quality,
                remesh=remesh,
                texture=texture,
                idempotency_key=idempotency_key,
            )
        except QueueCapacityError as exc:
            raise HTTPException(
                status_code=429,
                detail="생성 대기열이 가득 찼습니다. 잠시 후 다시 시도하세요.",
                headers={"Retry-After": "2"},
            ) from exc
        except IdempotencyConflictError as exc:
            raise HTTPException(
                status_code=409,
                detail="같은 Idempotency-Key가 다른 요청에 사용되었습니다.",
            ) from exc

        if replayed:
            job["idempotency_replayed"] = True
        return job

    @application.get("/v1/generations/{job_id}")
    def get_generation(job_id: str) -> dict[str, Any]:
        job = manager.store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="작업을 찾을 수 없습니다.")
        return manager.store.public(job)

    @application.delete("/v1/generations/{job_id}")
    def cancel_generation(job_id: str) -> dict[str, Any]:
        job = manager.cancel(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="작업을 찾을 수 없습니다.")
        if job["status"] in {"succeeded", "failed"}:
            raise HTTPException(
                status_code=409,
                detail="이미 종료된 작업은 취소할 수 없습니다.",
            )
        return manager.store.public(job)

    @application.get("/v1/generations/{job_id}/output")
    def download_output(job_id: str, request: Request) -> FileResponse:
        job = manager.store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="작업을 찾을 수 없습니다.")
        if job["status"] != "succeeded":
            raise HTTPException(
                status_code=409, detail="출력 파일이 아직 준비되지 않았습니다."
            )
        path = _verified_file(
            job.get("output_path"),
            manager.output_root,
            not_ready_detail="출력 파일이 아직 준비되지 않았습니다.",
        )
        checksum = sha256_file(path)
        if checksum != job["output_sha256"]:
            raise HTTPException(
                status_code=409, detail="출력 파일 checksum이 일치하지 않습니다."
            )
        return FileResponse(
            path,
            media_type="model/gltf-binary",
            filename=f"localforge-{job_id}.glb",
            headers=_checksum_headers(
                checksum,
                include_content_digest="range" not in request.headers,
            ),
        )

    @application.get("/v1/generations/{job_id}/manifest")
    def download_manifest(job_id: str, request: Request) -> FileResponse:
        job = manager.store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="작업을 찾을 수 없습니다.")
        if job["status"] != "succeeded":
            raise HTTPException(
                status_code=409, detail="manifest가 아직 준비되지 않았습니다."
            )
        path = _verified_file(
            job.get("manifest_path"),
            manager.manifest_root,
            not_ready_detail="manifest가 아직 준비되지 않았습니다.",
        )
        checksum = sha256_file(path)
        if checksum != job["manifest_sha256"]:
            raise HTTPException(
                status_code=409,
                detail="manifest checksum이 일치하지 않습니다.",
            )
        return FileResponse(
            path,
            media_type="application/json",
            filename=f"localforge-{job_id}-manifest.json",
            headers=_checksum_headers(
                checksum,
                include_content_digest="range" not in request.headers,
            ),
        )

    return application


app = create_app()
