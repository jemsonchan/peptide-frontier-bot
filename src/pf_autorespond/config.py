"""Config loading. YAML file for behaviour, env vars for secrets."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


@dataclass
class Budget:
    monthly_usd: Decimal = Decimal("6.00")
    daily_usd: Decimal = Decimal("0.19")
    # Stop before zero so a top-up is a choice, not an outage mid-thread.
    reserve_usd: Decimal = Decimal("0.30")


@dataclass
class Quotas:
    reply: int = 5
    quote: int = 1
    post: int = 1
    like: int = 0  # $0.015 for the lowest-weighted ranker signal. Off by default.
    read_list_posts: int = 2
    read_search: int = 1


@dataclass
class Harvest:
    mentions_max: int = 6
    own_posts_max: int = 4
    list_id: str = ""
    list_posts_max: int = 10
    search_queries: list[str] = field(default_factory=list)
    search_max: int = 10


@dataclass
class Selection:
    min_score_mention: float = 0.8
    min_score_outsider: float = 2.2
    min_score_quote: float = 2.6
    min_followers_outsider: int = 500
    author_cooldown_hours: float = 72.0
    max_age_hours: float = 24.0
    freshness_half_life: float = 4.0        # cold outbound: feed decay is real
    mention_half_life: float = 36.0         # your own threads: notification-driven
    mention_max_age_hours: float = 96.0
    # Hold one reply slot a day for a stranger's post, even when your own
    # backlog outranks it. Without this, mentions win every slot and the
    # growth lever goes quiet.
    reserved_outbound_per_day: int = 1
    # Answer more than one comment in a thread, but never back to back.
    max_replies_per_conversation: int = 2
    conversation_gap_hours: float = 3.0


@dataclass
class Nostr:
    """
    Nostr has no per-action cost and no ranking algorithm. Everything that
    forces restraint on X is absent here, so the same approved draft can go
    out unconditionally -- and links are fine.
    """
    enabled: bool = True
    relays: list[str] = field(default_factory=list)
    # Mirror even when the X publish was blocked by budget. Nostr is free;
    # a good reply shouldn't die because the wallet is empty.
    mirror_when_x_blocked: bool = True
    # A reply to a stranger reads as a non-sequitur on Nostr, where the parent
    # doesn't exist. Off by default.
    include_outsider_replies: bool = False
    thread_replies: bool = True
    map_path: str = "state/nostr_map.json"


@dataclass
class Config:
    budget: Budget = field(default_factory=Budget)
    quotas: Quotas = field(default_factory=Quotas)
    harvest: Harvest = field(default_factory=Harvest)
    selection: Selection = field(default_factory=Selection)
    nostr: Nostr = field(default_factory=Nostr)
    dry_run: bool = True
    # "review": draft, then wait for your approval before spending $0.015.
    # "auto":   publish immediately.
    mode: str = "review"
    approval_ttl_hours: float = 12.0   # after this a draft is stale, not cheap
    max_publish_per_run: int = 3
    queue_path: str = "state/queue.json"
    max_actions_per_run: int = 2
    use_critic: bool = True
    max_regenerations: int = 1
    # Random pause between two published actions in one run. Posting two
    # replies in the same second is the clearest automation fingerprint there
    # is. Set both to 0 in tests.
    gap_seconds_min: float = 20.0
    gap_seconds_max: float = 70.0
    state_path: str = "state/ledger.json"
    log_path: str = "state/actions.log"

    @classmethod
    def load(cls, path: str | os.PathLike[str] | None = None) -> "Config":
        cfg = cls()
        p = Path(path or os.getenv("PF_CONFIG", "config.yaml"))
        if p.exists() and yaml is not None:
            raw: dict[str, Any] = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            b = raw.get("budget", {})
            cfg.budget = Budget(
                monthly_usd=Decimal(str(b.get("monthly_usd", cfg.budget.monthly_usd))),
                daily_usd=Decimal(str(b.get("daily_usd", cfg.budget.daily_usd))),
                reserve_usd=Decimal(str(b.get("reserve_usd", cfg.budget.reserve_usd))),
            )
            cfg.quotas = Quotas(**{**cfg.quotas.__dict__, **raw.get("quotas", {})})
            cfg.harvest = Harvest(**{**cfg.harvest.__dict__, **raw.get("harvest", {})})
            cfg.selection = Selection(**{**cfg.selection.__dict__, **raw.get("selection", {})})
            cfg.nostr = Nostr(**{**cfg.nostr.__dict__, **raw.get("nostr", {})})
            for k in (
                "dry_run", "max_actions_per_run", "use_critic",
                "max_regenerations", "state_path", "log_path",
                "gap_seconds_min", "gap_seconds_max",
                "mode", "approval_ttl_hours", "max_publish_per_run", "queue_path",
            ):
                if k in raw:
                    setattr(cfg, k, raw[k])

        # Env always wins -- the workflow flips dry_run without editing the file.
        if os.getenv("PF_DRY_RUN") is not None:
            cfg.dry_run = os.getenv("PF_DRY_RUN", "1").lower() in ("1", "true", "yes")
        if os.getenv("PF_MONTHLY_BUDGET"):
            cfg.budget.monthly_usd = Decimal(os.environ["PF_MONTHLY_BUDGET"])
        if os.getenv("PF_DAILY_BUDGET"):
            cfg.budget.daily_usd = Decimal(os.environ["PF_DAILY_BUDGET"])
        if os.getenv("PF_MODE"):
            cfg.mode = os.environ["PF_MODE"]
        if os.getenv("PF_MAX_ACTIONS"):
            cfg.max_actions_per_run = int(os.environ["PF_MAX_ACTIONS"])
        return cfg

    def quota_map(self) -> dict[str, int]:
        return dict(self.quotas.__dict__)


def x_credentials() -> dict[str, str]:
    need = (
        "X_CONSUMER_KEY",
        "X_CONSUMER_SECRET",
        "X_ACCESS_TOKEN",
        "X_ACCESS_TOKEN_SECRET",
    )
    missing = [k for k in need if not os.getenv(k)]
    if missing:
        raise RuntimeError(f"missing X credentials: {', '.join(missing)}")
    return {
        "consumer_key": os.environ["X_CONSUMER_KEY"],
        "consumer_secret": os.environ["X_CONSUMER_SECRET"],
        "access_token": os.environ["X_ACCESS_TOKEN"],
        "access_token_secret": os.environ["X_ACCESS_TOKEN_SECRET"],
    }
