import importlib.util
from pathlib import Path

MODULE = Path(__file__).resolve().parents[2] / "scripts" / "diagnose_runtime.py"
spec = importlib.util.spec_from_file_location("localforge_preflight", MODULE)
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


def check(**changes):
    arguments = {
        "disk_free_bytes": 4 * 1024**3,
        "torch_installed": False,
        "gpu": {
            "name": "RTX 3070",
            "total_mib": 8192,
            "free_mib": 7900,
            "compute_capability": 8.6,
        },
        "trellis_source_present": False,
        "reserve_gib": 4,
        "install_budget_gib": 8,
    }
    return preflight.evaluate_environment(**(arguments | changes))


def test_8gb_is_candidate_capacity_not_working_inference():
    result = check()
    assert not result["trellis_adapter"]["preflight_passed"]
    assert not result["disk_budget_ok"]
    assert len(result["trellis_adapter"]["blocking_reasons"]) == 4
    for candidate in result["lower_memory_research_candidates"]:
        assert candidate["total_vram_meets_upstream_default"]
        assert (
            not candidate["adapter_implemented"] and not candidate["inference_tested"]
        )
    assert (
        result["inference_runs"] == result["paid_calls"] == result["network_calls"] == 0
    )


def test_other_gpu_users_reduce_available_capacity():
    result = check(
        gpu={
            "name": "RTX 3070",
            "total_mib": 8192,
            "free_mib": 2000,
            "compute_capability": 8.6,
        }
    )
    assert not result["lower_memory_research_candidates"][0][
        "free_vram_meets_upstream_default"
    ]


def test_missing_gpu_is_explicitly_unsuitable():
    result = check(gpu=None)
    assert result["gpu"] is None
    assert not result["trellis_adapter"]["hardware_policy_met"]


def test_good_package_preflight_still_cannot_claim_inference():
    result = check(
        disk_free_bytes=40 * 1024**3,
        torch_installed=True,
        trellis_source_present=True,
        gpu={
            "name": "RTX 5090",
            "total_mib": 32768,
            "free_mib": 30000,
            "compute_capability": 12.0,
        },
    )
    assert result["trellis_adapter"]["preflight_passed"]
    assert not result["trellis_adapter"]["inference_readiness_confirmed"]
