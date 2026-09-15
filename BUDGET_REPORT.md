# ROX Test — Budget & Cost Report

**Purpose:** what each piece of the pipeline costs (or doesn't), why we use it,
and a real estimate for a typical campaign.

No tech-stack or architecture here — just money. All prices are USD, current as
of September 2026, and re-checked against each provider's pricing pages where
public.

---

## 1. What we pay for, service by service

### 1.1 Lead discovery — OpenOutreach (`openoutreach` / `outfind`)
**Cost: FREE · What we pay: $0**

- We run OpenOutreach's **discovery + qualification stage only** (the `find N`
  command, **without** `--emails`).
- The tool is open-source (GPLv3). It searches a licensed lead-data source,
  and **searching that index is billed nothing** — OpenOutreach's own docs say
  discovery is "free, cannot spend."
- We **never** invoke its paid email-resolution stage, and in the ROX app we
  **never** call its sending/mailbox stage either (SMTP is out of scope here).

### 1.2 BetterContact — data/lead verification provider
**Cost: FREE for what we use · What we pay: $0**

- BetterContact powers OpenOutreach's **Lead Finder discovery**, and per
  BetterContact/OpenOutreach docs, **Lead Finder searches cost nothing**.
- BetterContact only charges a **credit when an actual email/phone is
  verified** (1 credit = 1 verified email). We do **not** resolve emails
  through BetterContact in ROX — that job went to Contact Compass instead.
- Free signup gives 40–50 credits and needs no card; paid tiers exist
  ($15/mo for 200 credits, $49/mo for 1,000) but are **not required** for ROX.

### 1.3 LinkedIn profile data — HarvestAPI (`harvestapi/linkedin-profile-scraper` on Apify)
**Cost: $4 per 1,000 profiles ($0.004 each) · Pay-per-event**

- This is the only genuinely **paid per-profile** part of the pipeline.
- We deliberately use the **"Profile details no email"** mode = **$4 per 1k**.
  The email-search mode would be **$10 per 1k** — but ROX gets emails from
  Contact Compass instead, so we pay the cheaper rate and avoid double-paying.
- No separate Apify platform/compute charge: the actor bills **only the fixed
  event price**.
- Apify's free account includes **$5 of platform credit per month** for usage,
  which already covers roughly **1,250 profiles** with nothing billed to a card.

### 1.4 Email lookup — Contact Compass (`api.contactcompass.io`)
**Cost: FREE for the first 20,000 lookups · paid after**

- A direct HTTP call to **Contact Compass** returns name + **verified** email
  (`email_status: "verified"`) for a LinkedIn public id.
- Signup includes **20,000 free lookups** — enough for **200 campaigns of
  100 people** before a single dollar is spent.
- Beyond the free pool, you move to a paid plan (pricing is account-specific;
  no public rate card on contactcompass.io) — but for 100 people 2–3×/month
  you will likely stay inside the free allowance for a long time.

### 1.5 Groq LLM — model `openai/gpt-oss-120b`
**Cost: pay-per-token, pennies per campaign**

| Token type | Price |
|---|---|
| Input | $0.15 per 1M tokens |
| Output | $0.60 per 1M tokens |
| Cached input | $0.075 per 1M tokens |

Groq is used in **three places**:
1. **Targeting criteria** (1 short call per campaign) — ~nothing.
2. **OpenOutreach's internal qualification** — the LLM reads candidate
   profiles and decides who is a fit. This is the biggest but still tiny cost
   (see estimate below).
3. **Personalized email writing** (1 call per lead, ~$0.0005–0.001 each).

---

## 2. Summary table — paid vs free

| Service | Used for | Free? | Paid rate (if applicable) | What ROX actually pays |
|---|---|---|---|---|
| OpenOutreach | find + qualify leads | ✅ free | — | **$0** |
| BetterContact | lead discovery data | ✅ free (search) | credits only on verified email | **$0** |
| HarvestAPI profile-scraper | LinkedIn profile data | ❌ | **$4 / 1k** profiles (no-email mode) | **~$0.40 per 100** |
| Contact Compass | find verified email | ⚠️ first 20k free | account plan after | **$0** (within free tier) |
| Groq `gpt-oss-120b` | targeting, qualification, email writing | ❌ | **$0.15/M in · $0.60/M out** | **~$0.15–0.50 per 100** |
| Streamlit + SQLite | UI / storage | ✅ | — | **$0** |

**Bottom line: every stage is free or pennies. Nothing in ROX requires a
subscription to run your volume.**

---

## 3. Your real scenario: 100 people per event, 2–3 times per month

The ask: **search 100 people via OpenOutreach, get all their info, find their
emails, and draft a personalized email for each.**

### 3.1 Per-event cost (100 people)

| Step | Quantity | Unit price | Cost |
|---|---|---|---|
| Lead discovery + qualification (OpenOutreach/BetterContact) | 100 leads | free | **$0.00** |
| LinkedIn profiles (HarvestAPI, no-email mode) | 100 profiles | $0.004 / profile | **$0.40** |
| Verified emails (Contact Compass) | 100 lookups | free within 20k | **$0.00** |
| Groq — targeting | 1 call | ~$0.001 | **~$0.00** |
| Groq — qualification inside OpenOutreach | ~100–500 short LLM calls, ~0.5M tokens total | ~$0.15–0.25 | **~$0.20** |
| Groq — 100 personalized emails | 100 calls | ~$0.0007 each | **~$0.07** |
| **TOTAL per event (100 people)** | | | **≈ $0.65 – $0.75** |

Round it: **under $1 per event of 100 people.** Realistically $0.70–$0.80.

### 3.2 Per month (2–3 events of 100 people each)

| Usage | Events | Cost |
|---|---|---|
| 2 events/month | 200 people | **≈ $1.50** |
| 3 events/month | 300 people | **≈ $2.25** |

Even being generous with the LLM estimate, you're looking at **$1–3 per month**
for 200–300 fully-researched leads with verified emails and drafted emails.

### 3.3 Why it stays so cheap

- **Discovery and qualification are free** (the only "expensive" raw material —
  trying lots of candidates — costs nothing).
- **HarvestAPI is cheap at $4/1k**, and we skip its $10/1k email mode.
- **Contact Compass emails are free** inside the 20k-lookup allowance.
- **Groq is the only usage-metered bill**, and a full 100-people campaign uses
  well under a million tokens (~$0.20–0.30).
- Apify's **free $5/month platform credit** already absorbs the $0.40 profile
  bill, so in practice the first ~1,250 profiles of each month are **$0**.

---

## 4. What COULD change the number (watch-outs)

| Factor | Effect on cost |
|---|---|
| **Contact Compass free pool runs out** (after 20k lookups) | Each email starts costing — plan price is account-specific, so check your dashboard before relying on it beyond the free tier. |
| **HarvestAPI volume across 1,250/mo** | After Apify's $5 free credit is used, profiles bill at $4/1k exactly. 300 people/mo = ~$1.20, still tiny. |
| **OpenOutreach qualification chatter** | More candidates scanned = more Groq tokens. Measured in tests it stayed under ~$0.30 for 100 qualified leads; if you scan far more than you keep, this is the variable that moves. |
| **Adding email sending (SMTP)** | Drafting is free; **sending volume/cold-email sender infra** (e.g. a warm domain, sending service) is the next real cost if you go there. Not part of ROX today. |
| **Switching models** | Every model has its own rate; `openai/gpt-oss-120b` was chosen for being available + cheap on your Groq account. |

---

## 5. The one-line answer

> **~$0.70–0.75 per event (100 people), i.e. roughly $1.50–2.50 a month for
> 2–3 events — and effectively $0 until you exceed Apify's free credit and
> Contact Compass's 20k free lookups.** The only real bill is HarvestAPI at
> $4/1k profiles + a few cents of Groq tokens, and both are covered by free
> allowances at your volume.