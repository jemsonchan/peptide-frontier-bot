# Peptide Frontier — Growth Strategy & Content System

*Prepared as a top-1% growth-strategist brief for @peptidefrontier. Current as of July 2026.*

---

## 1. The 2026 landscape (why this niche, now)

Peptides went fully mainstream in 2025–2026. The numbers that matter:

- Global peptide therapeutics hit **$52.6B in 2025**; roughly **1 in 8 US adults (12.4%)** now take a GLP-1 drug.
- Google searches for *"are peptides safe"* jumped **652% in twelve months** — a demand signal that is fear/uncertainty, not hype.
- **BPC-157** went from a Google-interest score of 8 (Jan 2023) to 100 (Apr 2026) — hockey-stick growth, with hundreds of thousands to millions of self-experimenters.
- Retatrutide's Phase 3 TRIUMPH data (up to ~28–30% body-weight loss) landed, with **seven more Phase 3 readouts due through 2026** (liver disease, sleep apnea, cardiovascular). A steady drip of real, postable news.
- Public sentiment flipped: **53% of Americans now view GLP-1 drugs favorably**, up from 42% in 2024.

The demand is enormous and still accelerating. The question is where the *supply* of good content is thin.

---

## 2. The Semantic Content Vacuum

**A Semantic Content Vacuum** = a sub-topic with a high subscriber-to-supply ratio, where a large, growing audience is actively searching but leaves *unsatisfied* because existing content fails them.

The peptide conversation splits into two failing camps:

- **Hype influencers** — inject on camera, promise glowing skin, dodge moderation by calling retatrutide "reta," "GLP-3," or "ratatouille" to evade drug-promo takedowns. High volume, zero rigor, actively misleading. The FDA issued warning letters (Dec 2024) over exactly this.
- **Academic / clinical sources** — accurate but dense, paywalled, slow, and written for clinicians, not the person holding a vial at their kitchen table.

**The vacuum is the middle.** The single most-searched, least-satisfied beginner question is not "does it work" — it's *"what dose, and how do I not screw up the math?"* Documented failure points:

- **Units ≠ milligrams** — the most misunderstood concept in the entire space.
- **Reconstitution math** — a 10 mg vial in 1 mL vs 3 mL is a completely different concentration, and people dose blind.
- **Bad-faith comparisons** — two people think they're discussing "the same amount" while working with different compounds/concentrations.
- **"Random screenshots and viral posts treated as reliable sources."**

### The vacuum, named: **"Decoded, not hyped."**

Evidence-grade, plain-language peptide education that a smart beginner can actually act on safely — the calm, cited, numerate voice that neither camp provides. Adjacent under-served veins feeding the same vacuum: **mechanism-of-action explainers** (why a triple agonist ≠ a GLP-1), **study-breakdowns within 48h of publication**, and **myth correction** ("no, more does not mean faster").

---

## 3. The Moat

A moat is the durable reason a reader follows *you* and not the next account. For Peptide Frontier it is a stack of four reinforcing advantages:

1. **The Rigor Moat (primary).** Every claim is traceable to a named source — a specific journal, trial, or mechanism — never "studies show." In a field defined by unsourced hype, *citation itself is the differentiator*. This is hard to copy because it's slower and less dopaminergic than hype; most competitors won't do the work.

2. **The Numeracy Moat.** Own the dosing/reconstitution/units math that everyone gets wrong and nobody explains cleanly. Reusable, evergreen, endlessly shareable, and screenshot-worthy — the exact content the vacuum demands.

3. **The Neutrality Moat.** No product, no affiliate links, no vial storefront. You are the trusted referee in a market full of sellers. Credibility compounds; a store would cap it. (This also keeps you clear of X's drug-promo enforcement and the FDA's crosshairs.)

4. **The Cadence Moat.** One genuinely good, novel post every single day. Consistency is a moat because it's operationally hard for humans and trivial for a well-built bot — which is the entire point of this project.

**Positioning line:** *"The signal in the peptide noise. Cited, numerate, neutral. Every day."*

**One-sentence bio (doubles as required bot disclosure):** `Decoding peptide science — cited, numerate, no hype, nothing to sell. Educational only, not medical advice. Automated 🤖`

---

## 4. The Research Pipeline

A five-stage daily pipeline, designed so the *facts come before the writing* (grounding kills hallucination — critical in a medical niche):

**Stage 1 — Topic selection.** Draw from a weighted content-pillar bank (`data/topics.json`) so pillars rotate and never repeat within a window. Five pillars:
- `mechanism` — how a compound actually works
- `numeracy` — dosing, reconstitution, units math
- `myth` — correcting a specific viral misconception
- `study` — breaking down one recent paper
- `landscape` — pipeline/regulatory/market context

One day per week is reserved for a **"Study of the Week"** triggered by live literature (Stage 2).

**Stage 2 — Grounding / retrieval.** Query **Europe PMC** and **PubMed E-utilities** (both free, no paid key) for recent, real papers on the chosen topic. Extract title, journal, year, and key finding. This gives the writer real citations to anchor to — the Rigor Moat, automated.

**Stage 3 — Drafting.** An LLM (Gemini 2.5 Flash — cheap, effectively free at this volume) composes the post using the **system prompt** (§5) plus the retrieved facts. The system prompt enforces voice, structure, and the no-advice/no-promo guardrails.

**Stage 4 — Guardrails / QA (deterministic, in code).** Before anything posts, the draft must pass:
- Length ≤ 275 chars (headroom under X's 280).
- No banned promo/hype tokens (buy, DM me, link in bio, coupon, "glow up," etc.).
- No first-person medical advice ("you should take…"); education framing only.
- Not a near-duplicate of any prior post (`data/posted_history.json`).
- Contains a source attribution when the pillar is `study`.

Fail → regenerate up to N times → if still failing, fall back to a safe templated post or skip (non-fatal, mirroring the weather bot's philosophy).

**Stage 5 — Publish & log.** Post text-only to **X** (avoids the $0.20 link surcharge) and optionally **Nostr** (free, censorship-resistant, on-brand). Append to `posted_history.json` and commit it back to the repo so tomorrow's dedupe sees today's post.

---

## 5. The System Prompt

The full production system prompt lives in [`system_prompt.md`](system_prompt.md) and is loaded by the bot at runtime. It encodes the moat as rules: cited, numerate, neutral, no medical advice, no drug promotion, ≤275 chars, one idea per post, hook-first, with an explicit banned-phrase list and an output contract the guardrail stage validates against.

---

## 6. Tech Stack (cost-efficient)

Mirrors the proven weather-bot architecture — **GitHub Actions cron + Python + tweepy**, zero servers — with the changes 2026 forces:

| Layer | Choice | Why / Cost |
|---|---|---|
| Scheduler / host | **GitHub Actions** (cron) | Free for public repos. Same pattern as weather-bot. No VPS. |
| Language / client | **Python + tweepy** | Identical to weather-bot; reuse `post_to_x`. |
| Grounding data | **Europe PMC + PubMed E-utilities** | Free, no API key required. |
| Writer LLM | **Gemini 2.5 Flash** ($0.30/$2.50 per 1M tok; free AI-Studio tier covers 1 post/day) | ~1–2k tokens/post ⇒ **< $0.01/month**. Swappable via `LLM_PROVIDER`. |
| X posting | **X API pay-per-use**, text-only | $0.015/post × 365 ≈ **$5.50/year**. Links go in a reply to dodge the $0.20 surcharge. |
| 2nd channel (optional) | **Nostr** | Free, permissionless, aligns with your Bitcoin/privacy stance. |
| State / dedupe | **JSON committed to repo** | Free, transparent, no database. |

**All-in run cost: roughly $6/year** (essentially just X post credits), plus the one-time work of X developer onboarding.

### 2026 compliance notes baked into the build
- **Bot labeling is day-one mandatory** — the account bio must identify as automated. Handled by the bio line in §3.
- **AI reply-bots need X's written pre-approval** — so this bot posts *original content only*, never automated replies. Stays inside policy.
- **No drug promotion / sales** — the Neutrality Moat is also the compliance strategy.
- Educational-only framing + "not medical advice" keeps it clear of FDA-style enforcement that hit the sellers.

---

## 7. Success metrics to watch (first 90 days)

Follower growth is a lagging vanity metric; watch these instead: **save/bookmark rate** (the true signal that you filled the vacuum — people save what they'll act on), **profile-visit-to-follow conversion**, **reply quality** (are clinicians/serious users engaging?), and **which pillar earns the most saves** — then reweight `topics.json` toward it. The bot logs every post so this analysis is trivial later.
