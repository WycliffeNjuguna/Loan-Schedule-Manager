"""
Journal Entry event hooks.

When a JE linked to a loan schedule is submitted, cancelled, or amended,
this module updates the schedule line and records the change in the
adjustment_log child table.
"""

import frappe
from frappe import _
from frappe.utils import flt, now_datetime


def _get_linked_schedule_and_line(je_doc):
    """
    Find the Bank Loan Schedule and specific line linked to this JE.
    Returns (schedule_doc, line_row) or (None, None).
    """
    schedule_name = getattr(je_doc, "custom_loan_schedule", None)
    line_date = getattr(je_doc, "custom_loan_schedule_line_date", None)

    if not schedule_name:
        # Try reverse lookup by JE name stored in schedule lines
        result = frappe.db.sql(
            """
            SELECT parent, due_date
            FROM `tabBank Loan Schedule Line`
            WHERE journal_entry = %s
            LIMIT 1
            """,
            je_doc.name,
            as_dict=True,
        )
        if result:
            schedule_name = result[0]["parent"]
            line_date = result[0]["due_date"]

    if not schedule_name:
        return None, None

    try:
        schedule_doc = frappe.get_doc("Bank Loan Schedule", schedule_name)
    except frappe.DoesNotExistError:
        return None, None

    if not line_date:
        return schedule_doc, None

    for line in schedule_doc.schedule_lines:
        if str(line.due_date) == str(line_date):
            return schedule_doc, line

    return schedule_doc, None


def _extract_amounts_from_je(je_doc, schedule_doc):
    """
    Extract actual principal and interest amounts paid from JE accounts.
    Identifies entries by matching account names on the schedule.
    """
    actual_principal = 0.0
    actual_interest = 0.0
    actual_total = 0.0

    for acc in je_doc.accounts:
        if acc.account == schedule_doc.loan_account:
            actual_principal += flt(acc.debit_in_account_currency)
        elif acc.account == schedule_doc.interest_account:
            actual_interest += flt(acc.debit_in_account_currency)
        elif acc.account == schedule_doc.bank_account:
            actual_total += flt(acc.credit_in_account_currency)

    return actual_principal, actual_interest, actual_total


def _log_adjustment(schedule_doc, line, je_doc, adj_type: str, notes: str = ""):
    """Append a row to the adjustment_log child table."""
    actual_principal, actual_interest, _ = _extract_amounts_from_je(je_doc, schedule_doc)

    schedule_doc.append("adjustment_log", {
        "adjustment_date": now_datetime(),
        "adjusted_by": frappe.session.user,
        "due_date_affected": line.due_date if line else None,
        "journal_entry": je_doc.name,
        "adjustment_type": adj_type,
        "scheduled_principal": flt(line.principal_amount) if line else 0,
        "actual_principal": actual_principal,
        "principal_variance": actual_principal - flt(line.principal_amount) if line else 0,
        "scheduled_interest": flt(line.interest_amount) if line else 0,
        "actual_interest": actual_interest,
        "interest_variance": actual_interest - flt(line.interest_amount) if line else 0,
        "notes": notes,
    })


# ──────────────────────────────────────────────────────────────────────────────
# EVENT HANDLERS
# ──────────────────────────────────────────────────────────────────────────────

def on_submit(doc, method=None):
    """
    Called when a JE is submitted.
    Normally JEs auto-created by the scheduler are already submitted, but
    if someone manually creates a JE and links it to a schedule line this
    ensures the line gets marked Posted.
    """
    schedule_doc, line = _get_linked_schedule_and_line(doc)
    if not schedule_doc or not line:
        return

    if line.status == "Posted" and line.journal_entry == doc.name:
        return  # already handled by scheduler

    actual_principal, actual_interest, actual_total = _extract_amounts_from_je(doc, schedule_doc)

    line.status = "Posted"
    line.journal_entry = doc.name
    line.journal_entry_date = doc.posting_date
    line.actual_principal_paid = actual_principal
    line.actual_interest_paid = actual_interest
    line.actual_total_paid = actual_total
    line.variance_principal = actual_principal - flt(line.principal_amount)
    line.variance_interest = actual_interest - flt(line.interest_amount)

    # Detect adjustment at point of manual submission
    has_variance = (
        abs(line.variance_principal) > 0.01 or
        abs(line.variance_interest) > 0.01
    )
    if has_variance:
        line.status = "Adjusted"
        adj_type = "Overpayment" if actual_total > flt(line.total_payment) else "Partial Payment"
        _log_adjustment(schedule_doc, line, doc, adj_type,
                        notes=f"Manual JE submission. Total paid: {actual_total}, scheduled: {line.total_payment}")

    schedule_doc.save(ignore_permissions=True)


def on_cancel(doc, method=None):
    """
    Called when a linked JE is cancelled.
    Resets the schedule line back to Pending and logs the cancellation.
    """
    schedule_doc, line = _get_linked_schedule_and_line(doc)
    if not schedule_doc or not line:
        return

    _log_adjustment(
        schedule_doc, line, doc, "JE Cancelled",
        notes=f"JE {doc.name} was cancelled. Line reset to Pending."
    )

    line.status = "Pending"
    line.journal_entry = None
    line.journal_entry_date = None
    line.actual_principal_paid = 0
    line.actual_interest_paid = 0
    line.actual_total_paid = 0
    line.variance_principal = 0
    line.variance_interest = 0

    # Reactivate schedule if it was completed
    if schedule_doc.status == "Completed":
        schedule_doc.status = "Active"

    schedule_doc.save(ignore_permissions=True)


def on_update_after_submit(doc, method=None):
    """
    Called when a submitted JE has an in-place field update (e.g. adding a remark).
    Also fires when an amended JE is submitted (amendment_date is set).

    For real amount changes ERPNext forces a cancel+amend workflow, so the
    true handler for amount adjustments is on_submit detecting an `amended_from`
    reference. We handle both cases here.
    """
    # If this is a fresh submission of an amended doc, delegate fully to on_submit
    if getattr(doc, "amended_from", None) and doc.docstatus == 1:
        _handle_amendment(doc)
        return

    # Otherwise treat as an in-place update
    schedule_doc, line = _get_linked_schedule_and_line(doc)
    if not schedule_doc or not line:
        return

    actual_principal, actual_interest, actual_total = _extract_amounts_from_je(doc, schedule_doc)

    old_principal = flt(line.actual_principal_paid)
    old_interest  = flt(line.actual_interest_paid)

    # Check if amounts actually changed
    principal_changed = abs(actual_principal - old_principal) > 0.01
    interest_changed  = abs(actual_interest  - old_interest)  > 0.01

    if not principal_changed and not interest_changed:
        return  # No relevant change — nothing to do

    # Determine adjustment type
    adj_type = "JE Amended"
    if actual_total > flt(line.total_payment) + 0.01:
        adj_type = "Overpayment"
    elif actual_total < flt(line.total_payment) - 0.01:
        adj_type = "Partial Payment"

    _log_adjustment(
        schedule_doc, line, doc, adj_type,
        notes=(
            f"JE updated in-place. "
            f"Principal: {old_principal} → {actual_principal}. "
            f"Interest: {old_interest} → {actual_interest}."
        )
    )

    line.actual_principal_paid = actual_principal
    line.actual_interest_paid  = actual_interest
    line.actual_total_paid     = actual_total
    line.variance_principal    = actual_principal - flt(line.principal_amount)
    line.variance_interest     = actual_interest  - flt(line.interest_amount)
    line.status = (
        "Adjusted"
        if (abs(line.variance_principal) > 0.01 or abs(line.variance_interest) > 0.01)
        else "Posted"
    )

    schedule_doc.save(ignore_permissions=True)


def _handle_amendment(doc):
    """
    Called when an amended JE (cancel + amend workflow) is submitted.
    The original JE's on_cancel already reset the line to Pending.
    This re-posts it with the new amounts from the amendment.

    The amended doc carries `amended_from` pointing to the cancelled original.
    We look up the schedule via the original JE name.
    """
    # Find the schedule line via the original (now-cancelled) JE
    original_je_name = doc.amended_from
    result = frappe.db.sql(
        """
        SELECT parent, due_date
        FROM `tabBank Loan Schedule Line`
        WHERE journal_entry = %s
        LIMIT 1
        """,
        original_je_name,
        as_dict=True,
    )

    # Also try the custom field on this amended doc
    if not result:
        schedule_name = getattr(doc, "custom_loan_schedule", None)
        line_date     = getattr(doc, "custom_loan_schedule_line_date", None)
        if not schedule_name:
            return
    else:
        schedule_name = result[0]["parent"]
        line_date     = result[0]["due_date"]

    try:
        schedule_doc = frappe.get_doc("Bank Loan Schedule", schedule_name)
    except frappe.DoesNotExistError:
        return

    line = None
    for l in schedule_doc.schedule_lines:
        if str(l.due_date) == str(line_date):
            line = l
            break

    if not line:
        return

    actual_principal, actual_interest, actual_total = _extract_amounts_from_je(doc, schedule_doc)

    adj_type = "JE Amended"
    if actual_total > flt(line.total_payment) + 0.01:
        adj_type = "Overpayment"
    elif actual_total < flt(line.total_payment) - 0.01:
        adj_type = "Partial Payment"

    _log_adjustment(
        schedule_doc, line, doc, adj_type,
        notes=(
            f"Amendment of {original_je_name}. "
            f"New amounts — Principal: {actual_principal}, Interest: {actual_interest}."
        )
    )

    # Update the line to point to the new (amended) JE
    line.status              = "Adjusted" if adj_type != "JE Amended" else "Posted"
    line.journal_entry       = doc.name
    line.journal_entry_date  = doc.posting_date
    line.actual_principal_paid = actual_principal
    line.actual_interest_paid  = actual_interest
    line.actual_total_paid     = actual_total
    line.variance_principal    = actual_principal - flt(line.principal_amount)
    line.variance_interest     = actual_interest  - flt(line.interest_amount)

    # Also stamp the new JE with the schedule reference
    frappe.db.set_value("Journal Entry", doc.name, {
        "custom_loan_schedule": schedule_name,
        "custom_loan_schedule_line_date": str(line_date),
    })

    schedule_doc.save(ignore_permissions=True)
