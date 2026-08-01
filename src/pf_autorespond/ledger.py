"""
Budget + quota ledger.

This is the load-bearing component. Under pay-per-usage every API call is a
withdrawal from a $6 wallet, so the ledger is checked BEFORE each action, not
reconciled after. Three independent brakes, any one of which stops the run:

  1. monthly budget   - hard stop, protects the wallet
  2. daily budget     - hard stop, stops one bad day draining the month
  3. per-action daily quota - stops the account looking like a bot even when
                              there is money left

State is a single JSON file committed back to the repo by the workflow. That
gives durability (GitHub Actions cache can be evicted mid-month) plus a
human-readable audit trail in git history.

All money is Decimal. Never float -- $0.005 is not representable in binary
floating point and rounding drift across thousands of entries is real.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from . import pricing

_CENT = Decimal("0.000001")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _day_key(when: datetime | None = None) -> str:
    return (when or _utcnow()).strftime("%Y-%m-%d")


def _month_key(when: datetime | None = None) -> str:
    return (when or _utcnow()).strftime("%Y-%m")


class BudgetExceeded(RuntimeError):
    """Raised when an action would breach a budget or quota."""


@dataclass
class LedgerEntry:
    ts: str
    action: str
    units: int
    cost: str
    ref: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "action": self.action,
            "units": self.units,
            "cost": self.cost,
            "ref": self.ref,
            "note": self.note,
        }


@dataclass
class Ledger:
    path: Path
    monthly_budget: Decimal = Decimal("6.00")
    daily_budget: Decimal = Decimal("0.20")
    daily_quotas: dict[str, int] = field(default_factory=dict)
    entries: list[LedgerEntry] = field(default_factory=list)
    # tweet/user ids already billed today -- X dedups within the UTC day, so a
    # second read of the same id is free and we must not double-count it.
    billed_today: dict[str, list[str]] = field(default_factory=dict)
    # ids we have already acted on, ever. Prevents double-replying after a
    # partial run or a re-run of the same workflow.
    acted: dict[str, str] = field(default_factory=dict)
    # conversation id -> list of timestamps we replied at.
    # X's DedupConversationFilter collapses branches of one conversation, so a
    # second reply in a thread buys goodwill with that person, not reach. We
    # allow a small number, spaced out, because ignoring one of two commenters
    # reads as rude to the humans watching even when the ranker doesn't care.
    joined_conversations: dict[str, str] = field(default_factory=dict)
    conversation_log: dict[str, list[str]] = field(default_factory=dict)
    # date -> {kind: count}. Used to hold a slot open for cold outbound.
    kind_counts: dict[str, dict[str, int]] = field(default_factory=dict)
    # per-author cooldown: last time we replied to each author id
    author_last_touch: dict[str, str] = field(default_factory=dict)
    paused_until: str | None = None
    pause_reason: str = ""

    # ------------------------------------------------------------- persistence
    @classmethod
    def load(
        cls,
        path: str | os.PathLike[str],
        *,
        monthly_budget: Decimal | str = "6.00",
        daily_budget: Decimal | str = "0.20",
        daily_quotas: dict[str, int] | None = None,
    ) -> "Ledger":
        p = Path(path)
        led = cls(
            path=p,
            monthly_budget=Decimal(str(monthly_budget)),
            daily_budget=Decimal(str(daily_budget)),
            daily_quotas=dict(daily_quotas or {}),
        )
        if p.exists():
            raw = json.loads(p.read_text(encoding="utf-8") or "{}")
            led.entries = [LedgerEntry(**e) for e in raw.get("entries", [])]
            led.billed_today = raw.get("billed_today", {})
            led.acted = raw.get("acted", {})
            led.joined_conversations = raw.get("joined_conversations", {})
            led.conversation_log = raw.get("conversation_log", {})
            led.kind_counts = raw.get("kind_counts", {})
            led.author_last_touch = raw.get("author_last_touch", {})
            led.paused_until = raw.get("paused_until")
            led.pause_reason = raw.get("pause_reason", "")
        led.prune()
        return led

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated": _utcnow().isoformat(),
            "month": _month_key(),
            "month_spend": str(self.month_spend()),
            "day_spend": str(self.day_spend()),
            "entries": [e.to_dict() for e in self.entries],
            "billed_today": self.billed_today,
            "acted": self.acted,
            "joined_conversations": self.joined_conversations,
            "conversation_log": self.conversation_log,
            "kind_counts": self.kind_counts,
            "author_last_touch": self.author_last_touch,
            "paused_until": self.paused_until,
            "pause_reason": self.pause_reason,
        }
        # atomic write: a half-written ledger after a killed runner would
        # otherwise reset spend to zero and blow the budget on the next run.
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

    def prune(self, keep_days: int = 90) -> None:
        """Drop history older than keep_days so the file stays small in git."""
        cutoff = _utcnow() - timedelta(days=keep_days)
        self.entries = [e for e in self.entries if _parse(e.ts) >= cutoff]
        today = _day_key()
        self.billed_today = {k: v for k, v in self.billed_today.items() if k == today}
        self.acted = {k: v for k, v in self.acted.items() if _parse(v) >= cutoff}
        self.joined_conversations = {
            k: v for k, v in self.joined_conversations.items() if _parse(v) >= cutoff
        }
        self.author_last_touch = {
            k: v for k, v in self.author_last_touch.items() if _parse(v) >= cutoff
        }
        self.conversation_log = {
            k: [t for t in v if _parse(t) >= cutoff]
            for k, v in self.conversation_log.items()
        }
        self.conversation_log = {k: v for k, v in self.conversation_log.items() if v}
        self.kind_counts = {k: v for k, v in self.kind_counts.items() if k == today}

    # ----------------------------------------------------------------- totals
    def month_spend(self) -> Decimal:
        mk = _month_key()
        return sum(
            (Decimal(e.cost) for e in self.entries if e.ts.startswith(mk)),
            Decimal("0"),
        ).quantize(_CENT)

    def day_spend(self, when: datetime | None = None) -> Decimal:
        dk = _day_key(when)
        return sum(
            (Decimal(e.cost) for e in self.entries if e.ts.startswith(dk)),
            Decimal("0"),
        ).quantize(_CENT)

    def count_today(self, action: str) -> int:
        dk = _day_key()
        return sum(1 for e in self.entries if e.ts.startswith(dk) and e.action == action)

    def remaining_month(self) -> Decimal:
        return (self.monthly_budget - self.month_spend()).quantize(_CENT)

    def remaining_day(self) -> Decimal:
        return (self.daily_budget - self.day_spend()).quantize(_CENT)

    def remaining_quota(self, action: str) -> int:
        limit = self.daily_quotas.get(action)
        if limit is None:
            return 10**6
        return max(0, limit - self.count_today(action))

    # ------------------------------------------------------------------ pause
    def is_paused(self) -> bool:
        if not self.paused_until:
            return False
        return _utcnow() < _parse(self.paused_until)

    def pause(self, hours: float, reason: str) -> None:
        self.paused_until = (_utcnow() + timedelta(hours=hours)).isoformat()
        self.pause_reason = reason

    def resume(self) -> None:
        self.paused_until = None
        self.pause_reason = ""

    # ----------------------------------------------------------------- checks
    def can_afford(self, action: str, units: int = 1) -> tuple[bool, str]:
        if self.is_paused():
            return False, f"paused until {self.paused_until}: {self.pause_reason}"
        cost = pricing.cost_of(action, units)
        if cost > self.remaining_day():
            return False, (
                f"daily budget: need ${cost}, ${self.remaining_day()} left of "
                f"${self.daily_budget}"
            )
        if cost > self.remaining_month():
            return False, (
                f"monthly budget: need ${cost}, ${self.remaining_month()} left of "
                f"${self.monthly_budget}"
            )
        if self.remaining_quota(action) <= 0:
            return False, (
                f"daily quota for {action} exhausted "
                f"({self.daily_quotas.get(action)}/day)"
            )
        hard = pricing.DAILY_HARD_LIMITS.get(action)
        if hard is not None and self.count_today(action) >= hard:
            return False, f"X hard daily limit for {action} ({hard})"
        return True, ""

    def require(self, action: str, units: int = 1) -> None:
        ok, why = self.can_afford(action, units)
        if not ok:
            raise BudgetExceeded(why)

    # ------------------------------------------------------------- accounting
    def billable_units(self, kind: str, ids: Iterable[str]) -> int:
        """
        How many of `ids` we will actually be charged for today, given X's
        24h UTC dedup window. Also records them as billed.
        """
        today = _day_key()
        seen = set(self.billed_today.setdefault(today, []))
        fresh = [i for i in ids if f"{kind}:{i}" not in seen]
        self.billed_today[today].extend(f"{kind}:{i}" for i in fresh)
        return len(fresh)

    def record(
        self, action: str, units: int = 1, ref: str = "", note: str = ""
    ) -> Decimal:
        cost = pricing.cost_of(action, units)
        self.entries.append(
            LedgerEntry(
                ts=_utcnow().isoformat(),
                action=action,
                units=units,
                cost=str(cost),
                ref=ref,
                note=note,
            )
        )
        return cost

    def mark_acted(self, target_id: str) -> None:
        self.acted[target_id] = _utcnow().isoformat()

    def has_acted(self, target_id: str) -> bool:
        return target_id in self.acted

    def mark_conversation(self, conversation_id: str) -> None:
        now = _utcnow().isoformat()
        self.joined_conversations[conversation_id] = now
        self.conversation_log.setdefault(conversation_id, []).append(now)

    def in_conversation(self, conversation_id: str) -> bool:
        return conversation_id in self.joined_conversations

    def conversation_replies(self, conversation_id: str) -> int:
        return len(self.conversation_log.get(conversation_id, []))

    def conversation_open(
        self, conversation_id: str, max_replies: int = 1, gap_hours: float = 3.0
    ) -> tuple[bool, str]:
        """
        May we reply in this thread again?

        Two brakes: a hard cap, and a minimum gap. The gap is the one that
        matters for looking human -- answering two comments on the same thread
        back to back inside one run is a machine-shaped pattern, and it's the
        pattern people notice.
        """
        log = self.conversation_log.get(conversation_id, [])
        if len(log) >= max_replies:
            return False, f"already replied {len(log)}x in this thread (cap {max_replies})"
        if log:
            since = _utcnow() - _parse(log[-1])
            if since < timedelta(hours=gap_hours):
                mins = gap_hours * 60 - since.total_seconds() / 60
                return False, f"replied here {since.total_seconds()/60:.0f}m ago; {mins:.0f}m to go"
        return True, ""

    def record_kind(self, kind: str) -> None:
        self.kind_counts.setdefault(_day_key(), {})
        self.kind_counts[_day_key()][kind] = self.count_kind_today(kind) + 1

    def count_kind_today(self, kind: str) -> int:
        return self.kind_counts.get(_day_key(), {}).get(kind, 0)

    def touch_author(self, author_id: str) -> None:
        self.author_last_touch[author_id] = _utcnow().isoformat()

    def author_cooling_down(self, author_id: str, hours: float) -> bool:
        last = self.author_last_touch.get(author_id)
        if not last:
            return False
        return _utcnow() - _parse(last) < timedelta(hours=hours)

    # ------------------------------------------------------------------ debug
    def summary(self) -> str:
        return (
            f"month ${self.month_spend()}/${self.monthly_budget} "
            f"(${self.remaining_month()} left) | "
            f"today ${self.day_spend()}/${self.daily_budget} "
            f"(${self.remaining_day()} left)"
        )


def _parse(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
