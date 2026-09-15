"""OpenOutreach / OpenOutFind integration.

OpenOutreach ships as a CLI (not a Python library), so this module wraps it
with subprocess and parses its JSON-Lines / CSV output.

The FREE discovery/qualification stage only is used. We never pass "emails"
or "--emails" to the CLI, so no paid email resolution runs.

Environment variables passed through to the CLI:
- OPENOUTFIND_PRODUCT_DOCS
- OPENOUTFIND_CAMPAIGN_TARGET
- OPENOUTFIND_AI_MODEL
- OPENOUTFIND_LLM_API_KEY
- OPENOUTFIND_BETTERCONTACT_API_KEY
- OPENOUTFIND_OPERATOR_EMAIL
- OPENOUTFIND_OPERATOR_COUNTRY
- OPENOUTFIND_ACCEPT_LEGAL_NOTICE
"""

import csv
import io
import json
import os
import shutil
import subprocess
from typing import Dict, List, Optional

from models.schemas import Lead, OpenOutreachResult

# Output columns produced by OpenOutreach (CSV), as of current docs.
_CSV_COLUMNS = [
    "email",
    "first_name",
    "last_name",
    "company",
    "title",
    "website",
    "linkedin_url",
    "reason",
    "lead_id",
    "qualified_at",
]


def find_executable() -> str:
    """Return the CLI executable name available on PATH.

    Prefers the bundled `openoutreach`, falls back to `outfind`.
    """
    if shutil.which("openoutreach"):
        return "openoutreach"
    if shutil.which("outfind"):
        return "outfind"
    raise RuntimeError(
        "OpenOutreach is not installed. Install it with:  pip install openoutreach\n"
        "Then verify with:  openoutreach status"
    )


def is_installed() -> bool:
    """Return True if either OpenOutreach or OpenOutFind CLI is on PATH."""
    return shutil.which("openoutreach") is not None or shutil.which("outfind") is not None


def build_command(num_leads: int, campaign_target: Optional[str] = None, use_json: bool = True) -> List[str]:
    """Build the CLI command list."""
    exe = find_executable()
    cmd = [exe, "find", str(num_leads)]
    if use_json:
        cmd.append("--json")
    return cmd


def _build_env(campaign_target: Optional[str] = None, product_docs: Optional[str] = None) -> Dict[str, str]:
    env = os.environ.copy()
    if campaign_target:
        env["OPENOUTFIND_CAMPAIGN_TARGET"] = _read_file_value(campaign_target, "campaign target")
    if product_docs:
        env["OPENOUTFIND_PRODUCT_DOCS"] = _read_file_value(product_docs, "product docs")
    else:
        env["OPENOUTFIND_PRODUCT_DOCS"] = _read_file_value(
            env.get("OPENOUTFIND_PRODUCT_DOCS", ""), "product docs"
        )
    return env


def _read_file_value(value: str, label: str) -> str:
    """OpenOutFind expects the campaign/product text, not a path.

    The env var is rendered verbatim into LLM prompts. If the value looks like an
    existing file path, read its text; otherwise pass it through as-is.
    """
    if os.path.isfile(value):
        try:
            with open(value, encoding="utf-8") as f:
                text = f.read().strip()
        except OSError as exc:
            raise RuntimeError(f"Could not read {label} file {value}: {exc}") from exc
        if not text:
            raise RuntimeError(f"{label} file {value} is empty.")
        return text
    return value


def find_leads(
    num_leads: int = 15,
    campaign_target: Optional[str] = None,
    product_docs: Optional[str] = None,
    timeout_seconds: int = 900,
) -> OpenOutreachResult:
    """Run `openoutreach find N` and parse the output.

    Tries JSON-Lines output first (`--json`), falls back to CSV if the flag
    is not supported by the installed version.

    Raises RuntimeError if the CLI is missing or fails to start.
    Never fabricates leads.
    """
    if not is_installed():
        raise RuntimeError(
            "OpenOutreach is not installed. Install it with:  pip install openoutreach\n"
            "Then verify with:  openoutreach status"
        )

    env = _build_env(campaign_target, product_docs)

    # Try JSON-Lines first.
    cmd = build_command(num_leads, campaign_target, use_json=True)
    exit_code, stdout, stderr = _run_command(cmd, env, timeout_seconds)

    # Retry without --json if the flag was rejected.
    if exit_code != 0 and _suggests_unknown_flag(stderr):
        cmd = build_command(num_leads, campaign_target, use_json=False)
        exit_code, stdout, stderr = _run_command(cmd, env, timeout_seconds)

    command_display = " ".join(cmd)

    leads: List[Lead] = []
    if exit_code == 0:
        leads = _parse_json_lines(stdout)
        if not leads:
            leads = _parse_csv_lines(stdout)

    return OpenOutreachResult(
        leads=leads,
        command=command_display,
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
    )


def _run_command(cmd: List[str], env: Dict[str, str], timeout_seconds: int):
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=env,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"OpenOutreach failed to start. Executable not found: {cmd[0]}\n"
            "Install with: pip install openoutreach"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"OpenOutreach timed out after {timeout_seconds}s while finding "
            f"{cmd[1] if len(cmd) > 1 else ''} leads."
        ) from exc


def _suggests_unknown_flag(stderr: str) -> bool:
    lowered = stderr.lower()
    markers = ["json", "no such option", "unrecognized", "unknown argument", "unexpected argument", "usage:"]
    return "json" in lowered or any(m in lowered for m in markers)


def _parse_json_lines(stdout: str) -> List[Lead]:
    """Parse JSON-Lines output from `openoutreach find --json`."""
    leads: List[Lead] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        lead = Lead.from_dict(data)
        if _looks_like_lead(lead):
            leads.append(lead)
    return leads


def _parse_csv_lines(stdout: str) -> List[Lead]:
    """Parse CSV output from `openoutreach find`."""
    leads: List[Lead] = []
    reader = csv.reader(io.StringIO(stdout))
    first_row = True
    for row in reader:
        if not row:
            continue
        if len(row) < 3:
            continue
        # Skip a header row if present.
        if first_row and row[0].strip().lower() in ("email", "first_name"):
            first_row = False
            continue
        first_row = False
        data = dict(zip(_CSV_COLUMNS[: len(row)], row))
        lead = Lead.from_dict(data)
        if _looks_like_lead(lead):
            leads.append(lead)
    return leads


def _looks_like_lead(lead: Lead) -> bool:
    return bool(lead.first_name or lead.last_name or lead.company or lead.linkedin_url)