"""ROX Test - main Streamlit application.

Functional prototype to verify the ROX lead-generation pipeline end to end:

    Event + Market
        -> Groq targeting
        -> OpenOutreach lead discovery (free stage only)
        -> user selects up to 4 leads
        -> HarvestAPI profile-scraper (profile details, no email)
        -> Contact Compass REST API (email lookup from LinkedIn URL)
        -> Groq personalized email
        -> review / approve (SMTP not connected yet)
"""

import hashlib
import json
import os
import tempfile
from typing import Any, Dict, List

import streamlit as st
from dotenv import load_dotenv

from database import db
from models.schemas import Lead, OpenOutreachResult, TargetingInstructions
from services import (
    apify_service,
    contact_compass_service,
    email_service,
    groq_service,
    linkedin_finder_service,
    openoutreach_service,
)

load_dotenv()
db.init_db()

st.set_page_config(page_title="ROX Test", page_icon="🚀", layout="wide")

MAX_SELECTION = 4

# ---------------------------------------------------------------------------
# Session state defaults
# ---------------------------------------------------------------------------
st.session_state.setdefault("event_name", "")
st.session_state.setdefault("market", "")
st.session_state.setdefault("num_leads", 15)
st.session_state.setdefault("phase", "input")            # input | targeting | results
st.session_state.setdefault("targeting", None)
st.session_state.setdefault("targeting_file", None)
st.session_state.setdefault("oo_result", None)
st.session_state.setdefault("leads", [])
st.session_state.setdefault("campaign_id", None)
st.session_state.setdefault("lead_db_ids", [])
st.session_state.setdefault("enrichments", {})           # lead index -> EnrichedProfile
st.session_state.setdefault("email_drafts", {})          # lead index -> EmailDraft
st.session_state.setdefault("approved", set())
st.session_state.setdefault("search_ran", False)
st.session_state.setdefault("emails_generated", False)
st.session_state.setdefault("emails_searched", False)
st.session_state.setdefault("discovery_method", "Fast (LLM + Google Search)")
st.session_state.setdefault("product", "")


def debug_expander(title: str, content: str):
    """Render a debug expandable section."""
    with st.expander(title):
        st.code(content)


def _lead_from_saved(row: Dict[str, Any]) -> Lead:
    """Restore a Lead from a DB row (they store a combined name)."""
    full_name = (row.get("name") or "").strip()
    first, _, last = full_name.partition(" ")
    return Lead(
        first_name=first,
        last_name=last,
        company=row.get("company") or "",
        title=row.get("title") or "",
        website=row.get("website") or "",
        linkedin_url=row.get("linkedin_url") or "",
        reason=row.get("relevance_reason") or "",
        email=row.get("email") or "",
    )


# ---------------------------------------------------------------------------
# Cached operations (LLM search, Apify profile enrichment, Contact Compass)
# ---------------------------------------------------------------------------
def _discovery_cache_key(event_name: str, market: str, product: str, num_leads: int) -> str:
    """Stable key for the persistent discovery cache (per product/market)."""
    raw = "|".join(
        [
            (event_name or "").strip().lower(),
            (market or "").strip().lower(),
            (product or "").strip().lower(),
            str(num_leads),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _lead_to_dict(lead: Lead) -> Dict[str, str]:
    return {
        "first_name": lead.first_name,
        "last_name": lead.last_name,
        "company": lead.company,
        "title": lead.title,
        "website": lead.website,
        "linkedin_url": lead.linkedin_url,
        "reason": lead.reason,
        "email": lead.email,
        "lead_id": lead.lead_id,
    }


@st.cache_data(show_spinner=False)
def cached_find_leads(event_name: str, market: str, num_leads: int, product: str) -> List[Lead]:
    """Discover leads, saved to SQLite so repeat searches for the same
    product / event / market load instantly instead of re-running the search."""
    cache_key = _discovery_cache_key(event_name, market, product, num_leads)

    cached = db.get_discovery_cache(cache_key)
    if cached:
        try:
            rows = json.loads(cached["leads_json"])
            if isinstance(rows, list):
                return [Lead.from_dict(r) for r in rows]
        except (json.JSONDecodeError, TypeError):
            pass

    leads = linkedin_finder_service.find_leads(
        event_name=event_name,
        market=market,
        num_leads=num_leads,
        product=product,
    )

    try:
        if leads:
            db.save_discovery_cache(
                cache_key,
                event_name or "",
                market or "",
                product or "",
                num_leads,
                json.dumps([_lead_to_dict(l) for l in leads], default=str),
            )
    except Exception:
        pass  # caching must never break the actual search flow

    return leads


@st.cache_data(show_spinner=False, ttl=86400)
def cached_apify_profile_actor(linkedin_url: str, first_name: str = "", last_name: str = ""):
    """Cache Apify profile enrichment per LinkedIn URL."""
    return apify_service.run_apify_profile_actor(
        linkedin_url,
        first_name=first_name,
        last_name=last_name,
    )


@st.cache_data(show_spinner=False, ttl=86400)
def _cached_contact_compass_lookup(
    linkedin_url: str,
    first_name: str = "",
    last_name: str = "",
    company: str = "",
    domain: str = "",
    max_lookups: int = 7,
    timeout_seconds: int = 15,
):
    """Cache Contact Compass email lookups per LinkedIn URL and person details."""
    return contact_compass_service.find_email_by_linkedin(
        linkedin_url,
        first_name=first_name,
        last_name=last_name,
        company=company,
        domain=domain,
        max_lookups=max_lookups,
        timeout_seconds=timeout_seconds,
    )


def cached_contact_compass_lookup(
    linkedin_url: str,
    first_name: str = "",
    last_name: str = "",
    company: str = "",
    domain: str = "",
    max_lookups: int = 7,
    timeout_seconds: int = 15,
):
    """Look up an email, caching only successful hits.

    Failed (empty) lookups are re-tried live on every run so a stale 24h cache
    miss never hides an email that a later attempt manages to find.
    """
    email, status, raw = _cached_contact_compass_lookup(
        linkedin_url,
        first_name=first_name,
        last_name=last_name,
        company=company,
        domain=domain,
        max_lookups=max_lookups,
        timeout_seconds=timeout_seconds,
    )
    if email:
        return email, status, raw
    return contact_compass_service.find_email_by_linkedin(
        linkedin_url,
        first_name=first_name,
        last_name=last_name,
        company=company,
        domain=domain,
        max_lookups=max_lookups,
        timeout_seconds=timeout_seconds,
    )


debug = False


# ---------------------------------------------------------------------------
# Sidebar & Cache Management
# ---------------------------------------------------------------------------
with st.sidebar:
    st.subheader("Cache Controls")
    if st.button("Clear Search & API Cache", type="secondary"):
        st.cache_data.clear()
        db.clear_discovery_cache()
        st.success("Cache cleared successfully!")

# ---------------------------------------------------------------------------
# Page header
# ---------------------------------------------------------------------------
st.title("ROX")
st.caption("Lead Generation & Enrichment Test")
st.divider()

# ---------------------------------------------------------------------------
# 1. Input
# ---------------------------------------------------------------------------
st.subheader("Find Relevant Leads")

# Resume last saved campaign (skip re-running OpenOutreach discovery).
if st.session_state.get("phase") == "input":
    last_campaign = db.latest_campaign()
    if last_campaign and st.session_state.campaign_id is None:
        saved_leads = db.get_leads(last_campaign["id"])
        if saved_leads:
            resume_col, _ = st.columns([1, 3])
            with resume_col:
                if st.button(
                    f"Resume last campaign: {last_campaign['event_name']} / "
                    f"{last_campaign['market']} ({len(saved_leads)} saved leads)"
                ):
                    st.session_state.campaign_id = last_campaign["id"]
                    st.session_state.event_name = last_campaign["event_name"]
                    st.session_state.market = last_campaign["market"]
                    st.session_state.leads = [
                        _lead_from_saved(s)
                        for s in saved_leads
                    ]
                    st.session_state.lead_db_ids = [s["id"] for s in saved_leads]
                    st.session_state.oo_result = OpenOutreachResult(
                        leads=st.session_state.leads,
                        command="(resumed from saved campaign)",
                        stdout="",
                        stderr="",
                        exit_code=0,
                    )
                    st.session_state.phase = "results"
                    st.rerun()

with st.form("lead_search_form"):
    event_name = st.text_input("Event Name:", placeholder="e.g. IDA 2026")
    market = st.text_input("Market / Industry:", placeholder="e.g. Automobile")
    product = st.text_input(
        "Product / Solution (optional):",
        placeholder="e.g. electric car, CRM software, industrial sensors",
        help="If filled, the LLM will find companies that buy or use this product and target their decision-makers.",
    )
    num_leads = st.selectbox("Number of leads:", options=[10, 15, 20], index=1)
    discovery_method = st.radio(
        "Discovery Method",
        options=["Fast (LLM + Google Search)", "OpenOutreach (slow)"],
        index=0,
        horizontal=True,
        help="Fast uses AI search to locate LinkedIn URLs directly. The pipeline option is slower but runs the full discovery workflow.",
    )
    find_leads_clicked = st.form_submit_button("Find Leads", type="primary")

if find_leads_clicked:
    _has_product = bool(product.strip())
    _has_event_market = bool(event_name.strip()) and bool(market.strip())
    if not _has_product and not _has_event_market:
        st.error("Please fill in either the Product / Solution, or both Event Name and Market / Industry.")
    else:
        st.session_state.event_name = event_name.strip()
        st.session_state.market = market.strip()
        st.session_state.product = product.strip()
        st.session_state.num_leads = num_leads
        st.session_state.discovery_method = discovery_method
        st.session_state.targeting = None
        st.session_state.oo_result = None
        st.session_state.leads = []
        st.session_state.campaign_id = None
        st.session_state.lead_db_ids = []
        st.session_state.enrichments = {}
        st.session_state.email_drafts = {}
        st.session_state.approved = set()
        st.session_state.search_ran = False
        st.session_state.emails_generated = False
        st.session_state.emails_searched = False
        st.session_state.phase = "input"

        if not groq_service.has_api_key():
            st.error("Groq API key is missing. Add GROQ_API_KEY to your .env file.")
        elif st.session_state.discovery_method == "Fast (LLM + Google Search)":
            # Fast path: LLM + Parallel Google / DDG search — skip targeting review step,
            # run discovery immediately and jump straight to results.
            with st.spinner(
                f"Finding leads for **{event_name.strip()}** / **{market.strip()}** "
                f"— orchestration agent is working in the background…"
            ):
                try:
                    fast_leads = cached_find_leads(
                        event_name=st.session_state.event_name,
                        market=st.session_state.market,
                        num_leads=st.session_state.num_leads,
                        product=st.session_state.product,
                    )
                    from models.schemas import OpenOutreachResult
                    st.session_state.oo_result = OpenOutreachResult(
                        leads=fast_leads,
                        command="(LLM + Parallel Google Search)",
                        stdout="",
                        stderr="",
                        exit_code=0,
                    )
                    st.session_state.leads = fast_leads
                    st.session_state.phase = "results"
                    st.rerun()
                except RuntimeError as exc:
                    st.error(f"Fast lead discovery failed:\n\n{exc}")
        else:
            # OpenOutreach path: generate targeting first, then show review step.
            with st.spinner("Orchestration agent is preparing the targeting strategy..."):
                try:
                    targeting = groq_service.generate_targeting_instructions(
                        st.session_state.event_name, st.session_state.market
                    )
                    st.session_state.targeting = targeting
                    st.session_state.phase = "targeting"
                except RuntimeError as exc:
                    st.error(f"Targeting generation failed:\n\n{exc}")

# ---------------------------------------------------------------------------
# 2. Targeting review + OpenOutreach run
# ---------------------------------------------------------------------------
if st.session_state.get("targeting") is not None:
    targeting: TargetingInstructions = st.session_state.targeting

    st.subheader("Generated Targeting Criteria")
    with st.expander("View targeting instructions", expanded=True):
        st.markdown(targeting.instructions)

    if debug:
        debug_expander("Debug: Groq targeting prompt", targeting.prompt_sent)

    col_a, col_b = st.columns([1, 3])
    with col_a:
        run_search = st.button(
            "Run Lead Search",
            type="primary",
            disabled=st.session_state.phase != "targeting",
            key="run_lead_search_btn",
        )
    with col_b:
        st.caption(
            f"This will run the discovery engine to find "
            f"{st.session_state.num_leads} qualified leads for "
            f"**{st.session_state.event_name}** / **{st.session_state.market}**. "
            f"Switch to the **Fast** method for a much quicker alternative."
        )

    if run_search:
        if not openoutreach_service.is_installed():
            st.error("OpenOutreach is not installed. Install it with: pip install openoutreach")
        elif not os.getenv("OPENOUTFIND_BETTERCONTACT_API_KEY") and not os.getenv("BETTERCONTACT_API_KEY"):
            st.error(
                "OpenOutreach requires OPENOUTFIND_BETTERCONTACT_API_KEY for its (free) discovery stage. "
                "Add it to your .env file."
            )
        else:
            # Write targeting text to a temp file - OpenOutreach reads the
            # campaign target from a file via OPENOUTFIND_CAMPAIGN_TARGET.
            if not st.session_state.targeting_file:
                fd, tmp_path = tempfile.mkstemp(suffix=".md", prefix="rox_target_")
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(targeting.instructions)
                st.session_state.targeting_file = tmp_path

            with st.spinner(
                f"Orchestration agent is scanning for "
                f"{st.session_state.num_leads} qualified leads (this can take a while)..."
            ):
                try:
                    oo_result = openoutreach_service.find_leads(
                        num_leads=st.session_state.num_leads,
                        campaign_target=st.session_state.targeting_file,
                    )
                    st.session_state.oo_result = oo_result
                    st.session_state.phase = "results"
                    st.session_state.leads = oo_result.leads
                except RuntimeError as exc:
                    st.error(f"OpenOutreach run failed:\n\n{exc}")

            if st.session_state.phase == "results":
                oo_result = st.session_state.oo_result
                st.session_state.leads = oo_result.leads

if debug and st.session_state.get("oo_result") is not None:
    oo_result: OpenOutreachResult = st.session_state.oo_result
    if oo_result.command.startswith("(resumed"):
        debug_expander("Debug: OpenOutreach command", oo_result.command)
    else:
        debug_expander("Debug: OpenOutreach command", oo_result.command)
        debug_expander("Debug: OpenOutreach stdout", oo_result.stdout)
        debug_expander("Debug: OpenOutreach stderr", oo_result.stderr)

# ---------------------------------------------------------------------------
# 3. Lead results
# ---------------------------------------------------------------------------
if st.session_state.phase == "results":
    oo_result: OpenOutreachResult = st.session_state.oo_result

    st.divider()
    st.subheader("Relevant Leads")

    if oo_result.exit_code != 0:
        st.error(
            "OpenOutreach failed.\n\n"
            f"Exit code: {oo_result.exit_code}\n\n"
            f"Error:\n{oo_result.stderr or '(no stderr captured)'}"
        )
        if debug:
            debug_expander("Debug: OpenOutreach stdout", oo_result.stdout)
        st.stop()

    if not oo_result.leads:
        if st.session_state.discovery_method == "Fast (LLM + Google Search)":
            st.warning(
                "Fast lead discovery returned 0 leads. "
                "Verify your GROQ_API_KEY, or check if your Google Custom Search API key is enabled in Google Cloud Console."
            )
        else:
            st.warning(
                "OpenOutreach completed but returned no leads. "
                "Check the Debug Output for what it found, and verify your "
                "OPENOUTFIND_* configuration and BetterContact key."
            )
        if debug:
            debug_expander("Debug: OpenOutreach stdout", oo_result.stdout)
            debug_expander("Debug: OpenOutreach stderr", oo_result.stderr)
        st.stop()

    st.markdown(f"**Found {len(oo_result.leads)} qualified leads**")

    if st.session_state.campaign_id is None:
        campaign_id = db.create_campaign(st.session_state.event_name, st.session_state.market)
        st.session_state.campaign_id = campaign_id
        db_ids = db.insert_leads(campaign_id, oo_result.leads)
        st.session_state.lead_db_ids = db_ids

    # --- Selection table ---
    leads = st.session_state.leads
    table_df = {
        "Select": [False] * len(leads),
        "Person": [lead.name for lead in leads],
        "Company": [lead.company for lead in leads],
        "Title": [lead.title for lead in leads],
        "LinkedIn": [lead.linkedin_url for lead in leads],
        "Why Relevant": [lead.reason for lead in leads],
    }

    selected = []

    if not st.session_state.search_ran:
        st.caption("Select up to 4 leads to enrich.")
        edited = st.data_editor(
            table_df,
            key="lead_selector",
            hide_index=True,
            disabled=["Person", "Company", "Title", "LinkedIn", "Why Relevant"],
            column_config={
                "Select": st.column_config.CheckboxColumn("Select", default=False),
                "LinkedIn": st.column_config.LinkColumn("LinkedIn"),
            },
            width="stretch",
        )
        if isinstance(edited, dict):
            selected = [
                i
                for i, row in enumerate(edited["Select"])
                if row is True
            ]
            if not any(edited["Select"]):
                # First render with default False - don't show an error yet
                pass
        else:
            selected = [i for i, v in enumerate(edited.get("Select", [])) if v]
    else:
        selected = [
            i for i in range(len(leads)) if i in st.session_state.enrichments
        ]

    num_selected = len(selected)
    if num_selected > MAX_SELECTION:
        st.error(f"**Selected: {num_selected}/{MAX_SELECTION}** — You may select at most {MAX_SELECTION} leads.")
    else:
        st.markdown(f"**Selected: {num_selected}/{MAX_SELECTION}**")

    if st.session_state.search_ran:
        st.info(
            "Research is complete for the selected leads. Edit the "
            "selection above is locked - start a new search to change leads."
        )

    # --- Research button ---
    research_clicked = st.button(
        "Research Selected Leads",
        type="primary",
        disabled=num_selected == 0 or num_selected > MAX_SELECTION or st.session_state.search_ran,
    )

    if research_clicked and (num_selected == 0 or num_selected > MAX_SELECTION):
        if num_selected > MAX_SELECTION:
            st.error(f"You selected {num_selected} leads. Maximum is {MAX_SELECTION}.")
        else:
            st.error("Select at least 1 lead before researching.")

    if research_clicked and 0 < num_selected <= MAX_SELECTION:
        st.session_state.search_ran = True
        progress_ph = st.empty()
        enrich = st.session_state.enrichments

        for pos, i in enumerate(selected):
            lead = leads[i]
            progress_text = "\n".join(
                "✓ " + leads[j].name if j in enrich
                else "● " + leads[j].name if j == i
                else "○ " + leads[j].name
                for j in selected
            )
            progress_ph.markdown(f"**Researching selected leads...**\n```\n{progress_text}\n```")

            if not lead.linkedin_url:
                st.warning(f"No LinkedIn URL for {lead.name} - skipping enrichment.")
                continue

            try:
                with st.spinner(f"Profile enrichment system is finding in-depth insights for {lead.name}..."):
                    profile_result = cached_apify_profile_actor(
                        lead.linkedin_url,
                        first_name=lead.first_name,
                        last_name=lead.last_name,
                    )
                if debug:
                    debug_expander(f"Debug: HarvestAPI profile output — {lead.name}", profile_result.raw_debug)

                profile = apify_service.normalize_profile(profile_result.items)
                if not profile.name:
                    profile.name = lead.name
                enrich[i] = profile

                # Persist enrichment (email lookup happens in a later step)
                db_id = st.session_state.lead_db_ids[i] if i < len(st.session_state.lead_db_ids) else None
                if db_id:
                    db.insert_enrichment(
                        db_id,
                        json.dumps(profile.raw, default=str) if profile.raw else json.dumps({}),
                        profile.email,
                    )
            except RuntimeError as exc:
                st.error(f"Enrichment failed for {lead.name}:\n\n{exc}")

        progress_ph.markdown("**Research complete.**\n```\n" + "\n".join(f"✓ {leads[j].name}" for j in selected) + "\n```")

    # --- 4. Profile cards ---
    if st.session_state.search_ran:
        st.divider()
        st.subheader("Enriched Profiles")
        enrich = st.session_state.enrichments
        for i in sorted(enrich):
            lead = leads[i]
            profile = enrich[i]
            st.markdown("---")
            st.markdown(f"### {profile.name or lead.name}")
            subtitle_parts = [
                profile.role or lead.title,
                profile.company or lead.company,
            ]
            st.caption(" · ".join(p for p in subtitle_parts if p) or lead.title)
            if lead.linkedin_url:
                st.markdown(f"**[Open Profile]({lead.linkedin_url})**")

            st.markdown("**About**")
            st.write(profile.about or "—")

            st.markdown("**Experience**")
            if profile.experience:
                for item in profile.experience[:6]:
                    title = item.get("title") or ""
                    comp = item.get("company") or ""
                    st.markdown(f"- {title} — {comp}")
            else:
                st.write("—")

            st.markdown("**Education**")
            if profile.education:
                for item in profile.education[:4]:
                    st.markdown(f"- {item.get('institution', '')} — {item.get('degree', '')}")
            else:
                st.write("—")

            st.markdown("**Skills**")
            if profile.skills:
                st.write(", ".join(profile.skills[:12]))
            else:
                st.write("—")

            st.markdown("**Email**")
            if profile.email:
                st.markdown(f"[{profile.email}](mailto:{profile.email})")
            else:
                if st.session_state.emails_searched:
                    st.write("Email not found for this profile.")
                else:
                    st.write("Click **Find Emails** below to look up contact details.")

        # --- 5. Find emails (fast, after profile enrichment) ---
        st.divider()
        st.subheader("Find Emails")
        find_emails_clicked = st.button(
            "Find Emails",
            type="secondary",
            disabled=not bool(st.session_state.enrichments) or st.session_state.emails_searched,
            help="Find contact emails via Contact Compass (fast, ~10s per person) "
            "with a name+domain pattern fallback.",
        )

        if find_emails_clicked or st.session_state.emails_searched:
            st.session_state.emails_searched = True
            enrich = st.session_state.enrichments
            for i in sorted(st.session_state.enrichments):
                lead = leads[i]
                profile = enrich[i]
                if profile.email:
                    continue
                try:
                    with st.spinner(f"Contact discovery is locating the best contact details for {lead.name}..."):
                        cc_email, cc_status, cc_raw = cached_contact_compass_lookup(
                            lead.linkedin_url,
                            first_name=lead.first_name,
                            last_name=lead.last_name,
                            company=lead.company,
                            domain=contact_compass_service.extract_company_domain(
                                profile.raw, lead.company
                            ),
                            max_lookups=2,
                            timeout_seconds=3,
                        )
                    if cc_email:
                        profile.email = cc_email
                        enrich[i] = profile
                        db_id = st.session_state.lead_db_ids[i] if i < len(st.session_state.lead_db_ids) else None
                        if db_id:
                            db.insert_enrichment(
                                db_id,
                                json.dumps(profile.raw, default=str) if profile.raw else json.dumps({}),
                                profile.email,
                            )
                        if debug:
                            debug_expander(
                                f"Debug: Contact Compass lookup — {lead.name}",
                                f"email: {cc_email}\nstatus: {cc_status}\n\n{cc_raw}",
                            )
                except RuntimeError as exc:
                    st.error(f"Email lookup failed for {lead.name}:\n\n{exc}")

        # --- 6. Email generation ---
        st.divider()
        st.subheader("Personalized Emails")
        generate_emails = st.button(
            "Generate Personalized Emails",
            type="primary",
            disabled=not bool(st.session_state.enrichments) or st.session_state.emails_generated,
        )

        if generate_emails or st.session_state.emails_generated:
            st.session_state.emails_generated = True
            drafts = st.session_state.email_drafts
            for i in sorted(st.session_state.enrichments):
                lead = leads[i]
                profile = st.session_state.enrichments[i]
                if i in drafts and drafts[i].body:
                    continue

                with st.spinner(f"Crafting a personalized email for {lead.name} — refining the message in the background..."):
                    try:
                        draft = groq_service.generate_personalized_email(
                            event_name=st.session_state.event_name,
                            market=st.session_state.market,
                            lead_name=lead.name,
                            company=lead.company,
                            title=lead.title,
                            relevance_reason=lead.reason,
                            profile_text=apify_service.profile_to_text(profile),
                            email=profile.email,
                            to_email=profile.email,
                        )
                        drafts[i] = draft
                        if debug:
                            debug_expander(f"Debug: Groq email prompt — {lead.name}", draft.prompt_sent)
                        db_id = st.session_state.lead_db_ids[i] if i < len(st.session_state.lead_db_ids) else None
                        if db_id:
                            db.insert_email(db_id, draft.subject, draft.body, "pending")
                    except RuntimeError as exc:
                        st.error(f"Email generation failed for {lead.name}:\n\n{exc}")

# ---------------------------------------------------------------------------
# 6. Email review / approve
# ---------------------------------------------------------------------------
if st.session_state.emails_generated:
    leads = st.session_state.leads
    drafts = st.session_state.email_drafts
    enrich = st.session_state.enrichments

    for i in sorted(drafts):
        lead = leads[i]
        draft = drafts[i]
        st.markdown("---")
        st.markdown(f"### Personalized Email — {lead.name}")

        to_addr = draft.to or enrich.get(i).email if enrich.get(i) else ""
        st.markdown(f"**To:** {to_addr or '(no email found)'}")
        st.markdown(f"**Subject:** {draft.subject}")

        edited_body = st.text_area(
            "Email Body",
            value=draft.body,
            key=f"email_body_{i}",
            height=260,
            label_visibility="collapsed",
        )
        if edited_body != draft.body:
            draft.body = edited_body
            drafts[i] = draft

        col_r, col_a, col_s = st.columns(3)
        with col_r:
            if st.button("Regenerate", key=f"regenerate_{i}"):
                profile = st.session_state.enrichments.get(i)
                try:
                    with st.spinner("Refining the email draft..."):
                        new_draft = groq_service.generate_personalized_email(
                            event_name=st.session_state.event_name,
                            market=st.session_state.market,
                            lead_name=lead.name,
                            company=lead.company,
                            title=lead.title,
                            relevance_reason=lead.reason,
                            profile_text=apify_service.profile_to_text(profile) if profile else "",
                            email=profile.email if profile else "",
                            to_email=to_addr,
                        )
                        st.session_state.email_drafts[i] = new_draft
                        db_id = st.session_state.lead_db_ids[i] if i < len(st.session_state.lead_db_ids) else None
                        if db_id:
                            db.insert_email(db_id, new_draft.subject, new_draft.body, "pending")
                        st.rerun()
                except RuntimeError as exc:
                    st.error(f"Regeneration failed for {lead.name}:\n\n{exc}")

        with col_a:
            if st.button("Approve", key=f"approve_{i}"):
                st.session_state.approved.add(i)
                db_id = st.session_state.lead_db_ids[i] if i < len(st.session_state.lead_db_ids) else None
                if db_id:
                    row = db.get_email(db_id)
                    if row:
                        db.update_email_status(row["id"], "approved")
                st.rerun()

        with col_s:
            st.write("")  # spacer

        if i in st.session_state.approved:
            st.success("✓ Email approved\n\nSMTP sending is not connected yet.")
            st.button(
                "Send Email",
                disabled=True,
                help=email_service.sending_placeholder_message(),
                key=f"send_email_{i}",
            )
            st.caption(email_service.sending_placeholder_message())

# ---------------------------------------------------------------------------
# Footer note
# ---------------------------------------------------------------------------
st.divider()
st.caption("ROX Test — prototype. Email sending (SMTP) is not connected in this version.")