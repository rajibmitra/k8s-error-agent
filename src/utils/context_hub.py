"""Context Hub integration — fetches LLM-optimized API docs via the chub CLI.

Used to enrich Claude's system prompt with accurate, up-to-date library
documentation so it avoids hallucinating field names or API signatures.

Requires: npm install -g @aisuite/chub  (see README for setup)
Falls back gracefully if chub is not installed.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from functools import lru_cache
from typing import Sequence

import structlog

logger = structlog.get_logger()

# Sections to extract from the jira/issues doc — focused on ticket creation
_JIRA_SECTIONS = ("### Issues", "### Error Handling", "## Error Handling")


def _chub_bin() -> str | None:
    """Locate the chub binary. Checks PATH and common user-local install paths."""
    if path := shutil.which("chub"):
        return path
    # Common user-local npm prefix (npm install -g --prefix ~/.npm-global)
    import os
    candidates = [
        os.path.expanduser("~/.npm-global/bin/chub"),
        os.path.expanduser("~/.local/bin/chub"),
        "/usr/local/bin/chub",
    ]
    return next((p for p in candidates if shutil.which(p) or __import__("os.path", fromlist=["exists"]).exists(p)), None)


def _run_chub(args: Sequence[str], timeout: int = 10) -> str | None:
    """Run a chub command and return stdout, or None on any failure."""
    binary = _chub_bin()
    if not binary:
        return None
    try:
        result = subprocess.run(
            [binary, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            logger.debug("chub command failed", args=args, stderr=result.stderr[:200])
            return None
        return result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        logger.debug("chub unavailable", error=str(e))
        return None


def _extract_sections(content: str, section_headers: Sequence[str]) -> str:
    """Extract specific markdown sections from a larger document.

    Each target section is extracted independently: from its header line up to
    (but not including) the next header at the same or higher level. Results
    are concatenated in document order.

    Code fences (``` blocks) are tracked so that Python comments starting
    with '#' inside code blocks are not mistaken for markdown headers.
    """
    lines = content.splitlines()
    extracted: list[str] = []

    for target in section_headers:
        target_level = len(target) - len(target.lstrip("#"))
        capturing = False
        in_code_block = False

        for line in lines:
            # Track code fence state
            if line.strip().startswith("```"):
                if capturing:
                    in_code_block = not in_code_block
                    extracted.append(line)
                continue

            if not capturing:
                if line.strip() == target:
                    capturing = True
                    extracted.append(line)
                continue

            # Only treat as a markdown header when outside a code block
            if not in_code_block:
                header_match = re.match(r"^(#{1,6})\s", line)
                if header_match and len(header_match.group(1)) <= target_level:
                    break

            extracted.append(line)

    return "\n".join(extracted).strip()


@lru_cache(maxsize=8)
def fetch_doc(doc_id: str, lang: str = "py") -> str | None:
    """Fetch a context-hub doc by ID and return its content.

    Results are cached in-process so each doc is only fetched once per run.

    Args:
        doc_id: context-hub doc ID, e.g. "jira/issues"
        lang:   language variant, e.g. "py" or "js"

    Returns:
        Doc content as a string, or None if chub is unavailable or doc not found.
    """
    if not _chub_bin():
        logger.info(
            "chub not found — skipping context enrichment",
            hint="npm install -g @aisuite/chub",
        )
        return None

    logger.info("Fetching context-hub doc", doc_id=doc_id, lang=lang)
    content = _run_chub(["get", doc_id, "--lang", lang])
    if content:
        logger.info("context-hub doc fetched", doc_id=doc_id, chars=len(content))
    return content


def jira_context() -> str:
    """Return focused Jira issue-creation docs for prompt enrichment.

    Fetches the full jira/issues doc and extracts only the sections relevant
    to creating and formatting tickets (Issues + Error Handling).
    """
    full = fetch_doc("jira/issues", lang="py")
    if not full:
        return ""

    extracted = _extract_sections(full, _JIRA_SECTIONS)
    if not extracted:
        # Fallback: return first 3000 chars if section extraction fails
        return full[:3000]

    return extracted
