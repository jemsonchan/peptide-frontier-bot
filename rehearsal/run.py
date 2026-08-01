"""
Offline rehearsal of the full pipeline.

Real code throughout: engine, selector, safety gates, ledger, queue, publisher,
Nostr event construction and signing. Only two things are substituted, and only
because they need credentials:

    * the X API  -> a fake client that serves fixtures and records writes
    * the LLM    -> a scripted responder standing in for the model

The script deliberately includes a draft that fails a gate and one the critic
kills. A rehearsal where everything works first time is a demo, not a test.
"""
import sys, json, os
from decimal import Decimal
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src")); sys.path.insert(0, str(ROOT))
os.environ.pop("NOSTR_NSEC", None); os.environ.pop("NOSTR_BUNKER_URI", None)

from rehearsal.fixtures import MENTIONS, OUTSIDERS, OWN, ago
from pf_autorespond import llm, nostr as N, safety
from pf_autorespond.config import Budget, Config, Harvest, Nostr, Quotas, Selection
from pf_autorespond.engine import Engine
from pf_autorespond.ledger import Ledger
from pf_autorespond.queue import Decision, Queue
from pf_autorespond.publisher import (PublishReport, apply_decisions, mirror_to_nostr,
                                      publish_approved)
from pf_autorespond.xclient import Post

C = dict(b="\033[1m", d="\033[2m", g="\033[32m", r="\033[31m", y="\033[33m",
         c="\033[36m", x="\033[0m")
def h(t): print(f"\n{C['b']}{t}{C['x']}\n" + "─"*76)
def ok(t): print(f"  {C['g']}✓{C['x']} {t}")
def no(t): print(f"  {C['r']}✗{C['x']} {t}")
def wn(t): print(f"  {C['y']}!{C['x']} {t}")
def dim(t): print(f"  {C['d']}{t}{C['x']}")

def mk(d, own=False):
    return Post(id=d["id"], text=d["text"], author_id=d.get("author_id","me"),
                conversation_id=d["conv"], created_at=ago(d["age"]),
                public_metrics=d.get("metrics", {}), author_handle=d.get("handle",""),
                author_followers=d.get("followers",0), author_created_at=ago(24*900),
                lang="en")

# ---------------------------------------------------------------- scripted LLM
DRAFTS = {
 # good: concedes the specific point, adds the mechanism
 "r2": ["Right, and that's the sharper version of it. Without human PK there's no "
        "exposure curve to scale from, so rodent mg/kg figures aren't convertible "
        "at all. The dosing debate sits on top of that gap rather than resolving it."],
 # first attempt invents a citation -> critic must kill it
 "r1": ["Anecdote is weak here specifically: the 2019 Kang trial (n=412) found no "
        "effect once controls were added, and self-reported tendon recovery regresses "
        "to the mean anyway.",
        "Anecdote struggles here for a structural reason: soft-tissue injuries improve "
        "on their own, so uncontrolled recovery reports can't separate the compound "
        "from time. That's the gap trials exist to close."],
 # first attempt smuggles a URL -> gate must kill it
 "o1": ["Agreed. Retatrutide's 24.2% is 48 weeks, SURMOUNT-1 ran 72 — full comparison "
        "at https://pubmed.ncbi.nlm.nih.gov/37870536",
        "Agreed, and the direction cuts the other way from how it's used: retatrutide's "
        "24.2% is 48 weeks against SURMOUNT-1's 72. Neither curve had plateaued, so the "
        "shorter readout is the more striking one, not the weaker one."],
 # first attempt invents a trial -> the critic must kill it
 "r5": ["Yes — the 2019 Kang trial (n=412) mapped the full dose-response curve and "
        "found the plateau at 2.4mg with adverse events rising linearly past it.",
        "Documented, yes: STEP and SUSTAIN both show adverse events rising faster than "
        "efficacy above the 2.4mg dose, which is why that's the licensed ceiling rather "
        "than the highest tolerated one."],
 # nothing to add -> SKIP
 "o4": ["SKIP"],
 "o5": ["That's VK2735, and the 13.1% is Phase 1 at 12 weeks — an early oral readout, "
        "not a head-to-head. Injectable comparators in the VENTURE study still lead on "
        "magnitude; the interesting part is the route, not the number."],
}
CRITIC_VERDICTS = {
 "Kang trial": "REJECT: cites a trial that cannot be verified",
}
_calls = {"gen": 0, "critic": 0}
_state = {}

def fake_generate(system, user, cfg=None):
    if "reviewing a draft reply" in system:            # the critic
        _calls["critic"] += 1
        draft = user.split("DRAFT:")[-1]
        for needle, verdict in CRITIC_VERDICTS.items():
            if needle in draft:
                return verdict
        return "PASS"
    _calls["gen"] += 1
    for key, seq in DRAFTS.items():
        marker = {"r1": "anecdotal evidence", "r2": "no human PK",
                  "r5": "just a thing", "o1": "SURMOUNT-1",
                  "o4": "secondary-prevention", "o5": "13.1%"}[key]
        if marker in user:
            i = _state.get(key, 0)
            _state[key] = min(i + 1, len(seq) - 1)
            return seq[i]
    return "SKIP"

llm.generate = fake_generate

# ------------------------------------------------------------------ fake X API
class FakeX:
    def __init__(self, ledger): self.ledger, self.writes, self.reads = ledger, [], []
    def me(self): return {"id": "me", "username": "PeptideFrontier"}
    def _bill(self, action, posts, ref):
        u = self.ledger.billable_units("post", [p.id for p in posts])
        self.ledger.record(action, u, ref=ref)
        self.reads.append((action, len(posts), u))
        return posts
    def mentions(self, uid, max_results=10, since_id=None):
        return self._bill("read_mentions", [mk(d) for d in MENTIONS][:max_results], uid)
    def own_posts(self, uid, max_results=5):
        return self._bill("read_own_timeline", [mk(d, True) for d in OWN][:max_results], uid)
    def list_posts(self, lid, max_results=10):
        self.ledger.require("read_list_posts")
        return self._bill("read_list_posts", [mk(d) for d in OUTSIDERS][:max_results], lid)
    def search_recent(self, q, max_results=10): return []
    def reply(self, text, to): return self._write("reply", text, to)
    def quote(self, text, to): return self._write("quote", text, to)
    def post(self, text): return self._write("post", text, "")
    def _write(self, action, text, to):
        if safety.URL_RE.search(text): raise ValueError("URL reached the API boundary")
        self.ledger.require(action)
        self.ledger.record(action, 1, ref=to, note=text[:50])
        self.writes.append((action, to, text))
        return {"data": {"id": f"posted_{len(self.writes)}", "text": text}}

# ------------------------------------------------------------------- channels
class RecordingChannel:
    name = "telegram"
    def __init__(self): self.sent = []
    def announce(self, d, ttl): self.sent.append(d); d.telegram_message = len(self.sent)
    def finalize(self, d, outcome, detail=""): self.sent.append((d.id, outcome, detail))

tmp = Path("/tmp/rehearsal"); tmp.mkdir(exist_ok=True)
for f in tmp.glob("*"): f.unlink()

cfg = Config(
    budget=Budget(Decimal("6.00"), Decimal("0.19"), Decimal("0.30")),
    quotas=Quotas(reply=5, quote=1, post=1, like=0, read_list_posts=1, read_search=1),
    harvest=Harvest(mentions_max=6, own_posts_max=4, list_id="L1", list_posts_max=9),
    selection=Selection(min_score_mention=0.8, min_score_outsider=2.2,
                        min_followers_outsider=500, mention_half_life=36.0,
                        mention_max_age_hours=96.0, reserved_outbound_per_day=1,
                        max_replies_per_conversation=2, conversation_gap_hours=3.0),
    nostr=Nostr(enabled=True, relays=["wss://nos.lol"], map_path=str(tmp/"map.json")),
    dry_run=False, mode="review", use_critic=True, max_regenerations=1,
    max_actions_per_run=2, gap_seconds_min=0, gap_seconds_max=0,
    state_path=str(tmp/"ledger.json"), queue_path=str(tmp/"queue.json"),
    log_path=str(tmp/"log"), approval_ttl_hours=12.0)

led = Ledger.load(cfg.state_path, monthly_budget=cfg.budget.monthly_usd,
                  daily_budget=cfg.budget.daily_usd, daily_quotas=cfg.quota_map())
q = Queue(cfg.queue_path); x = FakeX(led); ch = RecordingChannel()

print(f"\n{C['b']}PEPTIDE FRONTIER — OFFLINE REHEARSAL{C['x']}")
print(f"{C['d']}real pipeline · fake X API · scripted model · nothing published{C['x']}")

h("1. HARVEST + SELECT")
report = Engine(cfg, x, led, queue=q, channels=[ch]).run()
for action, n, billed in x.reads:
    saved = f"  {C['d']}({n-billed} free via 24h dedup){C['x']}" if billed < n else ""
    print(f"  read {action:20s} {n:2d} resources, {billed:2d} billable{saved}")
print(f"\n  considered {report.considered} candidate(s) after gating")

h("2. REFUSED — cost $0.00, never reached the model")
traps = {d["id"]: d.get("trap") for d in MENTIONS + OUTSIDERS}
for pid, reason in report.rejected:
    if traps.get(pid):
        no(f"{pid:3s} {traps[pid]:26s} {C['d']}{reason}{C['x']}")
for pid, reason in report.rejected:
    if not traps.get(pid) and "score" in reason:
        dim(f"{pid:3s} below threshold             {reason}")

h("3. DRAFTED")
for d in q.pending():
    print(f"  {C['c']}{d.id}{C['x']}  {d.action} → @{d.target_author}  score {d.score:.2f}")
    print(f"       {C['d']}re:{C['x']} {' '.join(d.target_text.split())[:70]}")
    print(f"       {d.text}")
    print()

h("4. KILLED BEFORE YOU SAW THEM")
for s in report.skipped:
    if "reason" in s and s.get("draft"):
        wn(f"{s['reason']}")
        dim(f"    draft was: {s['draft'][:100]}")
    elif s.get("reason", "").startswith("model returned SKIP"):
        wn(f"{s['id']}: model returned SKIP — nothing to add")
print(f"\n  {_calls['gen']} generations, {_calls['critic']} critic passes, "
      f"{len(q.pending())} survived")

h("5. YOU DECIDE  (telegram ✅ / github /edit)")
pend = q.pending()
decisions = []
if len(pend) >= 1:
    decisions.append(Decision(pend[0].id, "approve", "telegram", "281152522"))
    ok(f"{pend[0].id} approved on Telegram")
if len(pend) >= 2:
    edited = ("Anecdote struggles here structurally: soft-tissue injuries improve on "
              "their own, so uncontrolled recovery reports can't separate compound from "
              "time. That's the gap trials exist to close.")
    decisions.append(Decision(pend[1].id, "edit", "github", "paulthefree", new_text=edited))
    ok(f"{pend[1].id} edited on GitHub, then approved")
if len(pend) >= 3:
    decisions.append(Decision(pend[2].id, "reject", "telegram", "281152522"))
    no(f"{pend[2].id} rejected — cost $0.00")

prep = PublishReport(); apply_decisions(q, decisions, prep)

h("6. PUBLISH")
publish_approved(q, x, led, prep, max_per_run=cfg.max_publish_per_run, channels=[ch])
for p in prep.published:
    ok(f"{p['action']} → {p['target']}  posted {p['posted']}  "
       f"via {p['via']}{'  (edited)' if p['edited'] else ''}")

h("7. NOSTR MIRROR")
signed = []
class Pool:
    def __init__(self, relays, key, timeout=12): pass
    def publish(self, ev):
        signed.append(ev); return [N.PublishResult("wss://nos.lol", True)]
N.RelayPool = Pool
os.environ["NOSTR_NSEC"] = "0000000000000000000000000000000000000000000000000000000000000003"
mirror_to_nostr(q, cfg, prep)
for r in prep.nostr:
    if r.get("skipped"): dim(f"{r['draft']}: skipped — {r['skipped']}")
    else: ok(f"{r['draft']} → nostr {r['event'][:16]}…  "
             f"{r['relays_ok']}/{r['relays_total']} relays  $0.00")
from coincurve import PublicKeyXOnly
for ev in signed:
    v = PublicKeyXOnly(bytes.fromhex(ev.pubkey)).verify(bytes.fromhex(ev.sig),
                                                        bytes.fromhex(ev.id))
    dim(f"signature verifies: {v}")

h("8. THE MONEY")
rows = {}
for e in led.entries:
    rows.setdefault(e.action, [0, Decimal("0")])
    rows[e.action][0] += e.units; rows[e.action][1] += Decimal(e.cost)
print(f"  {'action':22s} {'units':>6s} {'cost':>9s}")
for a,(u,c) in sorted(rows.items(), key=lambda kv: -kv[1][1]):
    print(f"  {a:22s} {u:>6d} ${c:>8.4f}")
print(f"  {'':22s} {'':>6s} {'─'*9}")
print(f"  {C['b']}{'TOTAL THIS RUN':22s} {'':>6s} ${led.day_spend():>8.4f}{C['x']}")
print(f"\n  {led.summary()}")
# Naively multiplying this run by 4 is wrong twice over: the list read has a
# 1/day quota, and re-reading the same mentions is free inside the 24h window.
reads_once = sum(Decimal(e.cost) for e in led.entries if e.action.startswith("read"))
writes_used = sum(1 for e in led.entries if e.action in ("reply","quote","post"))
writes_left = max(0, cfg.quotas.reply - writes_used)
projected_day = led.day_spend() + Decimal("0.015") * writes_left
print(f"\n  {C['d']}projection (models the quotas, not a naive ×4):{C['x']}")
print(f"    runs 2-4 today: mentions + own posts re-read {C['g']}free{C['x']} "
      f"(24h dedup), list read quota already spent")
print(f"    at most {writes_left} more repl{'y' if writes_left==1 else 'ies'} today "
      f"= ${Decimal('0.015')*writes_left}")
print(f"    {C['b']}full day ≈ ${projected_day:.4f}  →  "
      f"${projected_day*31:.2f}/month{C['x']}  of ${cfg.budget.monthly_usd}")
print(f"\n  writes billed: {len(x.writes)} × $0.015 = ${Decimal('0.015')*len(x.writes)}")
print(f"  refused/skipped/rejected: {len(report.rejected)+len(report.skipped)+1} "
      f"× $0.00")
