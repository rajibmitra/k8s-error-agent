"""Log analyzer using Claude for structured summarization.

Sends error logs + events to Claude and gets back a structured summary
with severity, root cause, and remediation suggestions.
"""

from __future__ import annotations

import json
from typing import Any

import anthropic
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential

from src.models.schemas import LogSummary, PodErrorInfo

logger = structlog.get_logger()

SYSTEM_PROMPT = """You are a Kubernetes SRE expert. Analyze error logs and events from a pod
and produce a structured JSON summary.

You must respond with ONLY valid JSON (no markdown, no backticks, no preamble).

JSON schema:
{
  "summary": "2-3 sentence human-readable summary of the issue",
  "severity": "critical | high | medium | low",
  "root_cause": "Best guess at what caused this error",
  "suggested_fix": "Concrete, actionable remediation step",
  "error_category": "config | resource | network | application | dependency",
  "affected_service": "Name of the service/component most affected, or null"
}

Severity guidelines:
- critical: Data loss, security breach, complete service outage
- high: Partial outage, persistent crashes, resource exhaustion (OOMKilled)
- medium: Intermittent failures, degraded performance, config errors
- low: Transient issues, expected restarts, non-impacting warnings"""


class LogAnalyzer:
    """Analyzes Kubernetes error logs using Claude."""

    def __init__(self, config: dict[str, Any]):
        self.client = anthropic.Anthropic(api_key=config["anthropic_api_key"])
        llm_cfg = config.get("llm", {})
        self.model = llm_cfg.get("model", "claude-sonnet-4-20250514")
        self.max_tokens = llm_cfg.get("max_tokens", 1024)
        self.temperature = llm_cfg.get("temperature", 0.0)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
    )
    def analyze(self, pod_info: PodErrorInfo) -> LogSummary:
        """Analyze a pod's error logs and return a structured summary."""
        user_prompt = self._build_prompt(pod_info)

        logger.info(
            "Sending logs to LLM for analysis",
            pod=pod_info.pod_name,
            namespace=pod_info.namespace,
            model=self.model,
        )

        response = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )

        raw_text = response.content[0].text.strip()

        # Strip markdown fences if present
        if raw_text.startswith("```"):
            raw_text = raw_text.split("\n", 1)[-1]
            raw_text = raw_text.rsplit("```", 1)[0]

        try:
            data = json.loads(raw_text)
            summary = LogSummary(**data)
        except (json.JSONDecodeError, ValueError) as e:
            logger.error(
                "Failed to parse LLM response",
                error=str(e),
                raw=raw_text[:200],
            )
            # Return a fallback summary rather than failing the pipeline
            summary = LogSummary(
                summary=f"Pod {pod_info.pod_name} is in {pod_info.error_state} state. "
                f"Automated analysis failed to parse. Manual review required.",
                severity="medium",
                root_cause="Unable to determine (LLM parse failure)",
                suggested_fix="Review pod logs manually with: "
                f"kubectl logs {pod_info.pod_name} -n {pod_info.namespace}",
                error_category="application",
                affected_service=pod_info.owner_name,
            )

        logger.info(
            "Analysis complete",
            pod=pod_info.pod_name,
            severity=summary.severity.value,
            category=summary.error_category,
        )
        return summary

    def _build_prompt(self, pod_info: PodErrorInfo) -> str:
        """Build the analysis prompt from pod error info."""
        parts = [
            f"Pod: {pod_info.pod_name}",
            f"Namespace: {pod_info.namespace}",
            f"Container: {pod_info.container_name}",
            f"Error State: {pod_info.error_state}",
            f"Restart Count: {pod_info.restart_count}",
        ]

        if pod_info.owner_kind and pod_info.owner_name:
            parts.append(f"Owner: {pod_info.owner_kind}/{pod_info.owner_name}")

        if pod_info.node_name:
            parts.append(f"Node: {pod_info.node_name}")

        if pod_info.labels:
            label_str = ", ".join(f"{k}={v}" for k, v in pod_info.labels.items())
            parts.append(f"Labels: {label_str}")

        parts.append(f"\n--- Logs (last {len(pod_info.logs)} chars) ---")
        parts.append(pod_info.logs if pod_info.logs else "(no logs available)")

        parts.append("\n--- Events ---")
        parts.append(pod_info.events if pod_info.events else "(no events available)")

        return "\n".join(parts)
