from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from model import ModelParams, classify_position, compact_zone, generate_levels, nearest_levels, normalize_prices
from providers import get_provider


app = FastAPI(title="Online Stock Point Monitor", version="1.1.0-manual-prices")


@app.exception_handler(Exception)
def unhandled_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={
            "detail": f"{type(exc).__name__}: {exc}",
            "path": str(request.url.path),
        },
    )


class StockConfig(BaseModel):
    ticker: str
    grid_step: float = 5
    lookback: int = 252
    min_score: float = 2.2
    swing_radius: int | None = None
    max_distance_from_close: float | None = None
    touch_bonus_divisor: float | None = None
    touch_bonus_cap: float | None = None
    atr_zone_multiple: float | None = None
    min_zone_width: float | None = None
    current_price: float | None = None
    prices: list[dict[str, Any]] | None = None
    discord_webhook_url: str | None = None


class MonitorRequest(BaseModel):
    mode: str = Field("postclose", pattern="^(postclose|premarket|intraday)$")
    provider: str = "manual"
    stocks: list[StockConfig]
    include_news: bool = False
    include_events: bool = True
    manual_events: list[dict[str, Any]] = Field(default_factory=list)
    lookahead_days: int = 7
    send_discord: bool = False
    discord_webhook_url: str | None = None


def chunk_text(text, limit=1850):
    chunks = []
    current = ""
    for part in text.split("\n\n"):
        candidate = part if not current else current + "\n\n" + part
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
            while len(part) > limit:
                chunks.append(part[:limit])
                part = part[limit:]
            current = part
    if current:
        chunks.append(current)
    return chunks


def post_discord(webhook_url, text):
    for chunk in chunk_text(text):
        data = json.dumps({"content": chunk}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(webhook_url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read()


def event_applies(event, ticker):
    tickers = [str(x).upper() for x in event.get("tickers", ["ALL"])]
    return "ALL" in tickers or ticker.upper() in tickers


def mode_title(mode):
    return {"postclose": "收盘后点位监控", "premarket": "盘前交易预案", "intraday": "盘中点位监控"}[mode]


def mode_plan(mode, reference_price, filtered):
    nearest_below, nearest_above = nearest_levels(reference_price, filtered)
    lines = []
    if nearest_below:
        lines.append(f"下方最近节点: {compact_zone(nearest_below)} 分数 {nearest_below['score']:.2f}")
    if nearest_above:
        lines.append(f"上方最近节点: {compact_zone(nearest_above)} 分数 {nearest_above['score']:.2f}")
    if nearest_below and nearest_above:
        lines.append(f"近期震荡区间: {nearest_below['zone_low']:.2f}-{nearest_above['zone_high']:.2f}")
    if nearest_above and reference_price >= nearest_above["zone_low"]:
        verb = "盘中动作" if mode == "intraday" else "盘前预案"
        lines.append(f"{verb}: 贴近阻力，偏高抛/T 出；放量站稳后再上移目标。")
    elif nearest_below and reference_price <= nearest_below["zone_high"]:
        verb = "盘中动作" if mode == "intraday" else "盘前预案"
        lines.append(f"{verb}: 贴近支撑，偏低吸/T 买回；跌破下沿不抢，等下一层。")
    else:
        verb = "盘中动作" if mode == "intraday" else "盘前预案"
        lines.append(f"{verb}: 区间中部，减少操作频率，等靠近上下沿。")
    return lines


def model_params_from_stock(stock):
    values = {
        "grid_step": stock.grid_step,
        "lookback": stock.lookback,
        "min_score": stock.min_score,
    }
    optional_fields = (
        "swing_radius",
        "max_distance_from_close",
        "touch_bonus_divisor",
        "touch_bonus_cap",
        "atr_zone_multiple",
        "min_zone_width",
    )
    for field in optional_fields:
        value = getattr(stock, field)
        if value is not None:
            values[field] = value
    return ModelParams(**values)


def render_stock(mode, stock, provider):
    ticker = stock.ticker.upper()
    try:
        if stock.prices:
            raw_prices = stock.prices
        else:
            raw_prices = provider.historical_prices(ticker)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"{ticker}: historical prices failed: {type(exc).__name__}: {exc}")
    if len(raw_prices) < 220:
        raise HTTPException(status_code=400, detail=f"{ticker}: not enough historical price rows ({len(raw_prices)} found)")
    try:
        rows = normalize_prices(raw_prices)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"{ticker}: price normalization failed: {type(exc).__name__}: {exc}")
    try:
        quote = provider.quote(ticker) if provider and not stock.current_price else {}
    except Exception as exc:
        quote = {"error": f"{type(exc).__name__}: {exc}"}
    reference_price = stock.current_price or quote.get("price") or rows[-1]["Close"]
    params = model_params_from_stock(stock)
    levels, meta = generate_levels(rows, params=params)
    filtered = [x for x in levels if x["score"] >= params.min_score]
    supports = [x for x in filtered if x["role"] == "support"]
    resistances = [x for x in filtered if x["role"] == "resistance"]
    status, action, nearest_support, nearest_resistance = classify_position(reference_price, supports, resistances)
    buy_zones = supports[:4]
    sell_zones = resistances[:4]
    lines = [
        f"【{ticker} {mode_title(mode)}】",
        f"数据日: {meta['trade_date']}  收盘: {meta['close']:.2f}  参考价: {reference_price:.2f}  ATR14: {meta['atr14']:.2f}",
        f"状态: {status}",
        f"建议: {action}",
        "",
        "做T低吸区: " + " / ".join(compact_zone(x) for x in buy_zones),
        "做T高抛区: " + " / ".join(compact_zone(x) for x in sell_zones),
    ]
    if mode in ("premarket", "intraday"):
        lines.extend(mode_plan(mode, reference_price, filtered))
    if nearest_support:
        lines.append(f"最近支撑: {compact_zone(nearest_support)} 分数 {nearest_support['score']:.2f}")
    if nearest_resistance:
        lines.append(f"最近阻力: {compact_zone(nearest_resistance)} 分数 {nearest_resistance['score']:.2f}")
    record = {
        "ticker": ticker,
        "trade_date": meta["trade_date"],
        "close": meta["close"],
        "reference_price": reference_price,
        "status": status,
        "supports": buy_zones,
        "resistances": sell_zones,
        "quote": quote,
        "meta": meta,
    }
    return lines, record


def validate_manual_prices(req):
    if req.provider != "manual":
        return
    missing = [s.ticker.upper() for s in req.stocks if not s.prices]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"provider=manual requires prices for every stock. Missing: {', '.join(missing)}",
        )


@app.get("/health")
def health():
    return {"ok": True, "time": datetime.utcnow().isoformat() + "Z", "version": app.version}


@app.post("/monitor")
def monitor(req: MonitorRequest):
    validate_manual_prices(req)
    provider = get_provider(req.provider)

    all_events = list(req.manual_events)
    if req.include_events:
        try:
            all_events.extend(provider.economic_calendar(req.lookahead_days))
        except Exception as exc:
            all_events.append({"title": f"事件日历读取失败: {exc}", "tickers": ["ALL"]})

    messages = []
    stock_messages = []
    webhook_messages = []
    records = []
    run_title = f"{mode_title(req.mode)} {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    for stock in req.stocks:
        lines, record = render_stock(req.mode, stock, provider)
        ticker = stock.ticker.upper()
        events = [event for event in all_events if event_applies(event, ticker)]
        if req.include_events:
            try:
                events.extend(provider.earnings_calendar(ticker, req.lookahead_days))
            except Exception:
                pass
        if events:
            lines.append("")
            lines.append("未来一周事件:")
            for e in events[:8]:
                lines.append(f"- {e.get('date', '')} {e.get('time', '')} {e.get('title', '')} {e.get('impact', '')}".strip())
        news = []
        if req.include_news:
            try:
                news = provider.news(ticker, limit=3)
            except Exception:
                news = []
        if news:
            lines.append("")
            lines.append("近期新闻:")
            for n in news[:3]:
                suffix = f" {n.get('url')}" if n.get("url") else ""
                lines.append(f"- {n.get('title')}{suffix}")
        stock_text = "\n".join([run_title, "", *lines])
        messages.append("\n".join(lines))
        for idx, chunk in enumerate(chunk_text(stock_text)):
            stock_message = {
                "ticker": ticker,
                "part": idx + 1,
                "content": chunk,
            }
            stock_messages.append(stock_message)
            if stock.discord_webhook_url:
                webhook_messages.append(
                    {
                        **stock_message,
                        "discord_webhook_url": stock.discord_webhook_url,
                        "body": {"content": chunk},
                    }
                )
        record["events"] = events
        record["news"] = news
        records.append(record)

    text = f"{run_title}\n\n" + "\n\n---\n\n".join(messages)
    if req.send_discord:
        default_webhook = req.discord_webhook_url or os.environ.get("DISCORD_WEBHOOK_URL")
        if any(s.discord_webhook_url for s in req.stocks):
            for message in stock_messages:
                stock_config = next((s for s in req.stocks if s.ticker.upper() == message["ticker"]), None)
                webhook = stock_config.discord_webhook_url if stock_config else None
                webhook = webhook or default_webhook
                if not webhook:
                    raise HTTPException(status_code=400, detail=f"{message['ticker']}: discord_webhook_url is required")
                post_discord(webhook, message["content"])
        else:
            webhook = default_webhook
            if not webhook:
                raise HTTPException(status_code=400, detail="DISCORD_WEBHOOK_URL is required")
            post_discord(webhook, text)
    return {
        "text": text,
        "discord_messages": chunk_text(text),
        "stock_messages": stock_messages,
        "webhook_messages": webhook_messages,
        "records": records,
    }
