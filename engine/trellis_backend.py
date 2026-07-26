from __future__ import annotations

import os
import sys
from pathlib import Path


class TrellisBackend:
    def __init__(self) -> None:
        trellis_root = os.getenv("TRELLIS2_ROOT")
        if not trellis_root:
            raise RuntimeError("TRELLIS2_ROOT 환경 변수가 필요합니다.")

        root = Path(trellis_root).expanduser().resolve()
        if not (root / "trellis2").is_dir():
            raise RuntimeError(f"TRELLIS.2 소스를 찾을 수 없습니다: {root}")

        os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")
        os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
        os.environ.setdefault("ATTN_BACKEND", "sdpa")
        os.environ.setdefault("SPARSE_ATTN_BACKEND", "xformers")
        sys.path.insert(0, str(root))

        import torch
        from trellis2.pipelines import Trellis2ImageTo3DPipeline

        if not torch.cuda.is_available():
            raise RuntimeError("CUDA GPU를 찾지 못했습니다.")
        if torch.cuda.get_device_capability()[0] < 12:
            raise RuntimeError("이 구성은 RTX 50 시리즈(sm_120)용입니다.")

        self._torch = torch
        self._pipeline = Trellis2ImageTo3DPipeline.from_pretrained(
            "microsoft/TRELLIS.2-4B"
        )
        self._pipeline.cuda()

    def generate(
        self,
        image_path: Path,
        output_path: Path,
        quality: str,
        remesh: bool,
    ) -> None:
        import o_voxel
        from PIL import Image

        texture_size = 2048 if quality == "draft" else 4096
        decimation_target = 350_000 if quality == "draft" else 1_000_000
        with Image.open(image_path).convert("RGBA") as image:
            with self._torch.inference_mode():
                mesh = self._pipeline.run(image)[0]

        mesh.simplify(16_777_216)
        glb = o_voxel.postprocess.to_glb(
            vertices=mesh.vertices,
            faces=mesh.faces,
            attr_volume=mesh.attrs,
            coords=mesh.coords,
            attr_layout=mesh.layout,
            voxel_size=mesh.voxel_size,
            aabb=[[-0.5, -0.5, -0.5], [0.5, 0.5, 0.5]],
            decimation_target=decimation_target,
            texture_size=texture_size,
            remesh=remesh,
            remesh_band=1,
            remesh_project=0,
            verbose=True,
        )
        glb.export(str(output_path), extension_webp=True)
