import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pf_autorespond.ledger import Ledger  # noqa: E402
from pf_autorespond.xclient import Post  # noqa: E402


def iso(hours_ago: float = 0.0) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


@pytest.fixture
def ledger(tmp_path):
    return Ledger.load(
        tmp_path / "ledger.json",
        monthly_budget="6.00",
        daily_budget="0.18",
        daily_quotas={"reply": 3, "quote": 1, "like": 0, "read_list_posts": 2},
    )


def make_post(**kw) -> Post:
    base = dict(
        id="1",
        text="Retatrutide showed 24.2% weight loss at 48 weeks in a Phase 2 trial, "
        "notably larger than tirzepatide's comparable readouts.",
        author_id="a1",
        conversation_id="c1",
        created_at=iso(1),
        lang="en",
        public_metrics={"reply_count": 4, "like_count": 20, "quote_count": 1},
        author_handle="someone",
        author_followers=12_000,
        author_protected=False,
        author_created_at=iso(24 * 900),
    )
    base.update(kw)
    return Post(**base)


@pytest.fixture
def post_factory():
    return make_post
