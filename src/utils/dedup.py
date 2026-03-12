"""Deduplication for error logs using content hashing.

Prevents the agent from creating duplicate Jira tickets for the same
recurring error within a configurable TTL window.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any

import structlog

logger = structlog.get_logger()


class DedupStore:
    """In-memory deduplication store with TTL expiry."""

    def __init__(self, ttl_seconds: int = 86400):
        self._seen: dict[str, float] = {}  # hash -> timestamp
        self._ttl = ttl_seconds

    def _compute_hash(self, pod_name: str, namespace: str, logs: str) -> str:
        """Hash the error signature (not exact logs, but the error pattern).

        We hash the first 5 lines of the log + pod owner info to group
        errors from the same root cause even across pod restarts.
        """
        # Use first 5 non-empty lines as the error signature
        log_lines = [l.strip() for l in logs.splitlines() if l.strip()][:5]
        signature = f"{namespace}:{pod_name}:{'|'.join(log_lines)}"
        return hashlib.sha256(signature.encode()).hexdigest()[:16]

    def is_seen(self, pod_name: str, namespace: str, logs: str) -> tuple[bool, str]:
        """Check if this error has been seen within the TTL window.

        Returns:
            Tuple of (is_duplicate, hash_value)
        """
        self._evict_expired()
        error_hash = self._compute_hash(pod_name, namespace, logs)

        if error_hash in self._seen:
            logger.debug(
                "Duplicate error detected, skipping",
                hash=error_hash,
                pod=pod_name,
                namespace=namespace,
            )
            return True, error_hash

        return False, error_hash

    def mark_seen(self, error_hash: str) -> None:
        """Mark an error hash as seen."""
        self._seen[error_hash] = time.time()

    def _evict_expired(self) -> None:
        """Remove entries older than TTL."""
        now = time.time()
        expired = [h for h, ts in self._seen.items() if now - ts > self._ttl]
        for h in expired:
            del self._seen[h]

    @property
    def size(self) -> int:
        return len(self._seen)


class ConfigMapDedupStore(DedupStore):
    """Persistent deduplication backed by a Kubernetes ConfigMap.

    Survives agent restarts. Useful when running as a CronJob.
    """

    def __init__(
        self,
        ttl_seconds: int = 86400,
        configmap_name: str = "k8s-error-agent-state",
        namespace: str = "default",
    ):
        super().__init__(ttl_seconds)
        self._cm_name = configmap_name
        self._cm_namespace = namespace
        self._load_from_configmap()

    def _load_from_configmap(self) -> None:
        """Load seen hashes from ConfigMap on startup."""
        try:
            from kubernetes import client

            v1 = client.CoreV1Api()
            cm = v1.read_namespaced_config_map(self._cm_name, self._cm_namespace)
            if cm.data and "seen_hashes" in cm.data:
                import json

                entries = json.loads(cm.data["seen_hashes"])
                self._seen = {k: float(v) for k, v in entries.items()}
                self._evict_expired()
                logger.info("Loaded dedup state from ConfigMap", count=len(self._seen))
        except Exception as e:
            logger.warning("Could not load dedup ConfigMap, starting fresh", error=str(e))

    def mark_seen(self, error_hash: str) -> None:
        """Mark as seen and persist to ConfigMap."""
        super().mark_seen(error_hash)
        self._save_to_configmap()

    def _save_to_configmap(self) -> None:
        """Persist current state to ConfigMap."""
        try:
            import json

            from kubernetes import client

            v1 = client.CoreV1Api()
            body = client.V1ConfigMap(
                metadata=client.V1ObjectMeta(name=self._cm_name),
                data={"seen_hashes": json.dumps(self._seen)},
            )
            try:
                v1.replace_namespaced_config_map(self._cm_name, self._cm_namespace, body)
            except client.exceptions.ApiException as e:
                if e.status == 404:
                    v1.create_namespaced_config_map(self._cm_namespace, body)
                else:
                    raise
        except Exception as e:
            logger.warning("Could not persist dedup state to ConfigMap", error=str(e))


def create_dedup_store(config: dict[str, Any]) -> DedupStore:
    """Factory to create the appropriate dedup store from config."""
    dedup_cfg = config.get("dedup", {})
    ttl = dedup_cfg.get("ttl_seconds", 86400)
    backend = dedup_cfg.get("backend", "memory")

    if backend == "configmap":
        return ConfigMapDedupStore(
            ttl_seconds=ttl,
            configmap_name=dedup_cfg.get("configmap_name", "k8s-error-agent-state"),
            namespace=dedup_cfg.get("configmap_namespace", "default"),
        )
    return DedupStore(ttl_seconds=ttl)
