"""
X API pay-per-usage cost table.

Source: https://docs.x.com/x-api/getting-started/pricing  (verified 2026-08-01)

Two things in here will decide whether $6 lasts a month or a morning:

  1. POST_CREATE_WITH_URL is $0.200 -- 13.3x the cost of a plain post. The
     open-sourced ranker also deprioritises off-platform links (P(click) carries
     a low weight). So a URL in a post costs you 13x AND gets buried. We hard-ban
     URLs in generated text; see safety.py.

  2. Owned reads ($0.001/resource) are 5x cheaper than ordinary post reads
     ($0.005/resource). Your own mentions qualify. Replying to your own
     commenters is therefore the cheapest growth action available to you.

Reads are billed PER RESOURCE RETURNED, not per request. Asking for
max_results=100 and getting 100 posts bills 100 units. Always pass a tight
max_results.

Resources are deduplicated within a 24h UTC window: re-reading the same post id
the same UTC day is free. The ledger models this so estimates don't overstate.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

# ---------------------------------------------------------------- read costs
# Charged per resource returned in the response body.
POST_READ: Final = Decimal("0.005")
USER_READ: Final = Decimal("0.010")
LIST_READ: Final = Decimal("0.005")
LIKE_READ: Final = Decimal("0.001")
FOLLOW_READ: Final = Decimal("0.010")

# Owned reads: your own data, via your own app, authed as the owning user.
# GET /2/users/{id}/mentions, /tweets, /liked_tweets, /followers, ...
OWNED_READ: Final = Decimal("0.001")

# --------------------------------------------------------------- write costs
# Charged per request.
POST_CREATE: Final = Decimal("0.015")
POST_CREATE_WITH_URL: Final = Decimal("0.200")  # <-- the budget killer
POST_CREATE_SUMMONED: Final = Decimal("0.010")
USER_INTERACTION_CREATE: Final = Decimal("0.015")  # like, repost, follow
INTERACTION_DELETE: Final = Decimal("0.010")  # unlike, unfollow
CONTENT_MANAGE: Final = Decimal("0.005")
BOOKMARK: Final = Decimal("0.005")
TRENDS: Final = Decimal("0.010")

# ------------------------------------------------------------ action mapping
# Every action the engine can take, and what it costs. `unit` is "request" for
# writes (flat) and "resource" for reads (multiply by items returned).
ACTION_COSTS: Final[dict[str, tuple[Decimal, str]]] = {
    # reads
    "read_mentions": (OWNED_READ, "resource"),
    "read_own_timeline": (OWNED_READ, "resource"),
    "read_list_posts": (POST_READ, "resource"),
    "read_search": (POST_READ, "resource"),
    "read_conversation": (POST_READ, "resource"),
    "read_user": (USER_READ, "resource"),
    # writes
    "reply": (POST_CREATE, "request"),
    "reply_with_url": (POST_CREATE_WITH_URL, "request"),
    "quote": (POST_CREATE, "request"),
    "quote_with_url": (POST_CREATE_WITH_URL, "request"),
    "post": (POST_CREATE, "request"),
    "post_with_url": (POST_CREATE_WITH_URL, "request"),
    "like": (USER_INTERACTION_CREATE, "request"),
    "repost": (USER_INTERACTION_CREATE, "request"),
    "follow": (USER_INTERACTION_CREATE, "request"),
    "delete_post": (CONTENT_MANAGE, "request"),
}

# --------------------------------------------------------------- rate limits
# Per-user, OAuth 1.0a user context. Source: docs.x.com/x-api/fundamentals/rate-limits
# At a $6/mo budget these are never the binding constraint -- dollars are --
# but the client honours them so a misconfiguration can't spin.
RATE_LIMITS: Final[dict[str, tuple[int, int]]] = {
    # endpoint key -> (max_requests, window_seconds)
    "POST /2/tweets": (100, 900),
    "GET /2/users/:id/mentions": (300, 900),
    "GET /2/users/:id/tweets": (900, 900),
    "GET /2/lists/:id/tweets": (900, 900),
    "GET /2/tweets/search/recent": (300, 900),
    "POST /2/users/:id/likes": (50, 900),
    "POST /2/users/:id/retweets": (50, 900),
    "GET /2/users/me": (75, 900),
    "GET /2/usage/tweets": (50, 900),
}

# Hard daily ceilings imposed by X regardless of budget.
DAILY_HARD_LIMITS: Final[dict[str, int]] = {
    "like": 1000,
    "post": 10000,
}


def cost_of(action: str, units: int = 1) -> Decimal:
    """Cost of `action`. For reads, `units` = resources returned."""
    try:
        unit_cost, unit_kind = ACTION_COSTS[action]
    except KeyError as exc:  # pragma: no cover - programmer error
        raise KeyError(f"unknown action {action!r}; add it to ACTION_COSTS") from exc
    if unit_kind == "request":
        return unit_cost
    return unit_cost * units


def url_variant(action: str) -> str:
    """Map a write action to its far more expensive URL-bearing variant."""
    variant = f"{action}_with_url"
    return variant if variant in ACTION_COSTS else action
