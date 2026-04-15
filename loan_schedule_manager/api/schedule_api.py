"""
API endpoints for Bank Loan Schedule Manager.

Whitelisted methods callable from JS / the upload widget.
"""

import frappe
from frappe import _
from frappe.utils import today, nowdate, flt, cstr
from loan_schedule_manager.utils.pdf_parser import parse_schedule_pdf
import os
import tempfile


# ──────────────────────────────────────────────────────────────────────────────
# 1. UPLOAD & PARSE
# ──────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def upload_and_preview_schedule(file_doc_name: str) -> dict:
    """
    Step 1: Parse a PDF that was already uploaded to Frappe as a File document.
    Returns parsed data for preview + duplicate check result.

    Args:
        file_doc_name: Name of the File doctype record.

    Returns:
        {
            "parsed": { arrangement_id, product_name, customer_id, customer_name,
                        currency, disbursement_date, disbursement_amount,
                        schedule_lines: [...] },
            "duplicate": bool,
            "existing_doc": str | None
        }
    """
    file_doc = frappe.get_doc("File", file_doc_name)
    file_path = frappe.get_site_path() + file_doc.file_url.replace("/files/", "/public/files/")

    if not os.path.exists(file_path):
        # Try private path
        file_path = frappe.get_site_path() + "/private" + file_doc.file_url

    if not os.path.exists(file_path):
        frappe.throw(_("File not found on disk: {0}").format(file_doc.file_url))

    parsed = parse_schedule_pdf(file_path)

    if not parsed.get("arrangement_id"):
        frappe.throw(_("Could not extract Arrangement ID from PDF. Please check the file format."))

    # Duplicate check
    existing = frappe.db.get_value(
        "Bank Loan Schedule",
        {"arrangement_id": parsed["arrangement_id"]},
        "name"
    )

    return {
        "parsed": parsed,
        "duplicate": bool(existing),
        "existing_doc": existing,
        "file_doc_name": file_doc_name,   # echoed back so the wizard can pass it to create_loan_schedule
    }


# ──────────────────────────────────────────────────────────────────────────────
# 2. CREATE LOAN SCHEDULE
# ──────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def create_loan_schedule(parsed_data: dict, accounts: dict, file_doc_name: str = None) -> str:
    """
    Step 2: Create the Bank Loan Schedule document from parsed data + account config.

    Args:
        parsed_data: Output from upload_and_preview_schedule()["parsed"]
        accounts: {
            "loan_account": str,
            "interest_account": str,
            "principal_account": str,
            "bank_account": str,
            "cost_center": str (optional)
        }
        file_doc_name: Optional – attach the PDF to the new doc.

    Returns:
        Name of the created Bank Loan Schedule document.
    """
    if isinstance(parsed_data, str):
        import json
        parsed_data = json.loads(parsed_data)
    if isinstance(accounts, str):
        import json
        accounts = json.loads(accounts)

    arrangement_id = parsed_data.get("arrangement_id")

    # Safety: re-check duplicate
    if frappe.db.exists("Bank Loan Schedule", arrangement_id):
        frappe.throw(
            _("A loan schedule with Arrangement ID {0} already exists.").format(arrangement_id),
            frappe.DuplicateEntryError,
        )

    lines = parsed_data.get("schedule_lines", [])
    if not lines:
        frappe.throw(_("No repayment lines found in the parsed schedule."))

    # Aggregate totals
    total_principal = sum(flt(l.get("principal_amount", 0)) for l in lines)
    total_interest = sum(flt(l.get("interest_amount", 0)) for l in lines)
    last_outstanding = flt(lines[-1].get("outstanding_amount", 0)) if lines else 0
    typical_payment = flt(lines[0].get("total_payment", 0)) if lines else 0

    doc = frappe.get_doc({
        "doctype": "Bank Loan Schedule",
        "arrangement_id": arrangement_id,
        "product_name": parsed_data.get("product_name") or "Hire Purchase",
        "customer_id": parsed_data.get("customer_id"),
        "customer_name": parsed_data.get("customer_name"),
        "currency": parsed_data.get("currency", "USD"),
        "disbursement_date": parsed_data.get("disbursement_date"),
        "disbursement_amount": flt(parsed_data.get("disbursement_amount", 0)),
        "total_principal": total_principal,
        "total_interest": total_interest,
        "outstanding_amount": flt(parsed_data.get("disbursement_amount", 0)),
        "first_repayment_date": lines[0]["due_date"] if lines else None,
        "last_repayment_date": lines[-1]["due_date"] if lines else None,
        "monthly_installment": typical_payment,
        "status": "Active",
        # Accounts
        "loan_account": accounts.get("loan_account"),
        "interest_account": accounts.get("interest_account"),
        "principal_account": accounts.get("principal_account"),
        "bank_account": accounts.get("bank_account"),
        "cost_center": accounts.get("cost_center"),
        # PDF reference
        "pdf_file": frappe.db.get_value("File", file_doc_name, "file_url") if file_doc_name else None,
        # Schedule lines
        "schedule_lines": [
            {
                "doctype": "Bank Loan Schedule Line",
                "due_date": l["due_date"],
                "total_payment": flt(l.get("total_payment", 0)),
                "principal_amount": flt(l.get("principal_amount", 0)),
                "interest_amount": flt(l.get("interest_amount", 0)),
                "outstanding_amount": flt(l.get("outstanding_amount", 0)),
                "status": "Pending",
            }
            for l in lines
        ],
        "adjustment_log": [],
    })

    doc.insert(ignore_permissions=False)
    frappe.db.commit()

    return doc.name


# ──────────────────────────────────────────────────────────────────────────────
# 3. MANUAL JE CREATION TRIGGER (for testing / backfill)
# ──────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def create_journal_entry_for_line(schedule_name: str, line_due_date: str) -> str:
    """
    Manually trigger JE creation for a schedule line identified by its due_date.
    Matching by date is more reliable than array index across Frappe's child table ordering.

    Returns: Journal Entry name
    """
    from loan_schedule_manager.scheduler.tasks import _create_je_for_line
    doc = frappe.get_doc("Bank Loan Schedule", schedule_name)

    matched = [l for l in doc.schedule_lines if str(l.due_date) == str(line_due_date)]
    if not matched:
        frappe.throw(_("No schedule line found with due date {0}").format(line_due_date))
    line = matched[0]

    if line.status == "Posted":
        frappe.throw(_("Journal Entry already posted for this line: {0}").format(line.journal_entry))
    if line.status == "Cancelled":
        frappe.throw(_("This schedule line has been cancelled."))

    je_name = _create_je_for_line(doc, line)
    return je_name


@frappe.whitelist()
def get_schedule_summary(schedule_name: str) -> dict:
    """Return a summary of a loan schedule for display."""
    doc = frappe.get_doc("Bank Loan Schedule", schedule_name)
    posted = sum(1 for l in doc.schedule_lines if l.status == "Posted")
    pending = sum(1 for l in doc.schedule_lines if l.status == "Pending")
    adjusted = sum(1 for l in doc.schedule_lines if l.status == "Adjusted")
    total_paid_principal = sum(flt(l.actual_principal_paid) for l in doc.schedule_lines)
    total_paid_interest = sum(flt(l.actual_interest_paid) for l in doc.schedule_lines)

    return {
        "name": doc.name,
        "arrangement_id": doc.arrangement_id,
        "customer_name": doc.customer_name,
        "currency": doc.currency,
        "total_lines": len(doc.schedule_lines),
        "posted": posted,
        "pending": pending,
        "adjusted": adjusted,
        "total_paid_principal": total_paid_principal,
        "total_paid_interest": total_paid_interest,
        "outstanding_amount": doc.outstanding_amount,
        "status": doc.status,
    }
