"""Kubernetes log and event collector.

Scans namespaces for pods in error states and collects their logs + events
for downstream analysis.
"""

from __future__ import annotations

from typing import Any

import structlog
from kubernetes import client, config as k8s_config
from kubernetes.client.exceptions import ApiException

from src.models.schemas import PodErrorInfo

logger = structlog.get_logger()

# Default error states to detect
DEFAULT_ERROR_STATES = [
    "CrashLoopBackOff",
    "Error",
    "OOMKilled",
    "ImagePullBackOff",
    "CreateContainerConfigError",
]


class K8sCollector:
    """Collects error pod information from Kubernetes clusters."""

    def __init__(self, config: dict[str, Any]):
        self._init_k8s_client(config.get("kubernetes", {}))
        self.v1 = client.CoreV1Api()

        log_cfg = config.get("log_collection", {})
        self.tail_lines = log_cfg.get("tail_lines", 100)
        self.max_log_chars = log_cfg.get("max_log_chars", 4000)
        self.max_events = log_cfg.get("max_events", 20)
        self.error_states = log_cfg.get("error_states", DEFAULT_ERROR_STATES)

    def _init_k8s_client(self, k8s_cfg: dict[str, Any]) -> None:
        """Initialize the Kubernetes client (in-cluster or kubeconfig)."""
        kubeconfig = k8s_cfg.get("kubeconfig")
        context = k8s_cfg.get("context")

        try:
            if kubeconfig:
                k8s_config.load_kube_config(
                    config_file=kubeconfig, context=context
                )
                logger.info("Using kubeconfig", path=kubeconfig, context=context)
            else:
                k8s_config.load_incluster_config()
                logger.info("Using in-cluster Kubernetes config")
        except k8s_config.ConfigException:
            # Fallback to default kubeconfig
            k8s_config.load_kube_config(context=context)
            logger.info("Using default kubeconfig", context=context)

    def collect_errors(self, namespace: str) -> list[PodErrorInfo]:
        """Scan a namespace for pods in error states and collect their info."""
        logger.info("Scanning namespace for error pods", namespace=namespace)
        error_pods: list[PodErrorInfo] = []

        try:
            pods = self.v1.list_namespaced_pod(namespace)
        except ApiException as e:
            logger.error("Failed to list pods", namespace=namespace, error=str(e))
            return []

        for pod in pods.items:
            if not pod.status or not pod.status.container_statuses:
                continue

            for cs in pod.status.container_statuses:
                error_state = self._detect_error_state(cs)
                if not error_state:
                    continue

                pod_info = self._build_pod_info(pod, cs, error_state, namespace)
                error_pods.append(pod_info)
                logger.info(
                    "Found error pod",
                    pod=pod_info.pod_name,
                    namespace=namespace,
                    state=error_state,
                    restarts=pod_info.restart_count,
                )

        logger.info(
            "Namespace scan complete",
            namespace=namespace,
            error_count=len(error_pods),
        )
        return error_pods

    def _detect_error_state(self, container_status) -> str | None:
        """Check if a container is in a known error state."""
        if container_status.state:
            waiting = container_status.state.waiting
            if waiting and waiting.reason in self.error_states:
                return waiting.reason

            terminated = container_status.state.terminated
            if terminated and terminated.reason in self.error_states:
                return terminated.reason

        # Check last_state for recently crashed containers
        if container_status.last_state:
            terminated = container_status.last_state.terminated
            if terminated and terminated.reason in self.error_states:
                return terminated.reason

        return None

    def _build_pod_info(
        self, pod, container_status, error_state: str, namespace: str
    ) -> PodErrorInfo:
        """Build a PodErrorInfo from raw K8s objects."""
        pod_name = pod.metadata.name
        container_name = container_status.name

        # Get logs
        logs = self._get_pod_logs(pod_name, namespace, container_name)

        # Get events
        events = self._get_pod_events(pod_name, namespace)

        # Get owner reference (Deployment, StatefulSet, etc.)
        owner_kind, owner_name = None, None
        if pod.metadata.owner_references:
            owner = pod.metadata.owner_references[0]
            owner_kind = owner.kind
            owner_name = owner.name

        return PodErrorInfo(
            pod_name=pod_name,
            namespace=namespace,
            node_name=pod.spec.node_name,
            container_name=container_name,
            error_state=error_state,
            restart_count=container_status.restart_count or 0,
            logs=logs,
            events=events,
            labels=dict(pod.metadata.labels or {}),
            owner_kind=owner_kind,
            owner_name=owner_name,
        )

    def _get_pod_logs(
        self, pod_name: str, namespace: str, container: str
    ) -> str:
        """Fetch recent logs from a pod container."""
        try:
            logs = self.v1.read_namespaced_pod_log(
                name=pod_name,
                namespace=namespace,
                container=container,
                tail_lines=self.tail_lines,
                previous=False,
            )
            # Truncate to max chars
            return logs[: self.max_log_chars] if logs else ""
        except ApiException:
            # Try previous container logs (useful for CrashLoopBackOff)
            try:
                logs = self.v1.read_namespaced_pod_log(
                    name=pod_name,
                    namespace=namespace,
                    container=container,
                    tail_lines=self.tail_lines,
                    previous=True,
                )
                return logs[: self.max_log_chars] if logs else ""
            except ApiException as e:
                logger.warning(
                    "Could not fetch logs",
                    pod=pod_name,
                    namespace=namespace,
                    error=str(e),
                )
                return ""

    def _get_pod_events(self, pod_name: str, namespace: str) -> str:
        """Fetch recent events related to a pod."""
        try:
            events = self.v1.list_namespaced_event(
                namespace,
                field_selector=f"involvedObject.name={pod_name}",
            )
            # Format events as readable text, most recent first
            sorted_events = sorted(
                events.items,
                key=lambda e: e.last_timestamp or e.event_time or "",
                reverse=True,
            )
            lines = []
            for event in sorted_events[: self.max_events]:
                ts = event.last_timestamp or event.event_time or "unknown"
                lines.append(
                    f"[{ts}] {event.type}: {event.reason} - {event.message}"
                )
            return "\n".join(lines)
        except ApiException as e:
            logger.warning(
                "Could not fetch events",
                pod=pod_name,
                namespace=namespace,
                error=str(e),
            )
            return ""
