"""
PDF Parser for Bank Loan Repayment Schedules.

Handles the schedule format produced by core banking systems, as seen in
Arrangement Schedule Projection documents. PDF structure:
  - Header: Arrangement Id, Product Name, Customer Id, Customer Name, Currency
  - Rows (two sub-rows each):
      Row 1 (Account): Due Date | Total Payment | Due Type | Due Type Amt | Property=Account | Prop Amount (principal) | Outstanding Amount
      Row 2 (Interest): 0.00 | | | 0.00 | Principal Interest | interest_amount | 0.00
  - First row is a disbursement (negative Total Payment)
"""

import re
import frappe
from datetime import datetime


def parse_schedule_pdf(file_path: str) -> dict:
    """
    Parse a loan schedule PDF and return structured data.

    Returns:
        {
            "arrangement_id": str,
            "product_name": str,
            "customer_id": str,
            "customer_name": str,
            "currency": str,
            "disbursement_date": str (YYYY-MM-DD),
            "disbursement_amount": float,
            "schedule_lines": [
                {
                    "due_date": str (YYYY-MM-DD),
                    "total_payment": float,
                    "principal_amount": float,
                    "interest_amount": float,
                    "outstanding_amount": float,
                }
            ]
        }
    """
    try:
        import pdfplumber
    except ImportError:
        frappe.throw("pdfplumber is required. Run: bench pip install pdfplumber")

    result = {
        "arrangement_id": None,
        "product_name": None,
        "customer_id": None,
        "customer_name": None,
        "currency": "USD",
        "disbursement_date": None,
        "disbursement_amount": 0.0,
        "schedule_lines": [],
    }

    full_text = ""
    all_rows = []

    with pdfplumber.open(file_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            full_text += "\n" + text

            # Extract table rows from page
            tables = page.extract_tables()
            for table in tables:
                for row in table:
                    if row:
                        all_rows.append([cell.strip() if cell else "" for cell in row])

    # ── Parse header from text ──────────────────────────────────────────
    _parse_header(full_text, result)

    # ── Parse schedule lines from rows ─────────────────────────────────
    if all_rows:
        _parse_rows_from_table(all_rows, result)
    else:
        # Fallback: parse from raw text
        _parse_rows_from_text(full_text, result)

    return result


def _parse_header(text: str, result: dict):
    """Extract arrangement metadata from the text block."""

    # Arrangement Id
    m = re.search(r"Arrangement\s+Id\s*:\s*(\S+)", text)
    if m:
        result["arrangement_id"] = m.group(1).strip()

    # Product Name
    m = re.search(r"Product\s+Name\s*:\s*(.+?)(?:\n|Customer)", text, re.IGNORECASE)
    if m:
        result["product_name"] = m.group(1).strip()

    # Customer Id
    m = re.search(r"Customer\s+Id\s*:\s*(\d+)", text)
    if m:
        result["customer_id"] = m.group(1).strip()

    # Customer Name (appears after Customer Id on same or next line)
    m = re.search(r"Customer\s+Id\s*:\s*\d+\s+(.+?)(?:\n|Currency)", text, re.DOTALL)
    if m:
        result["customer_name"] = m.group(1).strip()

    # Currency
    m = re.search(r"Currency\s*:\s*(\w+)", text)
    if m:
        result["currency"] = m.group(1).strip()


def _parse_rows_from_table(rows: list, result: dict):
    """
    Parse schedule lines from pdfplumber table rows.
    Two consecutive rows represent one schedule entry:
      - Row A: date | total_payment | due_type | ... | Account | principal | outstanding
      - Row B: 0.00 | ... | Principal Interest | interest | 0.00
    """
    date_pattern = re.compile(r"^\d{2}/\d{2}/\d{2}$")
    num_pattern = re.compile(r"^-?[\d,]+\.?\d*$")

    def clean_num(s):
        if not s:
            return 0.0
        return float(s.replace(",", "").strip() or "0")

    def parse_date(s):
        try:
            return datetime.strptime(s.strip(), "%d/%m/%y").strftime("%Y-%m-%d")
        except Exception:
            return None

    i = 0
    pending = None  # accumulates row A data

    for row in rows:
        # Skip header rows
        if any(h in str(row) for h in ["Due Date", "Due Type", "Property", "Outstanding"]):
            continue

        # Check if this row starts with a date (row A)
        date_val = None
        for cell in row:
            if cell and date_pattern.match(cell.strip()):
                date_val = cell.strip()
                break

        if date_val:
            # Save previous pending if any
            if pending:
                result["schedule_lines"].append(pending)
                pending = None

            parsed_date = parse_date(date_val)
            if not parsed_date:
                continue

            # Find numeric values
            nums = [clean_num(c) for c in row if c and num_pattern.match(c.strip())]

            # Disbursement row: total_payment is negative
            if nums and nums[0] < 0:
                result["disbursement_date"] = parsed_date
                result["disbursement_amount"] = abs(nums[0])
                continue

            # Repayment row A
            total_payment = nums[0] if len(nums) > 0 else 0.0
            principal = 0.0
            outstanding = 0.0

            # Find Account property amount and outstanding
            # Structure: ... Account | prop_amount | outstanding
            for idx, cell in enumerate(row):
                if cell and cell.strip() == "Account":
                    # prop_amount is next non-empty cell, outstanding after that
                    numeric_after = [c for c in row[idx + 1:] if c and num_pattern.match(c.strip())]
                    if len(numeric_after) >= 2:
                        principal = clean_num(numeric_after[0])
                        outstanding = clean_num(numeric_after[1])
                    elif len(numeric_after) == 1:
                        principal = clean_num(numeric_after[0])
                    break

            pending = {
                "due_date": parsed_date,
                "total_payment": total_payment,
                "principal_amount": principal,
                "interest_amount": 0.0,
                "outstanding_amount": abs(outstanding),
            }

        elif pending is not None:
            # Row B: contains interest amount under "Principal Interest"
            for idx, cell in enumerate(row):
                if cell and "Principal Interest" in cell:
                    numeric_after = [c for c in row[idx + 1:] if c and re.match(r"^[\d,]+\.?\d*$", c.strip())]
                    if numeric_after:
                        pending["interest_amount"] = clean_num(numeric_after[0])
                    break

            # Row B finalises the entry
            result["schedule_lines"].append(pending)
            pending = None

    if pending:
        result["schedule_lines"].append(pending)


def _parse_rows_from_text(text: str, result: dict):
    """
    Fallback text-based parser when table extraction yields no rows.

    Each repayment block looks like two consecutive lines:
      Line A:  28/02/26  2,073.05  Constant Repayment  2,073.05  Account  1,360.89  -93,031.97
      Line B:  0.00  0.00  Principal Interest  712.16  0.00

    Strategy: find every date-bearing line, extract the last two numbers as
    (principal_or_prop_amount, outstanding), then look ahead for interest.
    """
    lines = text.split("\n")

    date_re = re.compile(r"(\d{2}/\d{2}/\d{2})")
    num_re  = re.compile(r"-?[\d,]+\.\d{2}")

    def clean(s):
        return float(s.replace(",", ""))

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        dm = date_re.search(line)
        if dm:
            date_str = dm.group(1)
            try:
                parsed_date = datetime.strptime(date_str, "%d/%m/%y").strftime("%Y-%m-%d")
            except Exception:
                i += 1
                continue

            nums = [clean(n) for n in num_re.findall(line)]

            # Disbursement: first significant number is negative
            if nums and nums[0] < 0:
                result["disbursement_date"] = parsed_date
                result["disbursement_amount"] = abs(nums[0])
                i += 1
                continue

            # Need at least: total_payment, ..., principal, outstanding
            if len(nums) < 3:
                i += 1
                continue

            total_payment = nums[0]
            # The last number is outstanding (negative in source, stored positive)
            outstanding = abs(nums[-1])
            # The second-to-last is the principal (prop_amount)
            principal = nums[-2]

            # Look ahead for interest row
            interest = 0.0
            if i + 1 < len(lines):
                next_line = lines[i + 1]
                if "Principal Interest" in next_line:
                    interest_nums = num_re.findall(next_line)
                    # Interest amount is the non-zero value
                    for n in interest_nums:
                        v = clean(n)
                        if v > 0:
                            interest = v
                            break
                    i += 1  # consume interest line

            result["schedule_lines"].append({
                "due_date": parsed_date,
                "total_payment": total_payment,
                "principal_amount": principal,
                "interest_amount": interest,
                "outstanding_amount": outstanding,
            })
        i += 1
