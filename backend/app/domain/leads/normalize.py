"""CSV header and value normalisation (rule 8). Pure functions, unit-tested."""

from __future__ import annotations

import re
import unicodedata

# Canonical lead column -> header spellings seen in Apollo exports and other tools (after normalize_header).
_ALIASES: dict[str, tuple[str, ...]] = {
    "email": ("email", "email_address", "work_email", "business_email", "e_mail", "emails"),
    "first_name": ("first_name", "firstname", "first", "given_name"),
    "last_name": ("last_name", "lastname", "last", "surname", "family_name"),
    "title": ("title", "job_title", "position", "role"),
    "company": (
        "company",
        "company_name",
        "organization",
        "organization_name",
        "account_name",
        "company_name_for_emails",
    ),
    "linkedin_url": ("person_linkedin_url", "linkedin_url", "linkedin", "linkedin_profile", "profile_url"),
    "website": ("website", "company_website", "domain", "company_domain", "url"),
    "industry": ("industry", "company_industry"),
    "city": ("city", "person_city"),
    "country": ("country", "person_country"),
    "company_size": (
        "employees",
        "number_of_employees",
        "num_employees",
        "employee_count",
        "company_size",
        "headcount",
    ),
}
ALIAS_MAP: dict[str, str] = {alias: canonical for canonical, aliases in _ALIASES.items() for alias in aliases}
LEAD_COLUMNS = tuple(_ALIASES)

_NON_WORD = re.compile(r"[^0-9a-z]+")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")  # same check as the leads.email constraint


def normalize_header(header: str) -> str:
    """'  First Name ' -> 'first_name'; '# Employees' -> 'employees'; 'E-mail' -> 'e_mail'."""
    text = unicodedata.normalize("NFKC", header).strip().lower()
    text = text.lstrip("﻿")
    return _NON_WORD.sub("_", text).strip("_")


def map_headers(headers: list[str]) -> dict[int, str]:
    """Column index -> canonical lead column, for the FIRST column matching each lead field."""
    mapped: dict[int, str] = {}
    taken: set[str] = set()
    for index, header in enumerate(headers):
        canonical = ALIAS_MAP.get(normalize_header(header))
        if canonical and canonical not in taken:
            mapped[index] = canonical
            taken.add(canonical)
    return mapped


def normalize_email(value: str | None) -> str | None:
    if value is None:
        return None
    email = unicodedata.normalize("NFKC", value).strip().lower().strip("<>").removeprefix("mailto:")
    return email if EMAIL_RE.match(email) and len(email) <= 320 else None


def clean_text(value: str | None, limit: int = 500) -> str | None:
    if value is None:
        return None
    text = unicodedata.normalize("NFKC", value).strip()
    return text[:limit] or None


def parse_size(value: str | None) -> int | None:
    """'1,200' -> 1200; '51-200' -> 51 (lower bound); junk -> None."""
    if not value:
        return None
    digits = re.match(r"\s*([\d,\.]+)", value)
    if not digits:
        return None
    try:
        number = int(float(digits.group(1).replace(",", "")))
    except ValueError:
        return None
    return number if 0 <= number < 10_000_000 else None


def lead_values(row: list[str], mapping: dict[int, str]) -> dict[str, object]:
    """The lead columns from one CSV row (email still raw; see normalize_email)."""
    values: dict[str, object] = {}
    for index, column in mapping.items():
        raw = row[index] if index < len(row) else None
        if column == "email":
            values[column] = raw
        elif column == "company_size":
            values[column] = parse_size(raw)
        else:
            values[column] = clean_text(raw, 2000 if column in ("linkedin_url", "website") else 500)
    return values
