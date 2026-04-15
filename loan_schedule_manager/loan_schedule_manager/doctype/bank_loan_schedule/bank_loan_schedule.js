/**
 * Bank Loan Schedule — Client Script
 *
 * Features:
 *  • Dashboard indicators (posted/pending/adjusted counts + outstanding)
 *  • "Post Next Due Entry"  — single-line quick action
 *  • "Bulk Post Entries"    — dialog showing ALL pending lines as a checklist;
 *                             user selects which ones to create draft JEs for
 *  • Overdue line highlighting (red = overdue, amber = due today)
 *  • List view: Upload button + status indicator + outstanding formatter
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

        const lines    = frm.doc.schedule_lines || [];
        const posted   = lines.filter(l => l.status === "Posted").length;
        const pending  = lines.filter(l => l.status === "Pending").length;
        const adjusted = lines.filter(l => l.status === "Adjusted").length;
        const total    = lines.length;
        const pct      = total ? Math.round((posted / total) * 100) : 0;

        frm.dashboard.add_indicator(
            `${posted} / ${total} Posted (${pct}%)`,
            posted === total ? "green" : "blue"
        );
        if (pending)  frm.dashboard.add_indicator(`${pending} Pending`,  "orange");
        if (adjusted) frm.dashboard.add_indicator(`${adjusted} Adjusted`, "yellow");

        const outstanding = frm.doc.outstanding_amount || 0;
        frm.dashboard.add_indicator(
            `Outstanding: ${format_currency(outstanding, frm.doc.currency)}`,
            outstanding > 0 ? "red" : "green"
        );

        // Disbursement JE — clickable link in dashboard
        if (frm.doc.disbursement_je) {
            const je = frm.doc.disbursement_je;
            // Add the indicator pill
            frm.dashboard.add_indicator(`Disbursement JE: ${je}`, "green");
            // Inject a proper anchor link below the indicators so it's always clickable
            frm.dashboard.set_headline(
                `<span style="font-size:.85rem;">
                    Disbursement JE:&nbsp;
                    <a href="/app/journal-entry/${je}" 
                       style="font-weight:600;color:var(--primary);"
                       onclick="event.preventDefault(); frappe.set_route('Form','Journal Entry','${je}');">
                        ${je}
                    </a>
                </span>`
            );
        } else {
            frm.dashboard.add_indicator("No Disbursement JE", "orange");
        }
    },

    // ── Action buttons ────────────────────────────────────────────────────────

    add_action_buttons(frm) {
        if (frm.is_new()) return;

        frm.add_custom_button(__("Bulk Post Entries"), () => {
            frm.trigger("open_bulk_post_dialog");
        }, __("Actions"));

        frm.add_custom_button(__("Post Next Due Entry"), () => {
            frm.trigger("post_next_due");
        }, __("Actions"));

        // Disbursement entry — show if not yet created
        if (!frm.doc.disbursement_je) {
            frm.add_custom_button(__("Create Disbursement Entry"), () => {
                frm.trigger("create_disbursement_entry");
            }, __("Actions"));
        } else {
            // Already exists — show a link to it instead
            frm.add_custom_button(__("View Disbursement Entry"), () => {
                frappe.set_route("Form", "Journal Entry", frm.doc.disbursement_je);
            }, __("Actions"));
        }

        frm.add_custom_button(__("Upload New Schedule"), () => {
            frappe.set_route("loan-schedule-upload");
        }, __("Actions"));

        frm.add_custom_button(__("Refresh"), () => {
            frm.reload_doc();
        });
    },

    // ── Create Disbursement Entry ─────────────────────────────────────────────

    create_disbursement_entry(frm) {
        const amount   = frm.doc.disbursement_amount;
        const date     = frm.doc.disbursement_date;
        const currency = frm.doc.currency;

        if (!amount) {
            frappe.msgprint({
                title:   __("Missing Data"),
                message: __("Disbursement amount is zero on this schedule."),
                indicator: "red",
            });
            return;
        }

        frappe.confirm(
            __(`Create a draft Bank Entry for the loan disbursement?<br><br>
               <b>Date:</b> ${date || "—"}<br>
               <b>Amount:</b> ${format_currency(amount, currency)}<br><br>
               <table style="width:100%;font-size:.85rem;border-collapse:collapse;margin-top:8px;">
                 <tr style="background:#f3f4f6;">
                   <th style="padding:6px 8px;text-align:left;">Entry</th>
                   <th style="padding:6px 8px;text-align:left;">Account</th>
                   <th style="padding:6px 8px;text-align:right;">Amount</th>
                 </tr>
                 <tr>
                   <td style="padding:6px 8px;">DR</td>
                   <td style="padding:6px 8px;">${frm.doc.bank_account} (Bank)</td>
                   <td style="padding:6px 8px;text-align:right;">${format_currency(amount, currency)}</td>
                 </tr>
                 <tr>
                   <td style="padding:6px 8px;">CR</td>
                   <td style="padding:6px 8px;">${frm.doc.loan_account} (Loan Liability)</td>
                   <td style="padding:6px 8px;text-align:right;">${format_currency(amount, currency)}</td>
                 </tr>
               </table>`),
            () => {
                frappe.call({
                    method: "loan_schedule_manager.api.schedule_api.create_disbursement_entry",
                    args:   { schedule_name: frm.docname },
                    freeze: true,
                    freeze_message: __("Creating Disbursement Entry…"),
                    callback(r) {
                        if (r.message) {
                            frappe.show_alert({
                                message: __(
                                    `Disbursement JE <a href="/app/journal-entry/${r.message}">${r.message}</a> created as draft. Review and submit it.`
                                ),
                                indicator: "green",
                            }, 8);
                            frm.reload_doc();
                        }
                    },
                });
            }
        );
    },

    // ── Bulk Post Dialog ──────────────────────────────────────────────────────

    open_bulk_post_dialog(frm) {
        const today = frappe.datetime.get_today();
        const lines = frm.doc.schedule_lines || [];
        const currency = frm.doc.currency;

        // Separate lines into groups for display
        const overdue   = lines.filter(l => l.status === "Pending" && l.due_date <  today);
        const due_today = lines.filter(l => l.status === "Pending" && l.due_date === today);
        const future    = lines.filter(l => l.status === "Pending" && l.due_date >  today);
        const has_draft = lines.filter(l => l.status === "Pending" && l.journal_entry);

        const all_pending = [...overdue, ...due_today, ...future];

        if (!all_pending.length) {
            frappe.msgprint({
                title: __("No Pending Lines"),
                message: __("All lines have been posted or there are no pending entries."),
                indicator: "blue",
            });
            return;
        }

        // ── Build the dialog HTML ─────────────────────────────────────────────
        const fmt = (v) => format_currency(v, currency);

        const group_html = (group_lines, label, label_color, checked) => {
            if (!group_lines.length) return "";
            const rows = group_lines.map(l => {
                const has_je   = l.journal_entry ? `<span style="color:#888;font-size:.8em;"> (draft: ${l.journal_entry})</span>` : "";
                const disabled = l.journal_entry ? "disabled title='Draft JE already exists'" : "";
                const check    = (checked && !l.journal_entry) ? "checked" : "";
                return `
                <tr>
                    <td style="width:36px;text-align:center;">
                        <input type="checkbox" class="bulk-line-check"
                               data-due="${l.due_date}" ${check} ${disabled}
                               style="width:16px;height:16px;cursor:pointer;">
                    </td>
                    <td style="padding:6px 8px;">${l.due_date}${has_je}</td>
                    <td style="padding:6px 8px;text-align:right;">${fmt(l.total_payment)}</td>
                    <td style="padding:6px 8px;text-align:right;">${fmt(l.principal_amount)}</td>
                    <td style="padding:6px 8px;text-align:right;">${fmt(l.interest_amount)}</td>
                    <td style="padding:6px 8px;text-align:right;">${fmt(l.outstanding_amount)}</td>
                </tr>`;
            }).join("");

            return `
            <tr>
                <td colspan="6" style="padding:6px 8px 2px;font-weight:600;
                    font-size:.78rem;text-transform:uppercase;letter-spacing:.05em;
                    color:${label_color};">
                    ${label} (${group_lines.length})
                </td>
            </tr>
            ${rows}`;
        };

        const thead = `
            <thead style="background:#f3f4f6;font-size:.82rem;">
                <tr>
                    <th style="width:36px;text-align:center;">
                        <input type="checkbox" id="bulk-select-all"
                               title="Select / deselect all"
                               style="width:16px;height:16px;cursor:pointer;">
                    </th>
                    <th style="padding:6px 8px;">Due Date</th>
                    <th style="padding:6px 8px;text-align:right;">Total</th>
                    <th style="padding:6px 8px;text-align:right;">Principal</th>
                    <th style="padding:6px 8px;text-align:right;">Interest</th>
                    <th style="padding:6px 8px;text-align:right;">Outstanding After</th>
                </tr>
            </thead>`;

        const tbody = `
            <tbody>
                ${group_html(overdue,   "⚠ Overdue",  "#dc2626", true)}
                ${group_html(due_today, "● Due Today", "#d97706", true)}
                ${group_html(future,    "○ Future",    "#6b7280", false)}
            </tbody>`;

        const total_pending = all_pending.filter(l => !l.journal_entry).length;

        const html = `
            <div style="margin-bottom:12px;display:flex;align-items:center;gap:16px;flex-wrap:wrap;">
                <span style="font-size:.85rem;color:#6b7280;">
                    ${total_pending} line(s) available &nbsp;|&nbsp;
                    <span id="bulk-selected-count" style="font-weight:600;color:#1e3a5f;">
                        ${overdue.length + due_today.length - has_draft.filter(l => l.due_date <= today).length} selected
                    </span>
                </span>
                <button class="btn btn-xs btn-default" id="bulk-select-overdue">
                    Select Overdue Only
                </button>
                <button class="btn btn-xs btn-default" id="bulk-select-all-due">
                    Select All Due (incl. today)
                </button>
                <button class="btn btn-xs btn-default" id="bulk-select-none">
                    Clear Selection
                </button>
            </div>
            <div style="overflow-x:auto;max-height:420px;overflow-y:auto;border:1px solid #e5e7eb;border-radius:6px;">
                <table style="width:100%;border-collapse:collapse;font-size:.85rem;">
                    ${thead}
                    ${tbody}
                </table>
            </div>
            <div id="bulk-post-result" style="margin-top:12px;display:none;"></div>`;

        // ── Create dialog ─────────────────────────────────────────────────────
        const d = new frappe.ui.Dialog({
            title:  __("Bulk Post Journal Entries"),
            size:   "extra-large",
            fields: [{ fieldtype: "HTML", fieldname: "bulk_html" }],
            primary_action_label: __("Create Draft JEs for Selected"),
            primary_action() {
                const selected = [...d.$wrapper.find(".bulk-line-check:checked:not(:disabled)")]
                    .map(el => el.dataset.due);

                if (!selected.length) {
                    frappe.msgprint(__("Please select at least one line."));
                    return;
                }

                d.get_primary_btn().prop("disabled", true).text(__("Creating…"));

                frappe.call({
                    method: "loan_schedule_manager.api.schedule_api.create_bulk_journal_entries",
                    args: {
                        schedule_name: frm.docname,
                        due_dates: JSON.stringify(selected),
                    },
                    callback(r) {
                        d.get_primary_btn().prop("disabled", false)
                            .text(__("Create Draft JEs for Selected"));

                        if (!r.message) return;
                        const { created, skipped, errors } = r.message;

                        let html = "";

                        if (created.length) {
                            const links = created.map(c =>
                                `<a href="/app/journal-entry/${c.je_name}" target="_blank">${c.je_name}</a> (${c.due_date})`
                            ).join("<br>");
                            html += `<div class="alert alert-success" style="margin-top:8px;">
                                ✅ <strong>${created.length} draft JE(s) created:</strong><br>${links}
                            </div>`;
                        }
                        if (skipped.length) {
                            const rows = skipped.map(s =>
                                `<li>${s.due_date}: ${s.reason}</li>`
                            ).join("");
                            html += `<div class="alert alert-warning" style="margin-top:8px;">
                                ⚠ <strong>${skipped.length} skipped:</strong><ul style="margin:4px 0 0 16px;">${rows}</ul>
                            </div>`;
                        }
                        if (errors.length) {
                            const rows = errors.map(e =>
                                `<li>${e.due_date}: ${e.error}</li>`
                            ).join("");
                            html += `<div class="alert alert-danger" style="margin-top:8px;">
                                ❌ <strong>${errors.length} error(s):</strong><ul style="margin:4px 0 0 16px;">${rows}</ul>
                            </div>`;
                        }

                        d.$wrapper.find("#bulk-post-result")
                            .html(html).show();

                        if (created.length) {
                            frm.reload_doc();
                        }
                    },
                });
            },
        });

        d.fields_dict.bulk_html.$wrapper.html(html);
        d.show();

        // ── Wire up selection helpers ─────────────────────────────────────────
        const $wrap = d.$wrapper;

        const update_count = () => {
            const n = $wrap.find(".bulk-line-check:checked:not(:disabled)").length;
            $wrap.find("#bulk-selected-count").text(`${n} selected`);
        };

        // Select-all header checkbox
        $wrap.find("#bulk-select-all").on("change", function() {
            $wrap.find(".bulk-line-check:not(:disabled)")
                 .prop("checked", this.checked);
            update_count();
        });

        // Individual checkbox — sync header
        $wrap.on("change", ".bulk-line-check", () => {
            const all   = $wrap.find(".bulk-line-check:not(:disabled)").length;
            const checked = $wrap.find(".bulk-line-check:checked:not(:disabled)").length;
            $wrap.find("#bulk-select-all").prop("checked", all === checked)
                                          .prop("indeterminate", checked > 0 && checked < all);
            update_count();
        });

        // "Select Overdue Only" button
        $wrap.find("#bulk-select-overdue").on("click", () => {
            $wrap.find(".bulk-line-check:not(:disabled)").prop("checked", false);
            overdue.filter(l => !l.journal_entry).forEach(l => {
                $wrap.find(`.bulk-line-check[data-due="${l.due_date}"]`)
                     .prop("checked", true);
            });
            update_count();
        });

        // "Select All Due" button (overdue + today)
        $wrap.find("#bulk-select-all-due").on("click", () => {
            $wrap.find(".bulk-line-check:not(:disabled)").prop("checked", false);
            [...overdue, ...due_today].filter(l => !l.journal_entry).forEach(l => {
                $wrap.find(`.bulk-line-check[data-due="${l.due_date}"]`)
                     .prop("checked", true);
            });
            update_count();
        });

        // "Clear" button
        $wrap.find("#bulk-select-none").on("click", () => {
            $wrap.find(".bulk-line-check:not(:disabled)").prop("checked", false);
            $wrap.find("#bulk-select-all").prop("checked", false).prop("indeterminate", false);
            update_count();
        });
    },

    // ── Post Next Due (single) ────────────────────────────────────────────────

    post_next_due(frm) {
        const today = frappe.datetime.get_today();
        const lines = frm.doc.schedule_lines || [];

        // Skip lines that already have a draft JE
        const due = lines.filter(l =>
            l.status === "Pending" && l.due_date <= today && !l.journal_entry
        );

        if (!due.length) {
            frappe.msgprint({
                title: __("Nothing Due"),
                message: __("No pending lines are due today or earlier (without an existing draft JE)."),
                indicator: "blue",
            });
            return;
        }

        const next = due[0];

        frappe.confirm(
            __(`Create draft Journal Entry for line due <b>${next.due_date}</b>?<br>
                Principal: <b>${format_currency(next.principal_amount, frm.doc.currency)}</b>&nbsp;
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
                                message: __(`Draft JE <a href="/app/journal-entry/${r.message}">${r.message}</a> created.`),
                                indicator: "green",
                            }, 6);
                            frm.reload_doc();
                        }
                    },
                });
            }
        );
    },

    // ── Overdue line highlighting ─────────────────────────────────────────────

    highlight_overdue_lines(frm) {
        setTimeout(() => {
            const today = frappe.datetime.get_today();
            (frm.doc.schedule_lines || []).forEach((line, idx) => {
                if (line.status !== "Pending") return;
                if (!line.due_date) return;

                const row_el = frm.fields_dict.schedule_lines?.grid?.get_row(idx)?.$row;
                if (!row_el) return;

                if (line.due_date < today) {
                    row_el.css("background", "rgba(255,80,80,0.07)");
                    row_el.attr("title", "Overdue – no journal entry yet");
                } else if (line.due_date === today) {
                    row_el.css("background", "rgba(255,180,0,0.1)");
                    row_el.attr("title", "Due today");
                }
            });
        }, 600);
    },
});


// ── Schedule Line child table events ─────────────────────────────────────────

frappe.ui.form.on("Bank Loan Schedule Line", {
    journal_entry(frm, cdt, cdn) {
        const row = locals[cdt][cdn];
        if (row.journal_entry) {
            window.open(`/app/journal-entry/${row.journal_entry}`, "_blank");
        }
    },
});


// ── List view ─────────────────────────────────────────────────────────────────

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
            if (!val || parseFloat(val) === 0)
                return `<span style="color:var(--green-500);">Paid off</span>`;
            return `<span style="color:var(--red-500);">${format_currency(val, doc.currency)}</span>`;
        },
    },
};