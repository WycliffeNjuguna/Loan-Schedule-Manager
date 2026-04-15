"""
Patch: Add custom fields to Journal Entry for Loan Schedule linkage.

These two hidden fields allow the system to trace every auto-created JE
back to its originating Bank Loan Schedule and schedule line date,
enabling the on_cancel / on_update_after_submit hooks to locate and
update the right schedule line automatically.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
    custom_fields = {
        "Journal Entry": [
            {
                "fieldname": "custom_loan_schedule",
                "label": "Loan Schedule",
                "fieldtype": "Link",
                "options": "Bank Loan Schedule",
                "insert_after": "user_remark",
                "hidden": 1,
                "read_only": 1,
                "no_copy": 1,
                "print_hide": 1,
                "report_hide": 0,
                "search_index": 1,
                "module": "Loan Schedule Manager",
                "description": "Auto-populated when JE is created by Loan Schedule Manager",
            },
            {
                "fieldname": "custom_loan_schedule_line_date",
                "label": "Loan Schedule Line Date",
                "fieldtype": "Date",
                "insert_after": "custom_loan_schedule",
                "hidden": 1,
                "read_only": 1,
                "no_copy": 1,
                "print_hide": 1,
                "module": "Loan Schedule Manager",
                "description": "Due date of the schedule line this JE was created for",
            },
        ]
    }

    create_custom_fields(custom_fields, ignore_validate=True)
    frappe.db.commit()
