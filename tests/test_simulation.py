"""
End-to-end simulation against the real @PeptideFrontier timeline as of
2026-08-01: the BPC-157 post from Jul 29 with two replies still unanswered,
plus a plausible outbound candidate and three posts that must be refused.

This is the acceptance test. It asserts the system picks the right targets,
refuses the wrong ones, and comes in under budget.
"""

from decimal import Decimal

import pytest

from pf_autorespond import llm
from pf_autorespond.config import Budget, Config, Harvest, Quotas, Selection
from pf_autorespond.engine import Engine
from tests.conftest import iso, make_post
from tests.test_engine import FakeX

BPC_ROOT = (
    "BPC-157's therapeutic potential is often overstated. Many claims trace back "
    "to preclinical studies, primarily in rodent models of injury. No large-scale "
    "human trials have established efficacy or safety for widespread use."
)

MENTIONS = [
    # the two real replies sitting unanswered
    make_post(id="r1", author_id="u_stranger", conversation_id="conv_bpc",
              text="True, but anecdotal evidence is pretty strong.",
              author_handle="stranger9824", author_followers=340, created_at=iso(6)),
    make_post(id="r2", author_id="u_vedichi", conversation_id="conv_bpc",
              text="no human PK either, so even the rodent doses don't translate "
                   "into a number for a person.",
              author_handle="vedichi_", author_followers=9_400, created_at=iso(5)),
    # must be refused: personal medical advice request
    make_post(id="r3", author_id="u_ask", conversation_id="conv_bpc",
              text="Should I stop taking mine then? I've been on it 6 weeks for a "
                   "shoulder tear and I'm not sure what to do here.",
              author_handle="asker", author_followers=120, created_at=iso(3)),
    # must be refused: vendor
    make_post(id="r4", author_id="u_vendor", conversation_id="conv_bpc",
              text="Research chemicals, not for human consumption. DM for price, "
                   "crypto only, domestic shipping, third party tested.",
              author_handle="peptideplug", author_followers=2_100, created_at=iso(2)),
]

OUTSIDERS = [
    make_post(id="o1", author_id="u_bio", conversation_id="conv_o1",
              text="Retatrutide's 24.2% at 48 weeks is being compared directly to "
                   "SURMOUNT-1 numbers all over the timeline. Different trial "
                   "lengths, different populations. The comparison is doing a lot "
                   "of unearned work.",
              author_handle="biotech_reader", author_followers=18_000,
              created_at=iso(2), public_metrics={"reply_count": 9, "like_count": 61, "quote_count": 2}),
    # must be refused: hostile bait
    make_post(id="o2", author_id="u_troll", conversation_id="conv_o2",
              text="Everyone posting peptide studies is a big pharma shill and a "
                   "bot account, total cope, nobody believes this garbage anymore",
              author_handle="angryguy", author_followers=4_000, created_at=iso(1)),
    # must be refused: vulnerable context
    make_post(id="o3", author_id="u_parent", conversation_id="conv_o3",
              text="My daughter is 15 and keeps asking about these weight loss "
                   "shots after seeing them on her feed. Where do I even start.",
              author_handle="aparent", author_followers=800, created_at=iso(1)),
]

DRAFTS = {
    "r2": "Agreed, and it's the sharper version of the point: without human PK "
          "there's no exposure curve to scale from, so rodent mg/kg figures "
          "aren't convertible at all. The dosing debate is downstream of that gap.",
    "o1": "The trial lengths are the load-bearing difference: retatrutide's 24.2% "
          "is 48 weeks, SURMOUNT-1 ran 72. Weight loss curves hadn't plateaued in "
          "either, so the shorter readout is the more impressive one, not less.",
}


@pytest.fixture
def scripted_sim(monkeypatch):
    seen = []

    def fake_generate(system, user, cfg=None):
        seen.append(user)
        for key, draft in DRAFTS.items():
            marker = {"r2": "human PK", "o1": "SURMOUNT-1"}[key]
            if marker in user:
                return draft
        return "SKIP"

    monkeypatch.setattr(llm, "generate", fake_generate)
    return seen


def sim_config(tmp_path):
    return Config(
        budget=Budget(Decimal("6.00"), Decimal("0.18"), Decimal("0.30")),
        quotas=Quotas(reply=3, quote=1, post=1, like=0, read_list_posts=2, read_search=1),
        harvest=Harvest(mentions_max=10, list_id="L1", list_posts_max=10),
        selection=Selection(
            min_score_mention=0.8, min_score_outsider=2.2, min_followers_outsider=500
        ),
        dry_run=False,
        max_actions_per_run=2,
        use_critic=False,
        gap_seconds_min=0.0,
        gap_seconds_max=0.0,
        state_path=str(tmp_path / "ledger.json"),
        log_path=str(tmp_path / "actions.log"),
    )


def test_full_run_on_real_timeline(tmp_path, ledger, scripted_sim):
    client = FakeX(
        ledger,
        mentions=MENTIONS,
        list_posts=OUTSIDERS,
        own=[make_post(id="root", author_id="me", conversation_id="conv_bpc", text=BPC_ROOT)],
    )
    report = Engine(sim_config(tmp_path), client, ledger).run()

    published = {p["target"] for p in report.published}
    refused = {pid for pid, _ in report.rejected}

    # acted on the two genuinely good targets
    assert published == {"r2", "o1"}, report.published
    # refused every unsafe one, without spending a cent on them
    assert {"r3", "r4", "o2", "o3"} <= refused, report.rejected
    # r1 ("anecdotal evidence is pretty strong") is safe but the model returned
    # SKIP -- nothing to add. A skip is a success, not a failure.
    assert not any(p["target"] == "r1" for p in report.published)

    reasons = dict(report.rejected)
    assert "advice_request" in reasons["r3"] or "advice" in reasons["r3"]
    assert "vendor" in reasons["r4"]
    assert "hostile" in reasons["o2"]
    assert "regulated" in reasons["o3"]


def test_full_run_stays_under_daily_budget(tmp_path, ledger, scripted_sim):
    client = FakeX(
        ledger,
        mentions=MENTIONS,
        list_posts=OUTSIDERS,
        own=[make_post(id="root", author_id="me", conversation_id="conv_bpc", text=BPC_ROOT)],
    )
    Engine(sim_config(tmp_path), client, ledger).run()

    # 4 mentions + 1 own post @ $0.001 = $0.005
    # 3 list posts @ $0.005 = $0.015
    # 2 replies @ $0.015 = $0.030
    assert ledger.day_spend() == Decimal("0.050")
    assert ledger.day_spend() < ledger.daily_budget
    # a full day of four such runs still fits the month
    assert ledger.day_spend() * 30 < Decimal("6.00") * 4


def test_second_run_same_day_adds_nothing(tmp_path, ledger, scripted_sim):
    cfg = sim_config(tmp_path)
    kw = dict(
        mentions=MENTIONS,
        list_posts=OUTSIDERS,
        own=[make_post(id="root", author_id="me", conversation_id="conv_bpc", text=BPC_ROOT)],
    )
    Engine(cfg, FakeX(ledger, **kw), ledger).run()
    spend_after_first = ledger.day_spend()

    report2 = Engine(cfg, FakeX(ledger, **kw), ledger).run()

    assert report2.published == []
    # Same ids inside the 24h UTC dedup window -> reads are free the second time.
    assert ledger.day_spend() == spend_after_first
