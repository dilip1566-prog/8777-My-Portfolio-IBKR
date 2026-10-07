"""Yahoo Finance data access for the US & UK portfolio dashboard.

Uses only the Python standard library. Yahoo's quoteSummary endpoint needs a
cookie + "crumb" pair, so we fetch those once and reuse them for the session.
"""

import gzip
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from email.utils import parsedate_to_datetime
from http.cookiejar import CookieJar
from xml.etree import ElementTree

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept-Encoding": "gzip",
}

QUOTE_MODULES = ",".join([
    "assetProfile",
    "price",
    "summaryDetail",
    "defaultKeyStatistics",
    "financialData",
    "calendarEvents",
    "earningsHistory",
])


class YahooError(RuntimeError):
    pass


class Yahoo:
    def __init__(self, timeout=25, retries=3, pause=0.4):
        self.timeout = timeout
        self.retries = retries
        self.pause = pause
        self._crumb = None
        self._lock = threading.Lock()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar())
        )

    # ---------------------------------------------------------------- plumbing
    def _raw(self, url):
        last = None
        for attempt in range(self.retries):
            try:
                req = urllib.request.Request(url, headers=HEADERS)
                with self.opener.open(req, timeout=self.timeout) as resp:
                    body = resp.read()
                    if resp.headers.get("Content-Encoding") == "gzip":
                        body = gzip.decompress(body)
                    return body.decode("utf-8", "replace")
            except urllib.error.HTTPError as exc:
                last = exc
                if exc.code in (401, 403):      # crumb probably went stale
                    self._crumb = None
                if exc.code == 404:
                    raise YahooError("not found (404)") from exc
            except Exception as exc:            # noqa: BLE001 - network is messy
                last = exc
            time.sleep(self.pause * (attempt + 1))
        raise YahooError(str(last))

    def _json(self, url):
        return json.loads(self._raw(url))

    def crumb(self):
        if self._crumb:
            return self._crumb
        with self._lock:
            if self._crumb:            # another thread won the race
                return self._crumb
            return self._fetch_crumb()

    def _fetch_crumb(self):
        # Priming a Yahoo page hands us the consent/session cookies.
        for primer in ("https://fc.yahoo.com", "https://finance.yahoo.com/quote/AAPL"):
            try:
                self._raw(primer)
            except Exception:                   # noqa: BLE001 - primer is best effort
                pass
        crumb = self._raw("https://query1.finance.yahoo.com/v1/test/getcrumb").strip()
        if not crumb or "<" in crumb:
            raise YahooError("could not obtain a Yahoo session crumb")
        self._crumb = crumb
        return crumb

    # -------------------------------------------------------------- endpoints
    def chart(self, ticker, rng="1y", interval="1d"):
        url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
               f"{urllib.parse.quote(ticker)}?range={rng}&interval={interval}"
               "&includePrePost=false")
        payload = self._json(url)
        chart = payload.get("chart") or {}
        if chart.get("error"):
            raise YahooError(chart["error"].get("description", "chart error"))
        results = chart.get("result") or []
        if not results:
            raise YahooError("no price data returned")
        return results[0]

    def summary(self, ticker, modules=QUOTE_MODULES):
        for attempt in range(2):
            url = ("https://query1.finance.yahoo.com/v10/finance/quoteSummary/"
                   f"{urllib.parse.quote(ticker)}?modules={modules}"
                   f"&crumb={urllib.parse.quote(self.crumb())}")
            try:
                payload = self._json(url)
            except YahooError:
                self._crumb = None
                if attempt == 0:
                    continue
                return {}
            block = payload.get("quoteSummary") or {}
            if block.get("error"):
                self._crumb = None
                if attempt == 0:
                    continue
                return {}
            results = block.get("result") or []
            return results[0] if results else {}
        return {}

    def _search_news(self, query, count):
        items = []
        url = ("https://query1.finance.yahoo.com/v1/finance/search?q="
               f"{urllib.parse.quote(query)}&newsCount={count}&quotesCount=0"
               "&enableFuzzyQuery=false")
        try:
            payload = self._json(url)
        except YahooError:
            return items
        for row in payload.get("news") or []:
            link = row.get("link") or ""
            title = (row.get("title") or "").strip()
            if title and link:
                items.append({
                    "title": title,
                    "publisher": row.get("publisher") or "Yahoo Finance",
                    "url": link,
                    "timestamp": row.get("providerPublishTime") or 0,
                })
        return items

    def _rss_news(self, ticker, region):
        """The per-ticker RSS feed is far better targeted than free-text search."""
        items = []
        lang = "en-GB" if region == "GB" else "en-US"
        url = ("https://feeds.finance.yahoo.com/rss/2.0/headline?s="
               f"{urllib.parse.quote(ticker)}&region={region}&lang={lang}")
        try:
            body = self._raw(url)
        except YahooError:
            return items
        try:
            root = ElementTree.fromstring(body)
        except ElementTree.ParseError:
            return items
        for node in root.iter("item"):
            title = (node.findtext("title") or "").strip()
            link = (node.findtext("link") or "").strip()
            if not title or not link:
                continue
            stamp = 0
            pub = node.findtext("pubDate")
            if pub:
                try:
                    stamp = int(parsedate_to_datetime(pub).timestamp())
                except (TypeError, ValueError):
                    stamp = 0
            host = urllib.parse.urlparse(link).netloc
            host = re.sub(r"^(www|finance|uk)\.", "", host).split(".")[0]
            items.append({
                "title": title,
                "publisher": host.replace("-", " ").title() or "Yahoo Finance",
                "url": link,
                "timestamp": stamp,
            })
        return items

    def _google_news(self, query, uk):
        """Google News search RSS - the best company-targeted free headline feed."""
        items = []
        locale = ("hl=en-GB&gl=GB&ceid=GB:en" if uk else "hl=en-US&gl=US&ceid=US:en")
        url = ("https://news.google.com/rss/search?q="
               f"{urllib.parse.quote(query + ' when:60d')}&{locale}")
        try:
            body = self._raw(url)
            root = ElementTree.fromstring(body)
        except (YahooError, ElementTree.ParseError):
            return items
        for node in root.iter("item"):
            title = (node.findtext("title") or "").strip()
            link = (node.findtext("link") or "").strip()
            if not title or not link:
                continue
            source = node.find("source")
            publisher = (source.text or "").strip() if source is not None else "Google News"
            # Google appends " - Publisher" to every headline.
            if publisher and title.endswith(" - " + publisher):
                title = title[: -(len(publisher) + 3)].strip()
            stamp = 0
            pub = node.findtext("pubDate")
            if pub:
                try:
                    stamp = int(parsedate_to_datetime(pub).timestamp())
                except (TypeError, ValueError):
                    stamp = 0
            items.append({
                "title": title,
                "publisher": publisher or "Google News",
                "url": link,
                "timestamp": stamp,
                "source": "google",
            })
        return items

    @staticmethod
    def short_name(company, ticker):
        trimmed = re.sub(r"\b(plc|inc|corp|corporation|company|co|ltd|limited|"
                         r"holdings?|the)\b\.?", "", company or "", flags=re.I)
        trimmed = " ".join(trimmed.split()[:3]).strip(" ,.&")
        return trimmed or ticker

    def news(self, ticker, company=None, region="US"):
        """Merge the Yahoo ticker feed with a company-targeted Google News search."""
        uk = region == "UK"
        items = []
        for row in self._rss_news(ticker, "GB" if uk else "US"):
            row["source"] = "yahoo-rss"
            items.append(row)
        name = self.short_name(company, ticker)
        items += self._google_news(
            '"%s" %s' % (name, "share price" if uk else "stock"), uk)
        for row in self._search_news(ticker, 12):
            row["source"] = "yahoo-search"
            items.append(row)
        return items


def num(node, *path, default=None):
    """Yahoo wraps most figures as {"raw": ..., "fmt": ...}. Unwrap safely."""
    cur = node
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
    if isinstance(cur, dict):
        cur = cur.get("raw")
    if cur is None:
        return default
    if isinstance(cur, (int, float)):
        return cur
    return default


def text(node, *path, default=None):
    cur = node
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
    if isinstance(cur, dict):
        cur = cur.get("fmt")
    if isinstance(cur, str) and cur.strip():
        return cur.strip()
    return default
