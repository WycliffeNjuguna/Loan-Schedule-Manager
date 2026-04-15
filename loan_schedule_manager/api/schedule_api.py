"""
API endpoints for Bank Loan Schedule Manager.

NOTE: No type hints on @frappe.whitelist() functions.
Frappe v15 Pydantic validation rejects JSON strings before our
isinstance() deserialization checks can run.
"""

import json
import os

import frappe
from frappe import _
from frappe.utils import flt

from loan_schedule_manager.utils.pdf_parser import parse_schedule_pdf


# ──────────────────────────────────────────────────────────────────────────────
# 1. UPLOAD & PARSE
# ──────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def upload_and_preview_schedule(file_doc_name):
    """
    Parse an uploaded PDF and return parsed data + duplicate check.

    Returns:
        {
            "parsed":       { arrangement_id, product_name, customer_id,
                              customer_name, currency, disbursement_date,
                              disbursement_amount, schedule_lines: [...] },
            "duplicate":    bool,
            "existing_doc": str | None,
            "file_doc_name": str
        }
    """
    file_doc  = frappe.get_doc("File", file_doc_name)
    file_path = frappe.get_site_path("public") + file_doc.file_url

    if not os.path.exists(file_path):
        file_path = frappe.get_site_path("private") + file_doc.file_url
    if not os.path.exists(file_path):
        file_path = frappe.get_site_path() + file_doc.file_url.replace("/files/", "/public/files/")
    if not os.path.exists(file_path):
        frappe.throw(_("File not found on disk: {0}").format(file_doc.file_url))

    parsed = parse_schedule_pdf(file_path)

    if not parsed.get("arrangement_id"):
        frappe.throw(_(
            "Could not extract Arrangement ID from PDF. "
            "Please check the file is an Arrangement Schedule Projection document."
        ))

    existing = frappe.db.get_value(
        "Bank Loan Schedule",
        {"arrangement_id": parsed["arrangement_id"]},
        "name",
    )

    return {
        "parsed":        parsed,
        "duplicate":     bool(existing),
        "existing_doc":  existing,
        "file_doc_name": file_doc_name,
    }


# ──────────────────────────────────────────────────────────────────────────────
# 2. CREATE LOAN SCHEDULE
# ──────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def create_loan_schedule(parsed_data, accounts, file_doc_name=None):
    """
    Create the Bank Loan Schedule document.

    Args:
        parsed_data  : dict or JSON string from upload_and_preview_schedule()["parsed"]
        accounts     : dict or JSON string with keys:
                         loan_account, interest_account, principal_account,
                         bank_account, cost_center (optional)
        file_doc_name: optional File docname to attach the source PDF

    Returns: Name of the created Bank Loan Schedule document.
    """
    if isinstance(parsed_data, str):
        parsed_data = json.loads(parsed_data)
    if isinstance(accounts, str):
        accounts = json.loads(accounts)

    arrangement_id = parsed_data.get("arrangement_id")
    if not arrangement_id:
        frappe.throw(_("Arrangement ID missing from parsed data."))

    if frappe.db.exists("Bank Loan Schedule", arrangement_id):
        frappe.throw(
            _("A loan schedule with Arrangement ID {0} already exists.").format(arrangement_id),
            frappe.DuplicateEntryError,
        )

    lines = parsed_data.get("schedule_lines", [])
    if not lines:
        frappe.throw(_("No repayment lines found in the parsed schedule."))

    total_principal  = sum(flt(l.get("principal_amount", 0)) for l in lines)
    total_interest   = sum(flt(l.get("interest_amount",  0)) for l in lines)
    typical_payment  = flt(lines[0].get("total_payment", 0))
    disbursement_amt = flt(parsed_data.get("disbursement_amount", 0))

    doc = frappe.get_doc({
        "doctype":             "Bank Loan Schedule",
        "arrangement_id":      arrangement_id,
        "product_name":        parsed_data.get("product_name") or "Hire Purchase",
        "customer_id":         parsed_data.get("customer_id"),
        "customer_name":       parsed_data.get("customer_name"),
        "currency":            parsed_data.get("currency", "USD"),
        "disbursement_date":   parsed_data.get("disbursement_date"),
        "disbursement_amount": disbursement_amt,
        "total_principal":     total_principal,
        "total_interest":      total_interest,
        "outstanding_amount":  disbursement_amt,
        "first_repayment_date": lines[0]["due_date"],
        "last_repayment_date":  lines[-1]["due_date"],
        "monthly_installment":  typical_payment,
        "status":              "Active",
        "loan_account":        accounts.get("loan_account"),
        "interest_account":    accounts.get("interest_account"),
        "principal_account":   accounts.get("principal_account"),
        "bank_account":        accounts.get("bank_account"),
        "cost_center":         accounts.get("cost_center"),
        "pdf_file": (
            frappe.db.get_value("File", file_doc_name, "file_url")
            if file_doc_name else None
        ),
        "schedule_lines": [
            {
                "doctype":            "Bank Loan Schedule Line",
                "due_date":           l["due_date"],
                "total_payment":      flt(l.get("total_payment",    0)),
                "principal_amount":   flt(l.get("principal_amount", 0)),
                "interest_amount":    flt(l.get("interest_amount",  0)),
                "outstanding_amount": flt(l.get("outstanding_amount", 0)),
                "status":             "Pending",
            }
            for l in lines
        ],
        "adjustment_log": [],
    })

    doc.insert(ignore_permissions=False)
    frappe.db.commit()

    return doc.name


# ──────────────────────────────────────────────────────────────────────────────
# 3. SINGLE LINE JE CREATION
# ──────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def create_journal_entry_for_line(schedule_name, line_due_date):
    """
    Create a draft Bank Entry for a single schedule line identified by due_date.
    Returns an existing draft JE if one already exists for that line.
    Returns: Journal Entry name.
    """
    from loan_schedule_manager.scheduler.tasks import _create_je_for_line

    doc     = frappe.get_doc("Bank Loan Schedule", schedule_name)
    matched = [l for l in doc.schedule_lines if str(l.due_date) == str(line_due_date)]

    if not matched:
        frappe.throw(_("No schedule line found with due date {0}").format(line_due_date))

    line = matched[0]

    if line.status == "Posted":
        frappe.throw(
            _("Journal Entry already posted for this line: {0}").format(line.journal_entry)
        )
    if line.status == "Cancelled":
        frappe.throw(_("This schedule line has been cancelled."))

    # Return existing draft instead of creating a duplicate
    if line.journal_entry:
        existing_docstatus = frappe.db.get_value(
            "Journal Entry", line.journal_entry, "docstatus"
        )
        if existing_docstatus == 0:
            return line.journal_entry

    return _create_je_for_line(doc, line)


# ──────────────────────────────────────────────────────────────────────────────
# 4. BULK JE CREATION
# ──────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def create_bulk_journal_entries(schedule_name, due_dates):
    """
    Create draft Bank Entries for multiple selected schedule lines.

    Args:
        schedule_name : Bank Loan Schedule name
        due_dates     : JSON list of due_date strings

    Returns:
        {
            "created": [ {due_date, je_name}, ... ],
            "skipped": [ {due_date, reason},  ... ],
            "errors":  [ {due_date, error},   ... ],
        }
    """
    from loan_schedule_manager.scheduler.tasks import _create_je_for_line

    if isinstance(due_dates, str):
        due_dates = json.loads(due_dates)

    doc      = frappe.get_doc("Bank Loan Schedule", schedule_name)
    line_map = {str(l.due_date): l for l in doc.schedule_lines}

    created, skipped, errors = [], [], []

    for due_date in due_dates:
        due_date = str(due_date)
        line     = line_map.get(due_date)

        if not line:
            skipped.append({"due_date": due_date, "reason": "Line not found"})
            continue

        if line.status == "Posted":
            skipped.append({"due_date": due_date,
                            "reason": f"Already posted ({line.journal_entry})"})
            continue

        if line.status == "Adjusted":
            skipped.append({"due_date": due_date,
                            "reason": "Has adjustments — post manually"})
            continue

        if line.status == "Cancelled":
            skipped.append({"due_date": due_date, "reason": "Line is cancelled"})
            continue

        # Return existing draft instead of creating a duplicate
        if line.journal_entry:
            existing_docstatus = frappe.db.get_value(
                "Journal Entry", line.journal_entry, "docstatus"
            )
            if existing_docstatus == 0:
                skipped.append({"due_date": due_date,
                                "reason": f"Draft JE already exists: {line.journal_entry}"})
                continue

        try:
            je_name = _create_je_for_line(doc, line)
            frappe.db.commit()
            created.append({"due_date": due_date, "je_name": je_name})
        except Exception as e:
            frappe.db.rollback()
            errors.append({"due_date": due_date, "error": str(e)})

    return {"created": created, "skipped": skipped, "errors": errors}


# ──────────────────────────────────────────────────────────────────────────────
# 5. SCHEDULE SUMMARY
# ──────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def get_schedule_summary(schedule_name):
    """Return a summary dict of a loan schedule."""
    doc          = frappe.get_doc("Bank Loan Schedule", schedule_name)
    posted       = sum(1 for l in doc.schedule_lines if l.status == "Posted")
    pending      = sum(1 for l in doc.schedule_lines if l.status == "Pending")
    adjusted     = sum(1 for l in doc.schedule_lines if l.status == "Adjusted")
    paid_principal = sum(flt(l.actual_principal_paid) for l in doc.schedule_lines)
    paid_interest  = sum(flt(l.actual_interest_paid)  for l in doc.schedule_lines)

    return {
        "name":                 doc.name,
        "arrangement_id":       doc.arrangement_id,
        "customer_name":        doc.customer_name,
        "currency":             doc.currency,
        "total_lines":          len(doc.schedule_lines),
        "posted":               posted,
        "pending":              pending,
        "adjusted":             adjusted,
        "total_paid_principal": paid_principal,
        "total_paid_interest":  paid_interest,
        "outstanding_amount":   doc.outstanding_amount,
        "status":               doc.status,
    }


# ──────────────────────────────────────────────────────────────────────────────
# 6. DISBURSEMENT JOURNAL ENTRY
# ──────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def create_disbursement_entry(schedule_name):
    """
    Create a draft Bank Entry recording the original loan disbursement.

    Accounting entries:
        DR  Bank / Cash Account      disbursement_amount  (cash received from lender)
        CR  Loan Liability Account   disbursement_amount  (liability created)

    Returns: Journal Entry name.
    """
    doc = frappe.get_doc("Bank Loan Schedule", schedule_name)

    # Guard: already exists
    if doc.disbursement_je:
        existing_docstatus = frappe.db.get_value(
            "Journal Entry", doc.disbursement_je, "docstatus"
        )
        if existing_docstatus is not None:
            label = "Draft" if existing_docstatus == 0 else "Submitted"
            frappe.throw(
                _("Disbursement Journal Entry already exists: {0} ({1})").format(
                    doc.disbursement_je, label
                )
            )

    if not flt(doc.disbursement_amount):
        frappe.throw(_("Disbursement amount is zero — cannot create entry."))

    if not doc.disbursement_date:
        frappe.throw(_("Disbursement date is missing on this schedule."))

    company = frappe.db.get_single_value("Global Defaults", "default_company")
    if not company:
        companies = frappe.get_all("Company", limit=1, pluck="name")
        company   = companies[0] if companies else None
    if not company:
        frappe.throw(_("No company configured. Please set a default company."))

    company_currency  = frappe.db.get_value("Company", company, "default_currency") or "KES"
    is_multi_currency = doc.currency != company_currency
    je_title          = f"Loan Disbursement – {doc.arrangement_id} – {doc.disbursement_date}"

    je = frappe.get_doc({
        "doctype":        "Journal Entry",
        "title":          je_title,
        "voucher_type":   "Bank Entry",
        "company":        company,
        "posting_date":   doc.disbursement_date,
        "multi_currency": 1 if is_multi_currency else 0,
        "user_remark":    je_title,
        "accounts": [
            {
                "account":                    doc.bank_account,
                "debit_in_account_currency":  flt(doc.disbursement_amount),
                "credit_in_account_currency": 0,
                "cost_center":                doc.cost_center,
                "user_remark": f"Loan disbursement received – {doc.arrangement_id}",
            },
            {
                "account":                    doc.loan_account,
                "debit_in_account_currency":  0,
                "credit_in_account_currency": flt(doc.disbursement_amount),
                "cost_center":                doc.cost_center,
                "user_remark": f"Loan liability created – {doc.arrangement_id}",
            },
        ],
    })

    je.insert(ignore_permissions=True)

    frappe.db.set_value(
        "Bank Loan Schedule", schedule_name,
        "disbursement_je", je.name,
        update_modified=True,
    )
    frappe.db.commit()

    return je.name