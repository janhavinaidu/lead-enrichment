"""Contact Compass email lookup via their REST API.

Flow:
    1. Extract the LinkedIn public id from a profile URL:
       https://www.linkedin.com/in/john-smith/  ->  john-smith
    2. POST to Contact Compass:
       POST https://api.contactcompass.io/v1/people/search
       {"filters": {"linkedin_public_id": "john-smith"}}
    3. Parse the returned email (and its verification status) if found.

The response may be a single contact object or wrapped in a results array,
so the parser searches the whole payload for the first plausible address.
"""

import json
import os
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

CONTACT_COMPASS_API_URL = "https://api.contactcompass.io/v1/people/search"


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


def find_email_by_linkedin(linkedin_url: str) -> Tuple[str, str, str]:
    """Look up an email for a LinkedIn profile via Contact Compass.

    Returns (email, email_status, raw_json_debug). email is "" when not found.
    Raises RuntimeError for missing key / network / HTTP errors.
    """
    api_key = get_api_key()
    if not api_key:
        raise RuntimeError(
            "CONTACT_COMPASS_API_KEY is not set. Add it to your .env file "
            "to look up emails via Contact Compass."
        )

    public_id = extract_linkedin_public_id(linkedin_url)
    if not public_id:
        return "", "", "{}"

    payload = json.dumps({"filters": {"linkedin_public_id": public_id}}).encode("utf-8")
    headers = {
        "x-api-token": api_key,
        "Content-Type": "application/json",
    }
    request = Request(CONTACT_COMPASS_API_URL, data=payload, headers=headers, method="POST")

    try:
        with urlopen(request, timeout=60) as response:
            raw_body = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8")
        except Exception:
            pass
        raise RuntimeError(
            f"Contact Compass lookup failed for {public_id}.\n"
            f"HTTP {exc.code}: {detail or exc.reason}"
        ) from exc
    except URLError as exc:
        raise RuntimeError(
            f"Contact Compass lookup failed for {public_id}.\n"
            f"Network error: {exc.reason}"
        ) from exc

    try:
        data = json.loads(raw_body)
    except json.JSONDecodeError:
        return "", "", raw_body

    email, status = _find_email_in_payload(data)
    return email, status, raw_body


def _find_email_in_payload(data: Any) -> Tuple[str, str]:
    """Recursively find the first plausible contact email + its status."""
    if isinstance(data, dict):
        email = _direct_email(data)
        if email:
            return email, str(data.get("email_status") or data.get("status") or "")
        for value in data.values():
            found = _find_email_in_payload(value)
            if found[0]:
                return found
    elif isinstance(data, list):
        for item in data:
            found = _find_email_in_payload(item)
            if found[0]:
                return found
    return "", ""


def _direct_email(obj: Dict[str, Any]) -> str:
    email_keys = [
        "email", "work_email", "workEmail", "email_address",
        "emailAddress", "personal_email", "contact_email",
    ]
    for key in email_keys:
        value = obj.get(key)
        if isinstance(value, str) and "@" in value:
            return value.strip()
    return ""