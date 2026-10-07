# Holdings Ledger — US & UK stock portfolio dashboard

A personal portfolio monitor for **US and UK listed shares only**. You enter the ticker,
the price you paid and the quantity; the dashboard fetches everything else from Yahoo
Finance and works out where you stand.

## Setting it up

Needs Python 3.8 or newer. There is nothing to install — it uses only the standard library.

```bash
git clone https://github.com/dilip1566-prog/8777-My-Portfolio-IBKR.git
cd 8777-My-Portfolio-IBKR
cp holdings.example.csv holdings.csv     # then edit holdings.csv with your own positions
python refresh.py
```

To read from a Google Sheet instead of the local file, set the sheet to
**Share → Anyone with the link → Viewer** and run it once with the link:

```bash
python refresh.py --sheet https://docs.google.com/spreadsheets/d/<your-sheet-id>/edit
```

The link is remembered in `source.json` afterwards, so later runs are just `python refresh.py`.

**Your holdings are never committed.** `holdings.csv`, `source.json`, `data.json` and the
generated `dashboard*.html` files are all in `.gitignore`, because they contain your actual
positions, purchase prices and the link to your sheet.

## Using it

Your holdings live in a Google Sheet. Keep editing that sheet as normal — the dashboard
reads it live on every build.

Link sharing is switched on and the sheet link is saved in `source.json`. **Every build
reads the sheet live** — nothing is ever cached from a previous run.

### The easy way: leave the server running

Double-click **Start Dashboard.bat** (or run `python serve.py`). It opens the dashboard and
keeps serving it at http://localhost:8777. From then on:

* Opening or reloading the page re-reads the sheet and re-fetches prices whenever the data
  is more than 10 minutes old.
* The **Refresh** button beside the snapshot time forces an immediate re-read, whatever the
  age. Edit your sheet, click Refresh, and the new holdings appear.

```bash
python serve.py --max-age 0     # re-read on every single page load
python serve.py --port 8778     # if 8777 is taken
```

### The one-shot way

```bash
python refresh.py
```

Or double-click **Refresh Dashboard.bat**. It reads the sheet, prints one line per ticker,
writes `dashboard.html` and opens it in your browser. A copy of the sheet is saved to
`holdings.csv` each time, so the dashboard still builds if Google is unreachable.

If the sheet ever becomes unreachable the script falls back to `holdings.csv` and says so.
To point at a different sheet, run `python refresh.py --sheet <link>` once; the new link is
remembered.

### The columns

The sheet needs a ticker, a purchase price and a quantity. Column names are matched
loosely, so `TICKET SYMBOL`, `Ticker`, `Symbol` and `Stock` all work, as do `Purchase
Price` / `Buy Price` / `Cost`, and `Quantity` / `Qty` / `Shares`. An optional `Date`
column is picked up if present. `$153.61`, `£0.70`, `1,000` and `50,00` all parse.

**Filtering.** The search box matches on reference, ticker, company name and sector. When
your sheet uses more than one **Our ref** value (say `IBKR` and `II`), a chip appears for
each one to the right of the market filters. Reference is its own filter dimension, so you
can combine it with US / UK / In profit / At a loss — for example `II` plus `In profit`.

Click any row in the dashboard to open the research panel for that holding. It slides in
from the right; close it with the × button, the Escape key, or by clicking outside it.

**Theme.** The Auto / Light / Dark switch sits top right. Auto follows your operating
system; Light and Dark override it, and your choice is remembered between refreshes.

## Tickers

| Market | Format | Examples |
| --- | --- | --- |
| US | plain ticker | `AAPL`, `NVDA`, `MSFT`, `JPM` |
| UK | ticker + `.L` | `LLOY.L`, `BP.L`, `VOD.L`, `HSBA.L`, `SHEL.L` |

Anything that is not a US or UK listing is rejected with a note on the dashboard rather
than being priced. Nothing is converted between currencies: US holdings are totalled in
dollars, UK holdings in pounds, and the two totals are shown side by side.

**Bare tickers that are really UK listings are handled.** `SGLN` on its own points at a
delisted US shell company on Yahoo, while your actual holding is `SGLN.L`, iShares
Physical Gold on the London exchange. The script tries the `.L` version too, ignores
over-the-counter shells, and uses your purchase price to pick the right one. When it
substitutes a ticker it says so on the dashboard. Writing `SGLN.L` in the sheet removes
the guesswork.

**How you write the price picks the listing and the unit.** This matters because tickers
collide across exchanges — `AETH` is a US Bitwise fund *and* a London 21Shares ETP.

| You write | It means |
| --- | --- |
| `$153.61` | the US listing, price in dollars |
| `£26.59` | the London listing, price in pounds |
| `6,049.38p` | pence — converted to £60.49 automatically |
| `60.49` (no marker) | the ticker exactly as typed |

So `AETH` at `£26.59` resolves to `AETH.L` on the London exchange, while `BLND` at `$5.42`
resolves to Blend Labs on the NYSE rather than British Land. Writing the explicit ticker
(`AETH.L`) works too and is never overridden.

**Obvious typos get flagged, not fixed.** A purchase price more than 85% below or 400%
above today's price raises a banner asking you to check the sheet, because a missing digit
looks identical to a spectacular trade.

## What each holding shows

The table columns are: **Our ref** (from your sheet's own column — the broker or account,
e.g. `IBKR`; falls back to a row number if the sheet has no such column), **Ticker**, **Purchase**
price, **No of shares**, **Value** (what you put in — purchase price × shares), **Current
price**, **24 hrs %**, **Weekly %**, **Monthly %**, **Profit / loss** in cash and percent,
**Earnings date**, and **Signals** (the strongest news category found).

Click through for: what the company does, the earnings calendar (with an **Estimated**
flag when the date is only an analyst forecast), 52-week range position, moving averages,
1/3/6/12-month returns, P/E, dividend yield, beta, analyst target and consensus, the
important news with a line on why each item matters, and an objective read of what is
happening, what supports the price, what works against it and what to watch next.

## Files

| File | What it is |
| --- | --- |
| Google Sheet | **Your input.** Edit your holdings there. |
| `holdings.example.csv` | Template showing the accepted column names and price formats. |
| `serve.py` | Runs the local dashboard and re-reads the sheet as you use it. |
| `refresh.py` | Fetches, calculates and renders. A single build. |
| `holdings_io.py` | Reads the Google Sheet or a CSV and makes sense of the columns. |
| `yahoo.py` | Yahoo Finance access (prices, fundamentals, earnings, news feeds). |
| `newsrank.py` | Scores headlines by likely price impact and filters out chatter. |
| `template.html` | The dashboard design. Edit to restyle. |
| `notes.json` | Optional. Your own written note per ticker, shown in the analysis panel. |
| `dashboard.html` | **Generated.** The dashboard — open this. |
| `dashboard_artifact.html` | **Generated.** Same page, ready to publish as a Claude Artifact. |
| `data.json` | **Generated.** The raw snapshot, if you want the numbers elsewhere. |
| `holdings.csv` | **Generated.** Offline copy of the sheet, and the manual-entry fallback. |
| `source.json` | **Generated.** Remembers which sheet to read. |

### Adding your own notes

Create `notes.json` next to the script:

```json
{
  "AAPL": "Holding through the next two quarters to see whether services margin holds.",
  "LLOY.L": "Watching the motor finance provision and the buyback pace."
}
```

Those appear under **What is happening** for that ticker.

## Options

```bash
python refresh.py --sheet <link>       # point at a different Google Sheet
python refresh.py --gid 1234567        # read a specific tab of that sheet
python refresh.py --only MRK,SGLN      # refresh just these
python refresh.py --no-open            # write the file, don't open the browser
python refresh.py --holdings other.csv # ignore the sheet, use a local CSV
```

## Reading a wide table

The ticker column stays pinned to the left edge and the header stays pinned to the top, so
you never lose track of which row and which column you are looking at when the table
scrolls. The table has its own scrollbars — the page itself never scrolls sideways.

## Notes on the data

* Prices are the last available regular-session price from Yahoo Finance, not live ticks.
  The snapshot time is printed at the top of the dashboard — re-run the script for fresher
  numbers.
* Upcoming earnings dates are frequently analyst estimates. Those are labelled
  **Estimated** and they do move.
* News is ranked by likely impact and recency, not by however many headlines exist.
  Listicles, price predictions and general market wraps are filtered out on purpose, so a
  genuinely quiet week shows few items rather than padding.
* Everything here is data and arithmetic on your own holdings. It is not investment advice
  and makes no recommendation to buy or sell.
