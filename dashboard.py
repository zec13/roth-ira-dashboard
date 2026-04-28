"""
Daily Roth IRA dashboard generator.
Pulls market data, computes returns, fetches news + macro context via Claude,
generates the HTML dashboard, and sends a TLDR email.
"""
import os
import json
import re
from datetime import datetime, timedelta, date
from pathlib import Path

import yfinance as yf
import pandas as pd
from anthropic import Anthropic
import resend
from jinja2 import Template

from holdings import (
    HOLDINGS, CLOSED_POSITIONS, PARTIAL_CLOSES,
    TOTAL_CONTRIBUTIONS, EMAIL_TO, EMAIL_FROM, DASHBOARD_URL,
)
from catalysts import CATALYSTS

# ---------------- Config ----------------
TODAY = date.today()
ALL_TICKERS = [t for t, *_ in HOLDINGS] + [t for t, *_ in CLOSED_POSITIONS]
HELD_TICKERS = [t for t, sh, _, _ in HOLDINGS if sh > 0]

OUT_DIR = Path("docs")
OUT_DIR.mkdir(exist_ok=True)


# ---------------- Market data ----------------
def fetch_prices():
    """Returns dict of {ticker: {price, day_change_pct, day_change_dollar, d30_pct, history_1y, history_all}}."""
    data = {}
    for ticker in ALL_TICKERS:
        try:
            t = yf.Ticker(ticker)
            # Fetch ~5 years for "all" view, weekly granularity
            hist_all = t.history(period="5y", interval="1wk", auto_adjust=True)
            # 1-year, daily
            hist_1y = t.history(period="1y", interval="1d", auto_adjust=True)
            # 30-day, daily
            hist_30d = t.history(period="1mo", interval="1d", auto_adjust=True)

            if hist_1y.empty:
                print(f"  WARN: no data for {ticker}")
                continue

            current = float(hist_1y["Close"].iloc[-1])
            prev_close = float(hist_1y["Close"].iloc[-2]) if len(hist_1y) > 1 else current
            day_pct = ((current - prev_close) / prev_close) * 100 if prev_close else 0

            d30_start = float(hist_30d["Close"].iloc[0]) if not hist_30d.empty else current
            d30_pct = ((current - d30_start) / d30_start) * 100 if d30_start else 0

            data[ticker] = {
                "price": current,
                "prev_close": prev_close,
                "day_pct": day_pct,
                "day_dollar": current - prev_close,
                "d30_pct": d30_pct,
                "history_1y": [
                    {"d": d.strftime("%Y-%m-%d"), "p": float(p)}
                    for d, p in zip(hist_1y.index, hist_1y["Close"])
                ],
                "history_all": [
                    {"d": d.strftime("%Y-%m-%d"), "p": float(p)}
                    for d, p in zip(hist_all.index, hist_all["Close"])
                ],
                "history_30d": [
                    {"d": d.strftime("%Y-%m-%d"), "p": float(p)}
                    for d, p in zip(hist_30d.index, hist_30d["Close"])
                ],
            }
            print(f"  {ticker}: ${current:.2f} ({day_pct:+.2f}% today)")
        except Exception as e:
            print(f"  ERROR fetching {ticker}: {e}")
    return data


# ---------------- Compute holdings ----------------
def compute_held(prices):
    """Returns list of dicts with all per-holding metrics, sorted by value desc."""
    out = []
    for ticker, shares, cost_basis, name in HOLDINGS:
        if ticker not in prices:
            continue
        p = prices[ticker]
        value = shares * p["price"]
        total_return = value - cost_basis
        total_pct = (total_return / cost_basis
