"""
Thin X API v2 client with cost accounting baked in.

Deliberately not tweepy. Under pay-per-usage the thing that matters is knowing
exactly how many resources each call will bill for BEFORE issuing it, and
tweepy's convenience layer hides pagination and field expansion -- both of
which are billed per resource. Every read here takes an explicit cap.

Auth is OAuth 1.0a user context (consumer key/secret + access token/secret),
which is what a single-account bot wants: no refresh-token rotation to babysit
inside a GitHub Actions runner.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import requests
from requests_oauthlib import OAuth1

from . import pricing
from .ledger import BudgetExceeded, Ledger

log = logging.getLogger(__name__)

API = "https://api.x.com/2"
UA = "peptide-frontier-autorespond/1.0"


class XAPIError(RuntimeError):
    def __init__(self, status: int, body: Any):
        super().__init__(f"X API {status}: {body}")
        self.status = status
        self.body = body


@dataclass
class Post:
    id: str
    text: str
    author_id: str
    conversation_id: str
    created_at: str
    in_reply_to_user_id: str | None = None
    referenced: list[dict[str, str]] = field(default_factory=list)
    lang: str = "en"
    public_metrics: dict[str, int] = field(default_factory=dict)
    author_handle: str = ""
    author_followers: int = 0
    author_protected: bool = False
    author_created_at: str = ""

    @property
    def is_reply(self) -> bool:
        return any(r.get("type") == "replied_to" for r in self.referenced)

    @property
    def is_retweet(self) -> bool:
        return any(r.get("type") == "retweeted" for r in self.referenced)


class XClient:
    """
    Every method that costs money takes the ledger, checks affordability first,
    and records the actual billable units after. `dry_run` short-circuits all
    writes and bills nothing -- use it for everything until you trust the thing.
    """

    def __init__(
        self,
        *,
        consumer_key: str,
        consumer_secret: str,
        access_token: str,
        access_token_secret: str,
        ledger: Ledger,
        dry_run: bool = True,
        timeout: int = 30,
    ):
        self.auth = OAuth1(
            consumer_key, consumer_secret, access_token, access_token_secret
        )
        self.ledger = ledger
        self.dry_run = dry_run
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["User-Agent"] = UA
        self._me: dict[str, Any] | None = None
        self.calls: list[tuple[str, str]] = []

    # ------------------------------------------------------------- transport
    def _request(
        self, method: str, path: str, *, params: dict | None = None, json: dict | None = None
    ) -> dict:
        url = f"{API}{path}"
        for attempt in range(4):
            resp = self.session.request(
                method, url, params=params, json=json, auth=self.auth, timeout=self.timeout
            )
            self.calls.append((method, path))

            if resp.status_code == 429:
                reset = int(resp.headers.get("x-rate-limit-reset", 0))
                wait = max(min(reset - time.time(), 900), 15)
                log.warning("429 on %s %s; sleeping %.0fs", method, path, wait)
                time.sleep(wait)
                continue

            # 402/403 with a credit message means the wallet is empty. Do not
            # retry -- retrying an out-of-credits call just wastes runner time.
            if resp.status_code in (401, 402, 403):
                raise XAPIError(resp.status_code, _safe_json(resp))

            if resp.status_code >= 500:
                time.sleep(2 ** attempt)
                continue

            if resp.status_code >= 400:
                raise XAPIError(resp.status_code, _safe_json(resp))

            return _safe_json(resp) or {}
        raise XAPIError(resp.status_code, _safe_json(resp))

    # ------------------------------------------------------------------- me
    def me(self) -> dict[str, Any]:
        """GET /2/users/me. Owned read, one resource: $0.001."""
        if self._me is None:
            units = self.ledger.billable_units("user", ["me"])
            data = self._request("GET", "/users/me", params={"user.fields": "public_metrics"})
            if units:
                self.ledger.record("read_mentions", units, ref="me", note="users/me")
            self._me = data.get("data", {})
        return self._me

    # ---------------------------------------------------------------- reads
    def mentions(self, user_id: str, *, max_results: int = 10, since_id: str | None = None) -> list[Post]:
        """
        GET /2/users/{id}/mentions -- OWNED read at $0.001/resource, five times
        cheaper than an ordinary post read. This is the cheapest signal we buy.
        """
        params = {
            "max_results": max(5, min(max_results, 100)),
            "tweet.fields": "created_at,conversation_id,in_reply_to_user_id,referenced_tweets,lang,public_metrics",
            "expansions": "author_id",
            "user.fields": "username,public_metrics,protected,created_at",
        }
        if since_id:
            params["since_id"] = since_id
        data = self._request("GET", f"/users/{user_id}/mentions", params=params)
        posts = _parse_posts(data)
        units = self.ledger.billable_units("post", [p.id for p in posts])
        if units:
            self.ledger.record("read_mentions", units, ref=user_id, note=f"{len(posts)} mentions")
        return posts

    def own_posts(self, user_id: str, *, max_results: int = 10) -> list[Post]:
        """
        GET /2/users/{id}/tweets -- OWNED read at $0.001/resource.

        Used to reconstruct what we originally said, so a reply to a commenter
        answers the actual point instead of guessing from their reply alone.
        Cheap enough ($0.01 for 10 posts) that the quality gain is free.
        """
        params = {
            "max_results": max(5, min(max_results, 100)),
            "tweet.fields": "created_at,conversation_id,referenced_tweets,lang,public_metrics",
            # Replies belong to a conversation. Mirrored standalone they read
            # as fragments ("@someone Not good"), so keep them out entirely.
            "exclude": "retweets,replies",
        }
        data = self._request("GET", f"/users/{user_id}/tweets", params=params)
        posts = _parse_posts(data)
        units = self.ledger.billable_units("post", [p.id for p in posts])
        if units:
            self.ledger.record("read_own_timeline", units, ref=user_id, note=f"{len(posts)} own")
        return posts

    def list_posts(self, list_id: str, *, max_results: int = 10) -> list[Post]:
        """
        GET /2/lists/{id}/tweets -- NOT an owned read. $0.005 per post returned.
        This is the most expensive routine call we make, so max_results is the
        single most important knob in the config.
        """
        params = {
            "max_results": max(1, min(max_results, 100)),
            "tweet.fields": "created_at,conversation_id,referenced_tweets,lang,public_metrics",
            "expansions": "author_id",
            "user.fields": "username,public_metrics,protected,created_at",
        }
        est = pricing.cost_of("read_list_posts", params["max_results"])
        ok, why = self.ledger.can_afford("read_list_posts", params["max_results"])
        if not ok:
            raise BudgetExceeded(f"list read (~${est}) blocked: {why}")
        data = self._request("GET", f"/lists/{list_id}/tweets", params=params)
        posts = _parse_posts(data)
        units = self.ledger.billable_units("post", [p.id for p in posts])
        # Always record, even at units=0: the request consumed a quota slot.
        self.ledger.record("read_list_posts", units, ref=list_id, note=f"{len(posts)} posts")
        return posts

    def search_recent(self, query: str, *, max_results: int = 10) -> list[Post]:
        """GET /2/tweets/search/recent -- $0.005 per post returned."""
        params = {
            "query": query,
            "max_results": max(10, min(max_results, 100)),
            "tweet.fields": "created_at,conversation_id,referenced_tweets,lang,public_metrics",
            "expansions": "author_id",
            "user.fields": "username,public_metrics,protected,created_at",
        }
        ok, why = self.ledger.can_afford("read_search", params["max_results"])
        if not ok:
            raise BudgetExceeded(f"search blocked: {why}")
        data = self._request("GET", "/tweets/search/recent", params=params)
        posts = _parse_posts(data)
        units = self.ledger.billable_units("post", [p.id for p in posts])
        self.ledger.record("read_search", units, ref=query[:40], note=f"{len(posts)} posts")
        return posts

    def get_post(self, post_id: str) -> Post | None:
        params = {
            "ids": post_id,
            "tweet.fields": "created_at,conversation_id,referenced_tweets,lang,public_metrics",
            "expansions": "author_id",
            "user.fields": "username,public_metrics,protected,created_at",
        }
        data = self._request("GET", "/tweets", params=params)
        posts = _parse_posts(data)
        if not posts:
            return None
        units = self.ledger.billable_units("post", [posts[0].id])
        if units:
            self.ledger.record("read_conversation", units, ref=post_id)
        return posts[0]

    def usage(self) -> dict[str, Any]:
        """GET /2/usage/tweets -- reconcile our ledger against X's own count."""
        return self._request("GET", "/usage/tweets")

    # --------------------------------------------------------------- writes
    def reply(self, text: str, in_reply_to: str) -> dict[str, Any]:
        return self._create_post(text, action="reply", reply_to=in_reply_to)

    def quote(self, text: str, quote_post_id: str) -> dict[str, Any]:
        return self._create_post(text, action="quote", quote_id=quote_post_id)

    def post(self, text: str) -> dict[str, Any]:
        return self._create_post(text, action="post")

    def _create_post(
        self,
        text: str,
        *,
        action: str,
        reply_to: str | None = None,
        quote_id: str | None = None,
    ) -> dict[str, Any]:
        from .safety import URL_RE

        # Belt and braces: safety.gate_reply already rejected URLs, but a
        # 13x billing surprise is worth checking twice at the boundary.
        if URL_RE.search(text):
            raise ValueError(
                f"refusing to publish: text contains a URL "
                f"(${pricing.POST_CREATE_WITH_URL} vs ${pricing.POST_CREATE})"
            )

        self.ledger.require(action)
        payload: dict[str, Any] = {"text": text}
        if reply_to:
            payload["reply"] = {"in_reply_to_tweet_id": reply_to}
        if quote_id:
            payload["quote_tweet_id"] = quote_id

        if self.dry_run:
            log.info("[dry-run] %s -> %s: %s", action, reply_to or quote_id or "-", text)
            return {"data": {"id": f"dryrun-{action}", "text": text}, "dry_run": True}

        data = self._request("POST", "/tweets", json=payload)
        self.ledger.record(
            action, 1, ref=data.get("data", {}).get("id", ""), note=text[:60]
        )
        return data

    def like(self, user_id: str, post_id: str) -> dict[str, Any]:
        """
        $0.015 -- the same price as a reply, for the lowest-weighted signal in
        the ranker. Default quota is 0 for a reason.
        """
        self.ledger.require("like")
        if self.dry_run:
            log.info("[dry-run] like %s", post_id)
            return {"data": {"liked": True}, "dry_run": True}
        data = self._request("POST", f"/users/{user_id}/likes", json={"tweet_id": post_id})
        self.ledger.record("like", 1, ref=post_id)
        return data


# ------------------------------------------------------------------ parsing
def _safe_json(resp: requests.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return resp.text[:500]


def _parse_posts(payload: dict) -> list[Post]:
    users = {
        u["id"]: u for u in payload.get("includes", {}).get("users", [])
    }
    out: list[Post] = []
    for t in payload.get("data", []) or []:
        author = users.get(t.get("author_id", ""), {})
        out.append(
            Post(
                id=t["id"],
                text=t.get("text", ""),
                author_id=t.get("author_id", ""),
                conversation_id=t.get("conversation_id", t["id"]),
                created_at=t.get("created_at", ""),
                in_reply_to_user_id=t.get("in_reply_to_user_id"),
                referenced=t.get("referenced_tweets", []) or [],
                lang=t.get("lang", "en"),
                public_metrics=t.get("public_metrics", {}) or {},
                author_handle=author.get("username", ""),
                author_followers=(author.get("public_metrics", {}) or {}).get(
                    "followers_count", 0
                ),
                author_protected=author.get("protected", False),
                author_created_at=author.get("created_at", ""),
            )
        )
    return out
