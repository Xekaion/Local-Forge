"""Read-only preflight; never installs packages, downloads weights or starts inference."""

from __future__ import annotations

import argparse
import csv
import importlib.util
import io
import json
import os
import shutil
import subprocess
from pathlib import Path

GIB = 1024**3


def evaluate_environment(
    *,
    disk_free_bytes: int,
    torch_installed: bool,
    gpu: dict | None,
    trellis_source_present: bool,
    reserve_gib: float,
    install_budget_gib: float,
) -> dict:
    """Candidates remain unimplemented until an actual adapter/inference trial exists."""
    disk_ok = disk_free_bytes >= (reserve_gib + install_budget_gib) * GIB
    capability = gpu.get("compute_capability") if gpu else None
    hardware_ok = bool(capability is not None and capability >= 12)
    reasons = []
    if not torch_installed:
        reasons.append("PyTorch is not installed in this Python environment.")
    if not trellis_source_present:
        reasons.append("TRELLIS2_ROOT does not name an installed source checkout.")
    if not hardware_ok:
        reasons.append(
            "The current TRELLIS.2 adapter requires compute capability 12 or newer."
        )
    if not disk_ok:
        reasons.append(
            "The requested download/install budget would consume the protected disk reserve."
        )
    candidates = []
    for name, url, licensing in (
        (
            "TripoSR",
            "https://github.com/VAST-AI-Research/TripoSR",
            "MIT code and pretrained models; retain notices",
        ),
        (
            "Stable Fast 3D",
            "https://github.com/Stability-AI/stable-fast-3d",
            "Stability AI Community License; gated weights; verify commercial eligibility",
        ),
    ):
        candidates.append(
            {
                "name": name,
                "official_source": url,
                "upstream_default_vram_gib": 6,
                "total_vram_meets_upstream_default": bool(
                    gpu and gpu["total_mib"] >= 6144
                ),
                "free_vram_meets_upstream_default": bool(
                    gpu and gpu["free_mib"] >= 6144
                ),
                "adapter_implemented": False,
                "weights_downloaded_by_preflight": False,
                "inference_tested": False,
                "license_note": licensing,
            }
        )
    return {
        "schema_version": 1,
        "kind": "localforge-runtime-preflight",
        "read_only": True,
        "disk_free_bytes": disk_free_bytes,
        "protected_disk_reserve_gib": reserve_gib,
        "additional_install_budget_gib": install_budget_gib,
        "disk_budget_ok": disk_ok,
        "gpu": gpu,
        "torch_installed": torch_installed,
        "trellis_adapter": {
            "source_present": trellis_source_present,
            "hardware_policy_met": hardware_ok,
            "preflight_passed": not reasons,
            "inference_readiness_confirmed": False,
            "blocking_reasons": reasons,
        },
        "lower_memory_research_candidates": candidates,
        "available_cpu_workflow": "scripts/surface_asset.py: licensed PBR timber surface authoring; not AI reconstruction",
        "inference_runs": 0,
        "paid_calls": 0,
        "network_calls": 0,
        "limitations": [
            "Package presence does not prove compatible CUDA, weights or inference success.",
            "Upstream default VRAM is not a measured guarantee for this workstation.",
            "No download or untested candidate adapter is selected automatically.",
        ],
    }


def query_gpu() -> dict | None:
    executable = shutil.which("nvidia-smi")
    if not executable:
        return None
    try:
        result = subprocess.run(
            [
                executable,
                "--query-gpu=name,memory.total,memory.free,compute_cap",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        first = next(csv.reader(io.StringIO(result.stdout)))
        return {
            "name": first[0].strip(),
            "total_mib": int(first[1]),
            "free_mib": int(first[2]),
            "compute_capability": float(first[3]),
        }
    except (OSError, ValueError, StopIteration, subprocess.SubprocessError):
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--reserve-gib", type=float, default=4.0)
    parser.add_argument(
        "--install-budget-gib",
        type=float,
        default=8.0,
        help="Planning allowance, not a measured model download size.",
    )
    arguments = parser.parse_args()
    if not (
        0 <= arguments.reserve_gib <= 1024 and 0 <= arguments.install_budget_gib <= 1024
    ):
        parser.error(
            "Disk allowances must be finite nonnegative values up to 1024 GiB."
        )
    source = os.environ.get("TRELLIS2_ROOT")
    report = evaluate_environment(
        disk_free_bytes=shutil.disk_usage(
            arguments.workspace.resolve(strict=True)
        ).free,
        torch_installed=importlib.util.find_spec("torch") is not None,
        gpu=query_gpu(),
        trellis_source_present=bool(source and (Path(source) / "trellis2").is_dir()),
        reserve_gib=arguments.reserve_gib,
        install_budget_gib=arguments.install_budget_gib,
    )
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
