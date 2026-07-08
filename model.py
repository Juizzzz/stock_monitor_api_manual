from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime


FIBS = [
    ("fib_23.6", 0.236, 2.0),
    ("fib_38.2", 0.382, 2.5),
    ("fib_50.0", 0.500, 1.8),
    ("fib_61.8", 0.618, 2.5),
    ("fib_78.6", 0.786, 1.6),
]
MA_WEIGHTS = [(20, 1.4), (50, 1.6), (100, 1.5), (200, 2.0)]
HL_WEIGHTS = [(20, 1.3), (60, 1.6), (120, 1.8), (252, 2.0)]
ATR_LADDER = [(1, 0.8), (2, 0.9), (3, 1.0)]


@dataclass
class ModelParams:
    lookback: int = 252
    swing_radius: int = 3
    grid_step: float = 5.0
    max_distance_from_close: float = 0.50
    min_score: float = 2.2
    touch_bonus_divisor: float = 8.0
    touch_bonus_cap: float = 2.0
    atr_zone_multiple: float = 0.45
    min_zone_width: float = 2.0


def parse_date(value):
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def normalize_prices(raw_rows):
    rows = []
    for r in raw_rows:
        rows.append(
            {
                "Date": parse_date(r.get("Date") or r.get("date")),
                "Open": float(r.get("Open") or r.get("open")),
                "High": float(r.get("High") or r.get("high")),
                "Low": float(r.get("Low") or r.get("low")),
                "Close": float(r.get("Close") or r.get("close")),
                "Volume": float(r.get("Volume") or r.get("volume") or 0),
            }
        )
    rows.sort(key=lambda x: x["Date"])
    add_indicators(rows)
    return rows


def add_indicators(rows):
    for i, r in enumerate(rows):
        for n in (20, 50, 100, 200):
            r[f"MA{n}"] = (
                sum(x["Close"] for x in rows[i + 1 - n : i + 1]) / n
                if i + 1 >= n
                else None
            )
        if i >= 14:
            trs = []
            for j in range(i - 13, i + 1):
                prev = rows[j - 1]["Close"] if j else rows[j]["Close"]
                trs.append(
                    max(
                        rows[j]["High"] - rows[j]["Low"],
                        abs(rows[j]["High"] - prev),
                        abs(rows[j]["Low"] - prev),
                    )
                )
            r["ATR14"] = sum(trs) / len(trs)
        else:
            r["ATR14"] = None


def idx_on_or_before(rows, cutoff):
    cutoff = parse_date(cutoff)
    ans = None
    for i, r in enumerate(rows):
        if r["Date"] <= cutoff:
            ans = i
        else:
            break
    if ans is None:
        raise ValueError(f"No price data on or before cutoff {cutoff}")
    return ans


def round_to_grid(price, step):
    return round(price / step) * step


def fmt_level(x):
    return f"{float(x):.2f}".rstrip("0").rstrip(".")


def local_extrema(rows, start, end, radius):
    lo = max(start + radius, radius)
    hi = min(end - radius, len(rows) - radius)
    for i in range(lo, hi):
        win = rows[i - radius : i + radius + 1]
        if rows[i]["High"] == max(x["High"] for x in win):
            yield "swing_high", rows[i]["High"], rows[i]["Date"]
        if rows[i]["Low"] == min(x["Low"] for x in win):
            yield "swing_low", rows[i]["Low"], rows[i]["Date"]


def add_candidate(raw_items, raw, source, weight, params):
    if raw is None or raw <= 0:
        return
    raw_items.append(
        {
            "raw": raw,
            "level": round_to_grid(raw, params.grid_step),
            "source": source,
            "weight": weight,
        }
    )


def generate_levels(rows, cutoff=None, params=None):
    if params is None:
        params = ModelParams()
    if cutoff is None:
        cutoff = rows[-1]["Date"]
    i = idx_on_or_before(rows, cutoff)
    current = rows[i]
    close = current["Close"]
    atr = current["ATR14"] or close * 0.025
    zone_half_width = max(params.min_zone_width, params.atr_zone_multiple * atr)
    start = max(0, i - params.lookback + 1)
    window = rows[start : i + 1]
    raw_items = []

    swing_low = min(window, key=lambda x: x["Low"])
    swing_high = max(window, key=lambda x: x["High"])
    low_price = swing_low["Low"]
    high_price = swing_high["High"]
    for name, ratio, weight in FIBS:
        value = high_price - (high_price - low_price) * ratio
        source = f"{name}: {swing_low['Date']} low {low_price:.2f} -> {swing_high['Date']} high {high_price:.2f}"
        add_candidate(raw_items, value, source, weight, params)

    for n, weight in MA_WEIGHTS:
        add_candidate(raw_items, current.get(f"MA{n}"), f"MA{n}", weight, params)

    for lb, weight in HL_WEIGHTS:
        ww = rows[max(0, i - lb + 1) : i + 1]
        add_candidate(raw_items, max(x["High"] for x in ww), f"{lb}d_high", weight, params)
        add_candidate(raw_items, min(x["Low"] for x in ww), f"{lb}d_low", weight, params)

    for kind, px, d in local_extrema(rows, start, i + 1, params.swing_radius):
        if abs(px - close) / close <= params.max_distance_from_close:
            add_candidate(raw_items, px, f"{kind}_{d}", 1.2, params)

    for k, weight in ATR_LADDER:
        add_candidate(raw_items, close + k * atr, f"+{k}ATR14", weight, params)
        add_candidate(raw_items, close - k * atr, f"-{k}ATR14", weight, params)

    grid_min = math.floor((close - 4 * atr) / params.grid_step) * params.grid_step
    grid_max = math.ceil((close + 5 * atr) / params.grid_step) * params.grid_step
    g = grid_min
    while g <= grid_max + 1e-9:
        add_candidate(raw_items, g, f"{fmt_level(params.grid_step)}_execution_grid", 0.25, params)
        g += params.grid_step

    grouped = defaultdict(lambda: {"score": 0.0, "sources": [], "raws": []})
    for item in raw_items:
        level = item["level"]
        if abs(level - close) / close > params.max_distance_from_close:
            continue
        grouped[level]["score"] += item["weight"]
        grouped[level]["sources"].append(item["source"])
        grouped[level]["raws"].append(item["raw"])

    levels = []
    for level, obj in grouped.items():
        touch_count = sum(
            1 for day in window if day["Low"] <= level + zone_half_width and day["High"] >= level - zone_half_width
        )
        score = obj["score"] + min(params.touch_bonus_cap, touch_count / params.touch_bonus_divisor)
        role = "support" if level < close else "resistance" if level > close else "pivot"
        levels.append(
            {
                "level": level,
                "zone_low": level - zone_half_width,
                "zone_high": level + zone_half_width,
                "role": role,
                "score": score,
                "touch_count": touch_count,
                "source_count": len(set(obj["sources"])),
                "sources": sorted(set(obj["sources"])),
            }
        )
    levels.sort(key=lambda x: (-x["score"], abs(x["level"] - close)))
    meta = {
        "trade_date": str(current["Date"]),
        "close": close,
        "atr14": atr,
        "zone_half_width": zone_half_width,
        "swing_low_date": str(swing_low["Date"]),
        "swing_low": low_price,
        "swing_high_date": str(swing_high["Date"]),
        "swing_high": high_price,
    }
    return levels, meta


def compact_zone(row):
    return f"{row['zone_low']:.2f}-{row['zone_high']:.2f}({row['level']:.0f})"


def nearest_levels(reference_price, filtered):
    below = [x for x in filtered if x["level"] <= reference_price]
    above = [x for x in filtered if x["level"] >= reference_price]
    return (
        max(below, key=lambda x: x["level"]) if below else None,
        min(above, key=lambda x: x["level"]) if above else None,
    )


def classify_position(reference_price, supports, resistances):
    nearest_support = max(supports, key=lambda x: x["level"]) if supports else None
    nearest_resistance = min(resistances, key=lambda x: x["level"]) if resistances else None
    status = "区间中部"
    action = "不追涨，等靠近支撑低吸或靠近阻力高抛。"
    if nearest_support and nearest_support["zone_low"] <= reference_price <= nearest_support["zone_high"]:
        return "处于支撑区", "适合观察小仓位低吸/T 买回；跌破支撑区下沿且两日收不回则等下一层。", nearest_support, nearest_resistance
    if nearest_resistance and nearest_resistance["zone_low"] <= reference_price <= nearest_resistance["zone_high"]:
        return "处于阻力区", "适合分批 T 出/减仓；若连续三日站稳阻力上沿，则阻力转支撑。", nearest_support, nearest_resistance
    if nearest_support and reference_price < nearest_support["zone_low"]:
        return "跌破最近支撑", "不要急着补，等待下一层支撑区或重新收回支撑。", nearest_support, nearest_resistance
    if nearest_resistance and reference_price > nearest_resistance["zone_high"]:
        return "突破最近阻力", "观察是否连续三日站稳，站稳后上移目标位。", nearest_support, nearest_resistance
    return status, action, nearest_support, nearest_resistance
