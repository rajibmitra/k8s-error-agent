"""
Validation for DedupStore logic — no cluster or API key needed.
"""
from __future__ import annotations
import sys
sys.path.insert(0, "/Users/rajib/k8s-error-agent")

from src.utils.dedup import DedupStore


def check(label: str, condition: bool) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}")
    if not condition:
        raise AssertionError(label)


def run() -> None:
    print("\n=== Dedup Store Validation ===\n")
    store = DedupStore(ttl_seconds=2)

    # First encounter — not a duplicate
    is_dup, h1 = store.is_seen("pod-a", "default", "ERROR: db connection refused\nFATAL: exit 1")
    check("First encounter is not a duplicate", not is_dup)
    check("Hash is 16 chars", len(h1) == 16)

    # Mark it seen
    store.mark_seen(h1)
    check("Store size is 1 after mark_seen", store.size == 1)

    # Second encounter with same logs — duplicate
    is_dup2, h2 = store.is_seen("pod-a", "default", "ERROR: db connection refused\nFATAL: exit 1")
    check("Second encounter is a duplicate", is_dup2)
    check("Same hash returned", h1 == h2)

    # Different pod, different logs — not a duplicate
    is_dup3, h3 = store.is_seen("pod-b", "default", "PANIC: nil pointer dereference")
    check("Different pod/logs is not a duplicate", not is_dup3)
    check("Different hash", h1 != h3)

    # Different namespace, same pod name — not a duplicate
    is_dup4, h4 = store.is_seen("pod-a", "production", "ERROR: db connection refused\nFATAL: exit 1")
    check("Same pod in different namespace is not a duplicate", not is_dup4)
    check("Different hash for different namespace", h1 != h4)

    # TTL expiry
    import time
    time.sleep(3)
    store._evict_expired()
    check("Entries evicted after TTL", store.size == 0)
    is_dup5, _ = store.is_seen("pod-a", "default", "ERROR: db connection refused\nFATAL: exit 1")
    check("Same error is not duplicate after TTL expiry", not is_dup5)

    print("\n  Result: PASS — all dedup checks passed")


if __name__ == "__main__":
    run()
