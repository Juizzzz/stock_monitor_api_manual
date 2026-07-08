from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from html import unescape
from xml.etree import ElementTree as ET


def http_json(url, headers=None, timeout=20):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_text(url, headers=None, timeout=20):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def google_news_rss(ticker, limit=3):
    query = f"{ticker.upper()} stock OR earnings OR analyst"
    params = urllib.parse.urlencode({"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"})
    xml_text = http_text(f"https://news.google.com/rss/search?{params}")
    root = ET.fromstring(xml_text)
    result = []
    for item in root.findall("./channel/item")[:limit]:
        result.append(
            {
                "title": item.findtext("title"),
                "site": item.findtext("source"),
                "publishedDate": item.findtext("pubDate"),
                "url": item.findtext("link"),
                "source_type": "google_news_rss",
            }
        )
    return result


MAJOR_BLS_KEYWORDS = (
    "Employment Situation",
    "Consumer Price Index",
    "Producer Price Index",
    "Job Openings and Labor Turnover",
    "Employment Cost Index",
    "Productivity and Costs",
    "U.S. Import and Export Price Indexes",
    "Real Earnings",
)
HIGH_IMPACT_KEYWORDS = (
    "FOMC",
    "Employment Situation",
    "Consumer Price Index",
    "Producer Price Index",
    "Job Openings and Labor Turnover",
    "Employment Cost Index",
    "Nonfarm",
    "Payroll",
    "CPI",
    "PPI",
    "GDP",
    "Retail",
)


def impact_for_title(title):
    return "high" if any(k.lower() in title.lower() for k in HIGH_IMPACT_KEYWORDS) else "medium"


def dedupe_events(events):
    seen = set()
    result = []
    for event in events:
        key = (event.get("date"), event.get("time", ""), event.get("title"))
        if key in seen:
            continue
        seen.add(key)
        result.append(event)
    return sorted(result, key=lambda x: (x.get("date") or "", x.get("time") or "", x.get("title") or ""))


def parse_ics_datetime(value):
    if not value:
        return None, ""
    value = value.strip()
    if "T" in value:
        for fmt in ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M"):
            try:
                dt = datetime.strptime(value.replace("Z", ""), fmt)
                return dt.date(), dt.strftime("%H:%M ET")
            except ValueError:
                pass
    try:
        return datetime.strptime(value[:8], "%Y%m%d").date(), ""
    except ValueError:
        return None, ""


def parse_ics_events(ics_text):
    events = []
    current = None
    unfolded = []
    for raw_line in ics_text.splitlines():
        if raw_line.startswith((" ", "\t")) and unfolded:
            unfolded[-1] += raw_line[1:]
        else:
            unfolded.append(raw_line)
    for raw_line in unfolded:
        line = raw_line.strip()
        if line == "BEGIN:VEVENT":
            current = {}
        elif line == "END:VEVENT":
            if current:
                events.append(current)
            current = None
        elif current is not None and ":" in line:
            key, value = line.split(":", 1)
            key = key.split(";", 1)[0]
            if key in {"SUMMARY", "DTSTART", "DESCRIPTION", "LOCATION"}:
                current[key] = value.replace("\\,", ",").replace("\\n", " ")
    return events


def bls_calendar(days=7):
    today = datetime.now(timezone.utc).date()
    end = today + timedelta(days=days)
    try:
        ics_text = http_text("https://www.bls.gov/schedule/news_release/bls.ics", timeout=20)
    except Exception:
        return []
    result = []
    for item in parse_ics_events(ics_text):
        event_date, event_time = parse_ics_datetime(item.get("DTSTART", ""))
        title = item.get("SUMMARY", "")
        if not event_date or not (today <= event_date <= end):
            continue
        if not any(k.lower() in title.lower() for k in MAJOR_BLS_KEYWORDS):
            continue
        result.append(
            {
                "date": str(event_date),
                "time": event_time,
                "title": title,
                "impact": impact_for_title(title),
                "tickers": ["ALL"],
                "source": "BLS",
            }
        )
    return result


def fed_month_url(year, month):
    month_name = date(year, month, 1).strftime("%B").lower()
    return f"https://www.federalreserve.gov/newsevents/{year}-{month_name}.htm"


def html_to_lines(html_text):
    html_text = re.sub(r"(?i)<br\s*/?>", "\n", html_text)
    html_text = re.sub(r"(?i)</(p|div|li|h[1-6]|td|th|tr)>", "\n", html_text)
    text = re.sub(r"<[^>]+>", "\n", html_text)
    return [unescape(x).strip() for x in text.splitlines() if unescape(x).strip()]


def fed_calendar(days=7):
    today = datetime.now(timezone.utc).date()
    end = today + timedelta(days=days)
    months = {(today.year, today.month), (end.year, end.month)}
    events = []
    important_sections = {"Speeches", "FOMC Meetings", "Beige Book"}
    time_pattern = re.compile(r"^\d{1,2}:\d{2}\s*(a|p)\.m\.$", re.I)
    date_list_pattern = re.compile(r"^\d{1,2}(,\s*\d{1,2})*$")
    for year, month in sorted(months):
        try:
            lines = html_to_lines(http_text(fed_month_url(year, month), timeout=20))
        except Exception:
            continue
        section = None
        for idx, line in enumerate(lines):
            if line in important_sections:
                section = line
                continue
            if line in {"Statistical Releases", "Other", "Conferences"}:
                section = None
                continue
            if not section or not time_pattern.match(line):
                continue
            title = None
            date_line = None
            for look_ahead in lines[idx + 1 : idx + 12]:
                if not title and look_ahead not in {"Time:", "Release Date(s):"} and not date_list_pattern.match(look_ahead):
                    title = look_ahead
                    continue
                if title and date_list_pattern.match(look_ahead):
                    date_line = look_ahead
                    break
            if not title or not date_line:
                continue
            for day_text in [x.strip() for x in date_line.split(",")]:
                try:
                    event_date = date(year, month, int(day_text))
                except ValueError:
                    continue
                if today <= event_date <= end:
                    events.append(
                        {
                            "date": str(event_date),
                            "time": line.upper().replace(".", ""),
                            "title": title,
                            "impact": impact_for_title(title),
                            "tickers": ["ALL"],
                            "source": f"Federal Reserve {section}",
                        }
                    )
    return events


class ManualProvider:
    name = "manual"

    def historical_prices(self, ticker, days=650):
        raise ValueError(f"{ticker}: provider=manual requires prices in the request")

    def quote(self, ticker):
        return {}

    def news(self, ticker, limit=3):
        return google_news_rss(ticker, limit=limit)

    def earnings_calendar(self, ticker, days=7):
        return []

    def economic_calendar(self, days=7):
        return dedupe_events([*bls_calendar(days), *fed_calendar(days)])[:12]


class FMPProvider:
    name = "fmp"

    def __init__(self, api_key=None):
        self.api_key = api_key or os.environ.get("FMP_API_KEY")
        if not self.api_key:
            raise ValueError("FMP_API_KEY is required for provider=fmp")
        self.base = os.environ.get("FMP_BASE_URL", "https://financialmodelingprep.com/stable")

    def _url(self, path, **params):
        if "from_" in params:
            params["from"] = params.pop("from_")
        params["apikey"] = self.api_key
        return f"{self.base}{path}?{urllib.parse.urlencode(params)}"

    def historical_prices(self, ticker, days=650):
        url = self._url("/historical-price-eod/full", symbol=ticker)
        data = http_json(url)
        rows = []
        historical = data.get("historical", []) if isinstance(data, dict) else data
        historical = sorted(historical, key=lambda x: x.get("date", ""))
        if days:
            historical = historical[-days:]
        for r in historical:
            rows.append(
                {
                    "date": r["date"],
                    "open": r["open"],
                    "high": r["high"],
                    "low": r["low"],
                    "close": r["close"],
                    "volume": r.get("volume", 0),
                }
            )
        rows.sort(key=lambda x: x["date"])
        return rows

    def quote(self, ticker):
        data = http_json(self._url("/quote", symbol=ticker))
        if isinstance(data, list) and data:
            r = data[0]
            return {
                "price": r.get("price") or r.get("close"),
                "change": r.get("change"),
                "changesPercentage": r.get("changesPercentage"),
                "timestamp": r.get("timestamp"),
            }
        return {}

    def news(self, ticker, limit=3):
        try:
            data = http_json(self._url("/news/stock", symbols=ticker, page=0, limit=limit))
            return [
                {
                    "title": r.get("title") or r.get("headline"),
                    "site": r.get("site") or r.get("publisher"),
                    "publishedDate": r.get("publishedDate") or r.get("date"),
                    "url": r.get("url") or r.get("link"),
                    "source_type": "fmp",
                }
                for r in data[:limit]
            ]
        except Exception:
            return google_news_rss(ticker, limit=limit)

    def earnings_calendar(self, ticker, days=7):
        start = datetime.now(timezone.utc).date()
        end = start + timedelta(days=days)
        data = http_json(self._url("/earnings-calendar", from_=str(start), to=str(end)))
        result = []
        for r in data:
            if str(r.get("symbol", "")).upper() == ticker.upper():
                result.append(
                    {
                        "date": r.get("date") or r.get("fiscalDateEnding"),
                        "title": f"{ticker.upper()} earnings",
                        "impact": "high",
                        "tickers": [ticker.upper()],
                    }
                )
        return result

    def economic_calendar(self, days=7):
        result = [*bls_calendar(days), *fed_calendar(days)]
        start = datetime.now(timezone.utc).date()
        end = start + timedelta(days=days)
        try:
            data = http_json(self._url("/economic-calendar", from_=str(start), to=str(end)))
        except Exception:
            try:
                data = http_json(self._url("/economic_calendar", from_=str(start), to=str(end)))
            except Exception:
                return dedupe_events(result)[:12]
        keywords = ("FOMC", "Nonfarm", "Payroll", "CPI", "PPI", "Jobless", "ISM", "PMI", "GDP", "Retail")
        for r in data:
            event = r.get("event") or r.get("title") or ""
            if any(k.lower() in event.lower() for k in keywords):
                result.append(
                    {
                        "date": str(r.get("date", ""))[:10],
                        "time": str(r.get("date", ""))[11:16],
                        "title": event,
                        "impact": r.get("impact") or "macro",
                        "tickers": ["ALL"],
                        "source": "FMP",
                    }
                )
        return dedupe_events(result)[:12]


def get_provider(name):
    if name == "manual":
        return ManualProvider()
    if name == "fmp":
        return FMPProvider()
    raise ValueError(f"Unknown provider: {name}")
