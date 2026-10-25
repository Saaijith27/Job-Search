"""Unit tests for tools/audit_tracker.py."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parent.parent
TOOL = REPO / "tools" / "audit_tracker.py"

sys.path.insert(0, str(REPO / "tools"))
import audit_tracker as at  # noqa: E402


class TestAuditTrackerCore(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.tmp = Path(self.temp_dir.name)
        self.tracker = self.tmp / "job_search_tracker.csv"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def write_csv(self, header: list[str], rows: list[list[str]]) -> Path:
        import csv

        with open(self.tracker, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(header)
            for r in rows:
                writer.writerow(r)
        return self.tracker

    def test_canonical_clean_row_passes(self) -> None:
        self.write_csv(
            at.CANONICAL_HEADER,
            [
                [
                    "2026-10-19",
                    "Acme Corp",
                    "Aerospace",
                    "Thermal Systems Engineer",
                    "Internship",
                    "LinkedIn",
                    "applied",
                    "Jane Doe",
                    "95",
                    "Applied via portal - strong CFD match",
                    "cv/main_acme_swe.pdf",
                    "cover_letters/cover_acme_swe.pdf",
                    "https://example.com/job/123",
                    "2026-11-01",
                ]
            ],
        )
        issues, rows = at.audit_tracker(self.tracker)
        errors = [i for i in issues if i.level == "error"]
        self.assertEqual(len(errors), 0, [str(e) for e in errors])
        self.assertEqual(rows, 1)

    def test_header_column_mismatch_fails(self) -> None:
        short_header = at.CANONICAL_HEADER[:-2]
        self.write_csv(short_header, [["val"] * len(short_header)])
        issues, _ = at.audit_tracker(self.tracker)
        errors = [i for i in issues if i.level == "error"]
        self.assertTrue(any("header column count mismatch" in e.message for e in errors))

    def test_header_field_name_mismatch_fails(self) -> None:
        scrambled = list(at.CANONICAL_HEADER)
        scrambled[0], scrambled[1] = scrambled[1], scrambled[0]
        self.write_csv(scrambled, [["val"] * len(scrambled)])
        issues, _ = at.audit_tracker(self.tracker)
        errors = [i for i in issues if i.level == "error"]
        self.assertTrue(any("header format mismatch" in e.message for e in errors))

    def test_all_canonical_statuses_pass(self) -> None:
        for status in at.CANONICAL_STATUSES:
            row = [
                "2026-10-19",
                "Acme Corp",
                "Tech",
                f"Role for {status}",
                "Full-time",
                "Portal",
                status,
                "",
                "80",
                "Clean note",
                "",
                "",
                "",
                "",
            ]
            self.write_csv(at.CANONICAL_HEADER, [row])
            issues, _ = at.audit_tracker(self.tracker)
            errors = [i for i in issues if i.level == "error" and i.field == "status"]
            self.assertEqual(len(errors), 0, f"Canonical status '{status}' should pass without error")

    def test_legacy_statuses_emit_warnings(self) -> None:
        for legacy in at.LEGACY_READ_STATUSES:
            row = [
                "2026-10-19",
                "Acme Corp",
                "Tech",
                "Engineer",
                "Full-time",
                "Portal",
                legacy,
                "",
                "80",
                "",
                "",
                "",
                "",
                "",
            ]
            self.write_csv(at.CANONICAL_HEADER, [row])
            issues, _ = at.audit_tracker(self.tracker)
            warnings = [i for i in issues if i.level == "warning" and i.field == "status"]
            self.assertTrue(len(warnings) > 0, f"Legacy status '{legacy}' should trigger a modernization warning")

    def test_archive_only_statuses_fail(self) -> None:
        for archive_status in at.ARCHIVE_ONLY_STATUSES:
            row = [
                "2026-10-19",
                "Acme Corp",
                "Tech",
                "Engineer",
                "Full-time",
                "Portal",
                archive_status,
                "",
                "80",
                "",
                "",
                "",
                "",
                "",
            ]
            self.write_csv(at.CANONICAL_HEADER, [row])
            issues, _ = at.audit_tracker(self.tracker)
            errors = [i for i in issues if i.level == "error" and i.field == "status"]
            self.assertTrue(len(errors) > 0, f"Archive status '{archive_status}' must fail in tracker CSV")
            self.assertIn("outcome.md archive status", errors[0].message)

    def test_unknown_status_fails(self) -> None:
        row = [
            "2026-10-19",
            "Acme Corp",
            "Tech",
            "Engineer",
            "Full-time",
            "Portal",
            "interviewing",  # non-standard spelling
            "",
            "80",
            "",
            "",
            "",
            "",
            "",
        ]
        self.write_csv(at.CANONICAL_HEADER, [row])
        issues, _ = at.audit_tracker(self.tracker)
        errors = [i for i in issues if i.level == "error" and i.field == "status"]
        self.assertTrue(len(errors) > 0)
        self.assertIn("unrecognized status", errors[0].message)

    def test_comma_in_notes_fails(self) -> None:
        row = [
            "2026-10-19",
            "Acme Corp",
            "Tech",
            "Engineer",
            "Full-time",
            "Portal",
            "applied",
            "",
            "80",
            "rejected, no feedback given",  # forbidden comma
            "",
            "",
            "",
            "",
        ]
        self.write_csv(at.CANONICAL_HEADER, [row])
        issues, _ = at.audit_tracker(self.tracker)
        errors = [i for i in issues if i.level == "error" and i.field == "notes"]
        self.assertTrue(len(errors) > 0)
        self.assertIn("comma", errors[0].message)

    def test_double_quote_in_notes_fails(self) -> None:
        row = [
            "2026-10-19",
            "Acme Corp",
            "Tech",
            "Engineer",
            "Full-time",
            "Portal",
            "applied",
            "",
            "80",
            'Spoke with "lead engineer"',  # forbidden quotes
            "",
            "",
            "",
            "",
        ]
        self.write_csv(at.CANONICAL_HEADER, [row])
        issues, _ = at.audit_tracker(self.tracker)
        errors = [i for i in issues if i.level == "error" and i.field == "notes"]
        self.assertTrue(len(errors) > 0)
        self.assertIn("double quote", errors[0].message)

    def test_date_validation(self) -> None:
        cases = [
            ("2026-10-19", True),
            ("2026-02-28", True),
            ("", True),  # empty is allowed
            ("2026-02-30", False),  # impossible date
            ("2026/10/19", False),  # wrong separator
            ("19-10-2026", False),  # wrong order
            ("yesterday", False),
        ]
        for val, should_pass in cases:
            row = [
                val,
                "Acme Corp",
                "Tech",
                "Engineer",
                "Full-time",
                "Portal",
                "applied",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
            ]
            self.write_csv(at.CANONICAL_HEADER, [row])
            issues, _ = at.audit_tracker(self.tracker)
            errors = [i for i in issues if i.level == "error" and i.field == "date"]
            if should_pass:
                self.assertEqual(len(errors), 0, f"Date '{val}' should be valid")
            else:
                self.assertTrue(len(errors) > 0, f"Date '{val}' should be flagged as invalid")

    def test_fit_rating_validation(self) -> None:
        cases = [
            ("0", True),
            ("50", True),
            ("100", True),
            ("", True),
            ("-1", False),
            ("101", False),
            ("95/100", False),
            ("high", False),
            ("85.5", False),
        ]
        for val, should_pass in cases:
            row = [
                "2026-10-19",
                "Acme Corp",
                "Tech",
                "Engineer",
                "Full-time",
                "Portal",
                "applied",
                "",
                val,
                "",
                "",
                "",
                "",
                "",
            ]
            self.write_csv(at.CANONICAL_HEADER, [row])
            issues, _ = at.audit_tracker(self.tracker)
            errors = [i for i in issues if i.level == "error" and i.field == "fit_rating"]
            if should_pass:
                self.assertEqual(len(errors), 0, f"fit_rating '{val}' should be valid")
            else:
                self.assertTrue(len(errors) > 0, f"fit_rating '{val}' should be flagged as invalid")

    def test_duplicate_applications_emit_warning(self) -> None:
        row1 = ["2026-10-19", "Acme", "Tech", "SWE", "Full", "Portal", "applied", "", "", "", "", "", "", ""]
        row2 = ["2026-10-20", "Acme", "Tech", "swe", "Full", "Portal", "drafted", "", "", "", "", "", "", ""]
        self.write_csv(at.CANONICAL_HEADER, [row1, row2])
        issues, _ = at.audit_tracker(self.tracker)
        warnings = [i for i in issues if i.level == "warning" and "duplicate" in i.message]
        self.assertEqual(len(warnings), 1)

    def test_check_files_flag(self) -> None:
        # Referenced files do not exist
        row = [
            "2026-10-19",
            "Acme",
            "Tech",
            "SWE",
            "Full",
            "Portal",
            "applied",
            "",
            "",
            "",
            "cv/nonexistent.pdf",
            "cover_letters/nonexistent.pdf",
            "",
            "",
        ]
        self.write_csv(at.CANONICAL_HEADER, [row])
        # Without flag -> no file warnings
        issues_no_check, _ = at.audit_tracker(self.tracker, check_files=False, root_dir=self.tmp)
        self.assertEqual(len([i for i in issues_no_check if "cv_file" in i.field]), 0)

        # With flag -> file warnings
        issues_check, _ = at.audit_tracker(self.tracker, check_files=True, root_dir=self.tmp)
        file_warnings = [i for i in issues_check if "file" in i.field]
        self.assertEqual(len(file_warnings), 2)


class TestAuditTrackerCLI(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.tmp = Path(self.temp_dir.name)
        self.tracker = self.tmp / "test_tracker.csv"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def run_cli(self, args: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(TOOL), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    def test_clean_tracker_exits_0(self) -> None:
        import csv

        with open(self.tracker, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(at.CANONICAL_HEADER)
            w.writerow(["2026-10-19", "Acme", "Tech", "SWE", "Full", "LinkedIn", "applied", "", "90", "Clean", "", "", "", ""])

        proc = self.run_cli([str(self.tracker)])
        self.assertEqual(proc.returncode, 0)
        self.assertIn("clean", proc.stdout.lower())

    def test_tracker_with_violations_exits_1(self) -> None:
        import csv

        with open(self.tracker, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(at.CANONICAL_HEADER)
            w.writerow(["invalid-date", "Acme", "Tech", "SWE", "Full", "LinkedIn", "invalid_status", "", "200", "Bad, note", "", "", "", ""])

        proc = self.run_cli([str(self.tracker)])
        self.assertEqual(proc.returncode, 1)
        self.assertIn("ERROR", proc.stdout)

    def test_json_flag_output(self) -> None:
        import csv

        with open(self.tracker, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(at.CANONICAL_HEADER)
            w.writerow(["2026-10-19", "Acme", "Tech", "SWE", "Full", "LinkedIn", "applied", "", "90", "Clean", "", "", "", ""])

        proc = self.run_cli([str(self.tracker), "--json"])
        self.assertEqual(proc.returncode, 0)
        data = json.loads(proc.stdout)
        self.assertTrue(data["clean"])
        self.assertEqual(data["rows"], 1)
        self.assertEqual(data["error_count"], 0)

    def test_missing_explicit_path_exits_2(self) -> None:
        proc = self.run_cli([str(self.tmp / "does_not_exist.csv")])
        self.assertEqual(proc.returncode, 2)

    def test_unicode_handling(self) -> None:
        import csv

        with open(self.tracker, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(at.CANONICAL_HEADER)
            # Unicode across Turkish, Cyrillic, CJK, Polish
            w.writerow(["2026-10-19", "Şişecam", "Üretim", "Proses Mühendisi", "Staj", "Kariyer", "applied", "Müçteba", "95", "Notlar düzgün", "", "", "", ""])
            w.writerow(["2026-10-19", "Яндекс", "Tech", "Инженер", "Full", "Direct", "interview", "", "88", "Clean", "", "", "", ""])

        proc = self.run_cli([str(self.tracker)])
        self.assertEqual(proc.returncode, 0)
        self.assertIn("2 rows, clean", proc.stdout)


if __name__ == "__main__":
    unittest.main()
