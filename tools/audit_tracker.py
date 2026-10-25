#!/usr/bin/env python3
"""Audit job_search_tracker.csv for structural and data-integrity issues.

The tracker CSV is the central data store of the job application framework.
Multiple commands (/apply, /outcome, /gmail-sync, /notion-sync, /html-report, /upskill)
and tools (tools/rank_state.py) read and write to it.

This tool audits the tracker against framework rules documented across
.claude/commands/outcome.md, .claude/commands/apply.md, and tests/test_tracker_*.py:

Checks:
- Header matches the canonical 14 columns in exact sequence
- Row length: exactly 14 columns per data row
- Status vocabulary: values must match canonical spellings from /outcome:
  `drafted`, `applied`, `interview`, `offer`, `hired`, `rejected`, `no_response`,
  `offer_declined`, `withdrawn` (or legacy read-tolerated `no response`, `offer declined`).
  Catches archive-only statuses like `in_progress` or `interview_only` mistakenly written to CSV.
- Free-form notes safety: notes field must not contain unescaped commas, quotes, or line breaks
  that break row parsing and shift columns.
- Date and deadline formatting: must match YYYY-MM-DD calendar dates (or be empty).
- Fit rating validation: must be an integer between 0 and 100 (or empty).
- Document references (--check-files): verifies that referenced cv_file
  and cover_letter_file exist on disk.
- Duplicate detection: flags potential duplicate applications (same company + role).

Usage:
  python3 tools/audit_tracker.py [job_search_tracker.csv] [--check-files] [--json]

Exit codes:
  0: Clean (no errors found, or default tracker absent)
  1: Violations found
  2: Explicit file not found or unreadable
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TRACKER = ROOT / "job_search_tracker.csv"

CANONICAL_HEADER = [
    "date",
    "company",
    "sector",
    "role",
    "role_type",
    "channel",
    "status",
    "contact_person",
    "fit_rating",
    "notes",
    "cv_file",
    "cover_letter_file",
    "source",
    "deadline",
]

# Authoritative definition from .claude/commands/outcome.md (## Tracker status vocabulary)
CANONICAL_STATUSES = {
    "drafted",
    "applied",
    "interview",
    "offer",
    "hired",
    "rejected",
    "no_response",
    "offer_declined",
    "withdrawn",
}

# Accepted on read for backward compatibility with older tracker files
LEGACY_READ_STATUSES = {
    "no response",
    "offer declined",
}

# Archive-only statuses that belong in outcome.md Status: enum, NEVER the CSV status column
ARCHIVE_ONLY_STATUSES = {
    "in_progress",
    "interview_only",
}

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class AuditIssue:
    def __init__(
        self,
        level: str,
        message: str,
        line: int | None = None,
        company: str = "",
        role: str = "",
        field: str = "",
    ) -> None:
        self.level = level  # "error" or "warning"
        self.message = message
        self.line = line
        self.company = company
        self.role = role
        self.field = field

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "level": self.level,
            "message": self.message,
        }
        if self.line is not None:
            d["line"] = self.line
        if self.company:
            d["company"] = self.company
        if self.role:
            d["role"] = self.role
        if self.field:
            d["field"] = self.field
        return d

    def __str__(self) -> str:
        loc = f"line {self.line}" if self.line is not None else "header"
        context = f" ({self.company} - {self.role})" if self.company or self.role else ""
        prefix = "WARNING" if self.level == "warning" else "ERROR"
        return f"[{prefix}] {loc}{context}: {self.message}"


def _force_utf8_output() -> None:
    """Write UTF-8 whatever the host's default encoding is."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8")


def validate_date(date_str: str) -> bool:
    """Check whether a date string is a valid YYYY-MM-DD calendar date."""
    if not DATE_RE.match(date_str):
        return False
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def audit_tracker(
    path: Path,
    check_files: bool = False,
    root_dir: Path = ROOT,
) -> tuple[list[AuditIssue], int]:
    """Audit a tracker CSV file. Returns (issues, row_count)."""
    issues: list[AuditIssue] = []

    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        issues.append(AuditIssue("error", f"file is not valid UTF-8: {exc}"))
        return issues, 0
    except OSError as exc:
        issues.append(AuditIssue("error", f"cannot read file: {exc}"))
        return issues, 0

    lines = content.splitlines()
    if not lines:
        issues.append(AuditIssue("error", "file is empty"))
        return issues, 0

    # 1. Header verification
    reader = csv.reader(lines)
    try:
        header = next(reader)
    except StopIteration:
        issues.append(AuditIssue("error", "missing header row"))
        return issues, 0

    if header != CANONICAL_HEADER:
        if len(header) != len(CANONICAL_HEADER):
            issues.append(
                AuditIssue(
                    "error",
                    f"header column count mismatch (expected {len(CANONICAL_HEADER)}: {CANONICAL_HEADER}, got {len(header)}: {header})",
                )
            )
        else:
            diffs = [
                f"col {i+1} expected '{exp}', got '{act}'"
                for i, (exp, act) in enumerate(zip(CANONICAL_HEADER, header))
                if exp != act
            ]
            issues.append(AuditIssue("error", f"header format mismatch: {', '.join(diffs)}"))

    seen_applications: dict[tuple[str, str], int] = {}
    row_count = 0

    # 2. Row by row audit
    for line_idx, row in enumerate(reader, start=2):
        row_count += 1
        if not row or (len(row) == 1 and not row[0].strip()):
            issues.append(AuditIssue("warning", "blank row", line=line_idx))
            continue

        if len(row) != len(CANONICAL_HEADER):
            issues.append(
                AuditIssue(
                    "error",
                    f"column count mismatch (expected {len(CANONICAL_HEADER)}, got {len(row)})",
                    line=line_idx,
                )
            )
            continue

        record = dict(zip(CANONICAL_HEADER, row))
        company = record["company"].strip()
        role = record["role"].strip()
        status = record["status"].strip()
        date_val = record["date"].strip()
        deadline_val = record["deadline"].strip()
        fit_val = record["fit_rating"].strip()
        notes_val = record["notes"]
        cv_path = record["cv_file"].strip()
        cover_path = record["cover_letter_file"].strip()

        # Company & role emptiness
        if not company:
            issues.append(AuditIssue("error", "missing company name", line=line_idx, field="company"))
        if not role:
            issues.append(AuditIssue("error", "missing role title", line=line_idx, company=company, field="role"))

        # Duplicate tracking
        if company and role:
            key = (company.lower(), role.lower())
            if key in seen_applications:
                issues.append(
                    AuditIssue(
                        "warning",
                        f"duplicate application entry (first seen on line {seen_applications[key]})",
                        line=line_idx,
                        company=company,
                        role=role,
                    )
                )
            else:
                seen_applications[key] = line_idx

        # Status validation
        if not status:
            issues.append(
                AuditIssue(
                    "error",
                    "missing status",
                    line=line_idx,
                    company=company,
                    role=role,
                    field="status",
                )
            )
        elif status in CANONICAL_STATUSES:
            pass  # canonical status, OK
        elif status in LEGACY_READ_STATUSES:
            issues.append(
                AuditIssue(
                    "warning",
                    f"legacy space spelling '{status}' should be modernized to '{status.replace(' ', '_')}'",
                    line=line_idx,
                    company=company,
                    role=role,
                    field="status",
                )
            )
        elif status in ARCHIVE_ONLY_STATUSES:
            issues.append(
                AuditIssue(
                    "error",
                    f"invalid status '{status}' (this is an outcome.md archive status, never a tracker CSV value)",
                    line=line_idx,
                    company=company,
                    role=role,
                    field="status",
                )
            )
        else:
            issues.append(
                AuditIssue(
                    "error",
                    f"unrecognized status '{status}' (expected one of: {', '.join(sorted(CANONICAL_STATUSES))})",
                    line=line_idx,
                    company=company,
                    role=role,
                    field="status",
                )
            )

        # Date validation
        if date_val and not validate_date(date_val):
            issues.append(
                AuditIssue(
                    "error",
                    f"invalid date '{date_val}' (must be YYYY-MM-DD calendar date or empty)",
                    line=line_idx,
                    company=company,
                    role=role,
                    field="date",
                )
            )

        # Deadline validation
        if deadline_val and not validate_date(deadline_val):
            issues.append(
                AuditIssue(
                    "error",
                    f"invalid deadline '{deadline_val}' (must be YYYY-MM-DD calendar date or empty)",
                    line=line_idx,
                    company=company,
                    role=role,
                    field="deadline",
                )
            )

        # Fit rating validation (0-100 integer or empty)
        if fit_val:
            try:
                fit_num = int(fit_val)
                if not (0 <= fit_num <= 100):
                    issues.append(
                        AuditIssue(
                            "error",
                            f"fit_rating '{fit_val}' out of range (must be 0-100)",
                            line=line_idx,
                            company=company,
                            role=role,
                            field="fit_rating",
                        )
                    )
            except ValueError:
                issues.append(
                    AuditIssue(
                        "error",
                        f"fit_rating '{fit_val}' is not a valid integer (must be 0-100 or empty)",
                        line=line_idx,
                        company=company,
                        role=role,
                        field="fit_rating",
                    )
                )

        # Notes safety: check for unescaped characters that corrupt CSV row parsing
        # (outcome.md Step 4 states notes must contain no commas, double quotes, or line breaks)
        if "," in notes_val:
            issues.append(
                AuditIssue(
                    "error",
                    "notes field contains comma (shifts columns for naive/DictReader split; use hyphen instead)",
                    line=line_idx,
                    company=company,
                    role=role,
                    field="notes",
                )
            )
        if '"' in notes_val:
            issues.append(
                AuditIssue(
                    "error",
                    'notes field contains double quote (\")',
                    line=line_idx,
                    company=company,
                    role=role,
                    field="notes",
                )
            )
        if "\n" in notes_val or "\r" in notes_val:
            issues.append(
                AuditIssue(
                    "error",
                    "notes field contains newline",
                    line=line_idx,
                    company=company,
                    role=role,
                    field="notes",
                )
            )

        # Optional file existence check
        if check_files:
            if cv_path:
                target_cv = root_dir / cv_path
                if not target_cv.exists():
                    issues.append(
                        AuditIssue(
                            "warning",
                            f"referenced cv_file does not exist: {cv_path}",
                            line=line_idx,
                            company=company,
                            role=role,
                            field="cv_file",
                        )
                    )
            if cover_path:
                target_cover = root_dir / cover_path
                if not target_cover.exists():
                    issues.append(
                        AuditIssue(
                            "warning",
                            f"referenced cover_letter_file does not exist: {cover_path}",
                            line=line_idx,
                            company=company,
                            role=role,
                            field="cover_letter_file",
                        )
                    )

    return issues, row_count


def main() -> int:
    _force_utf8_output()
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "tracker",
        nargs="?",
        default=str(DEFAULT_TRACKER),
        help=f"path to job_search_tracker.csv (default: {DEFAULT_TRACKER.name})",
    )
    parser.add_argument(
        "--check-files",
        action="store_true",
        help="verify that referenced cv_file and cover_letter_file exist on disk",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="output audit report as structured JSON",
    )
    args = parser.parse_args()

    tracker_path = Path(args.tracker)

    # If default tracker is not present, report gracefully and exit 0
    if not tracker_path.exists() and tracker_path == DEFAULT_TRACKER:
        if args.json:
            print(json.dumps({"path": str(tracker_path), "status": "absent", "errors": [], "warnings": []}))
        else:
            print(f"{tracker_path.name} not found (no applications tracked yet). Nothing to audit.")
        return 0

    if not tracker_path.exists():
        print(f"error: file not found: {tracker_path}", file=sys.stderr)
        return 2

    issues, row_count = audit_tracker(tracker_path, check_files=args.check_files)
    errors = [i for i in issues if i.level == "error"]
    warnings = [i for i in issues if i.level == "warning"]

    if args.json:
        report = {
            "path": str(tracker_path),
            "rows": row_count,
            "clean": len(errors) == 0,
            "error_count": len(errors),
            "warning_count": len(warnings),
            "errors": [i.to_dict() for i in errors],
            "warnings": [i.to_dict() for i in warnings],
        }
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 1 if errors else 0

    if not issues:
        print(f"Verified {tracker_path.name}: {row_count} rows, clean.")
        return 0

    for issue in issues:
        print(str(issue))

    summary = f"\nAudit result for {tracker_path.name}: {len(errors)} error(s), {len(warnings)} warning(s) across {row_count} row(s)."
    print(summary)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
