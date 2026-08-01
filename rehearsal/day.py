"""
A full simulated DAY: four runs at the real cron times.

This is what the single-run rehearsal can't show -- pacing. It exercises the
things that only appear across runs: the outbound reserve firing, the 3h thread
gap holding back a second reply, and the 24h dedup making later reads free.
"""
import sys, os
from decimal import Decimal
from pathlib import Path
from datetime import datetime, timedelta, timezone

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src")); sys.path.insert(0, str(ROOT))
os.environ.pop("NOSTR_NSEC", None); os.environ.pop("NOSTR_BUNKER_URI", None)

import rehearsal.run as R          # reuses the fake X client, scripted LLM, fixtures
from pf_autorespond import llm
from pf_autorespond.engine import Engine
from pf_autorespond.ledger import Ledger
from pf_autorespond.queue import Decision, Queue
from pf_autorespond.publisher import PublishReport, apply_decisions, publish_approved

C = R.C
tmp = Path("/tmp/rehearsal_day"); tmp.mkdir(exist_ok=True)
for f in tmp.glob("*"): f.unlink()

cfg = R.cfg
cfg.state_path = str(tmp/"ledger.json"); cfg.queue_path = str(tmp/"queue.json")
led = Ledger.load(cfg.state_path, monthly_budget=cfg.budget.monthly_usd,
                  daily_budget=cfg.budget.daily_usd, daily_quotas=cfg.quota_map())
q = Queue(cfg.queue_path)

print(f"\n{C['b']}SIMULATED DAY — 4 runs at the real cron times{C['x']}")
print(f"{C['d']}08:17 · 12:43 · 17:09 · 21:31 UTC{C['x']}")

prev = Decimal("0")
for label in ("08:17", "12:43", "17:09", "21:31"):
    x = R.FakeX(led)
    ch = R.RecordingChannel()
    rep = Engine(cfg, x, led, queue=q, channels=[ch]).run()
    spend = led.day_spend()
    delta = spend - prev; prev = spend

    print(f"\n{C['b']}── {label} UTC{C['x']}")
    billed = sum(b for _, _, b in x.reads); served = sum(n for _, n, _ in x.reads)
    free = served - billed
    print(f"   reads {served} resources, {billed} billable"
          + (f"  {C['g']}({free} free via 24h dedup){C['x']}" if free else ""))
    if not rep.queued:
        print(f"   {C['d']}nothing drafted{C['x']}")
    for d in rep.queued:
        kind = next((c for c in q.drafts if c.id == d["id"]), None)
        tag = f"{C['c']}[outbound]{C['x']}" if kind and kind.kind == "outsider" else "[mention] "
        print(f"   {tag} → @{d['target_author']:16s} score {d['score']:.2f}  "
              f"{d['text'][:46]}…")
    held = [r for _, r in rep.rejected if "thread:" in r and "to go" in r]
    for r in held[:2]:
        print(f"   {C['y']}held{C['x']}       {r}")
    print(f"   spend +${delta:.4f}  →  ${spend:.4f} today")

    # You tap approve on Telegram between runs. Until you decide, the queue
    # holds the thread open -- a second draft for the same conversation is
    # blocked while the first is undecided, which is why @stranger9824 never
    # appears if nothing is ever approved.
    pend = q.pending()
    if pend:
        prep = PublishReport()
        apply_decisions(q, [Decision(pend[0].id, "approve", "telegram", "281152522")], prep)
        publish_approved(q, x, led, prep, max_per_run=cfg.max_publish_per_run)
        for pu in prep.published:
            print(f"   {C['g']}✓ you approved{C['x']} → published to {pu['target']} "
                  f"(+$0.015)")
        prev = led.day_spend()

    # advance the clock so the next run sees a realistic gap
    for k, v in led.conversation_log.items():
        led.conversation_log[k] = [
            (datetime.fromisoformat(t) - timedelta(hours=4.5)).isoformat() for t in v
        ]
    for k, v in list(led.author_last_touch.items()):
        led.author_last_touch[k] = (
            datetime.fromisoformat(v) - timedelta(hours=4.5)).isoformat()

print(f"\n{'─'*76}")
kinds = {}
for d in q.drafts:
    kinds[d.kind] = kinds.get(d.kind, 0) + 1
print(f"  drafted over the day : {dict(sorted(kinds.items()))}")
print(f"  {C['b']}day total            : ${led.day_spend():.4f}{C['x']}   "
      f"→ ${led.day_spend()*31:.2f}/month of ${cfg.budget.monthly_usd}")
print(f"  reply quota used     : {led.count_today('reply')}/{cfg.quotas.reply}")
print(f"  outbound reserved    : {led.count_kind_today('outsider')}/"
      f"{cfg.selection.reserved_outbound_per_day}")
print(f"  {led.summary()}")
