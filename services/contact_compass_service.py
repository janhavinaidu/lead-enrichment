"""Contact Compass email lookup via their REST API.

Flow:
    1. Primary Lookup — Query by LinkedIn public id:
       {"filters": {"linkedin_public_id": "john-smith"}}

    2. Fallback Lookup — If public ID lookup fails, retry using Name + Company:
       {"filters": {"first_name": "John", "last_name": "Smith", "company_name": "Tata Motors"}}
"""

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

CONTACT_COMPASS_API_URL = "https://api.contactcompass.io/v1/people/search"

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def get_api_key() -> str:
    return os.getenv("CONTACT_COMPASS_API_KEY", "")


def has_api_key() -> bool:
    return bool(get_api_key())


def extract_linkedin_public_id(linkedin_url: str) -> str:
    """Extract the LinkedIn public id from a profile / search URL."""
    url = (linkedin_url or "").strip()
    if not url:
        return ""
    url = url.split("?")[0].split("#")[0].rstrip("/")
    if "/in/" in url:
        url = url.split("/in/")[-1]
    elif "/pub/" in url:
        url = url.split("/pub/")[-1]
    return url.strip("/")


def _looks_like_email(value: Any) -> bool:
    """True if the value is a string containing something email-shaped."""
    if not isinstance(value, str):
        return False
    return bool(EMAIL_RE.search(value))


def find_email_by_linkedin(
    linkedin_url: str,
    first_name: str = "",
    last_name: str = "",
    company: str = "",
    domain: str = "",
    max_lookups: int = 7,
    timeout_seconds: int = 15,
) -> Tuple[str, str, str]:
    """Look up an email for a LinkedIn profile via Contact Compass.

    Tries a cascade of lookups (best match first) and stops at the first hit:

      1. `linkedin_public_id` (exact LinkedIn slug)
      2. `linkedin_public_id` + first/last name
      3. Full `linkedin_url`
      4. first + last + company name
      5. first + last + shortened company name (legal suffix stripped)
      6. first + last + company domain
      7. first + last only

    `max_lookups` caps how many lookups run (for fast mode), and
    `timeout_seconds` bounds each HTTP call.

    If all lookups miss, ALWAYS falls back to a pattern-based guess built from
    the person's name + company domain (e.g. first.last@tatamotors.com). When
    no domain is available it derives one from the company name so a usable
    candidate is still returned. The fallback carries an explicit
    `pattern-guessed:<domain>` status so callers know it is inferred.

    Returns (email, email_status, raw_json_debug). email is "" when not found.
    """
    api_key = get_api_key()
    if not api_key:
        raise RuntimeError(
            "CONTACT_COMPASS_API_KEY is not set. Add it to your .env file "
            "to look up emails via Contact Compass."
        )

    public_id = extract_linkedin_public_id(linkedin_url)
    first = first_name.strip()
    last = last_name.strip()
    comp = company.strip()
    dom = _normalize_domain(domain)
    if not dom and comp:
        # Guarantee a pattern fallback even when no real domain was extracted.
        dom = _normalize_domain(_domain_from_company(comp))

    candidates: List[Dict[str, Any]] = []
    if public_id:
        candidates.append({"linkedin_public_id": public_id})
        if first and last:
            candidates.append(
                {"linkedin_public_id": public_id, "first_name": first, "last_name": last}
            )
    if linkedin_url and linkedin_url.strip():
        candidates.append({"linkedin_url": linkedin_url.strip()})
    if first and last:
        if comp:
            candidates.append(
                {"first_name": first, "last_name": last, "company_name": comp}
            )
            short_comp = _shorten_company(comp)
            if short_comp and short_comp != comp:
                candidates.append(
                    {"first_name": first, "last_name": last, "company_name": short_comp}
                )
        if dom:
            candidates.append(
                {"first_name": first, "last_name": last, "company_domain": dom}
            )
        candidates.append({"first_name": first, "last_name": last})

    seen: set = set()
    attempted = 0
    for filters in candidates:
        if attempted >= max_lookups:
            break
        signature = tuple(sorted(filters.items()))
        if signature in seen:
            continue
        seen.add(signature)
        attempted += 1

        email, status, raw_body = _execute_search(filters, api_key, timeout=timeout_seconds)
        if email:
            return email, status, raw_body

    # Last resort: contact patterns are fixed at many companies (name@domain).
    if first and last and dom:
        guessed = guess_email_by_pattern(first, last, dom)
        if guessed:
            return guessed[0], f"pattern-guessed:{dom}", '{"pattern": true}'

    return "", "", "{}"


_COMPANY_SUFFIXES = (
    "limited", "ltd", "inc", "incorporated", "corporation", "corp", "gmbh",
    "ag", "llc", "plc", "s.a.", "sa", "pvt", "pvt", "private limited",
    "private", "co.", "co", "company", "group", "holding", "holdings",
    "srl", "bv", "bvba", "nv", "oy", "spa", "s.p.a.", "sdn bhd", "pte ltd",
    "pt", "pty", "pty ltd",
)


def _shorten_company(company: str) -> str:
    """Strip common legal suffixes so 'Tata Motors Limited' -> 'Tata Motors'."""
    comp = company.strip()
    if not comp:
        return comp
    lowered = comp.lower()
    while True:
        matched = None
        for suffix in _COMPANY_SUFFIXES:
            if lowered.endswith(suffix) and len(comp) > len(suffix) + 1:
                matched = suffix
                break
        if matched is None:
            break
        comp = comp[: -len(matched)].strip(" .,")
        lowered = comp.lower()
    return comp


def _domain_from_company(company: str) -> str:
    """Best-effort placeholder domain from a company name ('Tata Motors' ->
    'tatamotors.com'). Used only for the pattern-based email fallback."""
    comp = _shorten_company(company or "")
    if not comp:
        return ""
    slug = re.sub(r"[^a-z0-9]+", "", comp.lower())
    if slug and len(slug) >= 3:
        return f"{slug}.com"
    return ""


_SOCIAL_HOSTS = {
    "linkedin.com", "linkedin.cn", "facebook.com", "fb.com", "x.com",
    "twitter.com", "instagram.com", "youtube.com", "youtu.be", "whatsapp.com",
    "wa.me", "t.me", "telegram.me", "medium.com", "github.io",
}

_MARKETING_SUBDOMAINS = {
    "www", "mail", "careers", "jobs", "hr", "recruitment", "intranet",
    "portal", "blog", "news", "press", "go", "app", "my", "info", "ops",
    "corp", "hcm", "connect", "join", "workday", "home", "web", "owa",
    "vpn", "outlook", "sales", "marketing",
}


def _normalize_domain(value: Any) -> str:
    """Return a usable domain like ``tatamotors.com`` or ``''``.

    Strips scheme, trailing path, common marketing subdomains, and excludes
    social-media and LinkedIn domains (those are *not* company mail domains).
    """
    if not isinstance(value, str):
        return ""
    text = value.strip().lower()
    text = re.sub(r"^[\w+.-]+://", "", text).strip()
    text = text.split("/")[0].split("?")[0].split("#")[0].split(":")[0].rstrip(".")
    parts = text.split(".")
    if len(parts) < 2:
        return ""
    host = ".".join(parts)
    if any(host == h or host.endswith("." + h) for h in _SOCIAL_HOSTS):
        return ""
    if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", host):
        return ""
    if host == "localhost":
        return ""
    # Strip a single leading marketing subdomain.
    if len(parts) > 2 and parts[0] in _MARKETING_SUBDOMAINS:
        host = ".".join(parts[1:])
    return host


def extract_company_domain(profile_raw: Dict[str, Any], company: str = "") -> str:
    """Pull a usable company domain from an enriched profile's raw data.

    Searches common field names that Apify / HarvestAPI actors emit.
    Returns ``''`` when no plausible company domain is found.
    """
    if not isinstance(profile_raw, dict):
        return ""

    direct_keys = (
        "companyDomain", "company_domain", "companyDomainName",
        "companyWebsite", "company_website", "companyWeb", "domain",
        "currentCompanyDomain", "current_company_domain", "companyUrl",
        "company_url",
    )
    for key in direct_keys:
        d = _normalize_domain(profile_raw.get(key))
        if d:
            return d

    for key in ("website", "webUrl", "siteUrl", "url"):
        d = _normalize_domain(profile_raw.get(key))
        if d:
            return d

    for key in ("experience", "workExperience", "positions", "work_experience"):
        items = profile_raw.get(key)
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                for sub in ("companyWebsite", "companyDomain", "company_domain",
                            "domain", "website", "companyUrl"):
                    d = _normalize_domain(item.get(sub))
                    if d:
                        return d
    return ""


def guess_email_by_pattern(first_name: str, last_name: str, domain: str) -> List[str]:
    """Generate pattern-based email candidates from a name + company domain.

    Many companies use a fixed email scheme such as first.last@domain.com.
    These candidates are best-effort guesses and should be treated as
    unverified by callers.

    Returns a list of candidate strings ordered by most-common pattern first.
    """
    first = first_name.strip()
    last = last_name.strip()
    dom = _normalize_domain(domain)
    if not (first and last and dom):
        return []

    def clean(s: str) -> str:
        return re.sub(r"[^a-z0-9]", "", s.lower())

    f, l = clean(first), clean(last)
    if not f or not l:
        return []

    f_init = f[0]
    patterns = [
        f"{f}.{l}",
        f"{f}{l}",
        f"{l}.{f}",
        f"{l}{f}",
        f"{f_init}.{l}",
        f"{f_init}{l}",
        f"{f}",
        f"{l}.{f_init}",
    ]
    seen: set = set()
    out: List[str] = []
    for p in patterns:
        if p and p not in seen:
            seen.add(p)
            out.append(f"{p}@{dom}")
    return out


def _execute_search(filters: Dict[str, Any], api_key: str, timeout: int = 15) -> Tuple[str, str, str]:
    """Execute a single POST search to Contact Compass."""
    payload = json.dumps({"filters": filters}).encode("utf-8")
    headers = {
        "x-api-token": api_key,
        "Content-Type": "application/json",
    }
    request = Request(CONTACT_COMPASS_API_URL, data=payload, headers=headers, method="POST")

    try:
        with urlopen(request, timeout=timeout) as response:
            raw_body = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8")
        except Exception:
            pass
        return "", "", f"HTTP {exc.code}: {detail or exc.reason}"
    except URLError as exc:
        return "", "", f"Network error: {exc.reason}"
    except Exception as exc:
        return "", "", str(exc)

    try:
        data = json.loads(raw_body)
    except json.JSONDecodeError:
        return "", "", raw_body

    email, status = _find_email_in_payload(data)
    return email, status, raw_body


_EMBEDDED_EMAIL_KEYS = (
    "emails", "email_addresses", "emailAddresses", "email_list", "emails_list",
)

_EMAIL_VALUE_KEYS = (
    "email", "work_email", "workEmail", "email_address", "emailAddress",
    "personal_email", "contact_email", "primary_email", "business_email",
    "company_email", "companyEmail", "verified_email", "verifiedEmail",
    "professional_email", "email_add", "value", "address",
)


def _find_email_in_payload(data: Any) -> Tuple[str, str]:
    """Recursively find the first plausible contact email + its status.

    Tolerates many response shapes: flat email fields, `emails` arrays of
    strings or `{value: ...}` objects, typed `{type: "email", value: ...}`
    records, and nested list/dict structures.
    """
    if isinstance(data, str):
        if _looks_like_email(data):
            return EMAIL_RE.search(data).group(0), ""
        return "", ""
    if isinstance(data, dict):
        direct = _direct_email(data)
        if direct:
            return direct, _status_of(data)
        for key, value in data.items():
            if isinstance(value, str) and _looks_like_email(value):
                return EMAIL_RE.search(value).group(0), _status_of(data)
            if key in _EMBEDDED_EMAIL_KEYS and isinstance(value, (dict, list)):
                found = _find_email_in_payload(value)
                if found[0]:
                    return found
        for value in data.values():
            if isinstance(value, (dict, list)):
                found = _find_email_in_payload(value)
                if found[0]:
                    return found
    elif isinstance(data, list):
        for item in data:
            found = _find_email_in_payload(item)
            if found[0]:
                return found
    return "", ""


def _status_of(data: Any) -> str:
    if isinstance(data, dict):
        return str(data.get("email_status") or data.get("status") or "")
    return ""


def _direct_email(obj: Dict[str, Any]) -> str:
    """Return a validated email from a single dict, or ''."""
    for key in _EMAIL_VALUE_KEYS:
        value = obj.get(key)
        if isinstance(value, str) and _looks_like_email(value):
            return EMAIL_RE.search(value).group(0)
        if isinstance(value, dict):
            found = _direct_email(value)
            if found:
                return found

    # Typed records, e.g. {"type": "email", "value": "..."} or
    # {"contact_data_type": "work", "value": "..."}
    contact_type = str(
        obj.get("type") or obj.get("contact_data_type") or obj.get("subType") or ""
    ).lower()
    if contact_type and "email" in contact_type:
        for key in ("value", "address", "email"):
            val = obj.get(key)
            if isinstance(val, str) and _looks_like_email(val):
                return EMAIL_RE.search(val).group(0)
    return ""