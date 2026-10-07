"""Load holdings from a Google Sheet or a local CSV.

The sheet only has to carry three things - a ticker, what you paid and how many
shares - under any of the usual column names.
"""

import csv
import io
import os
import re
import urllib.parse

# Column names people actually use, mapped to what the dashboard needs.
ALIASES = {
    "ticker": ["ticker", "ticket symbol", "ticker symbol", "symbol", "stock",
               "stock ticker", "ticket", "epic", "code"],
    "purchase_price": ["purchase price", "buy price", "price paid", "cost",
                       "cost price", "average price", "avg price", "entry price",
                       "purchase", "price"],
    "quantity": ["quantity", "qty", "shares", "no of shares", "number of shares",
                 "units", "holding", "share qty"],
    "purchase_date": ["purchase date", "date", "buy date", "date bought",
                      "entry date"],
    "ref": ["our ref", "ref", "reference", "account", "broker", "platform",
            "our reference", "held with"],
}

SHEET_ID = re.compile(r"/spreadsheets/d/([a-zA-Z0-9_-]{20,})")
SHEET_GID = re.compile(r"[#&?]gid=(\d+)")


class HoldingsError(RuntimeError):
    pass


def parse_number(raw):
    """Handle $153.61, £0.70, 1,000, and the European style 50,00."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    text = re.sub(r"[^\d.,\-]", "", text)
    if not text or text in ("-", ".", ","):
        return None

    has_dot, has_comma = "." in text, "," in text
    if has_dot and has_comma:
        # Whichever separator comes last is the decimal point.
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif has_comma:
        # "50,00" is a decimal comma; "1,000" and "1,234,567" are thousands.
        if re.fullmatch(r"-?\d+,\d{1,2}", text):
            text = text.replace(",", ".")
        else:
            text = text.replace(",", "")
    try:
        return float(text)
    except ValueError:
        return None


def _header_map(fieldnames):
    """Match the sheet's column names to the fields we need."""
    found = {}
    for raw in fieldnames or []:
        key = re.sub(r"[^a-z0-9 ]", " ", (raw or "").strip().lower())
        key = " ".join(key.split())
        if not key:
            continue
        for field, names in ALIASES.items():
            if field in found:
                continue
            if key in names:
                found[field] = raw
                break
    return found


def parse_rows(text, origin="holdings"):
    """Turn CSV text into holding rows, reporting anything unusable."""
    text = text.lstrip("﻿")
    lines = [ln for ln in text.splitlines() if ln.strip().strip(",")]
    if not lines:
        raise HoldingsError("%s is empty" % origin)

    # Sheets often carry a title row or blank columns above the real header.
    start = 0
    for i, line in enumerate(lines[:15]):
        probe = next(csv.reader([line]))
        if _header_map(probe).get("ticker"):
            start = i
            break
    else:
        raise HoldingsError(
            "%s has no ticker column. Name one column 'Ticker' (or 'Ticket Symbol', "
            "'Symbol') and add 'Purchase Price' and 'Quantity' beside it." % origin)

    reader = csv.DictReader(io.StringIO("\n".join(lines[start:])))
    cols = _header_map(reader.fieldnames)
    missing = [f for f in ("ticker", "purchase_price", "quantity") if f not in cols]
    if missing:
        raise HoldingsError(
            "%s is missing a column for: %s" % (origin, ", ".join(missing)))

    rows, problems = [], []
    for lineno, raw in enumerate(reader, start=start + 2):
        ticker = (raw.get(cols["ticker"]) or "").strip().upper()
        ticker = re.sub(r"\s+", "", ticker)
        if not ticker or ticker.startswith("#") or ticker in ("TOTAL", "TOTALS"):
            continue
        raw_price = raw.get(cols["purchase_price"]) or ""
        price = parse_number(raw_price)
        qty = parse_number(raw.get(cols["quantity"]))
        if price is None or qty is None:
            problems.append({"ticker": ticker, "reason":
                             "row %d: purchase price or quantity could not be read"
                             % lineno})
            continue
        if price <= 0 or qty <= 0:
            problems.append({"ticker": ticker, "reason":
                             "row %d: purchase price and quantity must both be above "
                             "zero" % lineno})
            continue
        date = None
        if "purchase_date" in cols:
            date = (raw.get(cols["purchase_date"]) or "").strip() or None

        # How you wrote the price tells us which listing you mean and in what
        # unit: "£26.59" is a London holding, "6,049.38p" is pence, "$5.42" is
        # the US listing. Far better evidence than guessing from the number.
        pence = bool(re.search(r"\d\s*p\b|\bpence\b|\bGBX\b", raw_price, re.I))
        if "£" in raw_price or pence or re.search(r"\bGBP\b", raw_price, re.I):
            hint = "GBP"
        elif "$" in raw_price or re.search(r"\bUSD\b", raw_price, re.I):
            hint = "USD"
        else:
            hint = None

        # The offline copy stores the hints as their own columns, because
        # writing bare numbers would throw away the £/$/p markers.
        for key, value in raw.items():
            name = (key or "").strip().lower()
            if name == "currency_hint" and (value or "").strip():
                hint = value.strip().upper()
            elif name == "in_pence" and (value or "").strip():
                pence = True

        ref = (raw.get(cols["ref"]) or "").strip() if "ref" in cols else ""
        rows.append({"ticker": ticker, "purchase_price": price, "quantity": qty,
                     "purchase_date": date, "currency_hint": hint,
                     "in_pence": pence, "ref": ref or (len(rows) + 1)})
    return rows, problems


# ------------------------------------------------------------- google sheets
def sheet_csv_url(reference, gid=None):
    """Accept a full Sheets URL or a bare file id and return its CSV export URL."""
    reference = (reference or "").strip()
    match = SHEET_ID.search(reference)
    file_id = match.group(1) if match else reference
    if not re.fullmatch(r"[a-zA-Z0-9_-]{20,}", file_id):
        raise HoldingsError("that does not look like a Google Sheets link or file id")
    if gid is None:
        gid_match = SHEET_GID.search(reference)
        gid = gid_match.group(1) if gid_match else None
    # Without a gid Google exports the first tab, which is what we want by
    # default - guessing "gid=0" fails on sheets whose first tab has another id.
    url = "https://docs.google.com/spreadsheets/d/%s/export?format=csv" % file_id
    return url + ("&gid=%s" % gid if gid else "")


def fetch_sheet(http, reference, gid=None):
    """Download the sheet as CSV. Needs link sharing to be switched on."""
    url = sheet_csv_url(reference, gid)
    body = http(url)
    if body.lstrip()[:9].lower().startswith("<!doctype") or "<html" in body[:400].lower():
        raise HoldingsError(
            "Google would not hand over that sheet. Open it, click Share, and set "
            "'General access' to 'Anyone with the link - Viewer', then run this "
            "again. (Nothing else about the sheet changes, and it stays out of "
            "search results.)")
    return body


def load(http, sheet=None, path=None, gid=None):
    """Prefer the live sheet; fall back to the local CSV."""
    if sheet:
        return parse_rows(fetch_sheet(http, sheet, gid), "the Google Sheet")
    if not path or not os.path.exists(path):
        raise HoldingsError("no holdings source found - pass --sheet <link> or create "
                            "holdings.csv")
    with open(path, encoding="utf-8-sig") as fh:
        return parse_rows(fh.read(), os.path.basename(path))


def write_csv(rows, path):
    """Keep a local copy so the dashboard still builds if the sheet is offline."""
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["our ref", "ticker", "purchase_price", "quantity",
                         "purchase_date", "currency_hint", "in_pence"])
        for row in rows:
            writer.writerow([row.get("ref") or "", row["ticker"], row["purchase_price"],
                             row["quantity"], row["purchase_date"] or "",
                             row.get("currency_hint") or "",
                             "yes" if row.get("in_pence") else ""])
