"""Code re-checks of the prompt rules (rule 5): never trust the prompt alone.

A violation means the job regenerates (up to its attempt limit), then marks the message failed with the reasons.
The DB constraints (follow_ups_have_no_links, links_only_when_interested_or_question) are the last backstop.
"""

from __future__ import annotations

import re

_IMG = re.compile(r"<img\b[^>]*>", re.I)
_LINK = re.compile(r"(<a[\s>]|https?://|www\.)", re.I)
_PLACEHOLDER = re.compile(
    r"(\{\{[^}]*\}\}|\{[a-z_]+\}|\[(first ?name|last ?name|name|company|your name|title|sender|link|date)[^\]]*\]"
    r"|<(first ?name|company|name)>|\bXX+\b|lorem ipsum)",
    re.I,
)
_SIGN_OFF_LINE = re.compile(
    r"^\s*(best regards|kind regards|warm regards|warmest regards|regards|best wishes|all the best|sincerely"
    r"|yours sincerely|yours truly|cheers|best|thanks|many thanks|thank you|talk soon|speak soon)\s*[,!.]?\s*$",
    re.I,
)
_BREAKS = re.compile(r"<\s*(br|/p|/div|/li)\s*/?>", re.I)
_TAGS = re.compile(r"<[^>]+>")


def strip_img(text: str) -> str:
    return _IMG.sub("", text or "")


def has_link(text: str) -> bool:
    return bool(_LINK.search(strip_img(text)))


def plain_lines(text: str) -> list[str]:
    """Non-empty lines of the text with HTML tags removed (paragraphs and <br> become line breaks)."""
    plain = _TAGS.sub("", _BREAKS.sub("\n", strip_img(text)))
    return [line.strip() for line in plain.splitlines() if line.strip()]


def _common(text: str, sender_name: str | None) -> list[str]:
    problems = []
    if _PLACEHOLDER.search(text):
        problems.append("unfilled placeholder")
    lines = plain_lines(text)
    if any(_SIGN_OFF_LINE.match(line) for line in lines[-3:]):
        problems.append("sign-off (the signature is added by Gazi)")
    if sender_name:
        last_lines = "\n".join(lines[-2:]).lower()
        first = sender_name.strip().split()[0].lower() if sender_name.strip() else ""
        if first and len(first) > 2 and re.search(rf"\b{re.escape(first)}\b", last_lines):
            problems.append("sender name at the end")
    return problems


def check_step(channel: str, step: int, subject: str | None, body: str, sender_name: str | None = None) -> list[str]:
    """Problems with one sequence message; empty means it may be saved."""
    problems = _common(f"{subject or ''}\n{body}", sender_name)
    # No links in any step: the prompt never asks for one, and follow-ups must not have them (rule 6).
    if has_link(f"{subject or ''} {body}"):
        problems.append("link" if step == 1 else "link in a follow-up")
    if channel == "email":
        if not (subject or "").strip():
            problems.append("missing subject")
        elif len(subject or "") > 120:
            problems.append("subject too long")
    else:
        if subject:
            problems.append("LinkedIn messages have no subject")
        if step == 1 and len(body) > 200:
            problems.append("connection request over 200 characters")
    if len(body.strip()) < 20:
        problems.append("too short")
    return problems


def check_reply(body: str, sender_name: str | None = None) -> list[str]:
    """Problems with a drafted reply. Links are added by code later (interested/question only), never by the model."""
    problems = _common(body, sender_name)
    if has_link(body):
        problems.append("link (Gazi adds the calendar link itself)")
    if len(body.strip()) < 5:
        problems.append("too short")
    return problems
