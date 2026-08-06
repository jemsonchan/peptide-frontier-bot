"""End-to-end with a fake X API and a scripted LLM. No network, no spend."""

from decimal import Decimal

import pytest

from pf_autorespond import llm
from pf_autorespond.config import Budget, Config, Harvest, Quotas, Selection
from pf_autorespond.engine import Engine
from pf_autorespond.xclient import XClient
from tests.conftest import iso, make_post


class FakeX(XClient):
    """Subclasses the real client so ledger accounting stays under test."""

    def __init__(self, ledger, *, mentions=(), list_posts=(), own=(), dry_run=False):
        self._own = list(own)
        self.ledger = ledger
        self.dry_run = dry_run
        self._mentions = list(mentions)
        self._list = list(list_posts)
        self._me = {"id": "me", "username": "PeptideFrontier"}
        self.published = []
        self.calls = []

    def me(self):
        return self._me

    def own_posts(self, user_id, *, max_results=10):
        posts = list(getattr(self, "_own", []))
        units = self.ledger.billable_units("post", [p.id for p in posts])
        if units:
            self.ledger.record("read_own_timeline", units, ref=user_id)
        return posts

    def mentions(self, user_id, *, max_results=10, since_id=None):
        posts = self._mentions[:max_results]
        units = self.ledger.billable_units("post", [p.id for p in posts])
        if units:
            self.ledger.record("read_mentions", units, ref=user_id)
        return posts

    def list_posts(self, list_id, *, max_results=10):
        self.ledger.require("read_list_posts")
        posts = self._list[:max_results]
        units = self.ledger.billable_units("post", [p.id for p in posts])
        self.ledger.record("read_list_posts", units, ref=list_id)
        return posts

    def search_recent(self, query, *, max_results=10):
        return []

    def _request(self, method, path, *, params=None, json=None):
        # Only the write path should ever reach transport in these tests.
        assert (method, path) == ("POST", "/tweets"), f"unexpected call {method} {path}"
        self.calls.append((method, path))
        self.published.append(json)
        return {"data": {"id": f"new{len(self.published)}", "text": json["text"]}}


def cfg_for(tmp_path, **over):
    c = Config(
        budget=Budget(Decimal("6.00"), Decimal("0.18"), Decimal("0.30")),
        quotas=Quotas(reply=3, quote=1, post=1, like=0, read_list_posts=2, read_search=1),
        harvest=Harvest(mentions_max=10, list_id="L1", list_posts_max=10),
        selection=Selection(min_score_outsider=1.0, min_followers_outsider=500),
        dry_run=False,
        max_actions_per_run=2,
        use_critic=False,
        gap_seconds_min=0.0,
        gap_seconds_max=0.0,
        state_path=str(tmp_path / "ledger.json"),
        log_path=str(tmp_path / "actions.log"),
    )
    for k, v in over.items():
        setattr(c, k, v)
    return c


@pytest.fixture
def scripted(monkeypatch):
    calls = {"n": 0, "drafts": []}

    def fake_generate(system, user, cfg=None):
        calls["n"] += 1
        return calls["drafts"].pop(0) if calls["drafts"] else (
            "No human PK data exists for this compound, so rodent doses don't "
            "translate to a human number. That gap sits under most of the debate."
        )

    monkeypatch.setattr(llm, "generate", fake_generate)
    monkeypatch.setattr("pf_autorespond.engine.llm.generate", fake_generate)
    return calls


def test_happy_path_publishes_and_bills(tmp_path, ledger, scripted):
    cfg = cfg_for(tmp_path)
    client = FakeX(
        ledger,
        mentions=[make_post(id="m1", author_id="u1", conversation_id="cm1", created_at=iso(2))],
        list_posts=[make_post(id="p1", author_id="u2", conversation_id="cp1", created_at=iso(1))],
    )
    report = Engine(cfg, client, ledger).run()

    assert len(report.published) == 2
    assert {p["action"] for p in report.published} == {"reply"}
    # 2 replies ($0.030) + 1 mention read ($0.001) + 1 list read of 1 post ($0.005)
    assert ledger.month_spend() == Decimal("0.036")
    assert ledger.count_today("reply") == 2


def test_dry_run_generates_but_never_bills_a_write(tmp_path, ledger, scripted):
    cfg = cfg_for(tmp_path, dry_run=True)
    client = FakeX(ledger, mentions=[make_post(id="m1", created_at=iso(1))], dry_run=True)
    report = Engine(cfg, client, ledger).run()

    assert report.published and all(p["dry_run"] for p in report.published)
    assert ledger.count_today("reply") == 0        # no write recorded
    # Reads still cost in dry-run -- you need real mentions to judge real
    # drafts. Only the $0.015 publish is suppressed.
    assert ledger.month_spend() == Decimal("0.001")  # one mention, empty list read


def test_url_in_draft_is_never_published(tmp_path, ledger, scripted):
    scripted["drafts"] = [
        "The full readout is at https://pubmed.ncbi.nlm.nih.gov/999 and worth reading closely.",
        "Second try also links to example.com/paper which is the same mistake.",
    ]
    cfg = cfg_for(tmp_path, max_actions_per_run=1, max_regenerations=1)
    client = FakeX(ledger, mentions=[make_post(id="m1", created_at=iso(1))])
    report = Engine(cfg, client, ledger).run()

    assert report.published == []
    assert any("url" in s.get("reason", "").lower() or "URL" in s.get("reason", "")
               for s in report.skipped)
    assert ledger.count_today("reply") == 0


def test_skip_token_costs_nothing(tmp_path, ledger, scripted):
    scripted["drafts"] = ["SKIP", "SKIP"]
    cfg = cfg_for(tmp_path, max_actions_per_run=2)
    client = FakeX(ledger, mentions=[make_post(id="m1", created_at=iso(1))])
    report = Engine(cfg, client, ledger).run()
    assert report.published == []
    assert ledger.count_today("reply") == 0


def test_reserve_stops_the_run_before_spending(tmp_path, scripted):
    from pf_autorespond.ledger import Ledger

    led = Ledger.load(tmp_path / "l.json", monthly_budget="6.00", daily_budget="5.00",
                      daily_quotas={"reply": 99})
    for _ in range(384):           # 384 x $0.015 = $5.76, leaves $0.24 < $0.30 reserve
        led.record("reply")
    cfg = cfg_for(tmp_path)
    client = FakeX(led, mentions=[make_post(id="m1", created_at=iso(1))])
    report = Engine(cfg, client, led).run()

    assert report.published == []
    assert any("reserve" in e for e in report.errors)
    assert client.calls == []      # did not even authenticate


def test_pause_is_a_hard_kill_switch(tmp_path, ledger, scripted):
    ledger.pause(12, "manual")
    cfg = cfg_for(tmp_path)
    client = FakeX(ledger, mentions=[make_post(id="m1")])
    report = Engine(cfg, client, ledger).run()
    assert report.published == []
    assert any("paused" in e for e in report.errors)


def test_quota_exhaustion_stops_mid_run(tmp_path, ledger, scripted):
    for _ in range(3):
        ledger.record("reply")          # quota is 3
    cfg = cfg_for(tmp_path)
    client = FakeX(ledger, mentions=[make_post(id="m1", created_at=iso(1))])
    report = Engine(cfg, client, ledger).run()
    assert report.published == []
    assert any("budget stop" in e or "quota" in e for e in report.errors)


def test_no_double_reply_across_runs(tmp_path, ledger, scripted):
    cfg = cfg_for(tmp_path, max_actions_per_run=1)
    m = make_post(id="m1", author_id="u1", conversation_id="cm1", created_at=iso(1))
    client = FakeX(ledger, mentions=[m])

    r1 = Engine(cfg, client, ledger).run()
    assert len(r1.published) == 1

    r2 = Engine(cfg, client, ledger).run()   # same mention still in the timeline
    assert r2.published == []
    assert any("already acted" in reason or "conversation" in reason
               for _, reason in r2.rejected)


def test_one_action_per_author_per_run(tmp_path, ledger, scripted):
    cfg = cfg_for(tmp_path, max_actions_per_run=3)
    client = FakeX(
        ledger,
        mentions=[
            make_post(id="m1", author_id="u1", conversation_id="c1", created_at=iso(1)),
            make_post(id="m2", author_id="u1", conversation_id="c2", created_at=iso(1)),
        ],
    )
    report = Engine(cfg, client, ledger).run()
    assert len(report.published) == 1


def test_list_read_skipped_when_no_write_quota_left(tmp_path, ledger, scripted):
    for _ in range(3):
        ledger.record("reply")
    ledger.record("quote")
    cfg = cfg_for(tmp_path)
    client = FakeX(ledger, mentions=[], list_posts=[make_post(id="p1")])
    report = Engine(cfg, client, ledger).run()
    # Never pay $0.005/post for candidates we have no quota left to reply to.
    assert report.harvested.get("list", 0) == 0
    assert ledger.count_today("read_list_posts") == 0

# ------------------------------------------------- skip-reason observability
def test_model_skip_reports_the_models_own_reason(tmp_path, ledger, monkeypatch):
    """
    A run where everything skips looked identical to a run where everything
    failed a gate. That ambiguity cost a diagnosis cycle on 2026-08-06.
    """
    monkeypatch.setattr(llm, "generate",
                        lambda s, u, c=None: "SKIP: post already states the effect size")
    cfg = cfg_for(tmp_path)
    client = FakeX(ledger, mentions=[make_post(id="m1", created_at=iso(1))])
    report = Engine(cfg, client, ledger).run()

    assert report.published == []
    reasons = [s["reason"] for s in report.skipped]
    assert any("model declined" in r and "already states the effect size" in r
               for r in reasons), reasons


def test_gate_rejection_is_named_as_such(tmp_path, ledger, monkeypatch):
    monkeypatch.setattr(llm, "generate",
                        lambda s, u, c=None: "Full data at https://pubmed.gov/1")
    cfg = cfg_for(tmp_path, max_regenerations=0)
    client = FakeX(ledger, mentions=[make_post(id="m1", created_at=iso(1))])
    report = Engine(cfg, client, ledger).run()

    reasons = " ".join(s["reason"] for s in report.skipped)
    assert "gate:" in reasons and "URL" in reasons
    assert "model declined" not in reasons


def test_llm_error_is_distinguishable_from_a_skip(tmp_path, ledger, monkeypatch):
    def boom(s, u, c=None):
        raise llm.LLMError("429 rate limited")

    monkeypatch.setattr(llm, "generate", boom)
    cfg = cfg_for(tmp_path)
    client = FakeX(ledger, mentions=[make_post(id="m1", created_at=iso(1))])
    report = Engine(cfg, client, ledger).run()

    assert any("llm error" in s["reason"] for s in report.skipped)
    assert any("429" in e for e in report.errors)


def test_skip_reason_parsing():
    assert llm.skip_reason("SKIP: nothing to add") == "nothing to add"
    assert llm.skip_reason("SKIP") == "no reason given"
    assert llm.skip_reason("SKIP - post is promotional") == "post is promotional"
