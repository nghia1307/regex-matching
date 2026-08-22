#!/usr/bin/env python
"""
Seed the bucket with test data.

Runs once at ``docker compose up`` and is idempotent: an object that already
exists is left alone, so restarting the stack does not regenerate a
two-million-row CSV.

Four files, each there for a reason:

* ``customers.csv``       -- the exact worked example from the brief, so the
                             happy path can be verified by eye.
* ``employees.xlsx``      -- two sheets, to exercise the Excel path and sheet
                             selection.
* ``support_tickets.csv`` -- messy free text (emails, URLs, IPs, cards mixed
                             into sentences) for testing patterns that must not
                             over-match.
* ``contacts_large.csv``  -- the scale file. Row count is configurable; the
                             default is two million so "holds up on a sizeable
                             dataset" can actually be demonstrated.

The large file is streamed to a temp file and uploaded with boto3's managed
multipart transfer, so peak memory stays flat regardless of row count.
"""
from __future__ import annotations

import csv
import io
import os
import random
import sys
import tempfile
import time
from pathlib import Path

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

ENDPOINT = os.environ.get("S3_ENDPOINT_URL") or None
BUCKET = os.environ.get("S3_BUCKET", "regex-data")
RAW_PREFIX = os.environ.get("S3_RAW_PREFIX", "raw/")
LARGE_ROWS = int(os.environ.get("SEED_LARGE_ROWS", "2000000"))
FORCE = os.environ.get("SEED_FORCE", "").lower() in {"1", "true", "yes"}

RNG = random.Random(20260822)

FIRST_NAMES = [
    "John", "Jane", "Alice", "Bob", "Carol", "David", "Emma", "Frank", "Grace",
    "Henry", "Isabel", "Jack", "Karen", "Liam", "Mia", "Noah", "Olivia", "Peter",
    "Quinn", "Rachel", "Sam", "Tara", "Umar", "Vera", "Will", "Xena", "Yusuf", "Zoe",
]
LAST_NAMES = [
    "Doe", "Smith", "Brown", "Johnson", "Williams", "Jones", "Garcia", "Miller",
    "Davis", "Martinez", "Lopez", "Wilson", "Anderson", "Taylor", "Thomas", "Nguyen",
    "Le", "Tran", "Kim", "Patel", "Singh", "Ivanov", "Muller", "Rossi", "Silva",
]
DOMAINS = [
    "example.com", "domain.com", "website.org", "mail.co.uk", "corp.io",
    "acme.net", "test.vn", "sample.de",
]
CITIES = [
    "London", "Hanoi", "Berlin", "Austin", "Toronto", "Lisbon", "Osaka",
    "Nairobi", "Bogota", "Dublin",
]
STATUSES = ["active", "churned", "trial", "suspended"]


def client():
    # Empty strings must become None, not "": passing "" as a key stops boto3
    # falling through to its default credential chain, which is what the EC2
    # instance profile relies on in the deployed stack.
    return boto3.client(
        "s3",
        endpoint_url=ENDPOINT,
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID") or None,
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY") or None,
        region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        use_ssl=(os.environ.get("S3_USE_SSL", "0").lower() in {"1", "true"}),
        config=Config(
            signature_version="s3v4",
            s3={
                "addressing_style": (
                    "path"
                    if os.environ.get("S3_PATH_STYLE", "1").lower() in {"1", "true"}
                    else "auto"
                )
            },
            retries={"max_attempts": 5, "mode": "standard"},
        ),
    )


def log(message: str) -> None:
    print(f"[seed] {message}", flush=True)


def ensure_bucket(s3) -> None:
    try:
        s3.head_bucket(Bucket=BUCKET)
        log(f"bucket {BUCKET} exists")
    except ClientError:
        s3.create_bucket(Bucket=BUCKET)
        log(f"created bucket {BUCKET}")


def exists(s3, key: str) -> bool:
    if FORCE:
        return False
    try:
        s3.head_object(Bucket=BUCKET, Key=key)
        return True
    except ClientError:
        return False


def put_text(s3, key: str, body: str, content_type: str = "text/csv") -> None:
    s3.put_object(
        Bucket=BUCKET, Key=key, Body=body.encode("utf-8"), ContentType=content_type
    )
    log(f"wrote s3://{BUCKET}/{key} ({len(body):,} bytes)")


# --------------------------------------------------------------------------- #
# generators
# --------------------------------------------------------------------------- #
def email_for(first: str, last: str, index: int) -> str:
    domain = DOMAINS[index % len(DOMAINS)]
    style = index % 4
    if style == 0:
        local = f"{first.lower()}.{last.lower()}"
    elif style == 1:
        local = f"{first.lower()}_{last.lower()}"
    elif style == 2:
        local = f"{first[0].lower()}{last.lower()}{index % 100}"
    else:
        local = f"{first.lower()}{last.lower()}"
    return f"{local}@{domain}"


def phone_for(index: int) -> str:
    style = index % 3
    if style == 0:
        return f"+1 {RNG.randint(200, 989)}-555-{RNG.randint(1000, 9999)}"
    if style == 1:
        return f"0{RNG.randint(20, 99)} {RNG.randint(1000, 9999)} {RNG.randint(1000, 9999)}"
    return f"+84 {RNG.randint(90, 99)} {RNG.randint(100, 999)} {RNG.randint(1000, 9999)}"


def card_for() -> str:
    return " ".join(str(RNG.randint(1000, 9999)) for _ in range(4))


def ip_for() -> str:
    return f"{RNG.randint(10, 220)}.{RNG.randint(0, 255)}.{RNG.randint(0, 255)}.{RNG.randint(1, 254)}"


def customers_csv() -> str:
    """The brief's example table, extended just enough to be interesting."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["ID", "Name", "Email", "Phone", "City", "Notes"])
    rows = [
        (1, "John Doe", "john.doe@example.com", "+1 415-555-0132", "London",
         "Prefers email. Backup: j.doe@work.example.com"),
        (2, "Jane Smith", "jane_smith@domain.com", "020 7946 0958", "London",
         "Called from 10.0.0.14 about invoice"),
        (3, "Alice Brown", "alice.brown@website.org", "+84 91 234 5678", "Hanoi",
         "See https://website.org/tickets/4821"),
        (4, "Bob Wilson", "bwilson88@mail.co.uk", "+1 212-555-7788", "Austin",
         "No email on file yet"),
        (5, "Carol Davis", "carol.davis@corp.io", "+1 305-555-0100", "Toronto",
         "Card ending 4242, expires 2027"),
        (6, "David Nguyen", "davidnguyen@test.vn", "+84 98 765 4321", "Hanoi",
         "Duplicate of row 9?"),
        (7, "Emma Muller", "emma.muller@sample.de", "+49 30 1234 5678", "Berlin",
         "Reply to emma.muller@sample.de only"),
        (8, "Frank Rossi", "frank_rossi@acme.net", "+39 06 5555 1234", "Lisbon",
         "Bounced twice"),
        (9, "David Nguyen", "davidnguyen@test.vn", "+84 98 765 4321", "Hanoi",
         "Merged account"),
        (10, "Grace Kim", "gkim42@example.com", "+82 2 555 1234", "Osaka",
         "VIP - do not redact name"),
        (11, "Henry Patel", "henry.patel@domain.com", "+91 22 5555 6789", "Nairobi",
         "Escalated by 192.168.1.77"),
        (12, "Isabel Silva", "isabel.silva@website.org", "+351 21 555 4321", "Bogota",
         "Requested data deletion"),
    ]
    writer.writerows(rows)
    return buffer.getvalue()


def support_tickets_csv(rows: int = 5000) -> str:
    """Free text with PII embedded mid-sentence -- over-matching shows up here."""
    templates = [
        "Customer {email} reported a failed login from {ip} at 09:{mm}.",
        "Refund requested for card {card}; contact {phone} before Friday.",
        "See {url} for the full trace. Reporter: {email}",
        "Duplicate ticket. Original raised by {email} ({phone}).",
        "No PII in this one, just a plain complaint about slow loading.",
        "Escalation: {email} cc {email2}, host {ip}, ref #{ref}.",
    ]
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["TicketID", "Priority", "Subject", "Body", "Assignee"])
    for index in range(1, rows + 1):
        first = FIRST_NAMES[index % len(FIRST_NAMES)]
        last = LAST_NAMES[index % len(LAST_NAMES)]
        template = templates[index % len(templates)]
        body = template.format(
            email=email_for(first, last, index),
            email2=email_for(last, first, index + 7),
            phone=phone_for(index),
            card=card_for(),
            ip=ip_for(),
            url=f"https://support.{DOMAINS[index % len(DOMAINS)]}/t/{index}",
            mm=f"{index % 60:02d}",
            ref=100000 + index,
        )
        writer.writerow(
            [
                f"TKT-{index:06d}",
                ["low", "normal", "high", "urgent"][index % 4],
                f"Issue with order {9000 + index}",
                body,
                email_for(LAST_NAMES[index % len(LAST_NAMES)], "support", index),
            ]
        )
    return buffer.getvalue()


def employees_xlsx() -> bytes:
    import openpyxl

    workbook = openpyxl.Workbook()
    staff = workbook.active
    staff.title = "Staff"
    staff.append(["EmployeeID", "FullName", "WorkEmail", "PersonalEmail", "Mobile", "SSN", "Office"])
    for index in range(1, 26):
        first = FIRST_NAMES[index % len(FIRST_NAMES)]
        last = LAST_NAMES[(index * 3) % len(LAST_NAMES)]
        staff.append(
            [
                f"EMP-{index:04d}",
                f"{first} {last}",
                f"{first.lower()}.{last.lower()}@acme.net",
                email_for(first, last, index),
                phone_for(index),
                f"{RNG.randint(100, 899)}-{RNG.randint(10, 99)}-{RNG.randint(1000, 9999)}",
                CITIES[index % len(CITIES)],
            ]
        )

    contractors = workbook.create_sheet("Contractors")
    contractors.append(["VendorID", "Contact", "Email", "Rate", "Status"])
    for index in range(1, 13):
        first = FIRST_NAMES[(index * 5) % len(FIRST_NAMES)]
        last = LAST_NAMES[(index * 7) % len(LAST_NAMES)]
        contractors.append(
            [
                f"VND-{index:03d}",
                f"{first} {last}",
                email_for(first, last, index * 2),
                f"${RNG.randint(40, 180)}.00",
                STATUSES[index % len(STATUSES)],
            ]
        )

    stream = io.BytesIO()
    workbook.save(stream)
    return stream.getvalue()


def write_large_csv(path: Path, rows: int) -> None:
    """Stream a wide-ish CSV to disk. Constant memory, ~100 bytes/row."""
    started = time.perf_counter()
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(
            ["ID", "Name", "Email", "Phone", "City", "Status", "Score", "Last Seen IP", "Notes"]
        )
        for index in range(1, rows + 1):
            first = FIRST_NAMES[index % len(FIRST_NAMES)]
            last = LAST_NAMES[(index // 7) % len(LAST_NAMES)]
            # Every 17th row has no email: null handling has to be exercised too.
            email = "" if index % 17 == 0 else email_for(first, last, index)
            writer.writerow(
                [
                    index,
                    f"{first} {last}",
                    email,
                    phone_for(index),
                    CITIES[index % len(CITIES)],
                    STATUSES[index % len(STATUSES)],
                    f"{(index * 37) % 1000 / 10:.1f}",
                    ip_for(),
                    f"contact {email or 'unknown'} re order {index}",
                ]
            )
            if index % 500_000 == 0:
                log(f"  ... {index:,} rows generated")
    size_mb = path.stat().st_size / 1024 / 1024
    log(f"generated {rows:,} rows ({size_mb:.1f} MB) in {time.perf_counter() - started:.1f}s")


def main() -> int:
    rows = LARGE_ROWS
    if len(sys.argv) > 1:
        rows = int(sys.argv[1])

    s3 = client()
    ensure_bucket(s3)

    small = {
        f"{RAW_PREFIX}customers.csv": customers_csv,
        f"{RAW_PREFIX}support_tickets.csv": lambda: support_tickets_csv(5000),
    }
    for key, factory in small.items():
        if exists(s3, key):
            log(f"skip {key} (already present)")
            continue
        put_text(s3, key, factory())

    excel_key = f"{RAW_PREFIX}employees.xlsx"
    if exists(s3, excel_key):
        log(f"skip {excel_key} (already present)")
    else:
        payload = employees_xlsx()
        s3.put_object(
            Bucket=BUCKET,
            Key=excel_key,
            Body=payload,
            ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        log(f"wrote s3://{BUCKET}/{excel_key} ({len(payload):,} bytes)")

    large_key = f"{RAW_PREFIX}contacts_large.csv"
    if rows <= 0:
        log("SEED_LARGE_ROWS <= 0, skipping the scale file")
    elif exists(s3, large_key):
        log(f"skip {large_key} (already present)")
    else:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "contacts_large.csv"
            write_large_csv(path, rows)
            log(f"uploading {large_key} (multipart) ...")
            started = time.perf_counter()
            s3.upload_file(str(path), BUCKET, large_key)
            log(f"uploaded in {time.perf_counter() - started:.1f}s")

    log("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
