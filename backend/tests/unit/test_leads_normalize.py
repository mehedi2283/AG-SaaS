from __future__ import annotations

import pytest

from app.domain.leads.importer import parse_csv
from app.domain.leads.normalize import map_headers, normalize_email, normalize_header, parse_size
from app.workers.context import JobFailed


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("First Name", "first_name"),
        ("  Last  Name ", "last_name"),
        ("# Employees", "employees"),
        ("E-mail", "e_mail"),
        ("﻿Email", "email"),
        ("".join(map(chr, (0xFF25, 0xFF4D, 0xFF41, 0xFF49, 0xFF4C))), "email"),  # full-width "Email", NFKC
        ("Person Linkedin Url", "person_linkedin_url"),
    ],
)
def test_normalize_header(raw: str, expected: str) -> None:
    assert normalize_header(raw) == expected


def test_apollo_headers_map_to_lead_columns() -> None:
    headers = [
        "First Name",
        "Last Name",
        "Title",
        "Company",
        "Company Name for Emails",
        "Email",
        "Person Linkedin Url",
        "Website",
        "Industry",
        "# Employees",
        "City",
        "Country",
        "Seniority",
    ]
    mapped = map_headers(headers)
    assert mapped[0] == "first_name" and mapped[5] == "email" and mapped[9] == "company_size"
    assert mapped[3] == "company" and 4 not in mapped  # the first company column wins
    assert 12 not in mapped  # unknown columns stay in raw only


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(" Lee@Example.COM ", "lee@example.com"), ("mailto:a@b.co", "a@b.co"), ("not-an-email", None), ("", None)],
)
def test_normalize_email(raw: str, expected: str | None) -> None:
    assert normalize_email(raw) == expected


@pytest.mark.parametrize(("raw", "expected"), [("1,200", 1200), ("51-200", 51), ("n/a", None), ("", None)])
def test_parse_size(raw: str, expected: int | None) -> None:
    assert parse_size(raw) == expected


def test_parse_csv_splits_valid_repeated_and_invalid_rows() -> None:
    data = (
        "﻿First Name,Email,Company,# Employees\r\n"
        "Lee,lee@acme.com,Acme,120\r\n"
        "Bad,not-an-email,Acme,\r\n"
        ",,,\r\n"
        "Lee again,LEE@acme.com,Acme,\r\n"
        "Sam,sam@beta.io,Beta,1,000\r\n"
    ).encode()
    parsed = parse_csv(data)
    assert parsed.total == 4 and parsed.failed == 1
    assert [lead["email"] for lead in parsed.leads] == ["lee@acme.com", "sam@beta.io"]
    assert [lead["email"] for lead in parsed.repeated] == ["lee@acme.com"]
    assert parsed.leads[0]["company_size"] == 120 and parsed.leads[0]["raw"]["first_name"] == "Lee"
    assert parsed.errors == [{"row": 3, "error": "missing or invalid email"}]


def test_parse_csv_needs_an_email_column() -> None:
    with pytest.raises(JobFailed) as exc:
        parse_csv(b"Name,Company\nLee,Acme\n")
    assert exc.value.error_code == "no_email_column"


def test_parse_csv_reads_semicolons_and_windows_encoding() -> None:
    parsed = parse_csv("Email;Company\nzoë@café.fr;Café\n".encode("cp1252"))
    assert parsed.leads[0]["company"] == "Café"
