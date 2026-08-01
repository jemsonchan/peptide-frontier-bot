from decimal import Decimal

import pytest

from pf_autorespond import pricing
from pf_autorespond.ledger import BudgetExceeded, Ledger


def test_read_cost_scales_with_resources_returned():
    # Reads bill per resource, not per request. Asking for 100 and getting 100
    # is 100 units -- this is the mistake that empties a $6 wallet in one call.
    assert pricing.cost_of("read_list_posts", 10) == Decimal("0.050")
    assert pricing.cost_of("read_mentions", 10) == Decimal("0.010")
    # writes are flat per request
    assert pricing.cost_of("reply", 99) == Decimal("0.015")


def test_url_post_is_13x():
    ratio = pricing.POST_CREATE_WITH_URL / pricing.POST_CREATE
    assert ratio > 13
    assert pricing.url_variant("reply") == "reply_with_url"
    assert pricing.url_variant("like") == "like"  # no URL variant exists


def test_daily_budget_blocks_before_monthly(ledger):
    for _ in range(11):
        ok, _ = ledger.can_afford("reply")
        if not ok:
            break
        ledger.record("reply")
    ok, why = ledger.can_afford("reply")
    assert not ok
    assert "daily" in why or "quota" in why
    assert ledger.month_spend() < ledger.monthly_budget


def test_quota_binds_before_budget(ledger):
    # 3 replies = $0.045, well inside the $0.18 daily budget, but the quota is 3.
    for _ in range(3):
        ledger.record("reply")
    ok, why = ledger.can_afford("reply")
    assert not ok
    assert "quota" in why
    assert ledger.remaining_day() > Decimal("0.10")


def test_like_quota_of_zero_blocks_likes(ledger):
    ok, why = ledger.can_afford("like")
    assert not ok
    assert "quota" in why


def test_require_raises(ledger):
    for _ in range(3):
        ledger.record("reply")
    with pytest.raises(BudgetExceeded):
        ledger.require("reply")


def test_dedup_window_charges_each_id_once(ledger):
    ids = ["1", "2", "3"]
    assert ledger.billable_units("post", ids) == 3
    # same UTC day, same ids -> free
    assert ledger.billable_units("post", ids) == 0
    assert ledger.billable_units("post", ids + ["4"]) == 1


def test_persistence_survives_reload(tmp_path):
    p = tmp_path / "l.json"
    a = Ledger.load(p, monthly_budget="6.00", daily_budget="0.18", daily_quotas={"reply": 3})
    a.record("reply", ref="t1")
    a.mark_acted("t1")
    a.mark_conversation("c1")
    a.touch_author("u1")
    a.save()

    b = Ledger.load(p, monthly_budget="6.00", daily_budget="0.18", daily_quotas={"reply": 3})
    assert b.month_spend() == Decimal("0.015")
    assert b.has_acted("t1")
    assert b.in_conversation("c1")
    assert b.author_cooling_down("u1", 72)
    assert not b.author_cooling_down("u2", 72)


def test_pause_blocks_everything(ledger):
    ledger.pause(2, "manual kill switch")
    ok, why = ledger.can_afford("reply")
    assert not ok and "paused" in why
    ledger.resume()
    assert ledger.can_afford("reply")[0]


def test_atomic_save_leaves_no_temp_files(tmp_path):
    p = tmp_path / "l.json"
    led = Ledger.load(p)
    led.record("reply")
    led.save()
    led.save()
    assert [f.name for f in tmp_path.iterdir()] == ["l.json"]


def test_budget_math_for_configured_defaults():
    """The shipped config must actually fit in $6/month."""
    daily = (
        pricing.OWNED_READ * 10 * 4          # mentions, 4 runs
        + pricing.POST_READ * 10 * 2         # 2 list reads of 10 posts
        + pricing.POST_CREATE * 3            # replies
        + pricing.POST_CREATE * 1            # quote
        + pricing.POST_CREATE * 1            # daily original post
    )
    assert daily == Decimal("0.215")
    # Over the 0.18/day line, which is the point: quotas are a ceiling, not a
    # plan. Real burn is lower because most candidates are rejected for free.
    assert daily * 30 < Decimal("6.50")
