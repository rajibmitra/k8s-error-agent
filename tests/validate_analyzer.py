"""
Validation for LogAnalyzer against real collected pods.
Uses a mock Anthropic client to verify prompt construction and response parsing
without requiring an API key.

Set ANTHROPIC_API_KEY to run against the real Claude API.
"""
from __future__ import annotations

import json
import os
import sys
import textwrap
from unittest.mock import MagicMock, patch

sys.path.insert(0, "/Users/rajib/k8s-error-agent")

from src.models.schemas import PodErrorInfo
from src.tools.log_analyzer import LogAnalyzer


def make_mock_response(payload: dict) -> MagicMock:
    response = MagicMock()
    response.content = [MagicMock(text=json.dumps(payload))]
    return response


def check(label: str, condition: bool) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}")
    if not condition:
        raise AssertionError(label)


def run_mock() -> None:
    print("\n=== LogAnalyzer Validation (mock) ===\n")

    config = {
        "anthropic_api_key": "test-key-not-real",
        "llm": {"model": "claude-sonnet-4-20250514", "max_tokens": 1024, "temperature": 0.0},
    }

    pod = PodErrorInfo(
        pod_name="test-crashloop",
        namespace="default",
        container_name="crasher",
        error_state="CrashLoopBackOff",
        restart_count=5,
        logs="ERROR: database connection refused at 10.0.0.5:5432\nFATAL: max retries exceeded",
        events="[2026-03-12] Warning: BackOff - Back-off restarting failed container",
        node_name="kind-control-plane",
        labels={"app": "test-crashloop"},
    )

    good_response = {
        "summary": "Pod is crash-looping due to a database connection failure at 10.0.0.5:5432.",
        "severity": "high",
        "root_cause": "Database at 10.0.0.5:5432 is unreachable",
        "suggested_fix": "Check database pod status and network policy",
        "error_category": "network",
        "affected_service": None,
    }

    analyzer = LogAnalyzer(config)
    with patch.object(analyzer.client.messages, "create", return_value=make_mock_response(good_response)):
        summary = analyzer.analyze(pod)

    check("severity parsed correctly", summary.severity.value == "high")
    check("summary is non-empty", len(summary.summary) > 0)
    check("root_cause is non-empty", len(summary.root_cause) > 0)
    check("suggested_fix is non-empty", len(summary.suggested_fix) > 0)
    check("error_category is 'network'", summary.error_category == "network")
    print(f"  summary     : {textwrap.shorten(summary.summary, 80)}")
    print(f"  severity    : {summary.severity.value}")
    print(f"  root_cause  : {textwrap.shorten(summary.root_cause, 80)}")
    print(f"  suggested   : {textwrap.shorten(summary.suggested_fix, 80)}")

    # Test malformed JSON fallback
    print("\n  --- Testing malformed JSON fallback ---")
    bad_response = MagicMock()
    bad_response.content = [MagicMock(text="Not JSON at all!!")]
    with patch.object(analyzer.client.messages, "create", return_value=bad_response):
        fallback = analyzer.analyze(pod)
    check("fallback summary contains pod name", pod.pod_name in fallback.summary)
    check("fallback severity is medium", fallback.severity.value == "medium")
    print(f"  fallback    : {textwrap.shorten(fallback.summary, 80)}")

    print("\n  Result: PASS — all analyzer (mock) checks passed")


def run_live(pod_info: PodErrorInfo) -> None:
    print("\n=== LogAnalyzer Validation (LIVE — Claude API) ===\n")
    api_key = os.environ["ANTHROPIC_API_KEY"]
    config = {
        "anthropic_api_key": api_key,
        "llm": {"model": "claude-sonnet-4-20250514", "max_tokens": 1024, "temperature": 0.0},
    }
    analyzer = LogAnalyzer(config)
    summary = analyzer.analyze(pod_info)
    print(f"  pod         : {pod_info.pod_name}")
    print(f"  severity    : {summary.severity.value}")
    print(f"  category    : {summary.error_category}")
    print(f"  summary     : {summary.summary}")
    print(f"  root_cause  : {summary.root_cause}")
    print(f"  fix         : {summary.suggested_fix}")
    print("\n  Result: PASS — live analysis succeeded")


if __name__ == "__main__":
    run_mock()

    if os.environ.get("ANTHROPIC_API_KEY"):
        # Run live against real pods from the kind cluster
        from src.tools.k8s_collector import K8sCollector
        collector = K8sCollector({
            "kubernetes": {"kubeconfig": None, "context": "kind-kind"},
            "log_collection": {
                "tail_lines": 50, "max_log_chars": 2000, "max_events": 10,
                "error_states": ["CrashLoopBackOff", "Error", "OOMKilled"],
            },
        })
        pods = collector.collect_errors("default")
        test_pods = [p for p in pods if p.labels.get("test") == "k8s-error-agent"]
        if test_pods:
            run_live(test_pods[0])
        else:
            print("\n[SKIP] No test pods found for live analysis")
    else:
        print("\n[SKIP] ANTHROPIC_API_KEY not set — skipping live analysis")
        print("       Set it and re-run to test Claude integration end-to-end")
