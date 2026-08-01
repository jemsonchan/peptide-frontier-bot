"""Transport-layer tests. No network: requests.Session is stubbed."""

from decimal import Decimal

import pytest

from pf_autorespond.ledger import BudgetExceeded
from pf_autorespond.xclient import XAPIError, XClient, _parse_posts

CREDS = dict(
    consumer_key="ck", consumer_secret="cs",
    access_token="at", access_token_secret="ats",
)


class FakeResponse:
    def __init__(self, status, payload, headers=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self.text = str(payload)

    def json(self):
        if isinstance(self._payload, Exception):
            raise ValueError("not json")
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []
        self.headers = {}

    def request(self, method, url, **kw):
        self.requests.append((method, url, kw))
        return self._responses.pop(0)


def client(ledger, responses, dry_run=False):
    c = XClient(**CREDS, ledger=ledger, dry_run=dry_run)
    c.session = FakeSession(responses)
    return c


def test_url_in_text_is_refused_before_any_spend(ledger):
    c = client(ledger, [])
    with pytest.raises(ValueError, match="URL"):
        c.reply("The data is at https://example.com/x", "123")
    assert ledger.month_spend() == Decimal("0")
    assert c.session.requests == []


def test_dry_run_never_touches_transport(ledger):
    c = client(ledger, [], dry_run=True)
    out = c.reply("A clean reply with a real number: 24.2% at 48 weeks, n=338.", "123")
    assert out["dry_run"] is True
    assert c.session.requests == []
    assert ledger.month_spend() == Decimal("0")


def test_quota_blocks_before_http(ledger):
    for _ in range(3):
        ledger.record("reply")           # quota is 3
    c = client(ledger, [])
    with pytest.raises(BudgetExceeded):
        c.reply("Perfectly fine text about a Phase 2 readout and its limits.", "123")
    assert c.session.requests == []


def test_like_with_zero_quota_blocked(ledger):
    c = client(ledger, [])
    with pytest.raises(BudgetExceeded):
        c.like("me", "123")
    assert c.session.requests == []


def test_successful_reply_bills_once(ledger):
    c = client(ledger, [FakeResponse(201, {"data": {"id": "999", "text": "ok"}})])
    c.reply("No human PK data exists, so rodent mg/kg figures don't convert.", "123")
    assert ledger.month_spend() == Decimal("0.015")
    assert ledger.count_today("reply") == 1
    method, url, kw = c.session.requests[0]
    assert method == "POST" and url.endswith("/tweets")
    assert kw["json"]["reply"] == {"in_reply_to_tweet_id": "123"}


def test_429_then_success(ledger, monkeypatch):
    slept = []
    monkeypatch.setattr("pf_autorespond.xclient.time.sleep", slept.append)
    monkeypatch.setattr("pf_autorespond.xclient.time.time", lambda: 1000.0)
    c = client(ledger, [
        FakeResponse(429, {"errors": []}, {"x-rate-limit-reset": "1060"}),
        FakeResponse(201, {"data": {"id": "1", "text": "ok"}}),
    ])
    c.reply("A clean substantive reply about trial duration differences here.", "5")
    assert slept == [60.0]
    assert ledger.month_spend() == Decimal("0.015")


def test_out_of_credits_is_not_retried(ledger):
    # 402 means the wallet is empty. Retrying just burns runner minutes.
    c = client(ledger, [FakeResponse(402, {"detail": "insufficient credits"})])
    with pytest.raises(XAPIError) as ei:
        c.reply("Text that would otherwise be fine and worth publishing today.", "5")
    assert ei.value.status == 402
    assert len(c.session.requests) == 1


def test_mentions_bill_per_resource_and_dedup(ledger):
    payload = {
        "data": [
            {"id": "1", "text": "a", "author_id": "u1", "conversation_id": "c1"},
            {"id": "2", "text": "b", "author_id": "u1", "conversation_id": "c1"},
        ],
        "includes": {"users": [{"id": "u1", "username": "bob",
                                "public_metrics": {"followers_count": 42}}]},
    }
    c = client(ledger, [FakeResponse(200, payload), FakeResponse(200, payload)])
    c.mentions("me", max_results=5)
    assert ledger.month_spend() == Decimal("0.002")   # 2 x $0.001
    c.mentions("me", max_results=5)
    assert ledger.month_spend() == Decimal("0.002")   # same ids, same day: free


def test_list_read_blocked_when_it_would_breach_budget(ledger):
    for _ in range(3):
        ledger.record("reply")            # $0.045
    ledger.record("read_list_posts", 26)  # $0.130 -> $0.175 of $0.18
    c = client(ledger, [])
    with pytest.raises(BudgetExceeded):
        c.list_posts("L1", max_results=10)   # would need $0.050
    assert c.session.requests == []


def test_parse_posts_joins_author_fields():
    posts = _parse_posts({
        "data": [{"id": "1", "text": "hi", "author_id": "u1", "conversation_id": "c1",
                  "referenced_tweets": [{"type": "replied_to", "id": "0"}]}],
        "includes": {"users": [{"id": "u1", "username": "alice", "protected": True,
                                "public_metrics": {"followers_count": 1234}}]},
    })
    p = posts[0]
    assert p.author_handle == "alice"
    assert p.author_followers == 1234
    assert p.author_protected is True
    assert p.is_reply and not p.is_retweet
