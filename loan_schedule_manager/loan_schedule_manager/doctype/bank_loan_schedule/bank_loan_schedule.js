/**
 * Bank Loan Schedule — Client Script
 *
 * Adds:
 *  • "Create Journal Entry" action button on individual Pending lines
 *  • Dashboard indicators (posted, pending, adjusted counts)
 *  • Visual highlight of overdue pending lines
 *  • "Upload New Schedule" shortcut button in the list view toolbar
 */

// ── Form view ─────────────────────────────────────────────────────────────────

frappe.ui.form.on("Bank Loan Schedule", {

    refresh(frm) {
        frm.trigger("render_indicators");
        frm.trigger("add_action_buttons");
        frm.trigger("highlight_overdue_lines");
    },

    // ── Dashboard indicators ──────────────────────────────────────────────────

    render_indicators(frm) {
        frm.dashboard.clear_headline();

        const lines = frm.doc.schedule_lines || [];
        const posted   = lines.filter(l => l.status === "Posted").length;
        const pending  = lines.filter(l => l.status === "Pending").length;
        const adjusted = lines.filter(l => l.status === "Adjusted").length;
        const total    = lines.length;

        const pct = total ? Math.round((posted / total) * 100) : 0;

        frm.dashboard.add_indicator(
            `${posted} / ${total} Posted (${pct}%)`,
            posted === total ? "green" : "blue"
        );
        if (pending)  frm.dashboard.add_indicator(`${pending} Pending`,  "orange");
        if (adjusted) frm.dashboard.add_indicator(`${adjusted} Adjusted`, "yellow");

        // Outstanding balance badge
        const outstanding = frm.doc.outstanding_amount || 0;
        frm.dashboard.add_indicator(
            `Outstanding: ${format_currency(outstanding, frm.doc.currency)}`,
            outstanding > 0 ? "red" : "green"
        );
    },

    // ── Custom action buttons ─────────────────────────────────────────────────

    add_action_buttons(frm) {
        if (frm.is_new()) return;

        // Button: manually trigger next due JE (for admins / backfill)
        frm.add_custom_button(__("Post Next Due Entry"), () => {
            frm.trigger("post_next_due");
        }, __("Actions"));

        // Button: open the Upload wizard
        frm.add_custom_button(__("Upload New Schedule"), () => {
            frappe.set_route("loan-schedule-upload");
        }, __("Actions"));

        // Button: refresh summary
        frm.add_custom_button(__("Refresh Summary"), () => {
            frm.reload_doc();
        });
    },

    // ── Post next due entry ───────────────────────────────────────────────────

    post_next_due(frm) {
        const today = frappe.datetime.get_today();
        const lines = frm.doc.schedule_lines || [];

        const due = lines
            .map((l, idx) => ({ ...l, _idx: idx }))
            .filter(l => l.status === "Pending" && l.due_date <= today);

        if (!due.length) {
            frappe.msgprint({
                title: __("Nothing Due"),
                message: __("No pending lines are due today or earlier."),
                indicator: "blue",
            });
            return;
        }

        const next = due[0];

        frappe.confirm(
            __(`Create Journal Entry for line due <b>${next.due_date}</b>?<br>
                Principal: <b>${format_currency(next.principal_amount, frm.doc.currency)}</b> &nbsp;
                Interest: <b>${format_currency(next.interest_amount, frm.doc.currency)}</b>`),
            () => {
                frappe.call({
                    method: "loan_schedule_manager.api.schedule_api.create_journal_entry_for_line",
                    args: { schedule_name: frm.docname, line_due_date: next.due_date },
                    freeze: true,
                    freeze_message: __("Creating Journal Entry…"),
                    callback(r) {
                        if (r.message) {
                            frappe.show_alert({
                                message: __(`Journal Entry <a href="/app/journal-entry/${r.message}">${r.message}</a> created.`),
                                indicator: "green",
                            }, 6);
                            frm.reload_doc();
                        }
                    },
                });
            }
        );
    },

    // ── Highlight overdue pending lines ───────────────────────────────────────

    highlight_overdue_lines(frm) {
        // Wait for grid to render
        setTimeout(() => {
            const today = frappe.datetime.get_today();
            (frm.doc.schedule_lines || []).forEach((line, idx) => {
                if (line.status !== "Pending") return;
                if (!line.due_date) return;

                const row_el = frm.fields_dict.schedule_lines?.grid?.get_row(idx)?.$row;
                if (!row_el) return;

                if (line.due_date < today) {
                    // Overdue – soft red tint
                    row_el.css("background", "rgba(255,80,80,0.07)");
                    row_el.attr("title", "Overdue – journal entry not yet posted");
                } else if (line.due_date === today) {
                    // Due today – soft amber
                    row_el.css("background", "rgba(255,180,0,0.1)");
                    row_el.attr("title", "Due today");
                }
            });
        }, 600);
    },
});


// ── Schedule Line child table events ─────────────────────────────────────────

frappe.ui.form.on("Bank Loan Schedule Line", {

    // Open JE in a new tab when clicking the link in the grid
    journal_entry(frm, cdt, cdn) {
        const row = locals[cdt][cdn];
        if (row.journal_entry) {
            window.open(`/app/journal-entry/${row.journal_entry}`, "_blank");
        }
    },
});


// ── List view toolbar button ──────────────────────────────────────────────────

frappe.listview_settings["Bank Loan Schedule"] = {
    onload(listview) {
        listview.page.add_inner_button(__("Upload Schedule PDF"), () => {
            frappe.set_route("loan-schedule-upload");
        });
    },

    get_indicator(doc) {
        const map = {
            Active:    ["Active",    "blue"],
            Completed: ["Completed", "green"],
            Cancelled: ["Cancelled", "grey"],
        };
        return map[doc.status] || ["Unknown", "grey"];
    },

    formatters: {
        outstanding_amount(val, df, doc) {
            if (!val) return `<span class="text-success">Paid off</span>`;
            return `<span class="text-danger">${format_currency(val, doc.currency)}</span>`;
        },
    },
};
