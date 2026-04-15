"""
Loan Schedule Notifications.

Sends a daily digest email to Accounts Managers listing any loan schedule
lines that are past due but not yet posted (i.e., JE creation failed or
the scheduler hasn't run yet).

Also flags lines that are due within the next 3 days as upcoming.
"""

import frappe
from frappe import _
from frappe.utils import getdate, today, add_days, flt


LOOKAHEAD_DAYS = 3   # warn about lines due within this many days


def send_overdue_alerts():
    """
    Daily task: find overdue + upcoming lines, email Accounts Manager role.
    Skips if there is nothing to report.
    """
    today_date = getdate(today())
    upcoming_threshold = getdate(add_days(today_date, LOOKAHEAD_DAYS))

    # Fetch all relevant pending lines in one query
    rows = frappe.db.sql(
        """
        SELECT
            l.parent          AS schedule_name,
            s.arrangement_id,
            s.customer_name,
            s.currency,
            l.due_date,
            l.total_payment,
            l.principal_amount,
            l.interest_amount,
            l.outstanding_amount,
            CASE
                WHEN l.due_date < %(today)s THEN 'overdue'
                ELSE 'upcoming'
            END AS alert_type
        FROM `tabBank Loan Schedule Line` l
        INNER JOIN `tabBank Loan Schedule` s ON s.name = l.parent
        WHERE
            l.status = 'Pending'
            AND s.status = 'Active'
            AND l.due_date <= %(threshold)s
        ORDER BY l.due_date ASC
        """,
        {"today": str(today_date), "threshold": str(upcoming_threshold)},
        as_dict=True,
    )

    if not rows:
        return  # Nothing to report

    overdue  = [r for r in rows if r["alert_type"] == "overdue"]
    upcoming = [r for r in rows if r["alert_type"] == "upcoming"]

    # Build HTML email
    subject = _("Loan Schedule Alert: {0} overdue, {1} upcoming payments").format(
        len(overdue), len(upcoming)
    )

    html = _build_email_html(overdue, upcoming, today_date)

    # Send to all users with Accounts Manager role
    recipients = _get_accounts_manager_emails()
    if not recipients:
        frappe.log_error(
            title="Loan Schedule Notification: No recipients",
            message="No Accounts Manager users found to send overdue alerts to.",
        )
        return

    frappe.sendmail(
        recipients=recipients,
        subject=subject,
        message=html,
        header=[_("Loan Repayment Alert"), "orange"],
    )

    frappe.logger().info(
        f"Loan schedule alert sent to {recipients}: "
        f"{len(overdue)} overdue, {len(upcoming)} upcoming."
    )


def _get_accounts_manager_emails():
    """Return list of email addresses for all enabled Accounts Manager users."""
    users = frappe.db.sql(
        """
        SELECT DISTINCT u.email
        FROM `tabUser` u
        INNER JOIN `tabHas Role` r ON r.parent = u.name
        WHERE r.role IN ('Accounts Manager', 'Accounts User')
          AND u.enabled = 1
          AND u.email IS NOT NULL
          AND u.email != ''
        """,
        as_list=True,
    )
    return [row[0] for row in users if row[0]]


def _build_email_html(overdue: list, upcoming: list, today_date) -> str:
    """Build the HTML body of the alert email."""

    def _table_rows(rows):
        html = ""
        for r in rows:
            html += f"""
            <tr>
                <td style="padding:6px 10px;">{r['due_date']}</td>
                <td style="padding:6px 10px;">{r['arrangement_id']}</td>
                <td style="padding:6px 10px;">{r['customer_name']}</td>
                <td style="padding:6px 10px;text-align:right;">{r['currency']} {flt(r['total_payment']):,.2f}</td>
                <td style="padding:6px 10px;text-align:right;">{flt(r['principal_amount']):,.2f}</td>
                <td style="padding:6px 10px;text-align:right;">{flt(r['interest_amount']):,.2f}</td>
                <td style="padding:6px 10px;text-align:right;">{flt(r['outstanding_amount']):,.2f}</td>
            </tr>"""
        return html

    thead = """
        <thead>
            <tr style="background:#f3f4f6;">
                <th style="padding:8px 10px;text-align:left;">Due Date</th>
                <th style="padding:8px 10px;text-align:left;">Arrangement ID</th>
                <th style="padding:8px 10px;text-align:left;">Customer</th>
                <th style="padding:8px 10px;text-align:right;">Total Payment</th>
                <th style="padding:8px 10px;text-align:right;">Principal</th>
                <th style="padding:8px 10px;text-align:right;">Interest</th>
                <th style="padding:8px 10px;text-align:right;">Outstanding After</th>
            </tr>
        </thead>"""

    overdue_section = ""
    if overdue:
        overdue_section = f"""
        <h3 style="color:#dc2626;margin:24px 0 8px;">⚠️ Overdue Lines ({len(overdue)})</h3>
        <p style="color:#6b7280;margin:0 0 12px;font-size:13px;">
            These lines are past their due date and no Journal Entry has been posted.
        </p>
        <table style="width:100%;border-collapse:collapse;font-size:13px;">
            {thead}
            <tbody>{_table_rows(overdue)}</tbody>
        </table>"""

    upcoming_section = ""
    if upcoming:
        upcoming_section = f"""
        <h3 style="color:#d97706;margin:24px 0 8px;">📅 Due in the Next 3 Days ({len(upcoming)})</h3>
        <table style="width:100%;border-collapse:collapse;font-size:13px;">
            {thead}
            <tbody>{_table_rows(upcoming)}</tbody>
        </table>"""

    site_url = frappe.utils.get_url()

    return f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:900px;margin:0 auto;">
        <div style="background:#1e3a5f;padding:20px 24px;border-radius:8px 8px 0 0;">
            <h2 style="color:#fff;margin:0;font-size:18px;">Loan Repayment Schedule Alert</h2>
            <p style="color:#93c5fd;margin:4px 0 0;font-size:13px;">As of {today_date}</p>
        </div>
        <div style="background:#fff;padding:24px;border:1px solid #e5e7eb;border-top:none;border-radius:0 0 8px 8px;">
            {overdue_section}
            {upcoming_section}
            <div style="margin-top:32px;padding-top:16px;border-top:1px solid #e5e7eb;">
                <a href="{site_url}/app/loan-schedule-manager"
                   style="background:#1e3a5f;color:#fff;padding:10px 20px;border-radius:6px;
                          text-decoration:none;font-size:13px;font-weight:600;">
                    Open Loan Schedule Manager →
                </a>
            </div>
        </div>
    </div>"""
