"""
Scheduler tasks for Loan Schedule Manager.

Runs daily to auto-create Journal Entries for repayment lines that are due.
"""

import frappe
from frappe import _
from frappe.utils import today, getdate, flt, now_datetime


def create_due_loan_journal_entries():
    """
    Daily scheduler task.
    Finds all active loan schedules and creates Journal Entries for lines
    whose due_date <= today and status == 'Pending'.
    """
    today_date = getdate(today())

    active_schedules = frappe.get_all(
        "Bank Loan Schedule",
        filters={"status": "Active"},
        fields=["name"],
    )

    for row in active_schedules:
        try:
            doc = frappe.get_doc("Bank Loan Schedule", row.name)
            _process_schedule(doc, today_date)
        except Exception:
            frappe.log_error(
                title=f"Loan Schedule JE Error: {row.name}",
                message=frappe.get_traceback(),
            )


def _process_schedule(doc, today_date):
    """Process one schedule: create JEs for due pending lines."""
    for line in doc.schedule_lines:
        if line.status != "Pending":
            continue
        if getdate(line.due_date) > today_date:
            continue

        try:
            _create_je_for_line(doc, line)
            frappe.db.commit()
        except Exception:
            frappe.log_error(
                title=f"JE Creation Failed: {doc.name} / {line.due_date}",
                message=frappe.get_traceback(),
            )
            frappe.db.rollback()


def _create_je_for_line(doc, line) -> str:
    """
    Create and submit a Journal Entry for a single schedule line.

    Accounting entries:
        DR  Loan Liability Account        principal_amount  (reduces the loan)
        DR  Interest Expense Account      interest_amount
        CR  Bank / Cash Account           total_payment     (cash out)

    Returns: Journal Entry name
    """
    company = frappe.db.get_single_value("Global Defaults", "default_company")
    if not company:
        companies = frappe.get_all("Company", limit=1, pluck="name")
        company = companies[0] if companies else None

    if not company:
        frappe.throw(_("No company configured. Please set a default company."))

    je_title = f"Loan Repayment – {doc.arrangement_id} – {line.due_date}"

    accounts = []

    if flt(line.principal_amount) > 0:
        accounts.append({
            "account": doc.loan_account,
            "debit_in_account_currency": flt(line.principal_amount),
            "credit_in_account_currency": 0,
            "cost_center": doc.cost_center,
            "user_remark": f"Principal repayment – {doc.arrangement_id}",
        })

    if flt(line.interest_amount) > 0:
        accounts.append({
            "account": doc.interest_account,
            "debit_in_account_currency": flt(line.interest_amount),
            "credit_in_account_currency": 0,
            "cost_center": doc.cost_center,
            "user_remark": f"Interest payment – {doc.arrangement_id}",
        })

    accounts.append({
        "account": doc.bank_account,
        "debit_in_account_currency": 0,
        "credit_in_account_currency": flt(line.total_payment),
        "cost_center": doc.cost_center,
        "user_remark": f"Loan repayment – {doc.arrangement_id}",
    })

    company_currency = frappe.db.get_value("Company", company, "default_currency") or "KES"
    is_multi_currency = doc.currency != company_currency

    je = frappe.get_doc({
        "doctype": "Journal Entry",
        "title": je_title,
        "voucher_type": "Journal Entry",
        "company": company,
        "posting_date": line.due_date,
        "multi_currency": 1 if is_multi_currency else 0,
        "accounts": accounts,
        "user_remark": je_title,
        "custom_loan_schedule": doc.name,
        "custom_loan_schedule_line_date": line.due_date,
    })

    je.flags.ignore_links = True
    je.insert(ignore_permissions=True)

    # Temporarily disable the on_submit hook to prevent it from saving the
    # parent doc — we will update via direct DB writes below to avoid the
    # TimestampMismatchError that occurs when two saves happen in rapid succession.
    je.flags.from_scheduler = True
    je.submit()

    # ── Update the schedule line via direct DB (no doc.save()) ──────────────
    frappe.db.set_value(
        "Bank Loan Schedule Line",
        line.name,
        {
            "status":                "Posted",
            "journal_entry":         je.name,
            "journal_entry_date":    str(line.due_date),
            "actual_principal_paid": flt(line.principal_amount),
            "actual_interest_paid":  flt(line.interest_amount),
            "actual_total_paid":     flt(line.total_payment),
            "variance_principal":    0.0,
            "variance_interest":     0.0,
        },
        update_modified=False,
    )

    # ── Update parent schedule outstanding + status via direct DB ───────────
    new_status = "Completed" if flt(line.outstanding_amount) == 0 else "Active"
    frappe.db.set_value(
        "Bank Loan Schedule",
        doc.name,
        {
            "outstanding_amount": flt(line.outstanding_amount),
            "status":             new_status,
        },
        update_modified=True,
    )

    # Keep the in-memory object in sync so callers see current state
    line.status               = "Posted"
    line.journal_entry        = je.name
    line.actual_principal_paid = flt(line.principal_amount)
    line.actual_interest_paid  = flt(line.interest_amount)
    line.actual_total_paid     = flt(line.total_payment)
    doc.outstanding_amount     = flt(line.outstanding_amount)
    doc.status                 = new_status

    frappe.logger().info(f"Created JE {je.name} for {doc.name} / {line.due_date}")
    return je.name