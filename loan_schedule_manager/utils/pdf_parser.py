"""
PDF Parser for Bank Loan Repayment Schedules.

WHY TEXT-ONLY (not extract_tables):
  pdfplumber's table extractor sees each repayment row as its own mini-table
  and completely drops the "Principal Interest" sub-row that carries the
  interest amount. The raw text stream contains everything correctly.

TEXT STRUCTURE PER PAGE:
  29/08/25  -99,180.00  Disburse Percen  99,180.00  Account  99,180.00  -99,180.00
  tage                                   <- word-wrap artifact, ignored
  28/10/25  1,994.40  Constant Repay  1,994.40  Account  776.69  -98,403.31
  ment                                   <- word-wrap artifact, ignored
  0.00  0.00  Principal Interest  1,217.71  0.00   <- 4th number = interest amount

IMPORTANT EDGE CASE:
  The last repayment on each page has its "Principal Interest" line as the
  FIRST line of the next page. We handle this by collecting all lines into a
  single stream and stripping page header/footer lines first.
"""

import re
import frappe
from datetime import datetime


_SKIP_RE = re.compile(
    r"^(Page\s+\d+\s+of\s+\d+"
    r"|\d{1,2}\s+\w+\s+\d{4}"
    r"|\d{2}:\d{2}:\d{2}"
    r"|Arrangement\s+Schedule"
    r"|Arrangement\s+Id"
    r"|Customer\s+Id"
    r"|Currency\s*:"
    r"|Due\s+Date"
    r"|^Amount$"
    r")",
    re.IGNORECASE,
)

_DATE_RE     = re.compile(r"^(\d{2}/\d{2}/\d{2})\s+")
_NUM_RE      = re.compile(r"-?[\d,]+\.\d{2}")
_INTEREST_RE = re.compile(r"Principal\s+Interest\s+([\d,]+\.\d{2})")


def _clean(s: str) -> float:
    return float(s.replace(",", ""))


def _parse_date(s: str):
    try:
        return datetime.strptime(s.strip(), "%d/%m/%y").strftime("%Y-%m-%d")
    except Exception:
        return None


def parse_schedule_pdf(file_path: str) -> dict:
    """
    Parse a loan schedule PDF and return structured data.
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

    full_text   = ""
    clean_lines = []

    with pdfplumber.open(file_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            full_text += "\n" + text
            for line in text.split("\n"):
                if not _SKIP_RE.match(line.strip()):
                    clean_lines.append(line)

    _parse_header(full_text, result)
    _parse_lines(clean_lines, result)

    return result


def _parse_header(text: str, result: dict):
    m = re.search(r"Arrangement\s+Id\s*:\s*(\S+)", text)
    if m:
        result["arrangement_id"] = m.group(1).strip()

    m = re.search(r"Product\s+Name\s*:\s*(.+?)(?:\n|$)", text, re.IGNORECASE)
    if m:
        result["product_name"] = m.group(1).strip()

    m = re.search(r"Customer\s+Id\s*:\s*(\d+)", text)
    if m:
        result["customer_id"] = m.group(1).strip()

    m = re.search(r"Customer\s+Id\s*:\s*\d+\s+(.+?)(?:\n|$)", text)
    if m:
        result["customer_name"] = m.group(1).strip()

    m = re.search(r"Currency\s*:\s*(\w+)", text)
    if m:
        result["currency"] = m.group(1).strip()


def _parse_lines(lines: list, result: dict):
    """
    Walk the cleaned line stream extracting every repayment entry.

    Each entry spans up to 3 source lines:
      LINE A:  DD/MM/YY  total  [Constant Repay]  total  Account  principal  -outstanding
      LINE B:  ment / tage  (word-wrap artifact — skipped)
      LINE C:  0.00  0.00  Principal Interest  <interest>  0.00

    LINE C may be the first line of the next page (page-boundary split).
    We look ahead up to 4 lines, stopping early only when another date line
    appears (which means a new entry started without an interest row).
    """
    i = 0
    while i < len(lines):
        line = lines[i].strip()

        dm = _DATE_RE.match(line)
        if not dm:
            i += 1
            continue

        date_str = dm.group(1)
        parsed_date = _parse_date(date_str)
        if not parsed_date:
            i += 1
            continue

        nums = [_clean(n) for n in _NUM_RE.findall(line)]

        # Disbursement row — first significant number is negative
        if nums and nums[0] < 0:
            result["disbursement_date"]   = parsed_date
            result["disbursement_amount"] = abs(nums[0])
            i += 1
            continue

        # Need at least: total_payment, ..., principal, outstanding
        if len(nums) < 2:
            i += 1
            continue

        total_payment = nums[0]
        principal     = nums[-2]    # Prop Amount column (principal portion)
        outstanding   = abs(nums[-1])  # Outstanding Amount (negative in source)

        # Find the "Principal Interest" line — look ahead up to 4 lines
        interest = 0.0
        j = i + 1
        while j < len(lines) and j <= i + 4:
            next_line = lines[j].strip()

            # Hit next repayment date — stop looking
            if _DATE_RE.match(next_line):
                break

            m = _INTEREST_RE.search(next_line)
            if m:
                interest = _clean(m.group(1))
                i = j    # advance past the interest line
                break

            j += 1

        result["schedule_lines"].append({
            "due_date":           parsed_date,
            "total_payment":      total_payment,
            "principal_amount":   principal,
            "interest_amount":    interest,
            "outstanding_amount": outstanding,
        })

        i += 1