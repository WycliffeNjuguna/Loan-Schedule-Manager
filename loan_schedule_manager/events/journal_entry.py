"""
Journal Entry event hooks for Loan Schedule Manager.
"""

import frappe
from frappe import _
from frappe.utils import flt, now_datetime


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _custom_fields_exist():
    """Check whether the custom fields exist in the DB schema."""
    columns = frappe.db.get_table_columns("Journal Entry")
    return "custom_loan_schedule" in columns


def _safe_stamp_je_fields(je_name, schedule_name, line_date):
    """Set custom fields on a JE only if the columns exist."""
    if not _custom_fields_exist():
        return
    try:
        frappe.db.set_value(
            "Journal Entry", je_name,
            {
                "custom_loan_schedule":           schedule_name,
                "custom_loan_schedule_line_date": str(line_date),
            },
            update_modified=False,
        )
    except Exception:
        pass


def _get_linked_schedule_and_line(je_doc):
    """
    Find the Bank Loan Schedule and line linked to this JE.
    Strategy:
      1. custom fields on this JE
      2. reverse lookup: line.journal_entry = this JE
      3. amendment chain: walk amended_from, look up original JE
    """
    schedule_name = None
    line_date     = None

    if _custom_fields_exist():
        schedule_name = getattr(je_doc, "custom_loan_schedule", None)
        line_date     = getattr(je_doc, "custom_loan_schedule_line_date", None)

    if schedule_name and line_date:
        return _load_schedule_and_line(schedule_name, line_date)

    row = frappe.db.sql(
        "SELECT parent, due_date FROM `tabBank Loan Schedule Line` "
        "WHERE journal_entry = %s LIMIT 1",
        je_doc.name, as_dict=True,
    )
    if row:
        return _load_schedule_and_line(row[0]["parent"], row[0]["due_date"])

    original_name = _get_original_je_name(je_doc)
    if original_name and original_name != je_doc.name:
        row = frappe.db.sql(
            "SELECT parent, due_date FROM `tabBank Loan Schedule Line` "
            "WHERE journal_entry = %s LIMIT 1",
            original_name, as_dict=True,
        )
        if row:
            return _load_schedule_and_line(row[0]["parent"], row[0]["due_date"])

        if _custom_fields_exist():
            root_vals = frappe.db.get_value(
                "Journal Entry", original_name,
                ["custom_loan_schedule", "custom_loan_schedule_line_date"],
                as_dict=True,
            )
            if root_vals and root_vals.get("custom_loan_schedule"):
                return _load_schedule_and_line(
                    root_vals["custom_loan_schedule"],
                    root_vals["custom_loan_schedule_line_date"],
                )

    return None, None


def _get_original_je_name(je_doc):
    """Walk amended_from chain to find the root original JE name."""
    name = getattr(je_doc, "amended_from", None)
    if not name:
        return None
    visited = set()
    while name and name not in visited:
        visited.add(name)
        parent = frappe.db.get_value("Journal Entry", name, "amended_from")
        if not parent:
            break
        name = parent
    return name


def _load_schedule_and_line(schedule_name, line_date):
    """Load the schedule doc and find the matching line by due date."""
    try:
        schedule_doc = frappe.get_doc("Bank Loan Schedule", schedule_name)
    except frappe.DoesNotExistError:
        return None, None

    for line in schedule_doc.schedule_lines:
        if str(line.due_date) == str(line_date):
            return schedule_doc, line

    return schedule_doc, None


def _extract_amounts_from_je(je_doc, schedule_doc):
    """Extract principal, interest, and total from JE account rows."""
    actual_principal = 0.0
    actual_interest  = 0.0
    actual_total     = 0.0

    for acc in je_doc.accounts:
        if acc.account == schedule_doc.loan_account:
            actual_principal += flt(acc.debit_in_account_currency)
        elif acc.account == schedule_doc.interest_account:
            actual_interest  += flt(acc.debit_in_account_currency)
        elif acc.account == schedule_doc.bank_account:
            actual_total     += flt(acc.credit_in_account_currency)

    return actual_principal, actual_interest, actual_total


def _compute_actual_outstanding(schedule_doc, line, actual_principal_paid):
    """
    Compute the true outstanding balance after an adjusted payment.

        actual_outstanding = previous_outstanding - actual_principal_paid

    'Previous outstanding':
      - For the first line: disbursement_amount (or total_principal as fallback)
      - For subsequent lines: the stored outstanding_amount of the previous line
    """
    sorted_lines = sorted(schedule_doc.schedule_lines, key=lambda l: str(l.due_date))

    current_idx = None
    for i, l in enumerate(sorted_lines):
        if str(l.due_date) == str(line.due_date):
            current_idx = i
            break

    if current_idx is None:
        return flt(line.outstanding_amount)

    if current_idx == 0:
        # Use disbursement_amount; fall back to total_principal if it's 0
        prev_outstanding = flt(schedule_doc.disbursement_amount) or flt(schedule_doc.total_principal)
        if not prev_outstanding:
            # Last resort: first line's outstanding + its principal = original balance
            prev_outstanding = flt(line.outstanding_amount) + flt(line.principal_amount)
    else:
        prev_line = sorted_lines[current_idx - 1]
        prev_actual = frappe.db.get_value(
            "Bank Loan Schedule Line", prev_line.name, "outstanding_amount"
        )
        prev_outstanding = flt(prev_actual) if prev_actual is not None else flt(prev_line.outstanding_amount)

    actual_outstanding = prev_outstanding - actual_principal_paid
    return max(actual_outstanding, 0.0)


def _update_line_and_schedule(schedule_doc, line, je_doc,
                               actual_principal, actual_interest, actual_total,
                               adj_type=None, notes=""):
    """
    Persist line + parent schedule updates via direct DB writes.
    Recomputes actual outstanding when amounts differ from schedule.
    """
    variance_p = actual_principal - flt(line.principal_amount)
    variance_i = actual_interest  - flt(line.interest_amount)
    has_variance = abs(variance_p) > 0.01 or abs(variance_i) > 0.01
    status = "Adjusted" if has_variance else "Posted"

    # ── Compute actual outstanding ────────────────────────────────────────────
    if has_variance:
        # Recompute based on what was actually paid
        actual_outstanding = _compute_actual_outstanding(
            schedule_doc, line, actual_principal
        )
    else:
        # Full scheduled payment — use the value from the PDF schedule
        actual_outstanding = flt(line.outstanding_amount)

    # ── Insert adjustment log row if needed ───────────────────────────────────
    if adj_type:
        try:
            frappe.db.sql(
                """INSERT INTO `tabBank Loan Schedule Adjustment`
                   (name, parent, parenttype, parentfield,
                    adjustment_date, adjusted_by, due_date_affected,
                    journal_entry, adjustment_type,
                    scheduled_principal, actual_principal, principal_variance,
                    scheduled_interest, actual_interest, interest_variance,
                    notes)
                   VALUES (%s,%s,'Bank Loan Schedule','adjustment_log',
                           %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (frappe.generate_hash(length=10), schedule_doc.name,
                 now_datetime(), frappe.session.user,
                 str(line.due_date), je_doc.name, adj_type,
                 flt(line.principal_amount), actual_principal, variance_p,
                 flt(line.interest_amount),  actual_interest,  variance_i,
                 notes or ""),
            )
        except Exception:
            frappe.log_error(title="Loan Schedule: adjustment log insert failed",
                             message=frappe.get_traceback())

    # ── Update schedule line ──────────────────────────────────────────────────
    frappe.db.set_value(
        "Bank Loan Schedule Line",
        line.name,
        {
            "status":                status,
            "journal_entry":         je_doc.name,
            "journal_entry_date":    str(je_doc.posting_date),
            "actual_principal_paid": actual_principal,
            "actual_interest_paid":  actual_interest,
            "actual_total_paid":     actual_total,
            "variance_principal":    variance_p,
            "variance_interest":     variance_i,
            "outstanding_amount":    actual_outstanding,   # ← updated with actual
        },
        update_modified=False,
    )

    # ── Update parent schedule outstanding + status ───────────────────────────
    # Query the DB for the outstanding of the LAST posted line in date order.
    # This handles bulk submissions where multiple JEs are submitted in any
    # order — the parent always reflects the true current balance.
    latest_outstanding = frappe.db.sql(
        """
        SELECT outstanding_amount
        FROM `tabBank Loan Schedule Line`
        WHERE parent = %s
          AND status IN ('Posted', 'Adjusted')
        ORDER BY due_date DESC
        LIMIT 1
        """,
        schedule_doc.name,
        as_list=True,
    )

    # Use the freshly computed outstanding for the current line if the DB
    # hasn't been written yet (first submission) or if it's more recent
    if latest_outstanding:
        parent_outstanding = flt(latest_outstanding[0][0])
        # Current line may be more recent — compare dates to pick the right one
        latest_posted_date = frappe.db.sql(
            """
            SELECT MAX(due_date)
            FROM `tabBank Loan Schedule Line`
            WHERE parent = %s AND status IN ('Posted', 'Adjusted')
            """,
            schedule_doc.name,
            as_list=True,
        )
        latest_date = str(latest_posted_date[0][0]) if latest_posted_date and latest_posted_date[0][0] else ""
        if str(line.due_date) >= latest_date:
            parent_outstanding = actual_outstanding
    else:
        parent_outstanding = actual_outstanding

    new_status = "Completed" if parent_outstanding == 0.0 else "Active"
    frappe.db.set_value(
        "Bank Loan Schedule",
        schedule_doc.name,
        {
            "outstanding_amount": parent_outstanding,
            "status":             new_status,
        },
        update_modified=True,
    )

    # Keep in-memory object in sync
    line.status                = status
    line.journal_entry         = je_doc.name
    line.actual_principal_paid = actual_principal
    line.actual_interest_paid  = actual_interest
    line.actual_total_paid     = actual_total
    line.variance_principal    = variance_p
    line.variance_interest     = variance_i
    line.outstanding_amount    = actual_outstanding
    schedule_doc.outstanding_amount = parent_outstanding
    schedule_doc.status             = new_status


# ─────────────────────────────────────────────────────────────────────────────
# Event Handlers
# ─────────────────────────────────────────────────────────────────────────────

def on_submit(doc, method=None):
    """Fires when any Journal Entry is submitted."""

    # Amendment: handle separately
    if getattr(doc, "amended_from", None):
        _handle_amendment_submit(doc)
        return

    schedule_doc, line = _get_linked_schedule_and_line(doc)
    if not schedule_doc or not line:
        return

    if line.status == "Posted" and line.journal_entry == doc.name:
        return  # already handled

    actual_principal, actual_interest, actual_total = _extract_amounts_from_je(
        doc, schedule_doc
    )

    variance_p = actual_principal - flt(line.principal_amount)
    variance_i = actual_interest  - flt(line.interest_amount)
    has_variance = abs(variance_p) > 0.01 or abs(variance_i) > 0.01

    adj_type = None
    notes    = ""
    if has_variance:
        adj_type = "Overpayment" if actual_total > flt(line.total_payment) + 0.01 else "Partial Payment"
        notes    = (
            f"JE submitted with variance. "
            f"Scheduled: {flt(line.total_payment):.2f}  "
            f"Paid: {actual_total:.2f}  "
            f"Principal diff: {variance_p:.2f}  "
            f"Interest diff: {variance_i:.2f}"
        )

    _update_line_and_schedule(
        schedule_doc, line, doc,
        actual_principal, actual_interest, actual_total,
        adj_type=adj_type, notes=notes,
    )

    _safe_stamp_je_fields(doc.name, schedule_doc.name, str(line.due_date))


def on_cancel(doc, method=None):
    """Resets the linked schedule line to Pending."""
    schedule_doc, line = _get_linked_schedule_and_line(doc)
    if not schedule_doc or not line:
        return

    # Log cancellation
    try:
        frappe.db.sql(
            """INSERT INTO `tabBank Loan Schedule Adjustment`
               (name, parent, parenttype, parentfield,
                adjustment_date, adjusted_by, due_date_affected,
                journal_entry, adjustment_type, notes)
               VALUES (%s,%s,'Bank Loan Schedule','adjustment_log',
                       %s,%s,%s,%s,'JE Cancelled',%s)""",
            (frappe.generate_hash(length=10), schedule_doc.name,
             now_datetime(), frappe.session.user,
             str(line.due_date), doc.name,
             f"JE {doc.name} cancelled. Line reset to Pending."),
        )
    except Exception:
        pass

    # Restore outstanding to the scheduled value (undo the adjustment)
    frappe.db.set_value(
        "Bank Loan Schedule Line",
        line.name,
        {
            "status":                "Pending",
            "journal_entry":         "",
            "journal_entry_date":    None,
            "actual_principal_paid": 0,
            "actual_interest_paid":  0,
            "actual_total_paid":     0,
            "variance_principal":    0,
            "variance_interest":     0,
            "outstanding_amount":    flt(line.outstanding_amount),
        },
        update_modified=False,
    )

    # Reactivate schedule if it was completed
    if schedule_doc.status == "Completed":
        frappe.db.set_value(
            "Bank Loan Schedule", schedule_doc.name,
            "status", "Active", update_modified=True,
        )

    # Recalculate parent outstanding from last posted line
    _recalculate_schedule_outstanding(schedule_doc)


def on_update_after_submit(doc, method=None):
    """Fires when a submitted JE is modified in place."""
    if getattr(doc, "amended_from", None):
        return

    schedule_doc, line = _get_linked_schedule_and_line(doc)
    if not schedule_doc or not line:
        return

    actual_principal, actual_interest, actual_total = _extract_amounts_from_je(
        doc, schedule_doc
    )

    old_p = flt(line.actual_principal_paid)
    old_i = flt(line.actual_interest_paid)

    if abs(actual_principal - old_p) <= 0.01 and abs(actual_interest - old_i) <= 0.01:
        return

    adj_type = "JE Amended"
    if actual_total > flt(line.total_payment) + 0.01:
        adj_type = "Overpayment"
    elif actual_total < flt(line.total_payment) - 0.01:
        adj_type = "Partial Payment"

    notes = (f"JE updated in-place. "
             f"Principal: {old_p:.2f}→{actual_principal:.2f}. "
             f"Interest: {old_i:.2f}→{actual_interest:.2f}.")

    _update_line_and_schedule(
        schedule_doc, line, doc,
        actual_principal, actual_interest, actual_total,
        adj_type=adj_type, notes=notes,
    )


def _handle_amendment_submit(doc):
    """
    Called from on_submit when doc.amended_from is set.
    Finds the original schedule line and updates it to point to the new JE.
    """
    original_name = getattr(doc, "amended_from", None)
    if not original_name:
        return

    # Find line via original JE name
    row = frappe.db.sql(
        "SELECT parent, due_date FROM `tabBank Loan Schedule Line` "
        "WHERE journal_entry = %s LIMIT 1",
        original_name, as_dict=True,
    )

    schedule_name = None
    line_date     = None

    if row:
        schedule_name = row[0]["parent"]
        line_date     = row[0]["due_date"]
    elif _custom_fields_exist():
        root_name = _get_original_je_name(doc) or original_name
        vals = frappe.db.get_value(
            "Journal Entry", root_name,
            ["custom_loan_schedule", "custom_loan_schedule_line_date"],
            as_dict=True,
        )
        if vals and vals.get("custom_loan_schedule"):
            schedule_name = vals["custom_loan_schedule"]
            line_date     = vals["custom_loan_schedule_line_date"]

    if not schedule_name or not line_date:
        return

    schedule_doc, line = _load_schedule_and_line(schedule_name, line_date)
    if not schedule_doc or not line:
        return

    _safe_stamp_je_fields(doc.name, schedule_name, str(line_date))

    actual_principal, actual_interest, actual_total = _extract_amounts_from_je(
        doc, schedule_doc
    )

    adj_type = "JE Amended"
    if actual_total > flt(line.total_payment) + 0.01:
        adj_type = "Overpayment"
    elif actual_total < flt(line.total_payment) - 0.01:
        adj_type = "Partial Payment"

    notes = (f"Amendment of {original_name} → {doc.name}. "
             f"Principal: {actual_principal:.2f}, Interest: {actual_interest:.2f}.")

    _update_line_and_schedule(
        schedule_doc, line, doc,
        actual_principal, actual_interest, actual_total,
        adj_type=adj_type, notes=notes,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Outstanding recalculation
# ─────────────────────────────────────────────────────────────────────────────

def _recalculate_schedule_outstanding(schedule_doc):
    """
    After a cancellation, recalculate what the current outstanding should be
    by looking at the last line that has a submitted JE, and using its
    outstanding_amount as the schedule's current balance.
    Falls back to disbursement_amount if nothing is posted yet.
    """
    sorted_lines = sorted(schedule_doc.schedule_lines,
                          key=lambda l: str(l.due_date))

    last_outstanding = flt(schedule_doc.disbursement_amount)
    new_status       = "Active"

    for line in sorted_lines:
        if line.status in ("Posted", "Adjusted"):
            last_outstanding = flt(line.outstanding_amount)

    if last_outstanding == 0:
        new_status = "Completed"

    frappe.db.set_value(
        "Bank Loan Schedule",
        schedule_doc.name,
        {
            "outstanding_amount": last_outstanding,
            "status":             new_status,
        },
        update_modified=True,
    )