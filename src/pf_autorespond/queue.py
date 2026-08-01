"""
Pending-draft queue.

The hybrid model splits one action into two phases that happen in different
workflow runs:

    draft   (autorespond.yml)  -- generate, gate, critique, enqueue.  $0 in X credits
    publish (publish.yml)      -- on your approval, actually post.    $0.015

That split is the whole point. A draft you reject costs nothing but a fraction
of a cent of LLM tokens; only approvals reach the X API. Running in review mode
is therefore strictly cheaper than running autonomously, not more expensive.

Drafts expire. A reply approved 20 hours late is worse than no reply: the
target has aged past the ranker's AgeFilter, so you would pay full price for
close to zero distribution. Expiry is not tidiness, it is budget protection.

state/queue.json is committed by the workflow, so the queue is the system of
record and every decision is in git history.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

Status = Literal["pending", "approved", "rejected", "published", "expired", "failed"]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class Draft:
    id: str                      # short deterministic id, used in callback data
    action: str                  # reply | quote | post
    target_id: str               # post we are replying to / quoting ("" for post)
    target_author: str
    target_text: str
    conversation_id: str
    text: str                    # what we would publish
    original_text: str = ""      # pre-edit draft, kept for the audit trail
    score: float = 0.0
    kind: str = ""
    created: str = field(default_factory=lambda: _utcnow().isoformat())
    status: Status = "pending"
    decided_by: str = ""
    decided_via: str = ""        # "github" | "telegram"
    decided_at: str = ""
    reject_reason: str = ""
    published_id: str = ""
    github_issue: int = 0
    telegram_message: int = 0
    nostr_event: str = ""
    nostr_relays_ok: int = 0

    def age_hours(self) -> float:
        return (_utcnow() - _parse(self.created)).total_seconds() / 3600.0

    def is_expired(self, ttl_hours: float) -> bool:
        return self.status == "pending" and self.age_hours() > ttl_hours

    def target_url(self) -> str:
        return f"https://x.com/i/status/{self.target_id}" if self.target_id else ""


class Queue:
    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self.drafts: list[Draft] = []
        if self.path.exists():
            raw = json.loads(self.path.read_text(encoding="utf-8") or "{}")
            self.drafts = [Draft(**d) for d in raw.get("drafts", [])]

    # ------------------------------------------------------------- mutation
    def add(self, draft: Draft) -> Draft:
        if any(d.id == draft.id for d in self.drafts):
            return draft
        self.drafts.append(draft)
        return draft

    def get(self, draft_id: str) -> Draft | None:
        return next((d for d in self.drafts if d.id == draft_id), None)

    def pending(self) -> list[Draft]:
        return [d for d in self.drafts if d.status == "pending"]

    def approved(self) -> list[Draft]:
        return [d for d in self.drafts if d.status == "approved"]

    def has_pending_for(self, target_id: str) -> bool:
        """Never queue two drafts for the same target across runs."""
        return any(
            d.target_id == target_id and d.status in ("pending", "approved")
            for d in self.drafts
        )

    def has_pending_in_conversation(self, conversation_id: str) -> bool:
        return any(
            d.conversation_id == conversation_id and d.status in ("pending", "approved")
            for d in self.drafts
        )

    def expire(self, ttl_hours: float) -> list[Draft]:
        out = []
        for d in self.drafts:
            if d.is_expired(ttl_hours):
                d.status = "expired"
                d.decided_at = _utcnow().isoformat()
                out.append(d)
        return out

    def prune(self, keep_days: int = 30) -> None:
        cutoff = _utcnow() - timedelta(days=keep_days)
        self.drafts = [
            d for d in self.drafts
            if d.status == "pending" or _parse(d.created) >= cutoff
        ]

    # ---------------------------------------------------------- persistence
    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated": _utcnow().isoformat(),
            "counts": {
                s: sum(1 for d in self.drafts if d.status == s)
                for s in ("pending", "approved", "rejected", "published", "expired", "failed")
            },
            "drafts": [asdict(d) for d in self.drafts],
        }
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def summary(self) -> str:
        c: dict[str, int] = {}
        for d in self.drafts:
            c[d.status] = c.get(d.status, 0) + 1
        return " ".join(f"{k}={v}" for k, v in sorted(c.items())) or "empty"


def make_id(action: str, target_id: str, text: str) -> str:
    """
    Deterministic and short. Deterministic so a re-run of the same workflow
    can't enqueue a duplicate; short because Telegram callback_data is capped
    at 64 bytes.
    """
    import hashlib

    h = hashlib.sha256(f"{action}|{target_id}|{text}".encode()).hexdigest()
    return h[:12]


@dataclass
class Decision:
    """A verdict harvested from an approval channel."""
    draft_id: str
    verdict: Literal["approve", "reject", "edit"]
    via: str
    by: str
    new_text: str = ""
    note: str = ""
