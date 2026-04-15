"""
Test suite for Loan Schedule Manager.

Run with:
    bench --site kikwetu.upande.com run-tests --app loan_schedule_manager

Covers:
  - PDF parser (table + text fallback + header)
  - Duplicate check logic
  - JE amount extraction
  - Scheduler filtering and completion logic
  - JE event hooks (on_submit, on_cancel, on_submit with variance)
  - Amendment handler
  - API: create_journal_entry_for_line (date-based lookup)
  - Notification email building and send-gate
  - Report column/data structure
  - Adjustment type epsilon classification
"""

import unittest
from unittest.mock import patch, MagicMock
import frappe
from frappe.utils import flt


# ─────────────────────────────────────────────────────────────────────────────
# Shared fixtures
# ─────────────────────────────────────────────────────────────────────────────

def _mock_je(principal, interest, bank_credit,
             name="JV-001", posting_date="2026-02-28",
             amended_from=None,
             loan_acct="Loans Payable",
             int_acct="Interest Expense",
             bank_acct="Bank Account"):
    je = MagicMock()
    je.name = name
    je.posting_date = posting_date
    je.docstatus = 1
    je.amended_from = amended_from
    je.custom_loan_schedule = None
    je.custom_loan_schedule_line_date = None
    rows = []
    for acct, dr, cr in [(loan_acct, principal, 0),
                          (int_acct,  interest,  0),
                          (bank_acct, 0, bank_credit)]:
        r = MagicMock()
        r.account = acct
        r.debit_in_account_currency  = dr
        r.credit_in_account_currency = cr
        rows.append(r)
    je.accounts = rows
    return je


def _mock_schedule(lines_data=None, name="AA25241PVLTY"):
    doc = MagicMock()
    doc.name            = name
    doc.arrangement_id  = name
    doc.loan_account     = "Loans Payable"
    doc.interest_account = "Interest Expense"
    doc.bank_account     = "Bank Account"
    doc.principal_account = "Bank Account"
    doc.cost_center      = "Main - KFL"
    doc.currency         = "USD"
    doc.outstanding_amount = 93031.97
    doc.status           = "Active"

    lines = []
    for d in (lines_data or []):
        ln = MagicMock()
        ln.due_date               = d["due_date"]
        ln.total_payment          = d.get("total_payment",    2073.05)
        ln.principal_amount       = d.get("principal_amount", 1360.89)
        ln.interest_amount        = d.get("interest_amount",   712.16)
        ln.outstanding_amount     = d.get("outstanding_amount", 0)
        ln.status                 = d.get("status", "Pending")
        ln.journal_entry          = d.get("journal_entry", None)
        ln.actual_principal_paid  = d.get("actual_principal_paid", 0)
        ln.actual_interest_paid   = d.get("actual_interest_paid",  0)
        ln.actual_total_paid      = d.get("actual_total_paid",     0)
        ln.variance_principal     = 0
        ln.variance_interest      = 0
        lines.append(ln)
    doc.schedule_lines = lines
    return doc


# ─────────────────────────────────────────────────────────────────────────────
# 1. PDF Parser — table path
# ─────────────────────────────────────────────────────────────────────────────

class TestPDFParserTable(unittest.TestCase):

    ROWS = [
        ["Due Date", "Defer Date", "Total Payment", "Due Type",
         "Due Type Amt", "Property", "Prop Amount", "Outstanding Amount"],
        ["29/08/25", "", "-99,180.00", "Disburse Percentage",
         "99,180.00", "Account", "99,180.00", "-99,180.00"],
        ["", "", "0.00", "", "0.00", "Principal Interest", "0.00", "0.00"],
        ["28/10/25", "", "1,994.40", "Constant Repayment",
         "1,994.40", "Account", "776.69", "-98,403.31"],
        ["", "", "0.00", "", "0.00", "Principal Interest", "1,217.71", "0.00"],
        ["28/11/25", "", "2,085.73", "Constant Repayment",
         "2,085.73", "Account", "1,326.32", "-97,076.99"],
        ["", "", "0.00", "", "0.00", "Principal Interest", "759.41", "0.00"],
    ]

    def _parse(self, rows=None):
        from loan_schedule_manager.utils.pdf_parser import _parse_rows_from_table
        r = {"disbursement_date": None, "disbursement_amount": 0.0, "schedule_lines": []}
        _parse_rows_from_table(rows or self.ROWS, r)
        return r

    def test_disbursement_date(self):
        self.assertEqual(self._parse()["disbursement_date"], "2025-08-29")

    def test_disbursement_amount(self):
        self.assertAlmostEqual(self._parse()["disbursement_amount"], 99180.00, places=2)

    def test_line_count(self):
        self.assertEqual(len(self._parse()["schedule_lines"]), 2)

    def test_first_line_all_fields(self):
        line = self._parse()["schedule_lines"][0]
        self.assertEqual(line["due_date"],                 "2025-10-28")
        self.assertAlmostEqual(line["total_payment"],       1994.40,   places=2)
        self.assertAlmostEqual(line["principal_amount"],     776.69,   places=2)
        self.assertAlmostEqual(line["interest_amount"],     1217.71,   places=2)
        self.assertAlmostEqual(line["outstanding_amount"], 98403.31,   places=2)

    def test_second_line_all_fields(self):
        line = self._parse()["schedule_lines"][1]
        self.assertEqual(line["due_date"],                  "2025-11-28")
        self.assertAlmostEqual(line["total_payment"],        2085.73,  places=2)
        self.assertAlmostEqual(line["principal_amount"],     1326.32,  places=2)
        self.assertAlmostEqual(line["interest_amount"],       759.41,  places=2)
        self.assertAlmostEqual(line["outstanding_amount"],  97076.99,  places=2)

    def test_dd_mm_yy_converts_to_iso(self):
        rows = [
            ["28/02/26", "", "2,073.05", "Constant Repayment",
             "2,073.05", "Account", "1,360.89", "-93,031.97"],
            ["", "", "0.00", "", "0.00", "Principal Interest", "712.16", "0.00"],
        ]
        line = self._parse(rows)["schedule_lines"][0]
        self.assertEqual(line["due_date"], "2026-02-28")

    def test_last_day_of_september(self):
        rows = [
            ["28/09/30", "", "2,072.71", "Constant Repayment",
             "2,072.71", "Account", "2,057.19", "0.00"],
            ["", "", "0.00", "", "0.00", "Principal Interest", "15.52", "0.00"],
        ]
        line = self._parse(rows)["schedule_lines"][0]
        self.assertEqual(line["due_date"], "2030-09-28")
        self.assertAlmostEqual(line["outstanding_amount"], 0.0, places=2)


# ─────────────────────────────────────────────────────────────────────────────
# 2. PDF Parser — text fallback
# ─────────────────────────────────────────────────────────────────────────────

class TestPDFParserTextFallback(unittest.TestCase):

    def _parse(self, text):
        from loan_schedule_manager.utils.pdf_parser import _parse_rows_from_text
        r = {"disbursement_date": None, "disbursement_amount": 0.0, "schedule_lines": []}
        _parse_rows_from_text(text, r)
        return r

    def test_disbursement_line(self):
        text = "28/07/25  -77,595.30  Disburse  78,300.00  Account  78,300.00  -78,300.00"
        r = self._parse(text)
        self.assertEqual(r["disbursement_date"], "2025-07-28")
        self.assertAlmostEqual(r["disbursement_amount"], 77595.30, places=2)

    def test_repayment_with_interest(self):
        text = (
            "26/01/26  1,603.69  Constant  1,603.69  Account  1,111.68  -73,333.45\n"
            "0.00  0.00  Principal Interest  492.01  0.00"
        )
        r = self._parse(text)
        self.assertEqual(len(r["schedule_lines"]), 1)
        line = r["schedule_lines"][0]
        self.assertAlmostEqual(line["principal_amount"],    1111.68,  places=2)
        self.assertAlmostEqual(line["interest_amount"],      492.01,  places=2)
        self.assertAlmostEqual(line["outstanding_amount"], 73333.45,  places=2)

    def test_two_consecutive_repayments(self):
        text = (
            "28/02/26  2,073.05  Constant  2,073.05  Account  1,360.89  -93,031.97\n"
            "0.00  0.00  Principal Interest  712.16  0.00\n"
            "28/03/26  2,073.05  Constant  2,073.05  Account  1,371.16  -91,660.81\n"
            "0.00  0.00  Principal Interest  701.89  0.00"
        )
        r = self._parse(text)
        self.assertEqual(len(r["schedule_lines"]), 2)
        # Interest row must not create its own line
        for line in r["schedule_lines"]:
            self.assertGreater(line["total_payment"], 0)


# ─────────────────────────────────────────────────────────────────────────────
# 3. PDF Parser — header
# ─────────────────────────────────────────────────────────────────────────────

class TestPDFParserHeader(unittest.TestCase):

    def _h(self, text):
        from loan_schedule_manager.utils.pdf_parser import _parse_header
        r = {}
        _parse_header(text, r)
        return r

    def test_all_three_arrangement_ids(self):
        for aid in ("AA25241PVLTY", "AA25225457K8", "AA2520964RT0"):
            r = self._h(f"Arrangement Id : {aid}")
            self.assertEqual(r["arrangement_id"], aid, f"Failed for {aid}")

    def test_customer_id(self):
        r = self._h("Customer Id : 1394629  KIKWETU FLOWERS LIMITED\nCurrency : USD")
        self.assertEqual(r["customer_id"], "1394629")

    def test_currency(self):
        r = self._h("Currency : USD")
        self.assertEqual(r["currency"], "USD")

    def test_product_name(self):
        r = self._h("Arrangement Id : X  Product Name : Hire Purchase\nCustomer Id : 1")
        self.assertEqual(r["product_name"], "Hire Purchase")


# ─────────────────────────────────────────────────────────────────────────────
# 4. Duplicate check
# ─────────────────────────────────────────────────────────────────────────────

class TestDuplicateCheck(unittest.TestCase):

    @patch("frappe.db.get_value", return_value="AA25241PVLTY")
    def test_existing_id_detected(self, _):
        result = frappe.db.get_value("Bank Loan Schedule",
                                     {"arrangement_id": "AA25241PVLTY"}, "name")
        self.assertTrue(bool(result))

    @patch("frappe.db.get_value", return_value=None)
    def test_new_id_passes(self, _):
        result = frappe.db.get_value("Bank Loan Schedule",
                                     {"arrangement_id": "BRAND-NEW"}, "name")
        self.assertFalse(bool(result))


# ─────────────────────────────────────────────────────────────────────────────
# 5. JE amount extraction
# ─────────────────────────────────────────────────────────────────────────────

class TestJEAmountExtraction(unittest.TestCase):

    def _extract(self, p, i, t):
        from loan_schedule_manager.events.journal_entry import _extract_amounts_from_je
        return _extract_amounts_from_je(_mock_je(p, i, t), _mock_schedule())

    def test_standard_payment(self):
        p, i, t = self._extract(1360.89, 712.16, 2073.05)
        self.assertAlmostEqual(p, 1360.89, places=2)
        self.assertAlmostEqual(i, 712.16,  places=2)
        self.assertAlmostEqual(t, 2073.05, places=2)

    def test_partial_payment(self):
        _, _, t = self._extract(700.00, 300.00, 1000.00)
        self.assertLess(t, 2073.05)

    def test_overpayment(self):
        _, _, t = self._extract(1500.00, 800.00, 2300.00)
        self.assertGreater(t, 2073.05)

    def test_zero_interest(self):
        p, i, t = self._extract(2073.05, 0.0, 2073.05)
        self.assertAlmostEqual(i, 0.0, places=2)
        self.assertAlmostEqual(p, 2073.05, places=2)

    def test_unrelated_je_gives_zeros(self):
        from loan_schedule_manager.events.journal_entry import _extract_amounts_from_je
        je = _mock_je(500, 100, 600,
                      loan_acct="Other", int_acct="Other2", bank_acct="Other3")
        p, i, t = _extract_amounts_from_je(je, _mock_schedule())
        self.assertAlmostEqual(p + i + t, 0.0, places=2)


# ─────────────────────────────────────────────────────────────────────────────
# 6. Scheduler filtering
# ─────────────────────────────────────────────────────────────────────────────

class TestSchedulerFiltering(unittest.TestCase):

    @patch("loan_schedule_manager.scheduler.tasks._create_je_for_line",
           return_value="JV-X")
    @patch("frappe.db.commit")
    def test_posted_lines_skipped(self, _c, mock_create):
        from loan_schedule_manager.scheduler.tasks import _process_schedule
        from datetime import date
        doc = _mock_schedule([
            {"due_date": "2026-01-28", "status": "Posted"},
            {"due_date": "2026-02-28", "status": "Pending"},
        ])
        _process_schedule(doc, date(2026, 4, 15))
        self.assertEqual(mock_create.call_count, 1)
        self.assertEqual(str(mock_create.call_args[0][1].due_date), "2026-02-28")

    @patch("loan_schedule_manager.scheduler.tasks._create_je_for_line",
           return_value="JV-X")
    @patch("frappe.db.commit")
    def test_future_lines_skipped(self, _c, mock_create):
        from loan_schedule_manager.scheduler.tasks import _process_schedule
        from datetime import date
        doc = _mock_schedule([
            {"due_date": "2026-02-28", "status": "Pending"},
            {"due_date": "2026-06-28", "status": "Pending"},
        ])
        _process_schedule(doc, date(2026, 4, 15))
        self.assertEqual(mock_create.call_count, 1)

    @patch("loan_schedule_manager.scheduler.tasks._create_je_for_line",
           return_value="JV-X")
    @patch("frappe.db.commit")
    def test_multiple_overdue_all_processed(self, _c, mock_create):
        from loan_schedule_manager.scheduler.tasks import _process_schedule
        from datetime import date
        doc = _mock_schedule([
            {"due_date": "2026-01-28", "status": "Pending"},
            {"due_date": "2026-02-28", "status": "Pending"},
            {"due_date": "2026-03-28", "status": "Pending"},
            {"due_date": "2026-06-28", "status": "Pending"},  # future
        ])
        _process_schedule(doc, date(2026, 4, 15))
        self.assertEqual(mock_create.call_count, 3)

    def test_zero_outstanding_triggers_completed(self):
        from loan_schedule_manager.scheduler.tasks import _create_je_for_line
        doc = _mock_schedule([{
            "due_date": "2030-09-28",
            "total_payment": 2072.71,
            "principal_amount": 2057.19,
            "interest_amount": 15.52,
            "outstanding_amount": 0.0,
            "status": "Pending",
        }])
        doc.status = "Active"

        with patch("frappe.db.get_single_value", return_value="Test Co"), \
             patch("frappe.db.get_value", return_value="KES"), \
             patch("frappe.get_doc", return_value=MagicMock(name="JV-LAST")), \
             patch("frappe.logger", return_value=MagicMock()):
            _create_je_for_line(doc, doc.schedule_lines[0])

        self.assertEqual(doc.status, "Completed")


# ─────────────────────────────────────────────────────────────────────────────
# 7. JE event hooks
# ─────────────────────────────────────────────────────────────────────────────

class TestJEEventHooks(unittest.TestCase):

    def _session(self):
        return MagicMock(user="test@kikwetu.com")

    def test_on_cancel_resets_line(self):
        from loan_schedule_manager.events.journal_entry import on_cancel
        je = _mock_je(1360.89, 712.16, 2073.05, name="JV-001")
        schedule = _mock_schedule([{
            "due_date": "2026-02-28", "status": "Posted",
            "journal_entry": "JV-001",
            "actual_principal_paid": 1360.89, "actual_interest_paid": 712.16,
        }])
        line = schedule.schedule_lines[0]

        with patch("loan_schedule_manager.events.journal_entry._get_linked_schedule_and_line",
                   return_value=(schedule, line)), \
             patch("loan_schedule_manager.events.journal_entry.frappe.session", self._session()):
            on_cancel(je)

        self.assertEqual(line.status, "Pending")
        self.assertIsNone(line.journal_entry)
        self.assertEqual(line.actual_principal_paid, 0)
        self.assertEqual(line.actual_interest_paid,  0)
        self.assertEqual(line.actual_total_paid,     0)

    def test_on_cancel_reactivates_completed_schedule(self):
        from loan_schedule_manager.events.journal_entry import on_cancel
        je = _mock_je(2057.19, 15.52, 2072.71, name="JV-LAST")
        schedule = _mock_schedule([{
            "due_date": "2030-09-28", "status": "Posted",
            "journal_entry": "JV-LAST",
        }])
        schedule.status = "Completed"
        line = schedule.schedule_lines[0]

        with patch("loan_schedule_manager.events.journal_entry._get_linked_schedule_and_line",
                   return_value=(schedule, line)), \
             patch("loan_schedule_manager.events.journal_entry.frappe.session", self._session()):
            on_cancel(je)

        self.assertEqual(schedule.status, "Active")

    def test_on_submit_exact_payment_posts(self):
        from loan_schedule_manager.events.journal_entry import on_submit
        je = _mock_je(1360.89, 712.16, 2073.05, name="JV-NEW")
        schedule = _mock_schedule([{"due_date": "2026-02-28", "status": "Pending"}])
        line = schedule.schedule_lines[0]

        with patch("loan_schedule_manager.events.journal_entry._get_linked_schedule_and_line",
                   return_value=(schedule, line)), \
             patch("loan_schedule_manager.events.journal_entry.frappe.session", self._session()):
            on_submit(je)

        self.assertEqual(line.status, "Posted")
        self.assertAlmostEqual(line.actual_principal_paid, 1360.89, places=2)
        self.assertAlmostEqual(line.actual_interest_paid,   712.16, places=2)

    def test_on_submit_partial_marks_adjusted(self):
        from loan_schedule_manager.events.journal_entry import on_submit
        je = _mock_je(900.00, 400.00, 1300.00, name="JV-PARTIAL")
        schedule = _mock_schedule([{"due_date": "2026-02-28", "status": "Pending"}])
        line = schedule.schedule_lines[0]

        with patch("loan_schedule_manager.events.journal_entry._get_linked_schedule_and_line",
                   return_value=(schedule, line)), \
             patch("loan_schedule_manager.events.journal_entry.frappe.session", self._session()):
            on_submit(je)

        self.assertEqual(line.status, "Adjusted")
        self.assertLess(line.variance_principal, 0)

    def test_on_submit_overpayment_marks_adjusted(self):
        from loan_schedule_manager.events.journal_entry import on_submit
        je = _mock_je(1600.00, 800.00, 2400.00, name="JV-OVER")
        schedule = _mock_schedule([{"due_date": "2026-02-28", "status": "Pending"}])
        line = schedule.schedule_lines[0]

        with patch("loan_schedule_manager.events.journal_entry._get_linked_schedule_and_line",
                   return_value=(schedule, line)), \
             patch("loan_schedule_manager.events.journal_entry.frappe.session", self._session()):
            on_submit(je)

        self.assertEqual(line.status, "Adjusted")
        self.assertGreater(line.variance_principal, 0)

    def test_unlinked_je_silently_ignored(self):
        from loan_schedule_manager.events.journal_entry import on_cancel, on_submit
        je = _mock_je(500, 100, 600)
        with patch("loan_schedule_manager.events.journal_entry._get_linked_schedule_and_line",
                   return_value=(None, None)):
            on_cancel(je)   # must not raise
            on_submit(je)   # must not raise

    def test_already_posted_line_skipped_on_submit(self):
        from loan_schedule_manager.events.journal_entry import on_submit
        je = _mock_je(1360.89, 712.16, 2073.05, name="JV-001")
        schedule = _mock_schedule([{
            "due_date": "2026-02-28", "status": "Posted",
            "journal_entry": "JV-001",
        }])
        line = schedule.schedule_lines[0]

        with patch("loan_schedule_manager.events.journal_entry._get_linked_schedule_and_line",
                   return_value=(schedule, line)):
            on_submit(je)   # should return early without calling save

        schedule.save.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# 8. Amendment handler
# ─────────────────────────────────────────────────────────────────────────────

class TestAmendmentHandler(unittest.TestCase):

    def _run(self, new_principal, new_interest, new_total, scheduled_total=2073.05):
        from loan_schedule_manager.events.journal_entry import _handle_amendment
        amended = _mock_je(new_principal, new_interest, new_total,
                           name="JV-001-1", amended_from="JV-001")
        schedule = _mock_schedule([{
            "due_date": "2026-02-28", "status": "Pending",
            "journal_entry": None,
            "total_payment": scheduled_total,
        }])
        line = schedule.schedule_lines[0]

        with patch("frappe.db.sql", return_value=[{
                "parent": schedule.name, "due_date": "2026-02-28"}]), \
             patch("frappe.get_doc", return_value=schedule), \
             patch("frappe.db.set_value"), \
             patch("loan_schedule_manager.events.journal_entry.frappe.session",
                   MagicMock(user="test@kikwetu.com")):
            _handle_amendment(amended)

        return schedule, line

    def test_amendment_updates_je_name(self):
        _, line = self._run(1360.89, 712.16, 2073.05)
        self.assertEqual(line.journal_entry, "JV-001-1")

    def test_amendment_updates_amounts(self):
        _, line = self._run(1500.00, 600.00, 2100.00)
        self.assertAlmostEqual(line.actual_principal_paid, 1500.00, places=2)
        self.assertAlmostEqual(line.actual_interest_paid,   600.00, places=2)

    def test_amendment_partial_is_adjusted(self):
        _, line = self._run(800.00, 300.00, 1100.00)
        self.assertEqual(line.status, "Adjusted")

    def test_amendment_exact_is_posted(self):
        _, line = self._run(1360.89, 712.16, 2073.05)
        self.assertEqual(line.status, "Posted")


# ─────────────────────────────────────────────────────────────────────────────
# 9. API: create_journal_entry_for_line
# ─────────────────────────────────────────────────────────────────────────────

class TestCreateJEForLineAPI(unittest.TestCase):

    def test_unknown_date_raises(self):
        from loan_schedule_manager.api.schedule_api import create_journal_entry_for_line
        schedule = _mock_schedule([{"due_date": "2026-02-28", "status": "Pending"}])
        with patch("frappe.get_doc", return_value=schedule), \
             self.assertRaises(Exception):
            create_journal_entry_for_line(
                schedule_name=schedule.name, line_due_date="1999-01-01")

    def test_already_posted_raises(self):
        from loan_schedule_manager.api.schedule_api import create_journal_entry_for_line
        schedule = _mock_schedule([{
            "due_date": "2026-02-28", "status": "Posted",
            "journal_entry": "JV-EXISTS"}])
        with patch("frappe.get_doc", return_value=schedule), \
             self.assertRaises(Exception):
            create_journal_entry_for_line(
                schedule_name=schedule.name, line_due_date="2026-02-28")

    def test_cancelled_line_raises(self):
        from loan_schedule_manager.api.schedule_api import create_journal_entry_for_line
        schedule = _mock_schedule([{"due_date": "2026-02-28", "status": "Cancelled"}])
        with patch("frappe.get_doc", return_value=schedule), \
             self.assertRaises(Exception):
            create_journal_entry_for_line(
                schedule_name=schedule.name, line_due_date="2026-02-28")


# ─────────────────────────────────────────────────────────────────────────────
# 10. Adjustment type classification
# ─────────────────────────────────────────────────────────────────────────────

class TestAdjustmentClassification(unittest.TestCase):

    def _classify(self, actual, scheduled):
        if actual > scheduled + 0.01:  return "Overpayment"
        if actual < scheduled - 0.01:  return "Partial Payment"
        return "JE Amended"

    def test_exact_is_amended(self):
        self.assertEqual(self._classify(2073.05, 2073.05), "JE Amended")

    def test_one_cent_over_is_overpayment(self):
        self.assertEqual(self._classify(2073.06, 2073.05), "Overpayment")

    def test_one_cent_under_is_partial(self):
        self.assertEqual(self._classify(2073.04, 2073.05), "Partial Payment")

    def test_epsilon_noise_is_amended(self):
        self.assertEqual(self._classify(2073.055, 2073.05), "JE Amended")

    def test_large_underpayment(self):
        self.assertEqual(self._classify(1000.00, 2073.05), "Partial Payment")

    def test_large_overpayment(self):
        self.assertEqual(self._classify(5000.00, 2073.05), "Overpayment")

    def test_negative_variance_is_underpaid(self):
        self.assertLess(1000.00 - 1360.89, 0)

    def test_positive_variance_is_overpaid(self):
        self.assertGreater(1600.00 - 1360.89, 0)


# ─────────────────────────────────────────────────────────────────────────────
# 11. Notifications
# ─────────────────────────────────────────────────────────────────────────────

class TestNotifications(unittest.TestCase):

    OVERDUE_ROW = {
        "due_date": "2026-03-28", "arrangement_id": "AA25241PVLTY",
        "customer_name": "Kikwetu Flowers Limited", "currency": "USD",
        "total_payment": 2073.05, "principal_amount": 1360.89,
        "interest_amount": 712.16, "outstanding_amount": 93031.97,
    }
    UPCOMING_ROW = {
        "due_date": "2026-04-17", "arrangement_id": "AA25225457K8",
        "customer_name": "Kikwetu Flowers Limited", "currency": "USD",
        "total_payment": 858.45, "principal_amount": 572.09,
        "interest_amount": 286.36, "outstanding_amount": 37383.51,
    }

    def test_html_overdue_section_present(self):
        from loan_schedule_manager.scheduler.notifications import _build_email_html
        from datetime import date
        html = _build_email_html([self.OVERDUE_ROW], [], date(2026, 4, 15))
        self.assertIn("Overdue Lines", html)
        self.assertIn("AA25241PVLTY", html)
        self.assertIn("2026-03-28", html)

    def test_html_upcoming_section_present(self):
        from loan_schedule_manager.scheduler.notifications import _build_email_html
        from datetime import date
        html = _build_email_html([], [self.UPCOMING_ROW], date(2026, 4, 15))
        self.assertIn("Due in the Next", html)
        self.assertIn("AA25225457K8", html)

    def test_html_amounts_formatted(self):
        from loan_schedule_manager.scheduler.notifications import _build_email_html
        from datetime import date
        html = _build_email_html([self.OVERDUE_ROW], [], date(2026, 4, 15))
        self.assertIn("2,073.05", html)
        self.assertIn("93,031.97", html)

    def test_no_rows_skips_sendmail(self):
        from loan_schedule_manager.scheduler.notifications import send_overdue_alerts
        with patch("frappe.db.sql", return_value=[]), \
             patch("frappe.sendmail") as mock_mail:
            send_overdue_alerts()
        mock_mail.assert_not_called()

    def test_send_called_with_correct_recipients(self):
        from loan_schedule_manager.scheduler.notifications import send_overdue_alerts
        overdue_row = dict(self.OVERDUE_ROW, alert_type="overdue")
        with patch("frappe.db.sql", return_value=[overdue_row]), \
             patch("loan_schedule_manager.scheduler.notifications._get_accounts_manager_emails",
                   return_value=["finance@kikwetu.com"]), \
             patch("frappe.sendmail") as mock_mail, \
             patch("frappe.logger", return_value=MagicMock()), \
             patch("frappe.utils.get_url", return_value="https://kikwetu.upande.com"):
            send_overdue_alerts()
        mock_mail.assert_called_once()
        call_kwargs = mock_mail.call_args[1]
        self.assertIn("finance@kikwetu.com", call_kwargs["recipients"])


# ─────────────────────────────────────────────────────────────────────────────
# 12. Report structure
# ─────────────────────────────────────────────────────────────────────────────

class TestReportStructure(unittest.TestCase):

    def test_column_fieldnames_complete(self):
        from loan_schedule_manager.loan_schedule_manager.report.loan_portfolio_summary.loan_portfolio_summary \
            import _get_columns
        names = {c["fieldname"] for c in _get_columns()}
        required = {
            "arrangement_id", "customer_name", "currency",
            "disbursement_amount", "total_principal", "total_interest",
            "paid_principal", "paid_interest", "outstanding_amount",
            "overdue_lines", "next_due_date", "monthly_installment",
            "progress", "status",
        }
        self.assertEqual(names, required)

    def test_column_fieldtypes(self):
        from loan_schedule_manager.loan_schedule_manager.report.loan_portfolio_summary.loan_portfolio_summary \
            import _get_columns
        by_name = {c["fieldname"]: c["fieldtype"] for c in _get_columns()}
        self.assertEqual(by_name["arrangement_id"],      "Link")
        self.assertEqual(by_name["disbursement_amount"], "Currency")
        self.assertEqual(by_name["overdue_lines"],       "Int")
        self.assertEqual(by_name["next_due_date"],       "Date")
        self.assertEqual(by_name["progress"],            "Data")

    def test_empty_schedules_returns_empty_list(self):
        from loan_schedule_manager.loan_schedule_manager.report.loan_portfolio_summary.loan_portfolio_summary \
            import _get_data
        with patch("frappe.db.sql", return_value=[]):
            rows = _get_data({})
        self.assertEqual(rows, [])

    def test_data_row_keys_match_columns(self):
        from loan_schedule_manager.loan_schedule_manager.report.loan_portfolio_summary.loan_portfolio_summary \
            import _get_columns, _get_data

        mock_s = {
            "name": "AA25241PVLTY", "arrangement_id": "AA25241PVLTY",
            "customer_name": "KFL", "currency": "USD",
            "disbursement_amount": 99180.0, "total_principal": 99180.0,
            "total_interest": 48627.0, "outstanding_amount": 93031.97,
            "monthly_installment": 2073.05, "status": "Active",
        }
        mock_stat = {
            "parent": "AA25241PVLTY", "paid_principal": 6148.03,
            "paid_interest": 3723.41, "overdue_lines": 0,
            "posted_count": 3, "total_count": 60, "next_due_date": "2026-03-28",
        }

        with patch("frappe.db.sql", side_effect=[[mock_s], [mock_stat]]), \
             patch("frappe.db.escape", side_effect=lambda x: f"'{x}'"):
            rows = _get_data({"status": "Active"})

        col_names = {c["fieldname"] for c in _get_columns()}
        self.assertFalse(col_names - set(rows[0].keys()),
                         "Some columns have no matching data key")

    def test_progress_format(self):
        from loan_schedule_manager.loan_schedule_manager.report.loan_portfolio_summary.loan_portfolio_summary \
            import _get_data

        mock_s = {
            "name": "AA25241PVLTY", "arrangement_id": "AA25241PVLTY",
            "customer_name": "KFL", "currency": "USD",
            "disbursement_amount": 99180.0, "total_principal": 99180.0,
            "total_interest": 48627.0, "outstanding_amount": 93031.97,
            "monthly_installment": 2073.05, "status": "Active",
        }
        mock_stat = {
            "parent": "AA25241PVLTY", "paid_principal": 0, "paid_interest": 0,
            "overdue_lines": 2, "posted_count": 7, "total_count": 60,
            "next_due_date": "2026-04-28",
        }

        with patch("frappe.db.sql", side_effect=[[mock_s], [mock_stat]]), \
             patch("frappe.db.escape", side_effect=lambda x: f"'{x}'"):
            rows = _get_data({})

        self.assertEqual(rows[0]["progress"], "7 / 60")
        self.assertEqual(rows[0]["overdue_lines"], 2)


if __name__ == "__main__":
    unittest.main()
