"""Rank headlines by likely share-price impact rather than by recency alone.

The aim is a short list of things that actually happened to the company, not a
feed of market chatter, so a headline has to name the company AND describe a
real event before it can score at all.
"""

import re
import time

# Each category carries a weight and the reason it can move a share price.
CATEGORIES = [
    ("Earnings", 10,
     r"\b(earnings|q[1-4] (?:results|report|earnings)|quarterly results|"
     r"full[- ]year results|half[- ]year results|interim results|trading (?:update|statement)|"
     r"profit warning|beats? (?:estimates|expectations|forecasts)|"
     r"miss(?:es|ed)? (?:estimates|expectations|forecasts)|reports? (?:profit|loss|revenue))\b",
     "Reported numbers reset expectations and usually move the price hardest."),

    ("Guidance", 9,
     r"\b(guidance|outlook (?:cut|raised|lowered|for)|raises? (?:its )?(?:guidance|outlook|forecast)|"
     r"cuts? (?:its )?(?:guidance|outlook|forecast)|warns? (?:on|of|over)|"
     r"lowers? (?:its )?(?:targets?|expectations)|guided to|guides? (?:to|for)|"
     r"pencill?ed in)\b",
     "Forward guidance drives estimates for every future quarter."),

    ("M&A", 9,
     r"\b(acquires?|acquiring|acquisition|to acquire|merger|merges with|takeover|buyout|"
     r"bid for|agree[sd]? to buy|stake in|divests?|divestment|spin[- ]?off|"
     r"sells? (?:its |the )?(?:unit|division|business|stake|arm))\b",
     "Deals change the size, shape and ownership of the business."),

    ("Regulatory", 9,
     r"\b(antitrust|regulators?|regulatory (?:approval|probe|review)|investigation into|"
     r"probe|fined|fines? of|sec charges|fca |cma |doj |ftc |fda (?:approval|clearance|rejects)|"
     r"sanctions?|tariffs?|export controls?|banned|approval for)\b",
     "Regulatory outcomes can force changes to revenue or add direct costs."),

    ("Legal", 8,
     r"\b(lawsuit|sues?|sued|settlement|settles?|court rules?|litigation|verdict|"
     r"patent (?:case|suit|dispute)|class action)\b",
     "Legal outcomes create direct financial exposure."),

    ("Financing", 8,
     r"\b(refinanc\w+|bond sale|rights issue|share placing|capital raise|equity raise|"
     r"debt (?:sale|raise|refinancing|burden)|bankrupt\w*|chapter 11|defaults? on|"
     r"credit rating|rating (?:cut|raised|downgrade))\b",
     "Balance-sheet changes affect risk and can dilute existing shareholders."),

    ("Analyst action", 7,
     r"\b(upgrade[sd]?|downgrade[sd]?|initiates? coverage|raises? price target|"
     r"cuts? price target|price target (?:to|raised|cut)|"
     r"to (?:overweight|underweight|outperform|underperform)|double[- ]upgrade)\b",
     "Broker rating and target changes shift sentiment and fund flows."),

    ("Contract win", 7,
     r"\b(wins? (?:a |the )?(?:contract|order|deal|tender|mandate|bid)|"
     r"awarded (?:a |the )?contract|contract worth|order book|"
     r"signs? (?:a |the )?(?:deal|agreement|contract|mou)|partnership with|"
     r"supply (?:deal|agreement)|loses? (?:a |the )?(?:contract|customer))\b",
     "New business feeds directly into future revenue."),

    ("Leadership", 7,
     r"\b(new (?:ceo|cfo|chairman|chief executive)|chief (?:executive|financial|risk|"
     r"operating) officer|resigns?|resignation|steps? down|exits?|ousted|"
     r"appoints?|names? (?:new )?(?:ceo|cfo|chair)|board (?:shake|overhaul))\b",
     "Senior changes affect strategy and how the market prices execution risk."),

    ("Shareholder returns", 7,
     r"\b(dividend (?:cut|raised|lifted|increase|hike|suspend|declare)|lifts? (?:its )?dividend|"
     r"raises? (?:its )?dividend|buyback|share repurchase|special dividend|"
     r"returns? cash to shareholders)\b",
     "Changes to cash returned to shareholders re-rate income expectations."),

    ("Product", 6,
     r"\b(launch(?:es|ed|ing)|unveil(?:s|ed|ing)|announces? (?:new|next[- ]gen)|"
     r"new (?:chip|gpu|model|product|drug|platform|service|generation)|"
     r"begins? (?:shipping|production)|rolls? out)\b",
     "New products decide whether future revenue targets are reachable."),

    ("Restructuring", 6,
     r"\b(layoffs?|job cuts|cutting \S+ jobs|restructur\w+|cost[- ](?:cutting|cuts?|savings)|"
     r"closes? (?:its )?(?:plant|site|stores?|factory)|scraps? (?:the )?\w+ brand|"
     r"exits? (?:the )?\w+ (?:market|business))\b",
     "Cost actions change the margin outlook."),

    ("Ownership", 6,
     r"\b(insider (?:buying|selling)|director (?:buys|sells)|activist (?:investor|stake)|"
     r"builds? (?:a )?stake|13d|13g|short seller|berkshire|takes? a position in)\b",
     "Insider and activist activity signals conviction about the shares."),

    ("Share price move", 6,
     r"(jumps?|surges?|soars?|rallies|slides?|slumps?|plunges?|tumbles?|sinks?|drops?)[^.]{0,28}\d+(?:\.\d+)?%|(?:hits?|falls? to) (?:a )?(?:record|all[- ]time|52[- ]week)",
     "A large single-session move means the market repriced something material."),

    ("Demand signal", 5,
     r"\b(record (?:revenue|demand|orders|sales)|demand (?:surge|slump|weak|strong)|"
     r"capex (?:increase|cut)|data cent(?:re|er) (?:spend|build)|"
     r"sales (?:jump|fall|slump|surge)|market share)\b",
     "A direct read on the demand that drives the company's revenue."),
]

# Headlines that are commentary, listicles or price speculation, not events.
NOISE = re.compile(
    r"(stocks? to (?:watch|buy|own)|things to know|dow jones futures|market wrap|"
    r"here(?:'s| is) (?:why|what|how much)|\b\d+ (?:reasons|stocks|shares|things|charts|etfs)\b|"
    r"zacks|motley fool|magpie|should you (?:buy|sell|own)|is it too late|"
    r"jim cramer|premarket (?:movers|stocks)|today'?s (?:top|best)|"
    r"moving average|technical (?:analysis|levels|outlook)|options activity|"
    r"trending tickers|worth watching|best (?:stocks|shares|dividend)|"
    r"[£$€][\d,.]+k? invested|invested .{0,25}(?:ago|now worth)|"
    r"passive income|penny stocks?|cheap shares|my top \d|"
    r"(?:share price|stock) (?:forecast|prediction)|"
    r"what (?:i|analysts|to) (?:think|expect|know)|"
    r"looks (?:cheap|expensive|fully valued)|could (?:be|make|turn|reach)|"
    r"adr|american depositary|edge (?:lower|higher) in|"
    r"variable rate fix|net asset value|holding\(s\) in company|"
    r"total voting rights|transaction in own shares|director/pdmr|"
    r"if you invested|index futures|prediction:|is it time to (?:sell|buy)|"
    r"where next for|asked chatgpt|fairly valued|will (?:hit|reach|crash)|"
    r"reasons why|my case for|turn [£$€][\d,]+ into|a buy,? a sell|"
    r"equal[- ]weighted|\betfs?\b|fund pays|hidden (?:debt|cost)|memorabilia)",
    re.I,
)

# Fallback tier: company is named and the headline is at least about the business.
BUSINESS = re.compile(
    r"\b(stock|shares?|revenue|sales|profit|earnings|guidance|price target|analysts?|"
    r"investors?|chips?|gpu|ai|deal|market cap|demand|orders|growth|forecast|quarter|"
    r"outlook|iphone|customers?|rival|competition|spending|capex|valuation|dividend|"
    r"margins?|jobs|regulator|contract|results)\b",
    re.I,
)

# "Will X hit £2?", "Can Y keep rising?" - speculation dressed as news.
OPINION = re.compile(r"^\s*(will|can|could|should|is|are|why|how|what|do|does|has)\b.*\?\s*$", re.I)

STOP_WORDS = {
    "plc", "inc", "corp", "corporation", "company", "co", "ltd", "limited", "group",
    "holdings", "holding", "the", "and", "international", "industries", "technologies",
    "systems", "solutions", "services", "enterprises", "banking", "bank", "class",
}

WEEK = 7 * 86400


def identity_tokens(ticker, company):
    """Words that prove a headline is about this company, not the wider market."""
    tokens = {ticker.lower(), ticker.split(".")[0].lower()}
    for word in re.split(r"[^A-Za-z0-9]+", company or ""):
        low = word.lower()
        if len(low) >= 4 and low not in STOP_WORDS:
            tokens.add(low)
    return {t for t in tokens if len(t) >= 3}


# Outlets whose company reporting is worth surfacing over SEO aggregators.
REPUTABLE = re.compile(
    r"(reuters|bloomberg|financial times|\bft\b|wall street journal|\bwsj\b|barron|"
    r"cnbc|marketwatch|yahoo|associated press|\bap\b|guardian|telegraph|the times|"
    r"sky news|\bbbc\b|city ?a\.?m|proactive|investing\.com|investor'?s business|"
    r"thestreet|benzinga|seeking alpha|morningstar|simply ?wall|tradingview|"
    r"interactive investor|hargreaves|sharecast|alliance news|pa media|forbes|"
    r"business insider|the register|techcrunch|nikkei|dow jones)", re.I)

# A headline naming an analyst target is analyst action first, whatever else it says.
TARGET_OVERRIDE = re.compile(r"price target|rating|upgrade|downgrade", re.I)


def classify(title):
    """Return (category, weight, why) for the strongest match, or None."""
    best = None
    padded = " " + title.lower() + " "
    for name, weight, pattern, why in CATEGORIES:
        if re.search(pattern, padded, re.I):
            if best is None or weight > best[1]:
                best = (name, weight, why)
    if best and TARGET_OVERRIDE.search(title):
        for name, weight, _pattern, why in CATEGORIES:
            if name == "Analyst action":
                return (name, max(weight, best[1] - 2), why)
    return best


def recency_bonus(stamp, now):
    if not stamp:
        return -2
    age = now - stamp
    if age <= 2 * 86400:
        return 6
    if age <= WEEK:
        return 4
    if age <= 4 * WEEK:
        return 2
    return 0


def rank(items, ticker, company, limit=5, max_age_days=120):
    """Return up to `limit` headlines, event-driven ones first."""
    now = time.time()
    tokens = identity_tokens(ticker, company)
    events, mentions, seen = [], [], set()

    for item in items:
        title = item["title"]
        key = re.sub(r"[^a-z0-9]+", "", title.lower())[:60]
        if key in seen:
            continue
        seen.add(key)

        stamp = item.get("timestamp") or 0
        if stamp and now - stamp > max_age_days * 86400:
            continue

        # The headline has to name the company, not just the wider market.
        words = set(re.split(r"[^a-z0-9]+", title.lower()))
        if not (tokens & words):
            continue

        publisher = item.get("publisher") or ""
        noisy = (bool(NOISE.search(title)) or bool(OPINION.match(title))
                 or bool(NOISE.search(publisher)))
        quality = 2 if REPUTABLE.search(publisher) else -2
        row = {
            "title": title,
            "publisher": item["publisher"],
            "url": item["url"],
            "timestamp": stamp,
        }

        hit = classify(title)
        if hit:
            category, score, why = hit
            score += recency_bonus(stamp, now) + quality
            if noisy:
                score -= 12
            if item.get("source") in ("yahoo-rss", "google"):
                score += 2
            if score >= 8:
                row.update(category=category, why=why, score=score)
                events.append(row)
        elif not noisy and BUSINESS.search(title):
            row.update(
                category="Company mention",
                why="Names the company but reports no confirmed corporate event.",
                score=recency_bonus(stamp, now) + quality,
            )
            mentions.append(row)

    events.sort(key=lambda r: (-r["score"], -r["timestamp"]))
    mentions.sort(key=lambda r: (-r["score"], -r["timestamp"]))

    # Cap each category so one busy theme cannot fill the whole panel.
    out, used = [], {}
    for row in events:
        if used.get(row["category"], 0) >= 2:
            continue
        used[row["category"]] = used.get(row["category"], 0) + 1
        out.append(row)
        if len(out) == limit:
            break
    if len(out) < 3:                      # quiet week - show context rather than nothing
        out += mentions[: 3 - len(out)]
    return out
