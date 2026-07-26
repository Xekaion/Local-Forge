# RTX 5090 + TRELLIS.2 setup

This setup targets a Windows 11 workstation with an RTX 5090 32GB. TRELLIS.2 is
officially tested on Linux with A100/H100 GPUs and requires at least 24GB VRAM.
RTX 5090 support currently depends on newer CUDA and locally compiled
extensions, so treat this as an engineering setup rather than a one-click
installer.

Primary references:

- [Microsoft TRELLIS.2](https://github.com/microsoft/TRELLIS.2)
- [TRELLIS.2 setup script](https://github.com/microsoft/TRELLIS.2/blob/main/setup.sh)
- [NVIDIA CUDA on WSL](https://docs.nvidia.com/cuda/wsl-user-guide/index.html)
- [PyTorch previous versions](https://pytorch.org/get-started/previous-versions/)
- [TRELLIS.2 RTX 5090 / CUDA 13 build report](https://github.com/microsoft/TRELLIS.2/issues/19)

## Before installation

- Use WSL2 with Ubuntu 24.04.
- Install the latest NVIDIA Windows driver.
- Inside WSL, `nvidia-smi` must show the RTX 5090.
- Keep the TRELLIS.2 repository in the WSL filesystem, such as `~/src`, rather
  than under `/mnt/c`.
- Keep at least 100GB of free SSD space and preferably 64GB system RAM.
- Do not install a Linux NVIDIA display driver inside WSL. Install only the CUDA
  toolkit package; WSL uses the Windows host driver.

## Base environment

```bash
git clone -b main https://github.com/microsoft/TRELLIS.2.git --recursive ~/src/TRELLIS.2
cd ~/src/TRELLIS.2

conda create -n trellis2 python=3.10 -y
conda activate trellis2

pip install torch==2.9.1 torchvision==0.24.1 torchaudio==2.9.1 \
  --index-url https://download.pytorch.org/whl/cu130

export CUDA_HOME=/usr/local/cuda-13.0
export TORCH_CUDA_ARCH_LIST=12.0
export MAX_JOBS=3
export ATTN_BACKEND=sdpa
export SPARSE_ATTN_BACKEND=xformers
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
```

Do not use `setup.sh --new-env`: the official script currently pins PyTorch
2.6/CUDA 12.4, which predates native RTX 5090 support. Build the extensions one
at a time so a failure is attributable:

```bash
source setup.sh --basic
source setup.sh --nvdiffrast
source setup.sh --nvdiffrec
source setup.sh --flexgemm
source setup.sh --cumesh
source setup.sh --o-voxel
```

Start with PyTorch SDPA and an RTX 5090-compatible xFormers build instead of the
script's pinned FlashAttention 2.7.3. CUDA 13 builds may require the CuMesh patch
described in TRELLIS.2 issue #19.

Verify the active stack:

```bash
python - <<'PY'
import torch
print("torch", torch.__version__)
print("cuda", torch.version.cuda)
print("gpu", torch.cuda.get_device_name())
print("capability", torch.cuda.get_device_capability())
PY
```

The capability should be `(12, 0)`.

## Run the LocalForge engine

From the `trellis2` Conda environment:

```bash
cd /mnt/c/path/to/localforge
pip install -r engine/requirements.txt

export LOCALFORGE_BACKEND=trellis2
export TRELLIS2_ROOT=~/src/TRELLIS.2
python -m uvicorn app:app --app-dir engine --host 127.0.0.1 --port 8000
```

Then run the web app on Windows with `npm run dev`. Begin with draft/512-class
generation and one job at a time. The first model load downloads the
`microsoft/TRELLIS.2-4B` checkpoint and may require Hugging Face access.

## Known risks

- Native Windows is not the recommended first path; official support is Linux.
- Several CUDA extensions must compile for `sm_120`.
- CUDA 13 may require small upstream patches, especially in CuMesh.
- The upstream model or its image encoder can change independently, so pin the
  working repository commit after the first successful generation.
