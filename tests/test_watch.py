"""The watcher's only job is to stay quiet. Test that it does."""

from pf_autorespond import watch


def fetcher_for(pages):
    def _f(url, timeout=30):
        return pages[url]
    return _f


SRC = {"pricing": "https://example.test/pricing"}


def test_first_run_captures_baseline_quietly(tmp_path):
    pages = {SRC["pricing"]: "# Pricing\nPosts: Read $0.005 per resource\n"}
    changes = watch.check(tmp_path, SRC, fetcher_for(pages))
    assert len(changes) == 1 and changes[0].first_seen
    # second run, unchanged -> silence
    assert watch.check(tmp_path, SRC, fetcher_for(pages)) == []


def test_price_change_is_flagged_critical(tmp_path):
    watch.check(tmp_path, SRC, fetcher_for(
        {SRC["pricing"]: "# Pricing\nPosts: Read $0.005 per resource\n"}))
    changes = watch.check(tmp_path, SRC, fetcher_for(
        {SRC["pricing"]: "# Pricing\nPosts: Read $0.008 per resource\n"}))

    assert len(changes) == 1
    c = changes[0]
    assert c.critical
    assert any("0.008" in ln for ln in c.added)
    assert any("0.005" in ln for ln in c.removed)
    assert "🔴" in watch.digest(changes)


def test_cosmetic_churn_does_not_trigger(tmp_path):
    base = "# Pricing\nPosts: Read $0.005 per resource\nLast updated 2026-01-01T00:00\n"
    noisy = "# Pricing\nPosts: Read $0.005 per resource\nLast updated 2026-08-01T09:23\n"
    watch.check(tmp_path, SRC, fetcher_for({SRC["pricing"]: base}))
    assert watch.check(tmp_path, SRC, fetcher_for({SRC["pricing"]: noisy})) == []


def test_prose_change_is_reported_but_not_critical(tmp_path):
    watch.check(tmp_path, SRC, fetcher_for({SRC["pricing"]: "# Pricing\nHello there\n"}))
    changes = watch.check(tmp_path, SRC, fetcher_for({SRC["pricing"]: "# Pricing\nHello world\n"}))
    assert changes and not changes[0].critical
    assert "🔴" not in watch.digest(changes)


def test_a_dead_source_does_not_kill_the_run(tmp_path):
    def boom(url, timeout=30):
        raise RuntimeError("502")
    assert watch.check(tmp_path, SRC, boom) == []
    assert (tmp_path / "last_check.json").exists()


def test_render_puts_critical_first(tmp_path):
    two = {"a": "https://x.test/a", "b": "https://x.test/b"}
    watch.check(tmp_path, two, fetcher_for({"https://x.test/a": "hello\n",
                                            "https://x.test/b": "cost $0.005\n"}))
    changes = watch.check(tmp_path, two, fetcher_for({"https://x.test/a": "goodbye\n",
                                                      "https://x.test/b": "cost $0.009\n"}))
    out = watch.render(changes)
    assert out.index("🔴 b") < out.index("### a")


def test_real_source_urls_are_the_ones_we_care_about():
    assert "xai-org/x-algorithm" in watch.SOURCES["x-algorithm-readme"]
    assert "pricing" in watch.SOURCES["x-api-pricing"]
    assert "rate-limits" in watch.SOURCES["x-api-rate-limits"]
