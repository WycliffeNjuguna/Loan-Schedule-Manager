"""
Scheduler tasks for Loan Schedule Manager.

Runs daily to auto-create DRAFT Journal Entries for repayment lines that are due.
The user reviews and submits each JE manually — submission triggers the
on_submit hook in events/journal_entry.py which marks the line as Posted.
"""

import frappe
from frappe import _
from frappe.utils import today, getdate, flt


def create_due_loan_journal_entries():
    """
    Daily scheduler task.
    Creates draft Bank Entry JEs for all Active schedule lines whose
    due_date <= today and status == 'Pending'.
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
    """Create draft JEs for all overdue pending lines in one schedule."""
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
    Create a DRAFT Bank Entry for a single schedule line.

    Accounting entries:
        DR  Loan Liability Account     principal_amount
        DR  Interest Expense Account   interest_amount
        CR  Bank / Cash Account        total_payment

    The JE is left as a draft (docstatus=0). When the user submits it,
    the on_submit hook in events/journal_entry.py updates the schedule line.

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
        "voucher_type": "Bank Entry",
        "company": company,
        "posting_date": line.due_date,
        "multi_currency": 1 if is_multi_currency else 0,
        "accounts": accounts,
        "user_remark": je_title,
        "custom_loan_schedule": doc.name,
        "custom_loan_schedule_line_date": str(line.due_date),
    })

    je.insert(ignore_permissions=True)
    # ── DRAFT only — do NOT call je.submit() ──────────────────────────────
    # The user will review and submit. The on_submit hook handles the rest.

    # Mark the line as "Draft JE Created" so the scheduler doesn't re-create it
    frappe.db.set_value(
        "Bank Loan Schedule Line",
        line.name,
        {
            "status":          "Pending",   # stays Pending until user submits
            "journal_entry":   je.name,     # link so user can find it
        },
        update_modified=False,
    )

    frappe.logger().info(
        f"Draft JE {je.name} created for {doc.name} / {line.due_date}"
    )
    return je.name