"""
Validation script for K8sCollector against a local kind cluster.
Requires test pods deployed with: kubectl apply -f tests/test-error-pods.yaml

Usage:
    python -m tests.validate_collector
"""

from __future__ import annotations

import sys
import textwrap

sys.path.insert(0, "/Users/rajib/k8s-error-agent")

from src.tools.k8s_collector import K8sCollector

# Minimal config — points to default kubeconfig (kind-kind context)
CONFIG = {
    "kubernetes": {
        "kubeconfig": None,   # uses ~/.kube/config
        "context": "kind-kind",
    },
    "log_collection": {
        "tail_lines": 100,
        "max_log_chars": 4000,
        "max_events": 20,
        "error_states": [
            "CrashLoopBackOff",
            "Error",
            "OOMKilled",
            "ImagePullBackOff",
            "CreateContainerConfigError",
        ],
    },
}

EXPECTED_PODS = {"test-crashloop", "test-oomkilled", "test-config-error"}


def separator(title: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {title}")
    print("=" * 60)


def run() -> bool:
    collector = K8sCollector(CONFIG)

    separator("Scanning namespace: default")
    errors = collector.collect_errors("default")

    if not errors:
        print("FAIL: No error pods detected — are the test pods running?")
        print("      Run: kubectl get pods -l test=k8s-error-agent")
        return False

    found_names = {p.pod_name for p in errors}
    print(f"\nDetected {len(errors)} error pod(s): {sorted(found_names)}")

    all_passed = True

    for pod in errors:
        separator(f"Pod: {pod.pod_name}")
        print(f"  namespace    : {pod.namespace}")
        print(f"  container    : {pod.container_name}")
        print(f"  error_state  : {pod.error_state}")
        print(f"  restarts     : {pod.restart_count}")
        print(f"  node         : {pod.node_name}")
        print(f"  labels       : {pod.labels}")

        # Validate logs collected
        if pod.logs:
            preview = textwrap.shorten(pod.logs.strip(), width=120, placeholder="...")
            print(f"  logs ({len(pod.logs)} chars): {preview}")
            print("  [PASS] logs collected")
        else:
            print("  [WARN] no logs — pod may not have started yet")

        # Validate events collected
        if pod.events:
            first_event = pod.events.splitlines()[0]
            print(f"  events       : {first_event} ...")
            print("  [PASS] events collected")
        else:
            print("  [WARN] no events yet (normal for very new pods)")

        # Check error state is one we care about
        if pod.error_state not in CONFIG["log_collection"]["error_states"]:
            print(f"  [FAIL] unexpected error_state: {pod.error_state}")
            all_passed = False
        else:
            print(f"  [PASS] error_state '{pod.error_state}' is in monitored list")

    # Check we found the expected test pods (subset check — cluster may have others)
    missing = EXPECTED_PODS - found_names
    if missing:
        print(f"\n[WARN] Some test pods not yet in error state: {missing}")
        print("       They may still be starting. Re-run in ~30s.")
    else:
        print(f"\n[PASS] All {len(EXPECTED_PODS)} expected test pods detected")

    separator("Summary")
    status = "PASS" if all_passed and not missing else "PARTIAL"
    print(f"Result: {status}")
    print(f"  Total error pods found : {len(errors)}")
    print(f"  Test pods accounted for: {len(EXPECTED_PODS - missing)}/{len(EXPECTED_PODS)}")
    return all_passed and not missing


if __name__ == "__main__":
    ok = run()
    sys.exit(0 if ok else 1)
