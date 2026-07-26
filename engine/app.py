from __future__ import annotations

import os
import threading
import time
import uuid
from pathlib import Path
from typing import Literal, TypedDict

import numpy as np
import trimesh
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from PIL import Image


ROOT = Path(__file__).resolve().parent
INPUT_ROOT = ROOT / "runtime" / "inputs"
OUTPUT_ROOT = ROOT / "runtime" / "outputs"
INPUT_ROOT.mkdir(parents=True, exist_ok=True)
OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

BACKEND = os.getenv("LOCALFORGE_BACKEND", "mock").lower()
ALLOWED_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
MAX_IMAGE_BYTES = 20 * 1024 * 1024


class Job(TypedDict, total=False):
    id: str
    status: Literal["queued", "running", "succeeded", "failed"]
    progress: int
    stage: str
    model_url: str
    error: str


jobs: dict[str, Job] = {}
jobs_lock = threading.Lock()
trellis_runner = None
trellis_lock = threading.Lock()

app = FastAPI(title="LocalForge Engine", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/outputs", StaticFiles(directory=OUTPUT_ROOT), name="outputs")


def update_job(job_id: str, **changes: object) -> None:
    with jobs_lock:
        jobs[job_id].update(changes)  # type: ignore[typeddict-item]


def create_preview_glb(image_path: Path, output_path: Path) -> None:
    with Image.open(image_path).convert("RGB") as image:
        sample = image.resize((32, 32))
        average = np.asarray(sample, dtype=np.float32).mean(axis=(0, 1))

    mesh = trimesh.creation.icosphere(subdivisions=4, radius=0.72)
    vertices = mesh.vertices
    height = (vertices[:, 2] - vertices[:, 2].min()) / np.ptp(vertices[:, 2])
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


def get_trellis_runner():
    global trellis_runner
    with trellis_lock:
        if trellis_runner is None:
            from trellis_backend import TrellisBackend

            trellis_runner = TrellisBackend()
    return trellis_runner


def process_job(
    job_id: str,
    image_path: Path,
    quality: str,
    remesh: bool,
) -> None:
    output_path = OUTPUT_ROOT / f"{job_id}.glb"
    try:
        update_job(job_id, status="running", progress=8, stage="입력 이미지 분석")
        if BACKEND == "mock":
            time.sleep(0.5)
            update_job(job_id, progress=34, stage="형상 미리보기 생성")
            time.sleep(0.7)
            create_preview_glb(image_path, output_path)
            update_job(job_id, progress=82, stage="GLB 패키징")
            time.sleep(0.4)
        elif BACKEND == "trellis2":
            update_job(job_id, progress=16, stage="TRELLIS.2 형상 생성")
            runner = get_trellis_runner()
            runner.generate(
                image_path=image_path,
                output_path=output_path,
                quality=quality,
                remesh=remesh,
            )
            update_job(job_id, progress=92, stage="PBR GLB 내보내기")
        else:
            raise RuntimeError(f"지원하지 않는 백엔드입니다: {BACKEND}")

        update_job(
            job_id,
            status="succeeded",
            progress=100,
            stage="완료",
            model_url=f"/outputs/{output_path.name}",
        )
    except Exception as exc:
        update_job(job_id, status="failed", error=str(exc), stage="실패")


@app.get("/health")
def health() -> dict[str, object]:
    model = "TRELLIS.2-4B" if BACKEND == "trellis2" else "BRIDGE / MOCK"
    return {
        "status": "online",
        "model": model,
        "backend": BACKEND,
        "gpu_required": BACKEND == "trellis2",
    }


@app.post("/v1/generations", status_code=202)
async def create_generation(
    image: UploadFile | None = File(default=None),
    prompt: str = Form(default=""),
    quality: str = Form(default="studio"),
    remesh: bool = Form(default=True),
    texture: bool = Form(default=True),
) -> Job:
    del texture
    if image is None:
        if prompt.strip():
            raise HTTPException(
                status_code=501,
                detail="텍스트→3D는 다음 단계에서 연결됩니다. 이미지를 넣어주세요.",
            )
        raise HTTPException(status_code=400, detail="입력 이미지가 필요합니다.")

    suffix = ALLOWED_TYPES.get(image.content_type or "")
    if suffix is None:
        raise HTTPException(status_code=415, detail="PNG, JPG, WEBP만 지원합니다.")
    contents = await image.read(MAX_IMAGE_BYTES + 1)
    if len(contents) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="이미지는 20MB 이하여야 합니다.")

    job_id = uuid.uuid4().hex
    image_path = INPUT_ROOT / f"{job_id}{suffix}"
    image_path.write_bytes(contents)
    job: Job = {
        "id": job_id,
        "status": "queued",
        "progress": 4,
        "stage": "대기열 등록",
    }
    with jobs_lock:
        jobs[job_id] = job

    worker = threading.Thread(
        target=process_job,
        args=(job_id, image_path, quality, remesh),
        daemon=True,
    )
    worker.start()
    return job


@app.get("/v1/generations/{job_id}")
def get_generation(job_id: str) -> Job:
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="작업을 찾을 수 없습니다.")
        return job.copy()
