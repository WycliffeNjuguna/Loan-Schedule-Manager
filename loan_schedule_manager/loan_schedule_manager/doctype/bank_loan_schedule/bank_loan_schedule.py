"""
Bank Loan Schedule DocType controller.
"""

import frappe
from frappe.model.document import Document
from frappe.utils import flt


class BankLoanSchedule(Document):

    def validate(self):
        self._validate_accounts()
        self._compute_totals()

    def _validate_accounts(self):
        required = ["loan_account", "interest_account", "principal_account", "bank_account"]
        for field in required:
            if not self.get(field):
                frappe.throw(
                    frappe._("Please set the {0} before saving.").format(
                        frappe.bold(self.meta.get_label(field))
                    )
                )

    def _compute_totals(self):
        if not self.schedule_lines:
            return
        self.total_principal = sum(flt(l.principal_amount) for l in self.schedule_lines)
        self.total_interest = sum(flt(l.interest_amount) for l in self.schedule_lines)

        non_zero_lines = [l for l in self.schedule_lines if flt(l.outstanding_amount) > 0]
        if non_zero_lines:
            # Latest outstanding after all posted lines
            posted = [l for l in self.schedule_lines if l.status == "Posted"]
            if posted:
                self.outstanding_amount = flt(posted[-1].outstanding_amount)
        else:
            self.outstanding_amount = 0.0

        dates = sorted(l.due_date for l in self.schedule_lines if l.due_date)
        if dates:
            self.first_repayment_date = dates[0]
            self.last_repayment_date = dates[-1]

        # Typical installment from first non-special line
        if self.schedule_lines:
            self.monthly_installment = flt(self.schedule_lines[0].total_payment)

    def on_submit(self):
        pass  # Schedules don't need a submit workflow; status managed via lines

    @frappe.whitelist()
    def reset_line_to_pending(self, line_idx):
        """Manually reset a line to Pending (admin action)."""
        idx = int(line_idx)
        if idx >= len(self.schedule_lines):
            frappe.throw(frappe._("Invalid line index."))
        line = self.schedule_lines[idx]
        if line.journal_entry:
            frappe.throw(
                frappe._("Cancel the Journal Entry {0} before resetting this line.").format(line.journal_entry)
            )
        line.status = "Pending"
        self.save()
