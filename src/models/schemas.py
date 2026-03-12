"""Pydantic models for structured data flowing through the agent pipeline."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

    @property
    def rank(self) -> int:
        return {"critical": 4, "high": 3, "medium": 2, "low": 1}[self.value]

    def __ge__(self, other: Severity) -> bool:
        return self.rank >= other.rank

    def __gt__(self, other: Severity) -> bool:
        return self.rank > other.rank


class PodErrorInfo(BaseModel):
    """Raw error information collected from Kubernetes."""

    pod_name: str
    namespace: str
    node_name: str | None = None
    container_name: str
    error_state: str  # e.g., CrashLoopBackOff, OOMKilled
    restart_count: int = 0
    logs: str = ""
    events: str = ""
    labels: dict[str, str] = Field(default_factory=dict)
    owner_kind: str | None = None  # Deployment, StatefulSet, DaemonSet, etc.
    owner_name: str | None = None
    collected_at: datetime = Field(default_factory=datetime.utcnow)


class LogSummary(BaseModel):
    """Structured summary produced by the LLM."""

    summary: str = Field(description="2-3 sentence human-readable summary")
    severity: Severity = Field(description="Assessed severity level")
    root_cause: str = Field(description="Best guess at the root cause")
    suggested_fix: str = Field(description="Actionable remediation step")
    error_category: str = Field(
        description="Category: config, resource, network, application, dependency"
    )
    affected_service: str | None = Field(
        default=None, description="Service or component most likely affected"
    )


class JiraTicketResult(BaseModel):
    """Result of creating a Jira ticket."""

    ticket_key: str  # e.g., SRE-1234
    ticket_url: str
    summary: str
    severity: Severity


class AgentResult(BaseModel):
    """Complete result of processing one error pod."""

    pod_info: PodErrorInfo
    log_summary: LogSummary
    jira_ticket: JiraTicketResult | None = None  # None in dry-run mode
    dedup_hash: str
    skipped: bool = False
    skip_reason: str | None = None
