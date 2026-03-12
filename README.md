# K8s Error Log AI Agent

An AI-powered Kubernetes SRE agent that automatically detects error pods, summarizes logs using Claude, and creates Jira tickets with structured root-cause analysis.

## Architecture

```
┌─────────────┐     ┌──────────────┐     ┌─────────────┐
│  Kubernetes  │────▶│  AI Agent    │────▶│    Jira     │
│  Cluster     │     │  (Claude)    │     │   Cloud     │
└─────────────┘     └──────────────┘     └─────────────┘
      │                    │
      ▼                    ▼
  Error Pods         Summarization
  Log Tailing        Severity Rating
  Event Collection   Root Cause Analysis
```

## Features

- **Auto-detection**: Finds pods in CrashLoopBackOff, Error, OOMKilled states
- **Event correlation**: Pulls both pod logs and cluster events for richer context
- **AI summarization**: Uses Claude to produce structured summaries with root cause + fix suggestions
- **Jira integration**: Creates tickets with severity-based priority mapping
- **Deduplication**: Tracks seen errors via content hashing to avoid duplicate tickets
- **Configurable**: YAML-based config for namespaces, polling intervals, severity thresholds

## Quick Start

### Prerequisites

- Python 3.11+
- Access to a Kubernetes cluster (kubeconfig or in-cluster)
- Anthropic API key
- Jira Cloud API token

### Installation

```bash
# Clone
git clone https://github.com/yourusername/k8s-error-agent.git
cd k8s-error-agent

# Install dependencies
pip install -r requirements.txt

# Configure
cp config/config.example.yaml config/config.yaml
# Edit config/config.yaml with your settings

# Set secrets as env vars
export ANTHROPIC_API_KEY="sk-ant-..."
export JIRA_API_TOKEN="your-jira-api-token"
```

### Run Locally

```bash
# One-shot mode (scan once and exit)
python -m src.main --once

# Continuous mode (poll every N seconds)
python -m src.main

# Dry-run (summarize but don't create Jira tickets)
python -m src.main --dry-run
```

### Deploy to Kubernetes

```bash
# Create secrets
kubectl create secret generic k8s-error-agent \
  --from-literal=ANTHROPIC_API_KEY=sk-ant-... \
  --from-literal=JIRA_API_TOKEN=...

# Deploy as CronJob (recommended)
kubectl apply -f deploy/cronjob.yaml

# Or deploy as a long-running Deployment
kubectl apply -f deploy/deployment.yaml
```

## Configuration

See `config/config.example.yaml` for all options:

| Key | Description | Default |
|-----|-------------|---------|
| `namespaces` | List of namespaces to monitor | `["default"]` |
| `poll_interval_seconds` | Seconds between scans | `300` |
| `log_tail_lines` | Number of log lines to fetch | `100` |
| `max_log_chars` | Truncate logs before sending to LLM | `4000` |
| `jira.project` | Jira project key | `SRE` |
| `jira.issue_type` | Jira issue type | `Bug` |
| `severity_threshold` | Min severity to create ticket | `low` |

## Project Structure

```
k8s-error-agent/
├── src/
│   ├── main.py              # Agent entrypoint & orchestration loop
│   ├── tools/
│   │   ├── k8s_collector.py  # Kubernetes log & event collection
│   │   ├── log_analyzer.py   # Claude-based log summarization
│   │   └── jira_reporter.py  # Jira ticket creation
│   ├── models/
│   │   └── schemas.py        # Pydantic models for structured data
│   └── utils/
│       ├── config.py         # Configuration loader
│       └── dedup.py          # Error deduplication via content hashing
├── config/
│   └── config.example.yaml   # Example configuration
├── deploy/
│   ├── cronjob.yaml          # K8s CronJob manifest
│   ├── deployment.yaml       # K8s Deployment manifest
│   └── rbac.yaml             # ServiceAccount + RBAC for pod/log access
├── tests/
│   ├── validate_collector.py # K8s collection validation against a live cluster
│   ├── validate_analyzer.py  # LogAnalyzer validation (mock + live Claude)
│   └── validate_dedup.py     # DedupStore unit validation
├── Dockerfile
├── requirements.txt
└── README.md
```

## Testing Locally with kind

You can validate the full pipeline against a local [kind](https://kind.sigs.k8s.io/) cluster without touching a real Kubernetes environment or Jira.

### Prerequisites

```bash
# Install kind (macOS)
brew install kind

# Create a cluster (skip if you already have one)
kind create cluster --name kind
```

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Deploy intentionally broken test pods

These pods simulate the three most common error states the agent monitors:

```bash
kubectl apply -f - <<'EOF'
# CrashLoopBackOff: exits with a simulated DB connection error
apiVersion: v1
kind: Pod
metadata:
  name: test-crashloop
  namespace: default
  labels:
    app: test-crashloop
    test: k8s-error-agent
spec:
  restartPolicy: Always
  containers:
  - name: crasher
    image: busybox
    command: ["sh", "-c", "echo 'ERROR: database connection refused at 10.0.0.5:5432'; echo 'FATAL: max retries exceeded'; exit 1"]
---
# OOMKilled: exceeds memory limit
apiVersion: v1
kind: Pod
metadata:
  name: test-oomkilled
  namespace: default
  labels:
    app: test-oomkilled
    test: k8s-error-agent
spec:
  restartPolicy: Always
  containers:
  - name: oom
    image: busybox
    command: ["sh", "-c", "echo 'Starting memory intensive operation'; dd if=/dev/zero bs=1M count=512 | cat > /dev/null"]
    resources:
      limits:
        memory: "32Mi"
---
# Error: missing required environment variables
apiVersion: v1
kind: Pod
metadata:
  name: test-config-error
  namespace: default
  labels:
    app: test-config-error
    test: k8s-error-agent
spec:
  restartPolicy: Never
  containers:
  - name: misconfig
    image: busybox
    command: ["sh", "-c", "echo 'ERROR: required env var DATABASE_URL not set'; echo 'ERROR: required env var REDIS_URL not set'; echo 'FATAL: cannot start without required configuration'; exit 1"]
EOF
```

Wait ~20 seconds for pods to enter their error states:

```bash
kubectl get pods -l test=k8s-error-agent
# NAME                READY   STATUS             RESTARTS
# test-config-error   0/1     Error              0
# test-crashloop      0/1     CrashLoopBackOff   2
# test-oomkilled      0/1     CrashLoopBackOff   1
```

### 3. Validate K8s collection (no API key needed)

Tests pod detection, log collection, and event gathering:

```bash
python3 tests/validate_collector.py
```

### 4. Validate deduplication logic (no API key needed)

Tests hash generation, duplicate detection, and TTL expiry:

```bash
python3 tests/validate_dedup.py
```

### 5. Validate Claude log analysis

Runs mock tests first, then live analysis against the real pods if `ANTHROPIC_API_KEY` is set:

```bash
# Mock only (no API key required)
python3 tests/validate_analyzer.py

# Live end-to-end (requires Anthropic API key)
export ANTHROPIC_API_KEY="sk-ant-..."
python3 tests/validate_analyzer.py
```

The live run collects real logs from the kind cluster and sends them to Claude, producing output like:

```
pod         : test-config-error
severity    : high
category    : config
summary     : Pod is failing to start due to missing required environment variables
              DATABASE_URL and REDIS_URL.
root_cause  : Missing required environment variables in pod specification
fix         : Add the missing environment variables via env, envFrom, or ConfigMap/Secret
```

### 6. Clean up

```bash
kubectl delete pods -l test=k8s-error-agent --grace-period=0
```

## RBAC

The agent needs read access to pods, logs, and events. See `deploy/rbac.yaml` for the minimal ClusterRole.

## Extending

- **Slack notifications**: Add a Slack tool to notify a channel when critical tickets are created
- **Prometheus metrics**: Expose a `/metrics` endpoint with error counts per namespace
- **Multi-cluster**: Use contexts to scan multiple clusters
- **LangGraph migration**: Wrap tools as LangGraph nodes for conditional branching

## License

MIT
