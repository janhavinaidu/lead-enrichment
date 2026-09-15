"""Apify Actor integration service.

Provides two reusable functions:
- run_apify_search_actor: Actor 1 - retrieves LinkedIn search-page / short profiles.
- run_apify_profile_actor: Actor 2 - retrieves full LinkedIn profile enrichment.

Both use the official `apify-client` package. Actor IDs come from .env:
- APIFY_SEARCH_ACTOR_ID
- APIFY_PROFILE_ACTOR_ID

Email finding: Actors do NOT resolve emails for the ROX flow. Emails are
looked up separately via the Contact Compass REST API
(see services/contact_compass_service.py).
"""

import json
import os
from typing import Any, Dict, List, Optional

from apify_client import ApifyClient

from models.schemas import ApifyRunResult, EnrichedProfile


def has_api_key() -> bool:
    return bool(os.getenv("APIFY_API_TOKEN"))


def get_client() -> Optional[ApifyClient]:
    token = os.getenv("APIFY_API_TOKEN")
    if not token:
        return None
    return ApifyClient(token=token)


def get_search_actor_id() -> str:
    return os.getenv("APIFY_SEARCH_ACTOR_ID", "")


def get_profile_actor_id() -> str:
    return os.getenv("APIFY_PROFILE_ACTOR_ID", "")


def _validate_actor_id(actor_id: str, label: str) -> None:
    if not actor_id:
        raise RuntimeError(
            f"Apify Actor {label} is not configured. "
            f"Set {label} expense to your Actor ID in .env"
        )


def _call_actor(
    actor_id: str,
    run_input: Dict[str, Any],
    label: str,
    timeout_minutes: int = 15,
) -> ApifyRunResult:
    """Run an Apify Actor, wait for it, and pull its default dataset items."""
    client = get_client()
    if client is None:
        raise RuntimeError("Apify API token is missing. Add APIFY_API_TOKEN to your .env file.")
    _validate_actor_id(actor_id, label)

    try:
        run = client.actor(actor_id).call(run_input=run_input)
    except Exception as exc:
        raise RuntimeError(f"Apify Actor failed.\n\nActor:\n{actor_id}\n\nError:\n{exc}") from exc

    if run is None:
        raise RuntimeError(
            f"Apify Actor failed.\n\nActor:\n{actor_id}\n\nError:\nActor run returned no result "
            "(it likely failed). Check the run in Apify Console."
        )

    items: List[Dict[str, Any]] = []
    try:
        items = client.dataset(run.default_dataset_id).list_items(clean=True).items
    except Exception as exc:
        # The run finished but dataset read failed - still surface the error loudly.
        raise RuntimeError(
            f"Apify Actor run finished but reading its dataset failed.\n\nActor:\n{actor_id}\n"
            f"\nError:\n{exc}"
        ) from exc

    raw_debug = json.dumps({"run_id": run.id, "status": run.status, "items": items}, indent=2, default=str)

    return ApifyRunResult(
        actor_id=actor_id,
        items=items,
        run_id=run.id,
        status=run.status,
        raw_debug=raw_debug,
    )


# ----------------------------------------------------------------------------
# Actor 1 - search / short profile
# ----------------------------------------------------------------------------

def run_apify_search_actor(
    linkedin_url: str,
    name: Optional[str] = None,
    company: Optional[str] = None,
    title: Optional[str] = None,
) -> ApifyRunResult:
    """Get short LinkedIn profile / search info for a lead.

    The input schema is built generically so it works with any Actor whose
    schema uses common keys (url / urls / profiles / handle). Unknown keys are
    ignored by the Actor if not in its schema.

    HarvestAPI compatible: `harvestapi/linkedin-profile-search-by-name`
    requires `profileScraperMode` and searches by first/last name, so those are
    sent too (name is split when only the full name is known). Use mode "Full"
    when the Actor should follow each hit to the full profile, otherwise short
    search results mode is used.
    """
    actor_id = get_search_actor_id()
    run_input: Dict[str, Any] = {
        "url": linkedin_url,
        "profiles": [linkedin_url],
        "includeEmail": False,
        "resolveName": bool(name),
        "profileScraperMode": os.getenv("APIFY_SEARCH_PROFILE_MODE", "Full"),
    }
    if name:
        run_input["name"] = name
        first, _, last = name.partition(" ")
        run_input["firstName"] = first
        run_input["lastName"] = last
    if company:
        run_input["company"] = company
    if title:
        run_input["title"] = title

    return _call_actor(actor_id, run_input, "APIFY_SEARCH_ACTOR_ID")


# ----------------------------------------------------------------------------
# Actor 2 - full profile enrichment
# ----------------------------------------------------------------------------

def run_apify_profile_actor(
    linkedin_url: str,
    first_name: str = "",
    last_name: str = "",
    include_email: bool = False,
) -> ApifyRunResult:
    """Get deep LinkedIn profile enrichment for a lead (no email finding).

    Uses `harvestapi/linkedin-profile-scraper` with the profile-only mode:
    "Profile details no email (4 per 1k)". Emails are NOT resolved by the
    Actor - they are looked up separately via the Contact Compass REST API
    (see services/contact_compass_service.py).

    Unknown keys are ignored by Actors that don't support them.
    """
    actor_id = get_profile_actor_id()
    run_input: Dict[str, Any] = {
        "profileUrls": [linkedin_url],
        "profile_urls": [linkedin_url],
        "urls": [linkedin_url],
        "includeEmail": include_email,
        "includeExperience": True,
        "includeEducation": True,
        "includeSkills": True,
        # HarvestAPI schema (harvestapi/linkedin-profile-scraper) - profile
        # details only. Email lookup is done via Contact Compass separately.
        # NB: The enum values contain literal "$" (e.g. "($4 per 1k)") - do not
        # strip them or the Actor rejects the input.
        "profileScraperMode": os.getenv(
            "APIFY_PROFILE_MODE", "Profile details no email ($4 per 1k)"
        ),
        "firstName": first_name,
        "lastName": last_name,
    }

    return _call_actor(actor_id, run_input, "APIFY_PROFILE_ACTOR_ID")


# ----------------------------------------------------------------------------
# Normalization
# ----------------------------------------------------------------------------

def normalize_profile(items: List[Dict[str, Any]]) -> EnrichedProfile:
    """Normalize an Actor 2 dataset into a common structure.

    Different Actors use different field names, so we inspect common variants
    and pull whatever is available. Never fabricates data.
    """
    profile = EnrichedProfile()
    if not items:
        return profile

    # The first item is usually the profile itself.
    raw = items[0] if isinstance(items[0], dict) else {}
    profile.raw = raw

    profile.name = _first_of(
        raw, ["name", "fullName", "full_name", "displayName", "publicIdentifier"]
    )
    profile.headline = _first_of(raw, ["headline", "title", "currentJobTitle", "role"])
    profile.about = _first_of(raw, ["about", "summary", "description", "aboutText"])
    profile.company = _first_of(
        raw, ["company", "currentCompany", "current_company", "companyName", "company_name"]
    )
    profile.role = _first_of(
        raw, ["roleTitle", "jobTitle", "position", "currentTitle", "occupation"]
    )
    profile.location = _first_of(raw, ["location", "geoLocation", "country", "city"])

    email = _extract_email(raw)
    if email:
        profile.email = email

    experience = raw.get("experience") or raw.get("workExperience") or []
    if isinstance(experience, list):
        profile.experience = [
            {
                "title": _first_of(exp, ["title", "role", "position", "company"],
                                   default="") if isinstance(exp, dict) else str(exp),
                "company": _first_of(exp, ["company", "companyName", "organization"],
                                     default="") if isinstance(exp, dict) else "",
            }
            for exp in experience if isinstance(exp, dict)
        ]

    education = raw.get("education") or []
    if isinstance(education, list):
        profile.education = [
            {
                "institution": _first_of(edu, ["school", "institution", "schoolName", "educationInstitution"],
                                         default="") if isinstance(edu, dict) else str(edu),
                "degree": _first_of(edu, ["degree", "degreeName", "fieldOfStudy"],
                                    default="") if isinstance(edu, dict) else "",
            }
            for edu in education if isinstance(edu, dict)
        ]

    skills = raw.get("skills") or raw.get("skillNames") or []
    if isinstance(skills, list):
        profile.skills = [str(s) for s in skills]

    return profile


def profile_to_text(profile: EnrichedProfile) -> str:
    """Render a profile into a readable text block for Groq."""
    lines = []
    if profile.headline:
        lines.append(f"Headline: {profile.headline}")
    if profile.company:
        lines.append(f"Company: {profile.company}")
    if profile.role:
        lines.append(f"Role: {profile.role}")
    if profile.location:
        lines.append(f"Location: {profile.location}")
    if profile.about:
        lines.append(f"About:\n{profile.about}")
    if profile.experience:
        lines.append("Experience:")
        for item in profile.experience[:6]:
            lines.append(f"- {item.get('title', '')} at {item.get('company', '')}".strip(" -"))
    if profile.education:
        lines.append("Education:")
        for item in profile.education[:4]:
            lines.append(f"- {item.get('institution', '')} ({item.get('degree', '')})".strip(" ( )"))
    if profile.skills:
        lines.append(f"Skills: {', '.join(profile.skills[:10])}")
    if profile.email:
        lines.append(f"Email: {profile.email}")
    return "\n".join(lines)


def _extract_email(obj: Dict[str, Any]) -> str:
    """Find a work email in an Actor output, inspecting common field shapes.

    Contact Compass / profile Actors may return email in a top-level string,
    a list, or a nested dict. We search common keys and pick the first
    plausible address. Never fabricates an email.
    """
    email_keys = [
        "email", "workEmail", "work_email", "emailAddress", "mail",
        "contactEmail", "personalEmail", "professionalEmail",
        "contactCompassEmail", "foundEmail", "businessEmail",
    ]
    for key in email_keys:
        value = obj.get(key)
        if value is None:
            continue
        if isinstance(value, str) and "@" in value:
            return value.strip()
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str) and "@" in item:
                    return item.strip()
                if isinstance(item, dict):
                    for sub_key in email_keys:
                        sub = item.get(sub_key)
                        if isinstance(sub, str) and "@" in sub:
                            return sub.strip()
        if isinstance(value, dict):
            found = _extract_email(value)
            if found:
                return found

    # Some actors nest all contact info under a "contacts" or "contactInfo" key.
    for container_key in ("contacts", "contactInfo", "contact_info", "emails"):
        container = obj.get(container_key)
        if not isinstance(container, (list, dict)):
            continue
        found = _search_container_for_email(container)
        if found:
            return found

    return ""


def _search_container_for_email(container) -> str:
    if isinstance(container, dict):
        values = list(container.values())
        if container.get("email"):
            return _extract_email(container)
        for value in values:
            if isinstance(value, str) and "@" in value:
                return value.strip()
        for value in values:
            if isinstance(value, (list, dict)):
                found = _search_container_for_email(value)
                if found:
                    return found
    elif isinstance(container, list):
        for item in container:
            if isinstance(item, str) and "@" in item:
                return item.strip()
            if isinstance(item, (dict, list)):
                found = _search_container_for_email(item)
                if found:
                    return found
    return ""


def _first_of(obj: Dict[str, Any], keys: List[str], default: str = "") -> str:
    for key in keys:
        value = obj.get(key)
        if value not in (None, "", [], {}):
            return str(value)
    return default