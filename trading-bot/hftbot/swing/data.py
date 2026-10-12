"""The coin basket and free hourly price history.

Prices for signals come from big centralized exchanges (deep, continuous history); trades go through
Jupiter on Solana, whose prices track them through arbitrage. The live trader refuses any swap whose
Jupiter price is far from the reference price, which also catches a wrong token address.

Sources:
- "binance": monthly and daily files from data.binance.vision for history, plus the klines API for the
  latest hours. The API refuses US addresses; use "coinbase" on a US server.
- "coinbase": the Coinbase Exchange candles API (300 candles per request).
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import logging
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass

from hftbot.swing.candles import HOUR, Candle

log = logging.getLogger("hftbot")

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_DECIMALS = 6


@dataclass(frozen=True)
class Coin:
    symbol: str
    mint: str
    """The SPL token traded on Solana. Checked on chain (decimals) and against the reference price before any swap."""
    decimals: int
    binance: str
    coinbase: str


COINS = {
    "SOL": Coin("SOL", "So11111111111111111111111111111111111111112", 9, "SOLUSDT", "SOL-USD"),
    "BTC": Coin("BTC", "cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij", 8, "BTCUSDT", "BTC-USD"),  # Coinbase cbBTC
    "ETH": Coin("ETH", "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs", 8, "ETHUSDT", "ETH-USD"),  # Wormhole WETH
    "JUP": Coin("JUP", "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN", 6, "JUPUSDT", "JUP-USD"),
}

Get = Callable[[str, float], bytes]


class NotFound(Exception):
    pass


def _urllib_get(url: str, timeout_s: float) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "hftbot/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise NotFound(url) from error
        raise


def _binance_time(value: str) -> int:
    t = int(value)
    # Spot files switched from milliseconds to microseconds in 2025.
    return t // 1_000_000 if t > 10**14 else t // 1000


def parse_binance_rows(rows: list[list[str]]) -> list[Candle]:
    out = []
    for r in rows:
        if not r or not r[0].strip().isdigit():
            continue  # header line, if any
        out.append(Candle(_binance_time(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])))
    return out


def _months(start: dt.date, end: dt.date) -> list[dt.date]:
    months, d = [], dt.date(start.year, start.month, 1)
    while d <= end:
        months.append(d)
        d = dt.date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    return months


def fetch_binance(symbol: str, start: dt.date, end: dt.date | None = None, get: Get = _urllib_get, recent_api: bool = True) -> list[Candle]:
    """Hourly candles from `start` up to now: monthly files, then daily files for the current month, then the API."""
    end = end or dt.datetime.now(dt.timezone.utc).date()
    base = "https://data.binance.vision/data/spot"
    candles: list[Candle] = []
    this_month = dt.date(end.year, end.month, 1)
    for month in _months(start, end):
        if month == this_month:
            days = [month + dt.timedelta(days=i) for i in range((end - month).days)]
            urls = [f"{base}/daily/klines/{symbol}/1h/{symbol}-1h-{d:%Y-%m-%d}.zip" for d in days]
        else:
            urls = [f"{base}/monthly/klines/{symbol}/1h/{symbol}-1h-{month:%Y-%m}.zip"]
        for url in urls:
            try:
                archive = zipfile.ZipFile(io.BytesIO(get(url, 30.0)))
            except NotFound:
                continue  # not listed yet, or the file is not published yet
            for name in archive.namelist():
                rows = list(csv.reader(io.TextIOWrapper(archive.open(name), encoding="utf-8")))
                candles.extend(parse_binance_rows(rows))
    if recent_api:
        try:
            candles.extend(fetch_binance_api(symbol, since=candles[-1].ts + HOUR if candles else int(time.time()) - 30 * 24 * HOUR, get=get))
        except (OSError, ValueError) as error:  # includes HTTP 451 for US addresses
            log.warning("binance API unavailable (%s): data ends at the last daily file; use source coinbase on US servers", error)
    return sorted({c.ts: c for c in candles}.values(), key=lambda c: c.ts)


def fetch_binance_api(symbol: str, since: int, get: Get = _urllib_get) -> list[Candle]:
    out: list[Candle] = []
    start = since
    while True:
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1h&startTime={start * 1000}&limit=1000"
        rows = json.loads(get(url, 15.0))
        batch = [Candle(int(r[0]) // 1000, float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])) for r in rows]
        out.extend(batch)
        if len(rows) < 1000:
            return out
        start = batch[-1].ts + HOUR


def fetch_coinbase(product: str, start: int, end: int | None = None, get: Get = _urllib_get, pause_s: float = 0.15) -> list[Candle]:
    """Hourly candles in [start, end) from the Coinbase Exchange API, 300 per request."""
    end = end or int(time.time())
    out: dict[int, Candle] = {}
    window = 300 * HOUR
    cursor = start
    while cursor < end:
        upper = min(cursor + window, end)
        url = (
            f"https://api.exchange.coinbase.com/products/{product}/candles?granularity=3600"
            f"&start={_iso(cursor)}&end={_iso(upper)}"
        )
        rows = json.loads(get(url, 15.0))
        if isinstance(rows, dict):
            raise ValueError(f"coinbase {product}: {rows.get('message', rows)}")
        for t, low, high, open_, close, volume in rows:  # newest first
            if start <= t < end:
                out[int(t)] = Candle(int(t), float(open_), float(high), float(low), float(close), float(volume))
        cursor = upper
        if pause_s:
            time.sleep(pause_s)
    return [out[t] for t in sorted(out)]


def _iso(ts: int) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch(coin: Coin, source: str, since: int, get: Get = _urllib_get) -> list[Candle]:
    """Hourly candles for `coin` from `since` (epoch seconds) to now."""
    if source == "binance":
        if time.time() - since < 3 * 86400:  # a routine update: one API call instead of a month of daily files
            try:
                return fetch_binance_api(coin.binance, since, get=get)
            except (OSError, ValueError) as error:
                log.warning("binance API unavailable (%s); falling back to daily files, which lag by a day", error)
        start = dt.datetime.fromtimestamp(since, dt.timezone.utc).date()
        return [c for c in fetch_binance(coin.binance, start, get=get) if c.ts >= since]
    if source == "coinbase":
        return fetch_coinbase(coin.coinbase, since, get=get)
    raise ValueError(f"unknown source {source!r}: use binance or coinbase")
