# Loan Schedule Manager

A Frappe/ERPNext custom app that imports bank loan repayment schedule PDFs, stores them as structured documents, and automatically posts monthly Journal Entries — with full adjustment tracking.

---

## Features

| Feature | Detail |
|---|---|
| **PDF Import** | Drag-and-drop wizard parses Arrangement Schedule Projection PDFs |
| **Duplicate Guard** | Checks Arrangement ID uniqueness before creating any record |
| **3-DocType Storage** | Schedule header + line items + audit log in clean relational structure |
| **Auto Journal Entries** | Daily scheduler creates and submits JEs on each repayment due date |
| **Adjustment Tracking** | Every JE cancellation or amendment is reflected on the schedule line and logged |
| **Visual Dashboard** | Progress indicators, overdue line highlights, outstanding balance badge |

---

## Architecture

```
Bank Loan Schedule (parent)
│
├── schedule_lines  →  Bank Loan Schedule Line  (one row per installment)
│                        due_date | scheduled amounts | actual amounts | JE link | status
│
└── adjustment_log  →  Bank Loan Schedule Adjustment  (append-only audit trail)
                         date | user | JE | type | variances | notes
```

### Accounting entries created per installment

```
DR  Loan Liability Account        principal_amount
DR  Interest Expense Account      interest_amount
CR  Bank / Cash Account           total_payment
```

---

## Installation

### Prerequisites
- ERPNext v14 or v15
- Python 3.10+
- `pdfplumber` Python package

### Steps

```bash
# 1. Get the app
bench get-app loan_schedule_manager https://github.com/upande/loan_schedule_manager
# — OR copy the folder directly into apps/

# 2. Install on your site
bench --site kikwetu.upande.com install-app loan_schedule_manager

# 3. Run migrations (creates DocTypes + custom fields on Journal Entry)
bench --site kikwetu.upande.com migrate

# 4. Install PDF parsing dependency
bench pip install pdfplumber

# 5. Build frontend assets
bench build --app loan_schedule_manager

# 6. Restart
bench restart
```

---

## PDF Format

The app is built for **Arrangement Schedule Projection** documents. Each PDF must contain:

```
Arrangement Id : <ID>        Product Name : Hire Purchase
Customer Id : <ID>   <CUSTOMER NAME>
Currency : USD

Due Date  |  Total Payment  |  Due Type       |  Property          |  Prop Amount  |  Outstanding
----------|-----------------|------------------|--------------------|---------------|-------------
29/08/25  |   -99,180.00   |  Disburse %      |  Account           |  99,180.00   |  -99,180.00
28/10/25  |    1,994.40    |  Constant Repay  |  Account           |    776.69    |  -98,403.31
          |        0.00    |                  |  Principal Interest |  1,217.71   |       0.00
...
```

Each installment occupies **two rows**: the Account row (principal) followed by a Principal Interest row.

---

## Usage

### 1. Upload a Schedule

Navigate to **Loan Schedule Upload** (via the ERPNext menu or the button in the Bank Loan Schedule list view).

1. Drop the PDF or click to browse
2. The system parses it and checks for duplicates
3. If new, review the preview and enter the 4 GL accounts:
   - **Loan Liability Account** — e.g. `Loans Payable - KFL`
   - **Interest Expense Account** — e.g. `Bank Interest Expense - KFL`
   - **Bank / Cash Account** — e.g. `Standard Chartered USD - KFL`
   - **Cost Center** (optional)
4. Click **Create Loan Schedule**

### 2. Automatic Monthly Entries

The daily scheduler (`create_due_loan_journal_entries`) runs every night and posts JEs for all lines where `due_date ≤ today` and `status = Pending`. No manual action needed.

### 3. Manual Posting

Open a **Bank Loan Schedule** record → **Actions → Post Next Due Entry** to manually trigger the next overdue line.

### 4. Handling Adjustments

If you need to change an amount on an auto-created JE:

1. Cancel the original JE — the schedule line reverts to **Pending** and the cancellation is logged
2. Create a new JE (or let the scheduler retry) with the correct amounts
3. If amounts differ from the schedule, the line is marked **Adjusted** and the variance is recorded in the **Adjustment Log** tab

---

## Custom Fields Added to Journal Entry

| Field | Purpose |
|---|---|
| `custom_loan_schedule` | Links the JE back to its Bank Loan Schedule |
| `custom_loan_schedule_line_date` | Records which schedule line (by due date) this JE covers |

Both fields are hidden and read-only; they are populated automatically by the scheduler.

---

## Running Tests

```bash
bench --site kikwetu.upande.com run-tests --app loan_schedule_manager
```

---

## File Reference

```
loan_schedule_manager/
├── hooks.py                          # scheduler + JE doc event wiring
├── modules.txt
├── patches.txt
├── patches/
│   └── v1_0/
│       └── add_custom_fields_to_journal_entry.py
├── doctype/
│   ├── bank_loan_schedule/
│   │   ├── bank_loan_schedule.json   # DocType definition
│   │   ├── bank_loan_schedule.py     # Python controller
│   │   └── bank_loan_schedule.js    # Client script (actions, indicators)
│   ├── bank_loan_schedule_line/
│   │   ├── bank_loan_schedule_line.json
│   │   └── bank_loan_schedule_line.py
│   └── bank_loan_schedule_adjustment/
│       ├── bank_loan_schedule_adjustment.json
│       └── bank_loan_schedule_adjustment.py
├── api/
│   └── schedule_api.py               # upload_and_preview_schedule, create_loan_schedule
├── events/
│   └── journal_entry.py              # on_submit / on_cancel / on_update_after_submit
├── scheduler/
│   └── tasks.py                      # daily JE creation task
├── utils/
│   └── pdf_parser.py                 # pdfplumber-based PDF parser
├── page/
│   └── loan_schedule_upload/
│       ├── loan_schedule_upload.json
│       └── loan_schedule_upload.js   # 3-step upload wizard UI
└── tests/
    └── test_loan_schedule.py
```
