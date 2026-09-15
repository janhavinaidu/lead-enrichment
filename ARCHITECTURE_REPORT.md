# ROX Test — Complete Architecture & Development Report

> A functional prototype that finds qualified B2B leads, enriches them with
> LinkedIn profiles and verified emails, and writes personalized outreach
> emails — end to end.
>
> Stack: **Streamlit UI + Python 3.13 + SQLite**, orchestrated around four
> external services: **Groq (LLM)**, **OpenOutreach/OpenOutFind (lead
> discovery)**, **Apify HarvestAPI (LinkedIn profiles)**, and **Contact Compass
> (email lookup)**.

---

## Table of Contents

1. [Problem Statement](#1-problem-statement)
2. [Solution Overview](#2-solution-overview)
3. [System Architecture](#3-system-architecture)
   - 3.1 High-level flow diagram
   - 3.2 Component responsibility table
4. [Project Structure](#4-project-structure)
5. [Detailed Pipeline (Stage by Stage)](#5-detailed-pipeline-stage-by-stage)
   - Stage 1 — User Input
   - Stage 2 — Groq Targeting
   - Stage 3 — OpenOutreach Discovery
   - Stage 4 — Lead Selection
   - Stage 5 — HarvestAPI Profile Enrichment
   - Stage 6 — Contact Compass Email Lookup
   - Stage 7 — Groq Personalized Email
   - Stage 8 — Review / Approve
   - Stage 9 — Actor 1 legacy search stage (removed)
6. [Every External Service — What, Why, Where](#6-every-external-service--what-why-where)
7. [Environment Configuration (`.env`)](#7-environment-configuration-env)
8. [Data Model (SQLite)](#8-data-model-sqlite)
9. [What Was Done & Changed (Development History)](#9-what-was-done--changed-development-history)
   - 9.1 Environment & setup fixes
   - 9.2 Service-layer developments
   - 9.3 Debugging & fixes timeline
10. [What Has Been Tested / Verified](#10-what-has-been-tested--verified)
11. [Known Limitations & Next Steps](#11-known-limitations--next-steps)
12. [Cost Considerations](#12-cost-considerations)

---

## 1. Problem Statement

**Goal:** For an event + target market, produce a list of *relevant, qualified*
professionals along with their LinkedIn profiles, **verified work emails**, and
a **personalized outreach email** — without manual research.

**Complications discovered during development:**

| Problem | How it surfaced |
|---|---|
| The original LLM model caused 404 errors | `llama-3.3-70b-versatile` no longer exists on Groq |
| OpenOutreach would not install | Old venv was Python 3.10; OpenOutreach requires ≥ 3.11 |
| OpenOutreach reported onboarding incomplete | Required `OPENOUTFIND_*` env vars incl. legal notice `'true'` |
| The LLM inside OpenOutreach received file *paths*, not content | Anchor generation failed ("I'm missing the actual content of the files...") |
| Apify rejected program input | Missing / wrong `profileScraperMode` enum strings |
| Contact Compass was unreachable | Wrong auth header name and a stale API key |
| Email approaches compared & discarded | GitHub email scraping and actor-passed ContactCompass fell out of favor |

The final architecture resolves each of these (see [§9](#9-what-was-done--changed-development-history)).

---

## 2. Solution Overview

A single Streamlit app orchestrates a 7-stage pipeline. Minimizing *paid*
usage was a design goal:

- **OpenOutreach** runs only its **free `find` stage** (never `--emails`).
- **HarvestAPI** runs the cheapest profile-only mode (`$4 per 1k`).
- **Contact Compass** supplies emails (user has free lookups).
- **Groq** writes both targeting criteria and personalized emails.

Selected leads (max **4** at a time) go through profile enrichment + email
lookup + email generation. Emails are **reviewed and approved in-app**; actual
**SMTP sending is intentionally not connected** in this prototype.

---

## 3. System Architecture

### 3.1 High-level flow diagram

```
┌───────────────────────────────────────────────────────────────────────────┐
│                           ROX Test (Streamlit app.py)                      │
│                                                                           │
│  ┌──────────┐   ┌───────────┐   ┌────────────────────────────┐            │
│  │  STAGE 1 │   │  STAGE 2  │   │         STAGE 3            │            │
│  │  User     │──▶│  Groq     │──▶│  OpenOutreach (free find)  │            │
│  │  Input    │   │ targeting │   │  │ env: OPENOUTFIND_*      │            │
│  │ (event+   │   │ criteria  │   │  └─ produces qualified     │            │
│  │  market)  │   │           │   │     leads (CSV/JSON)       │            │
│  └──────────┘   └───────────┘   └──────────┬──────────────────┘            │
│                                            ▼                              │
│                               leads persisted → SQLite  ──────────────┐   │
│                                            │                          │   │
│                                            ▼                          │   │
│                              ┌──────────────────────────┐             │   │
│                              │  STAGE 4: user selects   │             │   │
│                              │  up to 4 leads (or       │◀────────────┘   │
│                              │  "Resume saved campaign" │  (rox.db)       │
│                              └───────────┬──────────────┘                 │
│                                          ▼                                │
│                              ┌──────────────────────────┐                 │
│  STAGE 5                    │  STAGE 6                 │  STAGE 7         │
│  harvestapi/…-profile-        │  Contact Compass         │  Groq            │
│  scraper (per lead)           │  POST …/people/search    │  personalized    │
│  profile details, NO email    │  filter: linkedin_public_│  outreach email  │
│  mode "$4 per 1k"             │  id  → verified email    │  │               │
│      │                        │      │                   │                 │
│      ▼                        ▼      ▼                   ▼                 │
│  EnrichedProfile ──────────▶ email ─▶ profile_to_text ──▶ EmailDraft       │
│      │                                                                     │
│      ▼                                                                     │
│  STAGE 8: Review / approve emails (SMTP later, not connected now)          │
└───────────────────────────────────────────────────────────────────────────┘

Services (external):
  Groq API        ← targeting instructions, personalized email (gpt-oss-120b)
  OpenOutreach CLI← free lead discovery + qualification (BetterContact)
  Apify API       ← HarvestAPI profile-scraper actor
  Contact Compass ← email lookup by LinkedIn public id
```

### 3.2 Component responsibility table

| Component | Responsibility | Key entry point |
|---|---|---|
| `app.py` | Streamlit UI, session state, orchestration of all stages | run `streamlit run app.py` |
| `services/groq_service.py` | Two LLM tasks: targeting criteria + personalized email | `generate_targeting_instructions()`, `generate_personalized_email()` |
| `services/openoutreach_service.py` | Wraps the OpenOutreach CLI (subprocess), builds env, parses JSONL/CSV | `find_leads()` |
| `services/apify_service.py` | Runs Apify actors, normalizes actor output into `EnrichedProfile` | `run_apify_profile_actor()`, `normalize_profile()` |
| `services/contact_compass_service.py` | Direct REST email lookup from a LinkedIn URL | `find_email_by_linkedin()` |
| `services/email_service.py` | **Placeholder** SMTP sender | `send_email()` (raises NotImplementedError) |
| `database/db.py` | SQLite persistence: campaigns, leads, enrichment, emails | `insert_leads()`, `latest_campaign()` … |
| `models/schemas.py` | Dataclasses shared across the app | `Lead`, `EnrichedProfile`, `EmailDraft` … |

---

## 4. Project Structure

```
testing_mvp/
├── app.py                        # Streamlit UI + pipeline orchestration
├── requirements.txt              # Python dependencies
├── .env / .env.example           # API keys + configuration (see §7)
├── product_docs.md               # Our product description → OpenOutreach
├── rox.db                        # SQLite DB (created at runtime)
├── README.md                     # Setup guide
├── PLAN.md                       # Original build plan
├── models/
│   ├── schemas.py                # Lead, Campaign, EnrichedProfile, EmailDraft …
├── services/
│   ├── groq_service.py           # Groq LLM integration
│   ├── openoutreach_service.py   # OpenOutreach CLI wrapper
│   ├── apify_service.py          # Apify actor runner + normalizer
│   ├── contact_compass_service.py# Contact Compass REST email lookup
│   └── email_service.py          # SMTP placeholder
└── database/
    └── db.py                     # SQLite persistence layer
```

---

## 5. Detailed Pipeline (Stage by Stage)

### Stage 1 — User Input
The user enters an **Event Name** (e.g. `IDA 2026`) and a **Market/Industry**
(e.g. `Automobile`), and picks how many leads to find (10 / 15 / 20).
On submit the app resets all session state for a fresh campaign and proceeds.

Alternatively, a **"Resume last campaign"** button loads the most recent saved
campaign from SQLite — so the expensive OpenOutreach discovery step does **not**
have to be re-run to test later stages.

### Stage 2 — Groq Targeting
`groq_service.generate_targeting_instructions()` sends the event + market to a
Groq LLM (`openai/gpt-oss-120b`) with a system prompt that asks for precise
targeting criteria (roles, industries, attributes). The returning text is shown
to the user for review.

### Stage 3 — OpenOutreach Discovery (free)
- The targeting text is written to a temp `.md` file.
- `openoutreach_service.find_leads()` calls the **free** `find N` CLI command with
  `OPENOUTFIND_*` env vars populated from `.env` + build at runtime
  (file *contents* are read first — see §9.2).
- OpenOutreach internally: generates anchors → finds seed keywords → runs
  **BetterContact** discovery → Groq-qualifies each candidate → prints leads.
- The app parses the JSON-Lines output (falls back to CSV) and builds `Lead`s.
- Leads are persisted to SQLite (`db.insert_leads`) with a campaign id.
- ⚠️ We **never** pass `--emails`, so the paid email stage of OpenOutreach is
  never invoked.

### Stage 4 — Lead Selection
The user sees a table (Person / Company / Title / LinkedIn / Why Relevant) and
may check **up to 4 leads**. Research enriches only the selected leads.

### Stage 5 — HarvestAPI Profile Enrichment (per selected lead)
`apify_service.run_apify_profile_actor(linkedin_url, first_name, last_name)`:

```json
{
  "profileUrls": ["https://www.linkedin.com/in/<slug>"],
  "profile_urls": ["..."],
  "urls": ["..."],
  "includeEmail": false,
  "includeExperience": true,
  "includeEducation": true,
  "includeSkills": true,
  "profileScraperMode": "Profile details no email ($4 per 1k)",
  "firstName": "...",
  "lastName": "..."
}
```

- Runs `harvestapi/linkedin-profile-scraper` (URL-based, cookie-free).
- The mode enum **must include the literal `$`** character — e.g.
  `"Profile details no email ($4 per 1k)"` — or the actor rejects the input.
- Output is normalized by `normalize_profile()` into an `EnrichedProfile`
  (name, headline, about, company, role, location, experience, education,
  skills). **No email** is resolved here.

### Stage 6 — Contact Compass Email Lookup
`contact_compass_service.find_email_by_linkedin(linkedin_url)`:

1. **Extract the public id** from the URL:
   `https://www.linkedin.com/in/john-smith/` → `john-smith`.
2. **POST** to `https://api.contactcompass.io/v1/people/search`:

   ```json
   { "filters": { "linkedin_public_id": "john-smith" } }
   ```

   Auth header: `x-api-token: <CONTACT_COMPASS_API_KEY>`.
3. **Parse the response** (shape `{"success":true,"result":{"people":[…]}}`):

   ```json
   {
     "name": "Satya Nadella",
     "email": "satya@uchicago.edu",
     "title": "Member Board of Trustees",
     "linkedin_url": "http://www.linkedin.com/in/satyanadella",
     "linkedin_public_id": "satyanadella",
     "email_status": "verified"
   }
   ```

   The parser recursively scans plain top-level fields, `result.people[]`, and
   nested containers for the first plausible address, returning `(email, status)`.
4. The found email is attached to the `EnrichedProfile`.

### Stage 7 — Groq Personalized Email
`groq_service.generate_personalized_email(...)` is given the event, market,
lead name/company/title, relevance reason, the profile rendered as text
(`profile_to_text`), and the found email. The LLM (temperature 0.7) returns
`{"subject": "...", "body": "..."}` which is parsed with JSON fallback logic.
The draft is stored and persisted to SQLite as `pending`.

### Stage 8 — Review / Approve
Each draft shows **To / Subject / Body**. The user can:
- **Regenerate** (re-call Groq), or
- **Approve** (mark `approved` in DB).

Sending is a **disabled placeholder**: SMTP is intentionally not connected
(`email_service` raises `NotImplementedError`).

### Stage 9 — Actor 1 search stage (REMOVED)
Originally the pipeline ran a second Apify actor (`run_apify_search_actor`,
`harvestapi/linkedin-profile-search-by-name`) *before* the profile actor. This
was removed per the final design — the profile-scraper actor alone covers
profile data. The function still exists in `apify_service.py` but is **not
called** by the app anymore.

---

## 6. Every External Service — What, Why, Where

| Service | Used For | Where (stage) | Key config | Cost note |
|---|---|---|---|---|
| **Groq** | 1) convert event+market → targeting criteria; 2) write personalized outreach emails | Stage 2, Stage 7 | `GROQ_API_KEY`, `GROQ_MODEL=openai/gpt-oss-120b` | LLM tokens |
| **OpenOutreach** (`openoutreach`/`outfind` CLI) | Free lead discovery + AI qualification (anchors → keywords → BetterContact → LLM qualify) | Stage 3 | `OPENOUTFIND_*` §7 | Free discovery (we never run its paid email stage) |
| **BetterContact** | Lead-level discovery/verification *inside* OpenOutreach (free stage only) | Stage 3 | `OPENOUTFIND_BETTERCONTACT_API_KEY` / `BETTERCONTACT_API_KEY` | FREE |
| **Apify + HarvestAPI** | Deep LinkedIn profile enrichment (experience, education, skills, etc.), URL-based, no cookies | Stage 5 | `APIFY_API_TOKEN`, `APIFY_PROFILE_ACTOR_ID=harvestapi/linkedin-profile-scraper` | ~$0.10/page + $4 per 1k (profile-only mode) |
| **Contact Compass** | Verified work-email lookup from a LinkedIn URL | Stage 6 | `CONTACT_COMPASS_API_KEY` (header `x-api-token`) | User's free lookups (20k) |
| **Streamlit** | Entire UI + orchestration | all | n/a | n/a |
| **SQLite** | Persist campaigns, leads, enrichment, emails | all | `rox.db` | n/a |

> **Why email is split from profile enrichment:** HarvestAPI's built-in email
> search and Contact Compass were compared early on. Contact Compass won for
> email because it runs as a direct REST call (no extra actor), validates the
> email (`email_status: verified`), and uses the user's own lookups. This is also
> why the profile actor runs in **no-email** mode — `$4 per 1k` instead of the
> more expensive `$10 per 1k` email mode. See [§9.3](#93-debugging--fixes-timeline).

---

## 7. Environment Configuration (`.env`)

| Variable | Purpose |
|---|---|
| `GROQ_API_KEY` | Groq LLM API key |
| `GROQ_MODEL` | Groq model (must be a valid model id on the account) |
| `APIFY_API_TOKEN` | Apify API token |
| `BETTERCONTACT_API_KEY` | BetterContact key (OpenOutreach discovery) |
| `CONTACT_COMPASS_API_KEY` | Contact Compass token — sent as `x-api-token` header |
| `OPENOUTFIND_PRODUCT_DOCS` | Our product description (path to `product_docs.md`) — contents are **read into the env var** by the app |
| `OPENOUTFIND_CAMPAIGN_TARGET` | Targeting text — set at runtime from the Groq output file |
| `OPENOUTFIND_AI_MODEL` | `groq:openai/gpt-oss-120b` (LLM backend for OpenOutreach) |
| `OPENOUTFIND_LLM_API_KEY` | Groq key used by OpenOutreach as its LLM |
| `OPENOUTFIND_BETTERCONTACT_API_KEY` | BetterContact key for OpenOutreach |
| `OPENOUTFIND_OPERATOR_EMAIL` | Operator email (legal/abuse contact) |
| `OPENOUTFIND_OPERATOR_COUNTRY` | `IN` |
| `OPENOUTFIND_ACCEPT_LEGAL_NOTICE` | must be the **string** `true` or onboarding stays incomplete |
| `APIFY_SEARCH_ACTOR_ID` | `harvestapi/linkedin-profile-search-by-name` (no longer called by the app) |
| `APIFY_PROFILE_ACTOR_ID` | `harvestapi/linkedin-profile-scraper` (the active profile actor) |
| `APIFY_PROFILE_MODE` *(optional)* | Overrides profile mode default `Profile details no email ($4 per 1k)` |

---

## 8. Data Model (SQLite)

Tables created by `database/db.py`:

```
campaign (id, event_name, market, created_at)
lead     (id, campaign_id → campaign.id, name, company, title,
          linkedin_url, relevance_reason)
enrichment (id, lead_id → lead.id, profile_json, email, enriched_at)
email      (id, lead_id → lead.id, subject, body, status, created_at)
```

Notes
- `lead.name` stores the combined full name; `_lead_from_saved()` splits it
  back into first/last for the `Lead` dataclass.
- The most recent campaign is reused by the **Resume last campaign** button.

---

## 9. What Was Done & Changed (Development History)

### 9.1 Environment & setup fixes
- **Python 3.13 venv**: the original venv was 3.10 (pip 21.2.3), which made
  `pip install openoutreach` fail (`Python>=3.11 required`). Recreated the venv
  with `py -3.13`, upgraded pip, installed `requirements.txt` +
  `openoutreach 0.1.56` (bundling `openoutfind`, `openoutsend`) and
  `apify-client>=2.6.0`.
- **`.env` created & populated** from `.env.example` with real keys and the two
  HarvestAPI actor ids.
- **`GROQ_MODEL` changed** from `llama-3.3-70b-versatile` to
  `openai/gpt-oss-120b` (the old model 404'd on the Groq account).
- **`product_docs.md` written** — a realistic product description (conference
  tickets/booths for auto/mobility executives; IDA 2026 example) so OpenOutreach
  can anchor its seed search. **Edit this file** to match the real product.

### 9.2 Service-layer developments
- **`openoutreach_service.py`**: added `_build_env()` + `_read_file_value()` so
  `OPENOUTFIND_PRODUCT_DOCS` / `OPENOUTFIND_CAMPAIGN_TARGET` contain the file's
  **text**, not a path. Root cause of the earlier "I'm still missing the actual
  content of the two files…" LLM failure.
- **`apify_service.py`**: `run_apify_profile_actor()` extended to send the
  superset schema keys plus `profileScraperMode`, `firstName`, `lastName`.
  Contact-Compass-attached email fields were **removed** from the actor input
  (email is now a separate direct API call).
- **`app.py`**: passes `first_name`/`last_name` to the actor; checks BetterContact
  via `OPENOUTFIND_BETTERCONTACT_API_KEY` **or** `BETTERCONTACT_API_KEY`; added
  the Resume-last-campaign flow with a synthetic `OpenOutreachResult`.
- **`contact_compass_service.py`** (new): direct REST email lookup with
  `linkedin_public_id` extraction + response parsing.

### 9.3 Debugging & fixes timeline

| # | Symptom | Root cause | Fix |
|---|---|---|---|
| 1 | `openoutreach` install failed | Python 3.10 venv | Recreated venv with Python 3.13 |
| 2 | `Config: onboarding_incomplete — not ready to find` | `OPENOUTFIND_*` env vars incomplete; legal notice not `'true'` | Filled `.env` incl. `Openoutreach_ACCEPT_LEGAL_NOTICE=true` |
| 3 | Anchor generation failed — LLM saw no file content | env vars contained file **paths** | `_read_file_value()` reads contents into the env vars |
| 4 | Groq 404 on model | retired `llama-3.3-70b-versatile` | switched to `openai/gpt-oss-120b` |
| 5 | `Field input.profileScraperMode is required` (search-by-name) | Actor 1 input lacked the field | added `profileScraperMode` + first/last name to search input |
| 6 | `profileScraperMode must be one of…` (profile-scraper) | string lacked the literal `$` (`($4 per 1k)`), and wrong enum family vs the by-name actor | verified exact enum via Apify `validate_input` → `"Profile details no email ($4 per 1k)"` |
| 7 | Contact Compass `401` on every header | wrong header name **and** stale key | switched to `x-api-token` header + fresh key; confirmed `200` + verified email |
| 8 | `NameError: Dict`, `AttributeError: 'NoneType'…exit_code`, `Lead(name=…)` errors (resume path) | resume path didn't rebuild the synthetic result / dataclass field mismatch | added `_lead_from_saved()`, synthetic `OpenOutreachResult`, typing imports |
| 9 | Profile + email both failed repeatedly across actors | two-actor design was fragile & costly | **removed Actor 1**, unified on `…profile-scraper` (no email) + direct Contact Compass |

---

## 10. What Has Been Tested / Verified

✅ **Compilation** — `py_compile` passes for `app.py` and every service module.

✅ **App boot** — `streamlit run app.py` starts and renders on localhost.

✅ **API status sidebar** — all four integrations report connected/configured.

✅ **OpenOutreach** — `openoutreach status` → *Configuration: complete*;
  discovery run **completed successfully** end-to-end and returned qualified
  leads (user-confirmed).

✅ **Groq** — targeting-instruction generation works with `openai/gpt-oss-120b`.

✅ **Resume last campaign** — saved leads reload from `rox.db` without re-running
  discovery.

✅ **Apify profile actor** — input validated by Apify `validate_input`
  (`"Profile details no email ($4 per 1k)"` accepted); actor runs succeed.

✅ **Contact Compass** — live call with fresh key returned `200`:
  `satyanadella → satya@uchicago.edu` (status `verified`).

✅ **Profile naming / enrichment loop** — enrichment stage completes with
  profile + email combined into `EnrichedProfile`.

⚠️ **Not yet exercised end-to-end in one UI session** — the delivery path
  Groq email generation for the newly wired profile+email combination should be
  re-run once in the UI to confirm email text renders (expected to work — it
  uses the same `groq_service` already validated).

---

## 11. Known Limitations & Next Steps

| Limitation | Status / Next step |
|---|---|
| **SMTP sending** | Not implemented (`email_service.send_email` raises). Add SMTP (e.g. SMTP2Go / provider of choice) and enable the Send Email button. |
| **Actor 1 code remains** | `run_apify_search_actor` still exists but is unused — can be deleted for cleanliness. |
| **No UI pagination for leads** | Max 4 selected per research batch. |
| **Email fallback for missing leads** | Leads without a LinkedIn URL skip enrichment; Contact Compass may find nothing for some profiles — the UI already says "Email not found". |
| **OpenOutreach sending / OutSend** | Deliberately unused (paid) — own SMTP later. |
| **Retries/backoff** | API failures surface as errors; no automatic retry yet. |
| **Model choice** | `openai/gpt-oss-120b` selected because it is available on the user's Groq account; can be swapped via `GROQ_MODEL`. |
| **Caching API responses** | Not yet — each research click re-calls the actor for re-selected leads. |

**Next milestones (suggested order):**
1. Wire SMTP sender into `email_service` + enable Send button.
2. Remove checklist of unused actor code / keys.
3. Add per-lead retry & better failure surfacing.
4. Export CSV / campaign dashboard view.

---

## 12. Cost Considerations

| Step | Charge basis (approx) |
|---|---|
| OpenOutreach discovery | FREE stage only (BetterContact free) |
| HarvestAPI profile-scraper | ~$0.10 per search page + **$4 per 1k** profiles in no-email mode |
| Contact Compass | 20,000 free lookups (email status: verified) |
| Groq LLM | per-token inference cost on the chosen model |

By routing email to Contact Compass instead of HarvestAPI's email mode
($10 per 1k), and by never enabling OpenOutreach's paid email stage, the
prototype stays near zero-cost for testing.

---

*Report covers the project as of its current state (September 2026). Paths assume
the app runs from the `testing_mvp/` directory.*