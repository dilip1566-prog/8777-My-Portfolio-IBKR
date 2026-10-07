"""Build the US & UK portfolio dashboard from holdings.csv.

    python refresh.py                 # refresh everything in holdings.csv
    python refresh.py --only AAPL     # refresh a subset
    python refresh.py --no-open       # don't launch the browser afterwards

Reads  : holdings.csv, notes.json (optional), template.html
Writes : data.json, dashboard.html
"""

import argparse
import datetime as dt
from concurrent.futures import ThreadPoolExecutor
import json
import math
import os
import re
import sys
import webbrowser

import holdings_io
from newsrank import rank
from yahoo import Yahoo, YahooError, num, text

HERE = os.path.dirname(os.path.abspath(__file__))

US_EXCHANGES = {"NMS", "NYQ", "NGM", "NCM", "ASE", "PCX", "BTS", "NYS", "NAS", "ARCA"}
UK_EXCHANGES = {"LSE", "IOB", "AQS"}
# Over-the-counter shells share tickers with real listings, so never auto-pick one.
OTC_EXCHANGES = {"PNK", "PINK", "OTC", "OBB", "OTCM", "OTCB", "OQB", "OQX", "EXPM"}

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


# --------------------------------------------------------------------- helpers
def fmt_date(ts):
    if not ts:
        return None
    d = dt.datetime.fromtimestamp(ts, dt.timezone.utc)
    return "%d %s %d" % (d.day, MONTHS[d.month - 1], d.year)


def days_until(ts):
    if not ts:
        return None
    return round((ts - dt.datetime.now(dt.timezone.utc).timestamp()) / 86400)


def pct_change(new, old):
    if new is None or not old:
        return None
    return (new - old) / old * 100.0


def classify_market(meta, ticker):
    """Only proper US and UK listings are allowed through.

    The currency comes from the quote itself, never from the exchange: London
    lists plenty of products in dollars (WisdomTree Physical Bitcoin, for one),
    and treating those as pounds would silently add dollars to a sterling total.
    """
    exch = (meta.get("exchangeName") or "").upper()
    full = (meta.get("fullExchangeName") or "").upper()
    ccy = (meta.get("currency") or "").strip()

    if exch in OTC_EXCHANGES:
        return None, None
    if exch in UK_EXCHANGES or "LSE" in full:
        market = "UK"
    elif exch in US_EXCHANGES:
        market = "US"
    elif ticker.endswith(".L"):
        market = "UK"
    else:
        return None, None

    if ccy in ("GBp", "GBP"):
        return market, "GBP"
    if ccy == "USD":
        return market, "USD"
    return None, None


def resolve_ticker(api, ticker, currency_hint=None):
    """Find the listing the user actually means.

    A bare ticker such as SGLN hits a delisted over-the-counter shell on Yahoo
    while the real holding is SGLN.L on the London exchange, so try the London
    suffix too and prefer whichever is a genuine US or UK listing.
    """
    candidates = [ticker]
    if "." not in ticker:
        candidates.append(ticker + ".L")
    else:
        # Yahoo writes US class shares with a hyphen: BRK.B is BRK-B, BF.B is BF-B.
        # A ".L" suffix is a real London listing, so leave that alone.
        if not ticker.endswith(".L"):
            candidates.append(ticker.replace(".", "-"))

    good, rejected = [], []
    for symbol in candidates:
        try:
            chart = api.chart(symbol, rng="1y", interval="1d")
        except YahooError as exc:
            rejected.append("%s (%s)" % (symbol, exc))
            continue
        meta = chart.get("meta") or {}
        market, ccy = classify_market(meta, symbol)
        if market and meta.get("regularMarketPrice"):
            good.append((symbol, chart, meta, market, ccy))
        else:
            rejected.append("%s (%s in %s)" % (
                symbol, meta.get("fullExchangeName") or "unlisted",
                meta.get("currency") or "?"))

    if not good:
        raise YahooError("no US or UK listing found - tried " + ", ".join(rejected))

    # The currency you wrote the price in decides which listing you mean: "£26.59"
    # is the London line, "$5.42" the US one. Only when the sheet gives no marker
    # does the ticker as typed win, falling back to another spelling if it is not
    # a usable listing on its own.
    chosen = None
    if currency_hint == "GBP":
        chosen = next((g for g in good if g[3] == "UK"), None)
    elif currency_hint == "USD":
        chosen = next((g for g in good if g[3] == "US"), None)
    if chosen is None:
        chosen = next((g for g in good if g[0] == ticker), good[0])

    symbol, chart, meta, market, _ccy = chosen
    notes = []
    if symbol != ticker:
        why = ("you entered the price in pounds" if currency_hint == "GBP"
               else "%s on its own is not a usable US or UK listing" % ticker)
        notes.append("Priced as %s on %s, because %s."
                     % (symbol, meta.get("fullExchangeName") or "LSE", why))
    return symbol, chart, meta, market, notes


# ------------------------------------------------------------------- analysis
def build_analysis(h):
    """Objective, numbers-led observations. Not advice, not a recommendation."""
    sym = h["symbol"]
    pos, risk, watch, story = [], [], [], []

    def money(v):
        return "%s%s" % (sym, format(v, ",.2f")) if v is not None else "n/a"

    if h["pl_pct"] is not None:
        direction = "up" if h["pl_pct"] >= 0 else "down"
        outcome = "gain" if h["pl"] >= 0 else "loss"
        story.append("Your holding is %s %.1f%% against your purchase price of %s, "
                     "a %s %s on %s shares."
                     % (direction, abs(h["pl_pct"]), money(h["purchase_price"]),
                        money(abs(h["pl"])), outcome, format(h["quantity"], ",.0f")))
    if h["ret_1y"] is not None and h["ret_3m"] is not None:
        story.append("The share price is %+.1f%% over 12 months and %+.1f%% over "
                     "three months." % (h["ret_1y"], h["ret_3m"]))
    elif h["ret_1y"] is not None:
        story.append("The share price is %+.1f%% over 12 months." % h["ret_1y"])
    if h["w52_pos"] is not None:
        where = ("near its 12-month high" if h["w52_pos"] >= 85 else
                 "near its 12-month low" if h["w52_pos"] <= 15 else
                 "in the middle of its 12-month range")
        story.append("It is trading %s, %.0f%% of the way between %s and %s."
                     % (where, h["w52_pos"], money(h["w52_low"]), money(h["w52_high"])))
    if h["day_change_pct"] is not None:
        story.append("Latest session: %+.2f%% on the day." % h["day_change_pct"])

    if h["ma50"] and h["ma200"] and h["current_price"]:
        if h["current_price"] > h["ma50"] > h["ma200"]:
            pos.append("Price sits above both the 50-day and 200-day averages, so the "
                       "medium-term trend is upward.")
        elif h["current_price"] < h["ma50"] < h["ma200"]:
            risk.append("Price sits below both the 50-day and 200-day averages, so the "
                        "medium-term trend is downward.")
    if h["target_mean"] and h["current_price"]:
        gap = pct_change(h["target_mean"], h["current_price"])
        line = "Analyst average target is %s" % money(h["target_mean"])
        if gap is not None:
            line += ", %+.0f%% versus the current price" % gap
        if h["analyst_count"]:
            line += " (from %d analysts)." % int(h["analyst_count"])
        else:
            line += "."
        if gap is not None and gap > 5:
            pos.append(line)
        elif gap is not None and gap < -5:
            risk.append(line)
        else:
            watch.append(line)
    if h["revenue_growth"] is not None:
        if h["revenue_growth"] > 0.05:
            pos.append("Revenue is growing %.1f%% year on year."
                       % (h["revenue_growth"] * 100))
        elif h["revenue_growth"] < 0:
            risk.append("Revenue is shrinking %.1f%% year on year."
                        % abs(h["revenue_growth"] * 100))
    if h["profit_margin"] is not None and h["profit_margin"] > 0.15:
        pos.append("Net profit margin of %.1f%% leaves room to absorb cost pressure."
                   % (h["profit_margin"] * 100))
    if h["div_yield"]:
        pos.append("Pays a dividend yielding about %.2f%% at today's price."
                   % h["div_yield"])

    if h["pe_trailing"] and h["pe_trailing"] > 40:
        risk.append("A trailing P/E of %.0f prices in strong future growth, so any "
                    "disappointment tends to be punished." % h["pe_trailing"])
    if h["profit_margin"] is not None and h["profit_margin"] < 0:
        risk.append("The company is lossmaking at the net level.")
    if h["w52_pos"] is not None and h["w52_pos"] <= 15:
        risk.append("Trading close to its 12-month low, so the market is pricing in "
                    "some deterioration.")
    if h["beta"] and h["beta"] > 1.3:
        risk.append("A beta of %.2f means it typically moves more sharply than the "
                    "wider market in both directions." % h["beta"])
    if h["day_change_pct"] is not None and abs(h["day_change_pct"]) >= 4:
        risk.append("Moved %+.1f%% in the latest session, so something is being "
                    "repriced right now." % h["day_change_pct"])

    if h.get("is_fund"):
        watch.append("This is an exchange-traded fund or commodity product, not an "
                     "operating company, so there are no results to wait for. Its "
                     "price follows the assets it holds.")
        if h["ret_1y"] is not None:
            watch.append("Track the underlying asset rather than company news: the "
                         "price is %+.1f%% over 12 months." % h["ret_1y"])
        return {
            "story": story,
            "positives": pos or ["Performance comes from the assets the product "
                                 "holds, so company-level signals do not apply."],
            "risks": risk or ["Risk comes from the price of the underlying asset, "
                              "not from company results."],
            "watch": watch,
        }

    if h["earnings_next"]:
        line = "Next earnings on %s" % h["earnings_next"]
        if h["earnings_next_estimate"]:
            line += " (estimated date, not yet confirmed)"
        if h["earnings_next_days"] is not None and h["earnings_next_days"] >= 0:
            line += ", about %d days away" % h["earnings_next_days"]
        watch.append(line + ".")
    if h["eps_estimate"]:
        line = "Consensus expects earnings per share of about %.2f" % h["eps_estimate"]
        if h["revenue_estimate_fmt"]:
            line += " on revenue of %s" % h["revenue_estimate_fmt"]
        watch.append(line + ".")
    for item in h["news"][:2]:
        watch.append("Follow-through on: %s" % item["title"])
    if not watch:
        watch.append("No confirmed catalyst in the data. The next scheduled results "
                     "are the main checkpoint.")

    return {
        "story": story,
        "positives": pos or ["No standout positive signals in the current data."],
        "risks": risk or ["No standout risk signals in the current data."],
        "watch": watch,
    }


# ---------------------------------------------------------------------- build
def build_holding(api, row, notes):
    entered = row["ticker"]
    ticker, chart, meta, market, warnings = resolve_ticker(
        api, entered, row.get("currency_hint"))
    _market, ccy = classify_market(meta, ticker)

    # LSE quotes come back in pence (GBp), so normalise everything to pounds.
    scale = 0.01 if (meta.get("currency") or "") == "GBp" else 1.0
    symbol = "£" if ccy == "GBP" else "$"

    # A "p" or "GBX" suffix in the sheet says pence outright. Without one, fall
    # back to spotting a price about 100x the quote, which is the same mistake.
    if row.get("in_pence"):
        row = dict(row, purchase_price=row["purchase_price"] / 100.0)
    elif (meta.get("currency") or "") == "GBp":
        quoted = (meta.get("regularMarketPrice") or 0) * scale
        if quoted and row["purchase_price"] / quoted > 20:
            row = dict(row, purchase_price=row["purchase_price"] / 100.0)
            warnings.append(
                "Purchase price read as pence: %s in the sheet is £%.2f a share."
                % (format(row["purchase_price"] * 100, ",g"), row["purchase_price"]))

    quote = (chart.get("indicators", {}).get("quote") or [{}])[0]
    closes = [c * scale for c in (quote.get("close") or []) if c is not None]

    price = meta.get("regularMarketPrice")
    price = price * scale if price is not None else (closes[-1] if closes else None)
    if price is None:
        raise YahooError("no current price available")

    # "chartPreviousClose" is the close before the whole chart range, so on a
    # one-year chart it is a year old. The prior daily bar is the real previous
    # close, because the final bar is the session the current price belongs to.
    prev = closes[-2] if len(closes) > 1 else None
    if prev is None:
        raw_prev = meta.get("previousClose") or meta.get("regularMarketPreviousClose")
        prev = raw_prev * scale if raw_prev else None

    def back(days):
        idx = len(closes) - 1 - days
        return closes[idx] if 0 <= idx < len(closes) else None

    summary = api.summary(ticker)
    profile = summary.get("assetProfile") or {}
    detail = summary.get("summaryDetail") or {}
    fin = summary.get("financialData") or {}
    cal = (summary.get("calendarEvents") or {}).get("earnings") or {}
    stats = summary.get("defaultKeyStatistics") or {}
    price_mod = summary.get("price") or {}

    name = (price_mod.get("longName") or meta.get("longName")
            or meta.get("shortName") or ticker)

    earn_dates = [d for d in (num(x) for x in (cal.get("earningsDate") or [])) if d]
    call_dates = [d for d in (num(x) for x in (cal.get("earningsCallDate") or [])) if d]
    now_ts = dt.datetime.now(dt.timezone.utc).timestamp()
    all_dates = earn_dates + call_dates
    upcoming = sorted(d for d in all_dates if d >= now_ts - 86400)
    past = sorted(d for d in all_dates if d < now_ts - 86400)
    history = (summary.get("earningsHistory") or {}).get("history") or []
    last_quarter = num(history[-1], "quarter") if history else None

    news_items = rank(api.news(ticker, name, market), ticker, name)

    invested = row["purchase_price"] * row["quantity"]
    value = price * row["quantity"]
    w52_high = (meta.get("fiftyTwoWeekHigh") or 0) * scale or None
    w52_low = (meta.get("fiftyTwoWeekLow") or 0) * scale or None
    if closes:
        w52_high = max(w52_high or 0, max(closes)) or None
        w52_low = min(w52_low or min(closes), min(closes)) or None

    h = {
        "ref": row.get("ref"),
        "ticker": ticker,
        "entered_as": entered if entered != ticker else None,
        "name": name,
        "market": market,
        "currency": ccy,
        "symbol": symbol,
        "exchange": meta.get("fullExchangeName") or meta.get("exchangeName"),
        "sector": profile.get("sector"),
        "industry": profile.get("industry"),
        "website": profile.get("website"),
        "employees": profile.get("fullTimeEmployees"),
        "description": profile.get("longBusinessSummary"),

        "purchase_price": row["purchase_price"],
        "quantity": row["quantity"],
        "purchase_date": row["purchase_date"],
        "current_price": price,
        "previous_close": prev,
        "day_change": (price - prev) if prev else None,
        "day_change_pct": pct_change(price, prev),
        "price_time_display": fmt_date(meta.get("regularMarketTime")),
        "market_state": meta.get("marketState") or "",

        "invested": invested,
        "current_value": value,
        "pl": value - invested,
        "pl_pct": pct_change(price, row["purchase_price"]),
        "price_diff": price - row["purchase_price"],

        "w52_high": w52_high,
        "w52_low": w52_low,
        "w52_pos": (((price - w52_low) / (w52_high - w52_low) * 100)
                    if w52_high and w52_low and w52_high > w52_low else None),
        "ma50": (sum(closes[-50:]) / 50) if len(closes) >= 50 else None,
        "ma200": (sum(closes[-200:]) / 200) if len(closes) >= 200 else None,
        "ret_1w": pct_change(price, back(5)),
        "ret_1m": pct_change(price, back(21)),
        "ret_3m": pct_change(price, back(63)),
        "ret_6m": pct_change(price, back(126)),
        "ret_1y": pct_change(price, closes[0] if closes else None),
        "spark": [round(c, 4) for c in closes[-130:]],

        "pe_trailing": num(detail, "trailingPE"),
        "pe_forward": num(detail, "forwardPE"),
        "div_yield": ((num(detail, "dividendYield") or 0) * 100) or None,
        "market_cap_fmt": text(price_mod, "marketCap") or text(detail, "marketCap"),
        "beta": num(detail, "beta") or num(stats, "beta"),
        "target_mean": ((num(fin, "targetMeanPrice") or 0) * scale) or None,
        "target_high": ((num(fin, "targetHighPrice") or 0) * scale) or None,
        "target_low": ((num(fin, "targetLowPrice") or 0) * scale) or None,
        "analyst_count": num(fin, "numberOfAnalystOpinions"),
        "recommendation": (fin.get("recommendationKey") or "").replace("_", " ") or None,
        "revenue_growth": num(fin, "revenueGrowth"),
        "profit_margin": num(fin, "profitMargins"),

        "earnings_next": fmt_date(upcoming[0]) if upcoming else None,
        "earnings_next_days": days_until(upcoming[0]) if upcoming else None,
        "earnings_next_estimate": bool(cal.get("isEarningsDateEstimate")),
        "earnings_last": fmt_date(past[-1]) if past else None,
        "last_quarter_end": fmt_date(last_quarter),
        "eps_estimate": num(cal, "earningsAverage"),
        "revenue_estimate_fmt": text(cal, "revenueAverage"),

        "news": news_items,
        "notes": notes.get(ticker) or notes.get(entered),
        "warnings": warnings,
    }

    # Yahoo files some funds as equities, but a listing with no business profile
    # and no earnings calendar is a fund or commodity product, not a company.
    h["is_fund"] = bool(
        re.search(r"\b(etf|etc|fund|trust|index|ucits|physical)\b", name, re.I)
        or not (profile.get("longBusinessSummary") or profile.get("sector")
                or earn_dates or call_dates))

    # A purchase price wildly out of line with the market is usually a typo.
    if h["pl_pct"] is not None and (h["pl_pct"] < -85 or h["pl_pct"] > 400):
        h["warnings"].append(
            "Purchase price of %s%s is a long way from today's %s%s. Worth checking "
            "the sheet in case a digit or decimal point is missing."
            % (symbol, format(h["purchase_price"], ",.2f"),
               symbol, format(h["current_price"], ",.2f")))

    h["analysis"] = build_analysis(h)
    return h


def totals(holdings):
    out = {}
    for h in holdings:
        block = out.setdefault(h["currency"], {
            "symbol": h["symbol"], "invested": 0.0, "value": 0.0,
            "count": 0, "winners": 0, "losers": 0, "day_change": 0.0,
        })
        block["invested"] += h["invested"]
        block["value"] += h["current_value"]
        block["count"] += 1
        block["winners"] += 1 if h["pl"] >= 0 else 0
        block["losers"] += 1 if h["pl"] < 0 else 0
        if h["day_change"] is not None:
            block["day_change"] += h["day_change"] * h["quantity"]
    for block in out.values():
        block["pl"] = block["value"] - block["invested"]
        block["pl_pct"] = (block["pl"] / block["invested"] * 100
                           if block["invested"] else 0.0)
    return out


def load_holdings(api, args):
    """Google Sheet first, local CSV as the offline fallback."""
    config_path = os.path.join(HERE, "source.json")
    config = {}
    if os.path.exists(config_path):
        try:
            with open(config_path, encoding="utf-8") as fh:
                config = json.load(fh)
        except (OSError, ValueError):
            config = {}

    sheet = args.sheet or config.get("sheet")
    gid = args.gid or config.get("gid")
    csv_path = args.holdings

    if sheet:
        try:
            rows, problems = holdings_io.load(api._raw, sheet=sheet, gid=gid)
            print("  holdings from Google Sheet (%d rows)" % len(rows))
            with open(config_path, "w", encoding="utf-8") as fh:
                json.dump({"sheet": sheet, "gid": gid}, fh, indent=1)
            holdings_io.write_csv(rows, csv_path)   # offline copy
            return rows, problems, "Google Sheet"
        except holdings_io.HoldingsError as exc:
            print("  ! Google Sheet unavailable: %s" % exc)
            if not os.path.exists(csv_path):
                raise
            print("  falling back to the last saved holdings.csv")

    rows, problems = holdings_io.load(api._raw, path=csv_path)
    return rows, problems, os.path.basename(csv_path)


def main():
    ap = argparse.ArgumentParser(description="Refresh the US & UK portfolio dashboard.")
    ap.add_argument("--sheet", help="Google Sheets link or file id to read holdings from")
    ap.add_argument("--gid", help="tab id within the sheet (default: the first tab)")
    ap.add_argument("--holdings", default=os.path.join(HERE, "holdings.csv"))
    ap.add_argument("--only", help="comma-separated tickers to include")
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()

    api = Yahoo()
    try:
        rows, problems, source_name = load_holdings(api, args)
    except holdings_io.HoldingsError as exc:
        print("Could not read your holdings: %s" % exc)
        return 1

    if args.only:
        wanted = {t.strip().upper() for t in args.only.split(",")}
        rows = [r for r in rows if r["ticker"] in wanted]
    if not rows:
        print("No usable holdings found in %s." % source_name)
        for p in problems:
            print("  ! %s: %s" % (p["ticker"], p["reason"]))
        return 1

    notes = {}
    notes_path = os.path.join(HERE, "notes.json")
    if os.path.exists(notes_path):
        try:
            with open(notes_path, encoding="utf-8") as fh:
                notes = json.load(fh)
        except (OSError, ValueError) as exc:
            print("  ! notes.json ignored (%s)" % exc)

    holdings, rejected = [], list(problems)

    def fetch(row):
        try:
            return row, build_holding(api, row, notes), None
        except Exception as exc:                # noqa: BLE001 - one bad ticker
            return row, None, exc

    # Each holding needs four round trips, so a large sheet is painfully slow
    # one at a time. Warm the session crumb first so the workers do not all
    # race to fetch it, then fan out.
    try:
        api.crumb()
    except YahooError:
        pass

    print("  pricing %d holdings" % len(rows), flush=True)
    done = 0
    with ThreadPoolExecutor(max_workers=6) as pool:
        for row, holding, exc in pool.map(fetch, rows):
            done += 1
            if holding is not None:
                holdings.append(holding)
                print("  [%2d/%2d] %-10s ok" % (done, len(rows), row["ticker"]), flush=True)
            else:
                reason = str(exc) if isinstance(exc, YahooError) else repr(exc)
                rejected.append({"ticker": row["ticker"], "reason": reason})
                print("  [%2d/%2d] %-10s skipped - %s"
                      % (done, len(rows), row["ticker"], reason), flush=True)

    holdings.sort(key=lambda h: (h["market"], -h["current_value"]))
    now = dt.datetime.now()
    data = {
        "generated_iso": now.isoformat(timespec="seconds"),
        "generated_display": now.strftime("%d %b %Y at %H:%M"),
        "source": "Yahoo Finance",
        "holdings_source": source_name,
        "holdings": holdings,
        "totals": totals(holdings),
        "rejected": rejected,
    }

    with open(os.path.join(HERE, "data.json"), "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, ensure_ascii=False)

    with open(os.path.join(HERE, "template.html"), encoding="utf-8") as fh:
        template = fh.read()
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    page = template.replace('"__PORTFOLIO_DATA__"', payload)

    # The fragment (no <html>/<head>/<body>) is what claude.ai Artifacts expect;
    # the wrapped copy is the one that opens straight from Explorer.
    head, _, body = page.partition("<!--BODY-->")
    for name in ("dashboard_artifact.html", "dashboard_74_holdings.html"):
        with open(os.path.join(HERE, name), "w", encoding="utf-8") as fh:
            fh.write(page)

    out_path = os.path.join(HERE, "dashboard.html")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
                 '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
                 + head + "</head>\n<body>\n" + body + "\n</body>\n</html>\n")

    print("\n%d holding(s) priced. Dashboard written to dashboard.html" % len(holdings))
    for r in rejected:
        print("  ! %s: %s" % (r["ticker"], r["reason"]))
    if not args.no_open:
        webbrowser.open("file:///" + out_path.replace("\\", "/").replace(" ", "%20"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
