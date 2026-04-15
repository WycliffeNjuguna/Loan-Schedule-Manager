/**
 * Loan Schedule Upload Wizard
 * Frappe Page: loan-schedule-upload
 *
 * Flow:
 *   Step 1 – Upload PDF → parse → show preview + duplicate check
 *   Step 2 – Confirm accounts (loan, interest, bank, cost centre)
 *   Step 3 – Create + success
 */

frappe.pages["loan-schedule-upload"].on_page_load = function (wrapper) {
    const page = frappe.ui.make_app_page({
        parent: wrapper,
        title: "Loan Schedule Upload",
        single_column: true,
    });

    new LoanScheduleUploadWizard(page, wrapper);
};

class LoanScheduleUploadWizard {
    constructor(page, wrapper) {
        this.page = page;
        this.wrapper = wrapper;
        this.parsed = null;
        this.file_doc = null;
        this.step = 1;

        this._render_step1();
    }

    // ── STEP 1: Upload ─────────────────────────────────────────────────────────

    _render_step1() {
        $(this.wrapper).find(".page-content").html(`
            <div class="loan-upload-wizard" style="max-width:760px; margin:30px auto;">
                <div class="wizard-steps" style="display:flex; gap:8px; margin-bottom:32px;">
                    ${this._step_pill(1,"Upload PDF",true)}
                    ${this._step_pill(2,"Configure Accounts",false)}
                    ${this._step_pill(3,"Done",false)}
                </div>

                <div class="card shadow-sm p-4">
                    <h5 class="mb-3 text-muted text-uppercase" style="letter-spacing:.08em;font-size:.78rem;">
                        Step 1 — Upload Repayment Schedule PDF
                    </h5>
                    <div class="drop-zone" id="drop-zone"
                        style="border:2px dashed var(--border-color);border-radius:12px;padding:48px 24px;
                               text-align:center;cursor:pointer;transition:all .2s;">
                        <div style="font-size:2.5rem;">📄</div>
                        <p style="margin:12px 0 4px;font-weight:600;">Drop PDF here or click to browse</p>
                        <p class="text-muted" style="font-size:.85rem;">Arrangement Schedule Projection documents only</p>
                        <input type="file" id="pdf-input" accept=".pdf" style="display:none;">
                    </div>
                    <div id="upload-status" style="margin-top:16px;display:none;"></div>
                </div>
            </div>
        `);

        const dz = document.getElementById("drop-zone");
        const inp = document.getElementById("pdf-input");

        dz.addEventListener("click", () => inp.click());
        dz.addEventListener("dragover", e => { e.preventDefault(); dz.style.borderColor = "var(--primary)"; });
        dz.addEventListener("dragleave", () => { dz.style.borderColor = "var(--border-color)"; });
        dz.addEventListener("drop", e => {
            e.preventDefault();
            dz.style.borderColor = "var(--border-color)";
            if (e.dataTransfer.files[0]) this._handle_file(e.dataTransfer.files[0]);
        });
        inp.addEventListener("change", () => { if (inp.files[0]) this._handle_file(inp.files[0]); });
    }

    _handle_file(file) {
        if (!file.name.endsWith(".pdf")) {
            frappe.msgprint("Please upload a PDF file.");
            return;
        }

        const status = document.getElementById("upload-status");
        status.style.display = "block";
        status.innerHTML = `<div class="alert alert-info">⏳ Uploading and parsing <strong>${file.name}</strong>…</div>`;

        // Upload via Frappe's upload API
        const fd = new FormData();
        fd.append("file", file, file.name);
        fd.append("is_private", "0");
        fd.append("folder", "Home/Loan Schedules");

        fetch("/api/method/upload_file", {
            method: "POST",
            headers: { "X-Frappe-CSRF-Token": frappe.csrf_token },
            body: fd,
        })
        .then(r => r.json())
        .then(resp => {
            if (resp.exc) throw new Error(resp.exc);
            const file_doc_name = resp.message.name;
            return frappe.call({
                method: "loan_schedule_manager.api.schedule_api.upload_and_preview_schedule",
                args: { file_doc_name },
            });
        })
        .then(resp => {
            const result = resp.message;
            if (result.duplicate) {
                status.innerHTML = `
                    <div class="alert alert-danger">
                        ⛔ <strong>Duplicate detected.</strong> Arrangement ID
                        <code>${result.parsed.arrangement_id}</code> already exists.<br>
                        <a href="/app/bank-loan-schedule/${result.existing_doc}" target="_blank">
                            View existing record →
                        </a>
                    </div>`;
                return;
            }
            this.parsed = result.parsed;
            this.file_doc = result.file_doc_name;
            this._render_step2();
        })
        .catch(err => {
            status.innerHTML = `<div class="alert alert-danger">❌ Error: ${err.message || err}</div>`;
        });
    }

    // ── STEP 2: Preview + Accounts ─────────────────────────────────────────────

    _render_step2() {
        const p = this.parsed;

        const lines_html = (p.schedule_lines || []).slice(0, 6).map((l, i) => `
            <tr>
                <td>${l.due_date}</td>
                <td class="text-right">${this._fmt(l.total_payment)}</td>
                <td class="text-right">${this._fmt(l.principal_amount)}</td>
                <td class="text-right">${this._fmt(l.interest_amount)}</td>
                <td class="text-right">${this._fmt(l.outstanding_amount)}</td>
            </tr>
        `).join("") + (p.schedule_lines.length > 6
            ? `<tr><td colspan="5" class="text-muted text-center">… and ${p.schedule_lines.length - 6} more lines</td></tr>`
            : "");

        $(this.wrapper).find(".page-content").html(`
            <div class="loan-upload-wizard" style="max-width:900px; margin:30px auto;">
                <div class="wizard-steps" style="display:flex; gap:8px; margin-bottom:32px;">
                    ${this._step_pill(1,"Upload PDF",false)}
                    ${this._step_pill(2,"Configure Accounts",true)}
                    ${this._step_pill(3,"Done",false)}
                </div>

                <!-- Parsed Summary -->
                <div class="card shadow-sm p-4 mb-4">
                    <h5 class="mb-3 text-muted text-uppercase" style="letter-spacing:.08em;font-size:.78rem;">
                        Parsed Schedule Preview
                    </h5>
                    <div class="row">
                        <div class="col-md-6">
                            <table class="table table-sm table-borderless">
                                <tr><th style="width:45%">Arrangement ID</th><td><strong>${p.arrangement_id}</strong></td></tr>
                                <tr><th>Product</th><td>${p.product_name || "—"}</td></tr>
                                <tr><th>Customer ID</th><td>${p.customer_id || "—"}</td></tr>
                                <tr><th>Customer Name</th><td>${p.customer_name || "—"}</td></tr>
                                <tr><th>Currency</th><td>${p.currency}</td></tr>
                            </table>
                        </div>
                        <div class="col-md-6">
                            <table class="table table-sm table-borderless">
                                <tr><th style="width:55%">Disbursement Date</th><td>${p.disbursement_date || "—"}</td></tr>
                                <tr><th>Disbursement Amount</th><td>${this._fmt(p.disbursement_amount)}</td></tr>
                                <tr><th>Total Lines</th><td>${(p.schedule_lines||[]).length}</td></tr>
                                <tr><th>First Due Date</th><td>${p.schedule_lines?.[0]?.due_date || "—"}</td></tr>
                                <tr><th>Last Due Date</th><td>${p.schedule_lines?.[p.schedule_lines.length-1]?.due_date || "—"}</td></tr>
                            </table>
                        </div>
                    </div>
                    <div style="overflow-x:auto; margin-top:12px;">
                        <table class="table table-sm table-bordered" style="font-size:.85rem;">
                            <thead class="thead-light">
                                <tr>
                                    <th>Due Date</th>
                                    <th class="text-right">Total</th>
                                    <th class="text-right">Principal</th>
                                    <th class="text-right">Interest</th>
                                    <th class="text-right">Outstanding</th>
                                </tr>
                            </thead>
                            <tbody>${lines_html}</tbody>
                        </table>
                    </div>
                </div>

                <!-- Account Configuration -->
                <div class="card shadow-sm p-4 mb-4">
                    <h5 class="mb-3 text-muted text-uppercase" style="letter-spacing:.08em;font-size:.78rem;">
                        Step 2 — Configure Accounts
                    </h5>
                    <div class="row">
                        <div class="col-md-6">
                            <div class="form-group" id="field-loan_account"></div>
                            <div class="form-group" id="field-interest_account"></div>
                        </div>
                        <div class="col-md-6">
                            <div class="form-group" id="field-bank_account"></div>
                            <div class="form-group" id="field-cost_center"></div>
                        </div>
                    </div>
                    <div id="accounts-error" class="text-danger mt-2" style="display:none;"></div>
                </div>

                <div style="display:flex; gap:12px; justify-content:flex-end;">
                    <button class="btn btn-default" id="btn-back">← Back</button>
                    <button class="btn btn-primary" id="btn-create">
                        Create Loan Schedule →
                    </button>
                </div>
            </div>
        `);

        // Render Frappe Link fields
        this._make_link_field("field-loan_account", "Loan Liability Account", "Account", "loan_account");
        this._make_link_field("field-interest_account", "Interest Expense Account", "Account", "interest_account");
        this._make_link_field("field-bank_account", "Bank / Cash Account", "Account", "bank_account");
        this._make_link_field("field-cost_center", "Cost Center", "Cost Center", "cost_center", false);

        document.getElementById("btn-back").addEventListener("click", () => this._render_step1());
        document.getElementById("btn-create").addEventListener("click", () => this._create_schedule());
    }

    _make_link_field(container_id, label, doctype, field_name, required = true) {
        const $wrap = $(`#${container_id}`);
        $wrap.html(`
            <label>${label}${required ? ' <span class="text-danger">*</span>' : ''}</label>
            <div class="link-field-wrap" id="wrap-${field_name}"></div>
        `);

        const df = {
            fieldtype: "Link",
            fieldname: field_name,
            options: doctype,
            reqd: required ? 1 : 0,
            label: label,
        };

        const field = frappe.ui.form.make_control({
            df,
            parent: $(`#wrap-${field_name}`)[0],
            render_input: true,
        });

        field.refresh();
        this[`_field_${field_name}`] = field;
    }

    _get_account_values() {
        return {
            loan_account: this._field_loan_account?.get_value(),
            interest_account: this._field_interest_account?.get_value(),
            bank_account: this._field_bank_account?.get_value(),
            cost_center: this._field_cost_center?.get_value(),
        };
    }

    _create_schedule() {
        const accounts = this._get_account_values();
        const err = document.getElementById("accounts-error");

        const missing = ["loan_account", "interest_account", "bank_account"]
            .filter(k => !accounts[k]);

        if (missing.length) {
            err.style.display = "block";
            err.textContent = "Please fill in: " + missing.map(k => k.replace(/_/g, " ")).join(", ");
            return;
        }
        err.style.display = "none";

        const btn = document.getElementById("btn-create");
        btn.disabled = true;
        btn.textContent = "Creating…";

        frappe.call({
            method: "loan_schedule_manager.api.schedule_api.create_loan_schedule",
            args: {
                parsed_data: this.parsed,
                accounts: accounts,
                file_doc_name: this.file_doc,
            },
        })
        .then(resp => {
            this._render_step3(resp.message);
        })
        .catch(err => {
            btn.disabled = false;
            btn.textContent = "Create Loan Schedule →";
            frappe.msgprint({ title: "Error", message: err.message || String(err), indicator: "red" });
        });
    }

    // ── STEP 3: Success ────────────────────────────────────────────────────────

    _render_step3(doc_name) {
        $(this.wrapper).find(".page-content").html(`
            <div class="loan-upload-wizard" style="max-width:600px; margin:60px auto; text-align:center;">
                <div class="wizard-steps" style="display:flex; gap:8px; margin-bottom:48px; justify-content:center;">
                    ${this._step_pill(1,"Upload PDF",false)}
                    ${this._step_pill(2,"Configure Accounts",false)}
                    ${this._step_pill(3,"Done",true)}
                </div>
                <div style="font-size:4rem;">✅</div>
                <h3 style="margin:16px 0 8px;">Loan Schedule Created</h3>
                <p class="text-muted">
                    <strong>${doc_name}</strong> has been created successfully.<br>
                    Journal entries will be auto-posted on each due date.
                </p>
                <div style="margin-top:32px; display:flex; gap:12px; justify-content:center;">
                    <a href="/app/bank-loan-schedule/${doc_name}" class="btn btn-primary">
                        Open Record →
                    </a>
                    <button class="btn btn-default" id="btn-another">Upload Another</button>
                </div>
            </div>
        `);

        document.getElementById("btn-another").addEventListener("click", () => {
            this.parsed = null;
            this.file_doc = null;
            this._render_step1();
        });
    }

    // ── Helpers ────────────────────────────────────────────────────────────────

    _step_pill(num, label, active) {
        const bg = active ? "var(--primary)" : "var(--border-color)";
        const color = active ? "#fff" : "var(--text-muted)";
        return `
            <div style="display:flex;align-items:center;gap:6px;">
                <div style="width:26px;height:26px;border-radius:50%;background:${bg};
                            color:${color};display:flex;align-items:center;justify-content:center;
                            font-size:.78rem;font-weight:700;">${num}</div>
                <span style="font-size:.82rem;color:${color};font-weight:${active?'600':'400'};">${label}</span>
            </div>
            ${num < 3 ? '<span style="color:var(--border-color)">→</span>' : ''}
        `;
    }

    _fmt(val) {
        if (!val && val !== 0) return "—";
        return parseFloat(val).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }
}
