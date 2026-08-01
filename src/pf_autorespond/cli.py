"""Entry point. `python -m pf_autorespond.cli run --dry-run`"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from decimal import Decimal
from pathlib import Path

from .channels import build_channels
from .config import Config, x_credentials
from .engine import Engine, append_log
from .ledger import Ledger
from .queue import Queue
from .xclient import XClient


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _load(cfg: Config) -> Ledger:
    return Ledger.load(
        cfg.state_path,
        monthly_budget=cfg.budget.monthly_usd,
        daily_budget=cfg.budget.daily_usd,
        daily_quotas=cfg.quota_map(),
    )


def cmd_run(args) -> int:
    cfg = Config.load(args.config)
    if args.dry_run:
        cfg.dry_run = True
    if args.live:
        cfg.dry_run = False
    if args.max_actions:
        cfg.max_actions_per_run = args.max_actions

    ledger = _load(cfg)
    client = XClient(**x_credentials(), ledger=ledger, dry_run=cfg.dry_run)

    queue = channels = None
    if cfg.mode == "review":
        queue = Queue(cfg.queue_path)
        channels = build_channels(cfg)

    engine = Engine(cfg, client, ledger, queue=queue, channels=channels or [])
    report = engine.run()

    ledger.save()
    if queue is not None:
        queue.prune()
        queue.save()
    append_log(cfg.log_path, report)

    print(json.dumps(report.to_dict(), indent=2, default=str))
    print(f"\n{ledger.summary()}", file=sys.stderr)
    if report.queued:
        print(f"{len(report.queued)} draft(s) awaiting your approval", file=sys.stderr)
    if report.published:
        print(
            f"{len(report.published)} action(s) "
            f"{'simulated' if cfg.dry_run else 'published'}",
            file=sys.stderr,
        )
    return 1 if report.errors and not (report.published or report.queued) else 0


def cmd_status(args) -> int:
    cfg = Config.load(args.config)
    ledger = _load(cfg)
    print(ledger.summary())
    print(f"paused: {ledger.is_paused()} {ledger.pause_reason}")
    for action, limit in sorted(cfg.quota_map().items()):
        print(f"  {action:18s} {ledger.count_today(action)}/{limit} today")
    print(f"  conversations joined (90d): {len(ledger.joined_conversations)}")
    print(f"  targets acted on (90d):     {len(ledger.acted)}")
    return 0


def cmd_pause(args) -> int:
    cfg = Config.load(args.config)
    ledger = _load(cfg)
    ledger.pause(args.hours, args.reason or "manual")
    ledger.save()
    print(f"paused for {args.hours}h: {ledger.pause_reason}")
    return 0


def cmd_resume(args) -> int:
    cfg = Config.load(args.config)
    ledger = _load(cfg)
    ledger.resume()
    ledger.save()
    print("resumed")
    return 0


def cmd_estimate(args) -> int:
    """
    What a month of the current config costs, before you spend anything.

    Two columns, because the honest answer is a range:

      WORST   every read returns fully new resources every run.
      EXPECT  X deduplicates billable resources within a 24h UTC window, so
              re-reading the same mention or list post later the same day is
              free. With 4 runs/day most reads after the first are repeats.
              --new-frac sets how much of each later read is genuinely new.
    """
    from . import pricing

    cfg = Config.load(args.config)
    q, h = cfg.quotas, cfg.harvest
    runs = args.runs_per_day
    nf = Decimal(str(args.new_frac))  # Decimal throughout; never mix with float

    def read_cost(unit, per_call, calls):
        worst = unit * per_call * calls
        expect = unit * per_call * (Decimal(1) + (calls - 1) * nf) if calls else Decimal(0)
        return worst, expect

    rows = []
    w, e = read_cost(pricing.OWNED_READ, h.mentions_max, runs)
    rows.append(("mentions read", w, e, f"{h.mentions_max}/run x{runs} runs"))
    w, e = read_cost(pricing.POST_READ, h.list_posts_max, q.read_list_posts)
    rows.append(("list posts read", w, e, f"{h.list_posts_max} x{q.read_list_posts}/day"))
    w, e = read_cost(pricing.OWNED_READ, h.own_posts_max, runs)
    rows.append(("own timeline read", w, e, f"{h.own_posts_max}/run x{runs} runs"))
    for label, n, unit in (
        ("replies", q.reply, pricing.POST_CREATE),
        ("quotes", q.quote, pricing.POST_CREATE),
        ("original posts", q.post, pricing.POST_CREATE),
        ("likes", q.like, pricing.USER_INTERACTION_CREATE),
    ):
        rows.append((label, unit * n, unit * n, f"{n}/day"))

    worst = sum(r[1] for r in rows)
    expect = sum(r[2] for r in rows)

    print(f"{'line item':20s} {'WORST/day':>10s} {'EXPECT/day':>11s}   detail")
    print("-" * 74)
    for name, cw, ce, detail in rows:
        print(f"{name:20s} ${cw:>9.4f} ${ce:>10.4f}   {detail}")
    print("-" * 74)
    print(f"{'DAILY':20s} ${worst:>9.4f} ${expect:>10.4f}")
    print(f"{'MONTHLY (x31)':20s} ${worst * 31:>9.3f} ${expect * 31:>10.3f}")
    print(f"\nbudget: ${cfg.budget.daily_usd}/day, ${cfg.budget.monthly_usd}/month "
          f"(reserve ${cfg.budget.reserve_usd})")
    print(f"dedup assumption: {nf:.0%} of each repeat read is newly billable")

    problems = []
    if worst * 31 > cfg.budget.monthly_usd:
        problems.append(
            f"worst-case month ${worst * 31:.2f} exceeds ${cfg.budget.monthly_usd} "
            f"-- ledger will hard-stop early on a heavy month"
        )
    if worst > cfg.budget.daily_usd:
        problems.append(
            f"worst-case day ${worst:.4f} exceeds ${cfg.budget.daily_usd} "
            f"-- later runs each day get starved"
        )
    if expect * 31 > cfg.budget.monthly_usd:
        problems.append(
            f"EXPECTED month ${expect * 31:.2f} exceeds ${cfg.budget.monthly_usd} "
            f"-- this config does not fit, cut list_posts_max or read_list_posts"
        )

    if problems:
        print()
        for msg in problems:
            print(f"  !! {msg}")
    else:
        days = float(cfg.budget.monthly_usd / expect) if expect else 999
        print(f"\n  ok: at expected burn, ${cfg.budget.monthly_usd} lasts ~{days:.0f} days")
        write_share = float(sum(r[2] for r in rows[3:]) / expect)
        print(f"      the $0.015 writes are {write_share:.0%} "
              f"of spend; the rest is reads")
    return 1 if any("EXPECTED" in m for m in problems) else 0



def cmd_publish(args) -> int:
    """Poll approval channels, then publish what you approved."""
    import os

    from .channels import GitHubChannel, TelegramChannel
    from .publisher import (
        PublishReport, apply_decisions, expire, mirror_to_nostr, publish_approved,
    )

    cfg = Config.load(args.config)
    ledger = _load(cfg)
    queue = Queue(cfg.queue_path)
    channels = build_channels(cfg)
    report = PublishReport()

    pending = queue.pending() + queue.approved()
    report.polled = len(pending)

    decisions = []
    offset_file = Path(cfg.queue_path).parent / "telegram_offset"
    for ch in channels:
        try:
            if isinstance(ch, TelegramChannel):
                # Persist the update offset or every run reprocesses the same
                # callbacks and re-publishes what you already approved.
                offset = int(offset_file.read_text()) if offset_file.exists() else 0
                got, new_offset = ch.poll(pending, offset)
                decisions += got
                offset_file.parent.mkdir(parents=True, exist_ok=True)
                offset_file.write_text(str(new_offset))
            elif isinstance(ch, GitHubChannel):
                decisions += ch.poll(pending)
        except Exception as e:  # noqa: BLE001
            report.errors.append(f"poll {getattr(ch, 'name', '?')}: {e}")

    apply_decisions(queue, decisions, report)
    expire(queue, cfg.approval_ttl_hours, report, channels)

    if not args.no_publish:
        client = XClient(**x_credentials(), ledger=ledger, dry_run=cfg.dry_run)
        publish_approved(
            queue, client, ledger, report,
            max_per_run=cfg.max_publish_per_run, channels=channels,
        )

    # Nostr last, and outside the X budget path entirely: it is free, so it
    # runs even when the wallet stopped us publishing to X.
    try:
        mirror_to_nostr(queue, cfg, report)
    except Exception as e:  # noqa: BLE001 - never let a relay break the X flow
        report.errors.append(f"nostr: {e}")

    queue.prune()
    queue.save()
    ledger.save()
    report.queue_summary = queue.summary()
    report.ledger_summary = ledger.summary()
    print(json.dumps(report.to_dict(), indent=2, default=str))
    return 0


def cmd_queue(args) -> int:
    cfg = Config.load(args.config)
    queue = Queue(cfg.queue_path)
    print(f"queue: {queue.summary()}\n")
    for d in queue.drafts:
        if args.all or d.status in ("pending", "approved"):
            flag = {"pending": "…", "approved": "✓", "rejected": "✗",
                    "published": "→", "expired": "⌛", "failed": "!"}.get(d.status, "?")
            print(f"{flag} {d.id}  {d.status:9s} {d.action:5s} "
                  f"@{d.target_author or '-':<16s} {d.age_hours():5.1f}h  {d.text[:70]}")
    return 0


def cmd_decide(args) -> int:
    """Approve or reject from the terminal, without a channel round-trip."""
    from .publisher import PublishReport, apply_decisions
    from .queue import Decision

    cfg = Config.load(args.config)
    queue = Queue(cfg.queue_path)
    if queue.get(args.draft_id) is None:
        print(f"no draft {args.draft_id}", file=sys.stderr)
        return 1
    report = PublishReport()
    apply_decisions(
        queue,
        [Decision(args.draft_id, args.verdict, "cli", "local", new_text=args.text or "")],
        report,
    )
    queue.save()
    print(f"{args.draft_id}: {queue.get(args.draft_id).status}")
    if report.errors:
        for e in report.errors:
            print(f"  {e}", file=sys.stderr)
        return 1
    return 0


def cmd_watch(args) -> int:
    """Diff the algorithm repo and pricing docs. Silent unless something moved."""
    import os

    from . import watch as watcher
    from .channels import GitHubChannel, TelegramChannel

    cfg = Config.load(args.config)
    changes = watcher.check(args.state_dir)
    baseline_only = changes and all(c.first_seen for c in changes)

    if not changes:
        print("no changes")
        return 0
    if baseline_only and not args.announce_baseline:
        print(f"baseline captured for {len(changes)} source(s); staying quiet")
        return 0

    body = watcher.render(changes)
    line = watcher.digest(changes)
    print(line)
    print()
    print(body)

    if args.notify:
        for ch in build_channels(cfg):
            try:
                if isinstance(ch, GitHubChannel):
                    ch._req(
                        "POST",
                        f"/repos/{ch.repo}/issues",
                        json={
                            "title": f"[watch] {line[:80]}",
                            "body": body,
                            "labels": ["pf-watch"],
                        },
                    )
                elif isinstance(ch, TelegramChannel):
                    ch.notify(f"{line}\n\nDetails in the GitHub issue.")
            except Exception as e:  # noqa: BLE001
                print(f"notify via {getattr(ch, 'name', '?')} failed: {e}", file=sys.stderr)
    return 0



def cmd_nostr(args) -> int:
    from . import nip46
    from . import nostr as nostr_mod
    from .publisher import PublishReport, backfill_map, mirror_to_nostr, reconcile_nostr

    cfg = Config.load(args.config)
    relays = cfg.nostr.relays or nostr_mod.DEFAULT_RELAYS

    # Reachability and backfill are reads: they need no secret at all.
    if args.what == "check":
        results = nostr_mod.check_relays(relays)
        for r in results:
            print(f"  {'ok  ' if r['ok'] else 'FAIL'} {r['relay']:28s} "
                  f"{r.get('ms', 0):>5}ms  {r['detail']}")
        live = sum(1 for r in results if r["ok"])
        print(f"\n{live}/{len(results)} reachable")
        return 0 if live else 1

    if args.what == "backfill":
        queue = Queue(cfg.queue_path)
        mapped, seen = backfill_map(cfg, queue, pubkey=args.npub, limit=args.limit)
        print(f"fetched {seen} of your notes from {len(relays)} relay(s)")
        print(f"mapped  {mapped} X post(s) onto bridged Nostr events")
        if seen and not mapped:
            print("\n(nothing to map yet — the queue has no drafts referencing your "
                  "own posts. Run the drafting workflow first, then backfill.)")
        elif not seen:
            print("\n(no notes found — check the npub and that your bridge is "
                  "publishing to one of the configured relays)")
        return 0

    if args.what == "reconcile":
        ledger = _load(cfg)
        client = XClient(**x_credentials(), ledger=ledger, dry_run=not args.live)
        report = PublishReport()
        gaps = reconcile_nostr(
            cfg, client, ledger, report,
            pubkey=args.npub, lookback=args.limit, live=args.live,
        )
        head = report.nostr[0] if report.nostr else {}
        print(f"X posts checked   : {head.get('x_posts', 0)}")
        print(f"Nostr notes found : {head.get('nostr_notes', 0)}")
        print(f"already bridged   : {head.get('already_bridged', 0)}")
        print(f"map repaired      : {head.get('map_repaired', 0)}  (free)")
        print(f"gaps              : {len(gaps)}")
        if gaps:
            import datetime
            print()
            for g in gaps:
                when = (
                    datetime.datetime.fromtimestamp(
                        g.created_at, datetime.timezone.utc
                    ).strftime("%Y-%m-%d %H:%M UTC")
                    if g.created_at else "unknown time"
                )
                state = f"republished {g.matched_event[:12]}…" if g.matched_event else (
                    "would republish" if not args.live else "FAILED"
                )
                print(f"  {when}  {state}")
                print(f"      {' '.join(g.text.split())[:88]}")
        if not args.live and gaps:
            print("\n  dry run — add --live to republish (free; Nostr has no per-action cost)")
        ledger.save()
        for e in report.errors:
            print(f"  ! {e}", file=sys.stderr)
        return 0

    if args.what == "notes":
        pk = nostr_mod.pubkey_from(args.npub) if args.npub else None
        if pk is None:
            key = nostr_mod.key_from_env()
            if key is None:
                print("pass --npub, or set NOSTR_NSEC", file=sys.stderr)
                return 1
            pk = key.pubkey_hex
        notes = nostr_mod.fetch_own_notes(relays, pk, limit=args.limit)
        print(f"{len(notes)} note(s) for {pk[:16]}…\n")
        for n in notes[:args.limit]:
            import datetime
            when = datetime.datetime.fromtimestamp(
                n.get("created_at", 0), datetime.timezone.utc
            ).strftime("%Y-%m-%d %H:%M")
            body = " ".join((n.get("content") or "").split())[:96]
            print(f"  {when}  {n['id'][:12]}…  {body}")
        return 0

    key = nip46.get_signer()
    if key is None:
        print("no signer: set NOSTR_BUNKER_URI (preferred) or NOSTR_NSEC",
              file=sys.stderr)
        return 1

    if args.what == "whoami":
        kind = ("remote signer (NIP-46) — your key never left your device"
                if isinstance(key, nip46.BunkerSigner) else "local nsec in this process")
        try:
            print(f"npub   : {key.npub}")
            print(f"hex    : {key.pubkey_hex}")
        except nip46.BunkerError as e:
            print(f"bunker : {e}", file=sys.stderr)
            return 1
        print(f"signing: {kind}")
        print(f"relays : {', '.join(relays)}")
        return 0

    queue = Queue(cfg.queue_path)
    if args.what == "backfill":
        n = backfill_map(cfg, queue, limit=args.limit)
        print(f"mapped {n} X post(s) to bridged Nostr events")
        return 0

    if args.what == "test":
        ev = key.sign_event(nostr_mod.note(args.text or "Relay connectivity test."))
        for r in nostr_mod.RelayPool(relays, key).publish(ev):
            mark = "ok " if r.ok else "FAIL"
            auth = " (authed)" if r.authed else ""
            print(f"  {mark} {r.relay}{auth} {r.message}")
        print(f"\nevent {ev.id}")
        return 0

    report = PublishReport()
    mirror_to_nostr(queue, cfg, report)
    queue.save()
    print(json.dumps(report.nostr, indent=2))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="pf-autorespond")
    ap.add_argument("-c", "--config", default=None)
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="harvest, select, generate, publish")
    r.add_argument("--dry-run", action="store_true", help="generate but never publish")
    r.add_argument("--live", action="store_true", help="actually publish")
    r.add_argument("--max-actions", type=int, default=None)
    r.set_defaults(func=cmd_run)

    s = sub.add_parser("status", help="budget and quota state")
    s.set_defaults(func=cmd_status)

    p = sub.add_parser("pause", help="kill switch")
    p.add_argument("--hours", type=float, default=24)
    p.add_argument("--reason", default="")
    p.set_defaults(func=cmd_pause)

    u = sub.add_parser("resume")
    u.set_defaults(func=cmd_resume)

    pb = sub.add_parser("publish", help="poll approvals and publish what you approved")
    pb.add_argument("--no-publish", action="store_true", help="collect verdicts only")
    pb.set_defaults(func=cmd_publish)

    qq = sub.add_parser("queue", help="show pending drafts")
    qq.add_argument("--all", action="store_true")
    qq.set_defaults(func=cmd_queue)

    dd = sub.add_parser("decide", help="approve/reject/edit a draft locally")
    dd.add_argument("draft_id")
    dd.add_argument("verdict", choices=["approve", "reject", "edit"])
    dd.add_argument("--text", default="", help="replacement text for edit")
    dd.set_defaults(func=cmd_decide)

    wt = sub.add_parser("watch", help="diff algorithm repo + pricing docs")
    wt.add_argument("--state-dir", default="state/watch")
    wt.add_argument("--notify", action="store_true")
    wt.add_argument("--announce-baseline", action="store_true")
    wt.set_defaults(func=cmd_watch)

    ns = sub.add_parser("nostr", help="mirror drafts to Nostr / inspect keys")
    ns.add_argument(
        "what",
        choices=["check", "notes", "backfill", "reconcile", "mirror", "whoami", "test"],
        nargs="?", default="check",
    )
    ns.add_argument("--npub", default="", help="public key; reads never need the nsec")
    ns.add_argument("--limit", type=int, default=50)
    ns.add_argument("--text", default="")
    ns.add_argument("--live", action="store_true",
                    help="reconcile: actually republish the gaps")
    ns.set_defaults(func=cmd_nostr)

    e = sub.add_parser("estimate", help="cost model for the current config")
    e.add_argument("--runs-per-day", type=int, default=4)
    e.add_argument("--new-frac", type=float, default=0.35,
                   help="share of each repeat read that is newly billable")
    e.set_defaults(func=cmd_estimate)

    args = ap.parse_args(argv)
    _setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
