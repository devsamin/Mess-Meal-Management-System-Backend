"""CSV and Excel exports of a month report (always built from the report dict)."""

import csv
import io
from decimal import Decimal

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

BALANCE_HEADERS = [
    ("name", "Member"),
    ("meals", "Meals"),
    ("opening_balance", "Opening balance"),
    ("net_deposits", "Deposits (net)"),
    ("personal_paid", "Paid personally"),
    ("total_credit", "Total credit"),
    ("meal_cost", "Meal cost"),
    ("shared_cost", "Shared cost"),
    ("bill_charges", "Extra bills"),
    ("total_charges", "Total charges"),
    ("balance", "Balance"),
]

SUMMARY_ROWS = [
    ("total_meals", "Total meals"),
    ("meal_pool", "Meal expenses (in meal rate)"),
    ("meal_rate", "Meal rate"),
    ("shared_expenses", "Shared expenses (outside meal rate)"),
    ("total_extra_bills", "Extra bills"),
    ("total_spending", "Total spending"),
    ("net_deposits", "Deposits (net of refunds)"),
    ("personal_contributions", "Paid personally by members"),
    ("opening_balance_total", "Opening balances"),
    ("cash_in_hand", "Cash in hand"),
]


def _num(value):
    try:
        return Decimal(value)
    except Exception:
        return value


def report_csv(report: dict) -> bytes:
    s = report["summary"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([f"Monthly report — {s['label']}", f"Status: {s['status']}"])
    w.writerow([])
    for key, label in SUMMARY_ROWS:
        w.writerow([label, s[key]])
    w.writerow([])
    w.writerow([label for _, label in BALANCE_HEADERS] + ["Status"])
    for m in s["members"]:
        w.writerow([m[key] for key, _ in BALANCE_HEADERS] + [m["status"]])
    w.writerow([])
    w.writerow(["Expenses"])
    w.writerow(["Date", "Category", "Description", "Purchaser", "Paid from", "In meal rate", "Amount"])
    for e in report["expenses"]:
        w.writerow([e["date"], e["category"], e["description"], e["purchaser"] or "", e["payment_source"],
                    "yes" if e["in_meal_rate"] else "no", e["amount"]])
    w.writerow([])
    w.writerow(["Deposits"])
    w.writerow(["Date", "Member", "Type", "Method", "Note", "Amount"])
    for d in report["deposits"]:
        w.writerow([d["date"], d["member"], d["kind"], d["method"], d["note"], d["amount"]])
    # UTF-8 BOM so Excel shows Bangla text correctly
    return ("﻿" + buf.getvalue()).encode("utf-8")


def _sheet(wb, title, headers, rows, money_cols=()):
    ws = wb.create_sheet(title)
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="0F766E")
        cell.alignment = Alignment(horizontal="center")
    for row in rows:
        ws.append(row)
    for idx in money_cols:
        for (cell,) in ws.iter_rows(min_row=2, min_col=idx, max_col=idx):
            cell.number_format = "#,##0.00"
    for i, _ in enumerate(headers, start=1):
        width = max(len(str(c.value or "")) for c in ws[get_column_letter(i)])
        ws.column_dimensions[get_column_letter(i)].width = min(max(width + 2, 8), 40)
    ws.freeze_panes = "A2"
    return ws


def report_xlsx(report: dict) -> bytes:
    s = report["summary"]
    wb = Workbook()
    wb.remove(wb.active)

    _sheet(wb, "Summary", ["Item", "Value"],
           [["Month", s["label"]], ["Status", s["status"]]] + [[label, _num(s[key])] for key, label in SUMMARY_ROWS],
           money_cols=(2,))
    _sheet(wb, "Balances", [label for _, label in BALANCE_HEADERS] + ["Status"],
           [[m["name"]] + [_num(m[k]) for k, _ in BALANCE_HEADERS[1:]] + [m["status"]] for m in s["members"]],
           money_cols=range(3, 12))

    grid = report["meal_grid"]
    _sheet(wb, "Meals", ["Member"] + [d[-2:] for d in grid["days"]] + ["Total"],
           [[r["name"]] + [_num(r["days"][d]) for d in grid["days"]] + [_num(r["total"])] for r in grid["rows"]]
           + [["Total"] + [_num(grid["day_totals"][d]) for d in grid["days"]] + [_num(s["total_meals"])]])

    _sheet(wb, "Expenses", ["Date", "Category", "Description", "Purchaser", "Paid from", "In meal rate", "Amount"],
           [[e["date"], e["category"], e["description"], e["purchaser"] or "", e["payment_source"],
             "yes" if e["in_meal_rate"] else "no", _num(e["amount"])] for e in report["expenses"]],
           money_cols=(7,))
    _sheet(wb, "Deposits", ["Date", "Member", "Type", "Method", "Note", "Amount"],
           [[d["date"], d["member"], d["kind"], d["method"], d["note"], _num(d["amount"])]
            for d in report["deposits"]],
           money_cols=(6,))
    _sheet(wb, "Extra bills", ["Date", "Bill ID", "Title", "Paid by", "In meal rate", "Amount", "Shares"],
           [[b["date"], b["bill_ref"], b["title"], b["paid_by"] or "Fund", "yes" if b["in_meal_rate"] else "no",
             _num(b["amount"]), ", ".join(f"{x['member']}: {x['amount']}" for x in b["shares"])]
            for b in report["extra_bills"]],
           money_cols=(6,))

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
