"""Email service.

Placeholder for SMTP sending. SMTP is NOT implemented yet - this module
exists so the rest of the pipeline has a stable interface to connect to later.
"""


def send_email(to: str, subject: str, body: str) -> bool:
    """Placeholder. Always raises - sending is wired up in a later iteration.

    When SMTP is added, this function should perform the actual send and
    return True on success.
    """
    raise NotImplementedError(
        "Email sending is not connected yet. SMTP will be implemented in a later iteration."
    )


def sending_placeholder_message() -> str:
    """Message shown in the UI for the Send Email placeholder."""
    return "Email sending will be connected through SMTP later."