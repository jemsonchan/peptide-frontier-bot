"""
Weekly change watcher.

Snapshots a few pages that decide how this account performs and what it costs,
diffs them against the last snapshot, and stays silent unless something moved.
Silence is the product: a watcher that pings every week gets muted, and then
you miss the week that mattered.

Watched:
  * xai-org/x-algorithm README -- xAI committed to ~4-weekly updates with
    developer notes. Ranking weight changes show up here before they show up
    in your analytics.
  * X API pricing -- the docs say rates are "subject to change", and at a $6
    balance a rate change is the difference between 40 days and 4.
  * X API rate limits -- quieter, but a limit cut can silently break the run.

Costs nothing: plain HTTP to public pages, no X credits, no LLM.

Snapshots live in state/watch/ and are committed, so the diff survives runner
recycling and you can read the history in git.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import requests

log = logging.getLogger(__name__)

SOURCES: dict[str, str] = {
    "x-algorithm-readme": "https://raw.githubusercontent.com/xai-org/x-algorithm/main/README.md",
    "x-api-pricing": "https://docs.x.com/x-api/getting-started/pricing.md",
    "x-api-rate-limits": "https://docs.x.com/x-api/fundamentals/rate-limits.md",
}

# Lines that churn without meaning anything -- timestamps, build hashes, star
# counts. Diffing these produces a weekly ping that says nothing.
NOISE = re.compile(
    r"(last updated|generated on|\d{4}-\d{2}-\d{2}T\d{2}:\d{2}|"
    r"stars?|forks?|watchers?|badge|shields\.io|\?s=[0-9a-f]{8,})",
    re.IGNORECASE,
)

# Lines worth shouting about. A change to any of these is not routine.
CRITICAL = re.compile(
    r"(\$\s?0?\.\d+|per (?:resource|request)|price|cost|billing|credit|"
    r"rate limit|/15min|/24hrs|weight|deprecat|breaking|removed|owned read)",
    re.IGNORECASE,
)


@dataclass
class Change:
    name: str
    url: str
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    critical: bool = False
    first_seen: bool = False

    def summary(self) -> str:
        if self.first_seen:
            return f"{self.name}: baseline captured ({len(self.added)} lines)"
        return f"{self.name}: +{len(self.added)} / -{len(self.removed)}"


def _normalise(text: str) -> list[str]:
    return [ln.rstrip() for ln in text.splitlines() if ln.strip() and not NOISE.search(ln)]


def fetch(url: str, timeout: int = 30) -> str:
    r = requests.get(url, timeout=timeout, headers={"User-Agent": "pf-autorespond-watch/1.0"})
    r.raise_for_status()
    return r.text


def check(state_dir: str | Path, sources: dict[str, str] | None = None,
          fetcher=fetch) -> list[Change]:
    d = Path(state_dir)
    d.mkdir(parents=True, exist_ok=True)
    changes: list[Change] = []

    for name, url in (sources or SOURCES).items():
        snap = d / f"{name}.txt"
        try:
            current = _normalise(fetcher(url))
        except Exception as e:  # noqa: BLE001 - one dead source must not kill the run
            log.warning("fetch %s failed: %s", name, e)
            continue

        if not snap.exists():
            snap.write_text("\n".join(current), encoding="utf-8")
            changes.append(Change(name, url, added=current[:5], first_seen=True))
            continue

        previous = snap.read_text(encoding="utf-8").splitlines()
        if previous == current:
            continue

        diff = list(difflib.unified_diff(previous, current, lineterm="", n=0))
        added = [ln[1:].strip() for ln in diff if ln.startswith("+") and not ln.startswith("+++")]
        removed = [ln[1:].strip() for ln in diff if ln.startswith("-") and not ln.startswith("---")]
        critical = any(CRITICAL.search(ln) for ln in added + removed)
        snap.write_text("\n".join(current), encoding="utf-8")
        changes.append(Change(name, url, added=added, removed=removed, critical=critical))

    (d / "last_check.json").write_text(
        json.dumps(
            {
                "checked": datetime.now(timezone.utc).isoformat(),
                "changed": [c.name for c in changes],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return changes


def render(changes: list[Change], max_lines: int = 25) -> str:
    """Markdown report. Critical changes first, because that's what you'll read."""
    parts: list[str] = []
    for c in sorted(changes, key=lambda x: not x.critical):
        head = "🔴 " if c.critical else ""
        parts.append(f"### {head}{c.name}\n\n<{c.url}>\n")
        if c.first_seen:
            parts.append("_Baseline captured. Future runs will diff against this._\n")
            continue
        if c.added:
            body = "\n".join(f"+ {ln}" for ln in c.added[:max_lines])
            more = f"\n… +{len(c.added) - max_lines} more" if len(c.added) > max_lines else ""
            parts.append(f"**Added**\n```diff\n{body}{more}\n```\n")
        if c.removed:
            body = "\n".join(f"- {ln}" for ln in c.removed[:max_lines])
            more = f"\n… +{len(c.removed) - max_lines} more" if len(c.removed) > max_lines else ""
            parts.append(f"**Removed**\n```diff\n{body}{more}\n```\n")
    return "\n".join(parts)


def digest(changes: list[Change]) -> str:
    if not changes:
        return ""
    crit = [c for c in changes if c.critical]
    lead = "🔴 pricing/ranking change detected" if crit else "Change detected"
    return f"{lead}: " + "; ".join(c.summary() for c in changes)


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12]
