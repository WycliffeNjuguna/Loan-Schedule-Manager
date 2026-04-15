"""
Loan Portfolio Summary — Script Report

Provides a cross-schedule view of all active (and recently completed) loans:
  • One row per Bank Loan Schedule
  • Columns: Arrangement ID, Customer, Currency, Disbursement Amount,
             Total Principal, Total Interest, Paid Principal, Paid Interest,
             Outstanding, Overdue Lines, Next Due Date, Status
  • Filter: Status, Customer, As Of Date (defaults to today)
"""

import frappe
from frappe import _
from frappe.utils import flt, getdate, today


def execute(filters=None):
    filters = filters or {}
    columns = _get_columns()
    data    = _get_data(filters)
    return columns, data


def _get_columns():
    return [
        {
            "label": _("Arrangement ID"),
            "fieldname": "arrangement_id",
            "fieldtype": "Link",
            "options": "Bank Loan Schedule",
            "width": 160,
        },
        {
            "label": _("Customer"),
            "fieldname": "customer_name",
            "fieldtype": "Data",
            "width": 180,
        },
        {
            "label": _("Currency"),
            "fieldname": "currency",
            "fieldtype": "Data",
            "width": 70,
        },
        {
            "label": _("Disbursed"),
            "fieldname": "disbursement_amount",
            "fieldtype": "Currency",
            "options": "currency",
            "width": 120,
        },
        {
            "label": _("Sched. Principal"),
            "fieldname": "total_principal",
            "fieldtype": "Currency",
            "options": "currency",
            "width": 130,
        },
        {
            "label": _("Sched. Interest"),
            "fieldname": "total_interest",
            "fieldtype": "Currency",
            "options": "currency",
            "width": 120,
        },
        {
            "label": _("Paid Principal"),
            "fieldname": "paid_principal",
            "fieldtype": "Currency",
            "options": "currency",
            "width": 120,
        },
        {
            "label": _("Paid Interest"),
            "fieldname": "paid_interest",
            "fieldtype": "Currency",
            "options": "currency",
            "width": 110,
        },
        {
            "label": _("Outstanding"),
            "fieldname": "outstanding_amount",
            "fieldtype": "Currency",
            "options": "currency",
            "width": 120,
        },
        {
            "label": _("Overdue Lines"),
            "fieldname": "overdue_lines",
            "fieldtype": "Int",
            "width": 110,
        },
        {
            "label": _("Next Due Date"),
            "fieldname": "next_due_date",
            "fieldtype": "Date",
            "width": 110,
        },
        {
            "label": _("Installment"),
            "fieldname": "monthly_installment",
            "fieldtype": "Currency",
            "options": "currency",
            "width": 110,
        },
        {
            "label": _("Posted / Total"),
            "fieldname": "progress",
            "fieldtype": "Data",
            "width": 100,
        },
        {
            "label": _("Status"),
            "fieldname": "status",
            "fieldtype": "Data",
            "width": 90,
        },
    ]


def _get_data(filters):
    as_of = getdate(filters.get("as_of_date") or today())
    status_filter = filters.get("status")
    customer_filter = filters.get("customer_name")

    conditions = []
    if status_filter:
        conditions.append(f"s.status = {frappe.db.escape(status_filter)}")
    else:
        conditions.append("s.status != 'Cancelled'")
    if customer_filter:
        conditions.append(f"s.customer_name LIKE {frappe.db.escape('%' + customer_filter + '%')}")

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    schedules = frappe.db.sql(
        f"""
        SELECT
            s.name,
            s.arrangement_id,
            s.customer_name,
            s.currency,
            s.disbursement_amount,
            s.total_principal,
            s.total_interest,
            s.outstanding_amount,
            s.monthly_installment,
            s.status
        FROM `tabBank Loan Schedule` s
        {where}
        ORDER BY s.status, s.customer_name
        """,
        as_dict=True,
    )

    if not schedules:
        return []

    schedule_names = [s["name"] for s in schedules]

    # Frappe's db.sql handles list/tuple for %s but NOT for %(name)s dict params.
    # Build the IN clause manually with proper escaping.
    escaped_names = ", ".join(frappe.db.escape(n) for n in schedule_names)

    # Aggregate line-level stats in a single query
    line_stats = frappe.db.sql(
        f"""
        SELECT
            l.parent,
            SUM(CASE WHEN l.status IN ('Posted','Adjusted') THEN IFNULL(l.actual_principal_paid, 0) ELSE 0 END) AS paid_principal,
            SUM(CASE WHEN l.status IN ('Posted','Adjusted') THEN IFNULL(l.actual_interest_paid,  0) ELSE 0 END) AS paid_interest,
            SUM(CASE WHEN l.status = 'Pending' AND l.due_date <= %(as_of)s THEN 1 ELSE 0 END)                  AS overdue_lines,
            COUNT(CASE WHEN l.status IN ('Posted','Adjusted') THEN 1 END)                                       AS posted_count,
            COUNT(*)                                                                                             AS total_count,
            MIN(CASE WHEN l.status = 'Pending' THEN l.due_date END)                                            AS next_due_date
        FROM `tabBank Loan Schedule Line` l
        WHERE l.parent IN ({escaped_names})
        GROUP BY l.parent
        """,
        {"as_of": str(as_of)},
        as_dict=True,
    )

    stats_by_name = {r["parent"]: r for r in line_stats}

    rows = []
    for s in schedules:
        st = stats_by_name.get(s["name"], {})
        paid_principal = flt(st.get("paid_principal", 0))
        paid_interest  = flt(st.get("paid_interest",  0))
        overdue        = int(st.get("overdue_lines",   0))
        next_due       = st.get("next_due_date")
        posted         = int(st.get("posted_count",    0))
        total          = int(st.get("total_count",     0))

        rows.append({
            "arrangement_id":      s["arrangement_id"],
            "customer_name":       s["customer_name"],
            "currency":            s["currency"],
            "disbursement_amount": flt(s["disbursement_amount"]),
            "total_principal":     flt(s["total_principal"]),
            "total_interest":      flt(s["total_interest"]),
            "paid_principal":      paid_principal,
            "paid_interest":       paid_interest,
            "outstanding_amount":  flt(s["outstanding_amount"]),
            "overdue_lines":       overdue,
            "next_due_date":       next_due,
            "monthly_installment": flt(s["monthly_installment"]),
            "progress":            f"{posted} / {total}",
            "status":              s["status"],
        })

    return rows
