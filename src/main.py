"""K8s Error Log AI Agent — main entrypoint.

Orchestrates the Observe → Analyze → Act pipeline:
1. Collect error pods from Kubernetes
2. Summarize logs with Claude
3. Create Jira tickets

Usage:
    python -m src.main              # Continuous polling
    python -m src.main --once       # Single scan
    python -m src.main --dry-run    # Analyze but don't create tickets
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

import structlog

from src.models.schemas import AgentResult, Severity
from src.tools.jira_reporter import JiraReporter
from src.tools.k8s_collector import K8sCollector
from src.tools.log_analyzer import LogAnalyzer
from src.utils.config import load_config
from src.utils.dedup import create_dedup_store

logger = structlog.get_logger()


class K8sErrorAgent:
    """Main agent that orchestrates the error detection pipeline."""

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.namespaces = config.get("namespaces", ["default"])
        self.poll_interval = config.get("poll_interval_seconds", 300)
        self.dry_run = config.get("dry_run", False)
        self.severity_threshold = Severity(
            config.get("severity_threshold", "low")
        )

        # Initialize components
        self.collector = K8sCollector(config)
        self.analyzer = LogAnalyzer(config)
        self.dedup = create_dedup_store(config)

        if not self.dry_run:
            self.reporter = JiraReporter(config)
        else:
            self.reporter = None
            logger.info("Running in dry-run mode — no Jira tickets will be created")

    def run_once(self) -> list[AgentResult]:
        """Execute a single scan across all namespaces."""
        all_results: list[AgentResult] = []

        for namespace in self.namespaces:
            try:
                results = self._process_namespace(namespace)
                all_results.extend(results)
            except Exception as e:
                logger.error(
                    "Error processing namespace",
                    namespace=namespace,
                    error=str(e),
                    exc_info=True,
                )

        # Print summary
        created = [r for r in all_results if r.jira_ticket]
        skipped = [r for r in all_results if r.skipped]
        logger.info(
            "Scan complete",
            total_errors=len(all_results),
            tickets_created=len(created),
            skipped=len(skipped),
        )

        return all_results

    def run_continuous(self) -> None:
        """Run the agent in continuous polling mode."""
        logger.info(
            "Starting continuous mode",
            poll_interval=self.poll_interval,
            namespaces=self.namespaces,
        )

        while True:
            try:
                self.run_once()
            except KeyboardInterrupt:
                logger.info("Shutting down gracefully...")
                break
            except Exception as e:
                logger.error("Unexpected error in main loop", error=str(e), exc_info=True)

            logger.info("Sleeping until next scan", seconds=self.poll_interval)
            time.sleep(self.poll_interval)

    def _process_namespace(self, namespace: str) -> list[AgentResult]:
        """Process all error pods in a single namespace."""
        results: list[AgentResult] = []

        # Step 1: Collect errors
        error_pods = self.collector.collect_errors(namespace)
        if not error_pods:
            logger.info("No error pods found", namespace=namespace)
            return results

        for pod_info in error_pods:
            # Step 2: Check deduplication
            is_dup, error_hash = self.dedup.is_seen(
                pod_info.pod_name, pod_info.namespace, pod_info.logs
            )
            if is_dup:
                results.append(
                    AgentResult(
                        pod_info=pod_info,
                        log_summary=None,  # type: ignore
                        dedup_hash=error_hash,
                        skipped=True,
                        skip_reason="duplicate",
                    )
                )
                continue

            # Step 3: Analyze with LLM
            try:
                summary = self.analyzer.analyze(pod_info)
            except Exception as e:
                logger.error(
                    "LLM analysis failed",
                    pod=pod_info.pod_name,
                    error=str(e),
                )
                continue

            # Step 4: Check severity threshold
            if not summary.severity >= self.severity_threshold:
                logger.info(
                    "Below severity threshold, skipping ticket",
                    pod=pod_info.pod_name,
                    severity=summary.severity.value,
                    threshold=self.severity_threshold.value,
                )
                results.append(
                    AgentResult(
                        pod_info=pod_info,
                        log_summary=summary,
                        dedup_hash=error_hash,
                        skipped=True,
                        skip_reason=f"below_threshold ({summary.severity.value})",
                    )
                )
                self.dedup.mark_seen(error_hash)
                continue

            # Step 5: Create Jira ticket
            jira_result = None
            if self.reporter and not self.dry_run:
                try:
                    jira_result = self.reporter.create_ticket(
                        pod_info, summary, error_hash
                    )
                except Exception as e:
                    logger.error(
                        "Jira ticket creation failed",
                        pod=pod_info.pod_name,
                        error=str(e),
                    )
            elif self.dry_run:
                logger.info(
                    "[DRY RUN] Would create Jira ticket",
                    pod=pod_info.pod_name,
                    severity=summary.severity.value,
                    summary=summary.summary[:100],
                )

            # Mark as seen after successful processing
            self.dedup.mark_seen(error_hash)

            results.append(
                AgentResult(
                    pod_info=pod_info,
                    log_summary=summary,
                    jira_ticket=jira_result,
                    dedup_hash=error_hash,
                )
            )

        return results


def setup_logging(config: dict[str, Any]) -> None:
    """Configure structured logging."""
    log_cfg = config.get("logging", {})
    level = log_cfg.get("level", "INFO")
    fmt = log_cfg.get("format", "console")

    processors = [
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]

    if fmt == "json":
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="K8s Error Log AI Agent")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single scan and exit",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Analyze errors but don't create Jira tickets",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to config file (default: config/config.yaml)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    # Load configuration
    config = load_config(args.config)
    if args.dry_run:
        config["dry_run"] = True

    # Setup logging
    setup_logging(config)

    # Create and run agent
    agent = K8sErrorAgent(config)

    if args.once:
        results = agent.run_once()
        sys.exit(0 if results is not None else 1)
    else:
        agent.run_continuous()


if __name__ == "__main__":
    main()
