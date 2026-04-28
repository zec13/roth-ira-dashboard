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
HELD_TICKERS = [t for t, sh, _, _, _ in HOLDINGS if sh > 0]

OUT_DIR = Path("docs")
OUT_DIR.mkdir(exist_ok=True)


# ---------------- Market data ----------------
def fetch_prices():
    """Returns dict of {ticker: {price, day_change_pct, day_change_dollar, d30_pct, history_1y, history_all}}."""
    from curl_cffi import requests as curl_requests
    # Impersonate a real Chrome browser — Yahoo blocks the default Python User-Agent on GitHub Actions IPs
    session = curl_requests.Session(impersonate="chrome")
    data = {}
    for ticker in ALL_TICKERS:
        try:
            t = yf.Ticker(ticker, session=session)
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
    for ticker, shares, cost_basis, name, purchase_date in HOLDINGS:
        if ticker not in prices:
            continue
        p = prices[ticker]
        value = shares * p["price"]
        total_return = value - cost_basis
        total_pct = (total_return / cost_basis) * 100 if cost_basis else 0
        avg_cost = cost_basis / shares
        day_value_change = shares * p["day_dollar"]

        out.append({
            "ticker": ticker,
            "name": name,
            "shares": shares,
            "price": p["price"],
            "cost_basis": cost_basis,
            "avg_cost": avg_cost,
            "value": value,
            "day_pct": p["day_pct"],
            "day_dollar": day_value_change,
            "d30_pct": p["d30_pct"],
            "total_return": total_return,
            "total_pct": total_pct,
            "history_1y": p["history_1y"],
            "history_all": p["history_all"],
            "history_30d": p["history_30d"],
            "purchase_date": purchase_date,
        })
    out.sort(key=lambda h: h["value"], reverse=True)
    return out


# ---------------- Investment returns curve ----------------
def compute_returns_curve(held_holdings):
    """
    Builds the cumulative investment-returns time series (returns = portfolio_value - contributions).
    Samples weekly so the 30D toggle has enough points to render.
    Approximates contributions as a linear ramp from $0 (Apr 2022) to TOTAL_CONTRIBUTIONS (today).
    """
    from datetime import timedelta
    start = date(2022, 4, 1)
    end = TODAY

    # Sample weekly — gives ~210 points over 4 years; 4-5 points within any 30-day window
    sample_dates = []
    cur = start
    while cur <= end:
        sample_dates.append(cur)
        cur = cur + timedelta(days=7)
    if sample_dates[-1] != end:
        sample_dates.append(end)

    # Build a per-ticker date->price map for fast lookup
    history_maps = {}
    for h in held_holdings:
        m = {entry["d"]: entry["p"] for entry in h["history_all"]}
        history_maps[h["ticker"]] = (m, sorted(m.keys()))

    portfolio_values = []
    for d in sample_dates:
        d_str = d.strftime("%Y-%m-%d")
        v = 0.0
        for h in held_holdings:
            m, sorted_keys = history_maps[h["ticker"]]
            # Find largest historical date <= d_str
            price = None
            for k in reversed(sorted_keys):
                if k <= d_str:
                    price = m[k]
                    break
            if price is not None:
                v += h["shares"] * price
        portfolio_values.append(v)

    # Linear contribution ramp
    n = len(sample_dates)
    contributions = [TOTAL_CONTRIBUTIONS * (i / (n - 1)) for i in range(n)] if n > 1 else [TOTAL_CONTRIBUTIONS]

    points = []
    for i, d in enumerate(sample_dates):
        v = portfolio_values[i] - contributions[i]
        # Defensive: filter NaN/inf so the chart doesn't break
        if v != v or v == float("inf") or v == float("-inf"):
            v = 0.0
        points.append({"d": d.strftime("%Y-%m-%d"), "v": round(v, 2)})

    # Force the last point to match the headline number exactly
    if points:
        current_total_value = sum(h["value"] for h in held_holdings)
        points[-1] = {
            "d": TODAY.strftime("%Y-%m-%d"),
            "v": round(current_total_value - TOTAL_CONTRIBUTIONS, 2),
        }

    return points


# ---------------- Catalysts ----------------
def upcoming_catalysts(days=60):
    out = []
    cutoff = TODAY + timedelta(days=days)
    for date_str, ticker, kind, desc in CATALYSTS:
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            continue  # skip quarter-only entries
        if TODAY <= d <= cutoff:
            out.append({
                "date": d,
                "date_str": d.strftime("%b %-d"),
                "ticker": ticker,
                "kind": kind,
                "desc": desc,
            })
    out.sort(key=lambda c: c["date"])
    return out


# ---------------- News + macro via Claude ----------------
def fetch_news_and_macro(held_tickers):
    """
    Asks Claude (with web search) for today's significant news per ticker, plus any
    macro news affecting VOO/SPY. Returns dict: {ticker: {src, text}, "MACRO": {...}}.
    Returns empty dict if anything fails — dashboard works without news.
    """
    client = Anthropic()
    today_str = TODAY.strftime("%A, %B %d, %Y")
    tickers_str = ", ".join(held_tickers)

    prompt = f"""Today is {today_str}. I need you to find significant market news from TODAY ONLY for these stocks: {tickers_str}.

For EACH ticker, search for news from today and decide:
1. Is there a SIGNIFICANT news headline from today? (clinical trial results, FDA decisions, earnings, M&A, major analyst actions, partnerships, lawsuits, executive changes). Skip routine coverage, price commentary, generic market wraps.
2. If yes, return: source name, time if known, and a one-sentence headline (paraphrased, NOT quoted from the source).
3. If no significant news today, omit that ticker from your response.

Also: check for any MAJOR macroeconomic news from today that would meaningfully affect broad market index funds (VOO, SPY) — Fed decisions, CPI/jobs prints, major geopolitical events, market-wide circuit breakers. Skip routine market commentary.

Return ONLY a JSON object in this exact format (no markdown, no preamble):
{{
  "AQST": {{"src": "Reuters", "text": "Headline paraphrased here"}},
  "IOVA": {{"src": "BioPharma Dive", "text": "Headline paraphrased here"}},
  "MACRO": {{"src": "WSJ", "text": "Headline paraphrased here"}}
}}

If nothing significant for a ticker, OMIT it. If no macro news, omit MACRO. Return {{}} if nothing significant anywhere.
"""

    try:
        msg = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=2000,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 8}],
            messages=[{"role": "user", "content": prompt}],
        )
        # Extract last text block
        text = ""
        for block in msg.content:
            if hasattr(block, "text"):
                text = block.text
        # Strip code fences if present
        text = re.sub(r"^```(json)?\s*", "", text.strip())
        text = re.sub(r"\s*```$", "", text)
        result = json.loads(text)
        print(f"  News found: {list(result.keys())}")
        return result
    except Exception as e:
        print(f"  WARN: news fetch failed: {e}")
        return {}


# ---------------- Email ----------------
def send_email(summary, dashboard_url):
    resend.api_key = os.environ["RESEND_API_KEY"]
    body = build_email_body(summary, dashboard_url)
    subject = f"Roth IRA close — {TODAY.strftime('%a %b %-d')} — Portfolio {summary['day_sign']}{abs(summary['day_pct']):.2f}%"
    r = resend.Emails.send({
        "from": EMAIL_FROM,
        "to": [EMAIL_TO],
        "subject": subject,
        "text": body,
    })
    print(f"  Email sent: id={r.get('id')}")


def build_email_body(s, url):
    lines = [
        f"Markets close, {TODAY.strftime('%a %b %-d %Y')}.",
        "",
        f"Portfolio value:    ${s['portfolio_value']:,.2f}",
        f"Contributed:        ${TOTAL_CONTRIBUTIONS:,.2f}",
        f"Investment returns: {s['returns_sign']}${abs(s['returns_dollar']):,.2f} ({s['returns_sign']}{abs(s['returns_pct']):.2f}%)",
        f"Day:                {s['day_sign']}${abs(s['day_dollar']):,.2f} ({s['day_sign']}{abs(s['day_pct']):.2f}%)",
        f"30-day:             {s['d30_sign']}${abs(s['d30_dollar']):,.2f} ({s['d30_sign']}{abs(s['d30_pct']):.2f}%)",
        "",
        f"Top mover:    {s['top_mover']}",
        f"Worst mover:  {s['worst_mover']}",
    ]

    if s["news"]:
        lines += ["", "Notable today:"]
        for ticker, n in s["news"].items():
            label = "Macro" if ticker == "MACRO" else ticker
            lines.append(f"  • {label} — {n['text']} ({n['src']})")

    if s["catalysts"]:
        lines += ["", "Upcoming catalysts (next 60 days):"]
        for c in s["catalysts"][:5]:
            lines.append(f"  • {c['ticker']} — {c['date_str']} — {c['desc'][:80]}")

    lines += ["", f"→ Open full dashboard: {url}"]
    return "\n".join(lines)


# ---------------- Dashboard HTML ----------------
DASHBOARD_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Roth IRA Dashboard — {{ today_str }}</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<style>
  :root {
    --text: #1a1a1a;
    --text-2: #555;
    --text-3: #888;
    --bg: #fafaf7;
    --bg-2: #f1efe8;
    --border: #e5e3dc;
    --green: #1D9E75;
    --red: #A32D2D;
    --green-bg: rgba(29, 158, 117, 0.15);
    --red-bg: rgba(163, 45, 45, 0.15);
    --amber-bg: #FAEEDA; --amber-fg: #633806;
    --red-tag-bg: #FCEBEB; --red-tag-fg: #791F1F;
    --purple-bg: #EEEDFE; --purple-fg: #3C3489;
  }
  * { box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--bg); color: var(--text); margin: 0; padding: 24px 16px; }
  .container { max-width: 760px; margin: 0 auto; }
  h1 { font-size: 22px; font-weight: 500; margin: 4px 0 0; }
  .header-meta { font-size: 12px; color: var(--text-3); text-transform: uppercase; letter-spacing: 0.05em; }
  .stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin: 24px 0; }
  @media (max-width: 600px) { .stats { grid-template-columns: repeat(2, 1fr); } }
  .stat { background: var(--bg-2); border-radius: 10px; padding: 14px; }
  .stat-label { font-size: 12px; color: var(--text-2); }
  .stat-value { font-size: 20px; font-weight: 500; margin-top: 6px; }
  .stat-sub { font-size: 11px; margin-top: 2px; }
  .green { color: var(--green); }
  .red { color: var(--red); }
  .section-label { font-size: 14px; color: var(--text-2); margin: 0 0 8px; }
  .section-title { font-size: 16px; font-weight: 500; margin: 32px 0 12px; }
  .toggle-group { display: inline-flex; border: 0.5px solid var(--border); border-radius: 8px; overflow: hidden; font-size: 11px; }
  .toggle-group button { border: none; background: transparent; padding: 4px 10px; cursor: pointer; font-family: inherit; color: var(--text-2); }
  .toggle-group button + button { border-left: 0.5px solid var(--border); }
  .toggle-group button.active { background: var(--bg-2); color: var(--text); font-weight: 500; }
  .chart-row { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 8px; }
  .chart-container { position: relative; width: 100%; height: 220px; }
  .holding-card { border: 0.5px solid var(--border); border-radius: 10px; padding: 14px 16px; margin-bottom: 10px; background: white; }
  .holding-head { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 4px; gap: 12px; }
  .holding-title { font-weight: 500; font-size: 15px; }
  .holding-shares { font-size: 12px; color: var(--text-3); margin-left: 8px; }
  .holding-name { font-size: 12px; color: var(--text-3); margin-top: 2px; }
  .holding-price { font-weight: 500; font-size: 15px; text-align: right; }
  .holding-mini-chart { position: relative; width: 100%; height: 100px; margin: 8px 0; }
  .holding-line-note { font-size: 11px; color: var(--text-3); margin-bottom: 8px; }
  .holding-line-note .dash { display: inline-block; width: 14px; border-top: 1px dashed rgba(0,0,0,0.45); vertical-align: middle; margin-right: 4px; }
  .holding-stats { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 8px; font-size: 12px; }
  .holding-stat-label { color: var(--text-3); font-size: 11px; }
  .holding-stat-val { font-weight: 500; }
  .news-row { margin-top: 10px; padding-top: 10px; border-top: 0.5px solid var(--border); display: flex; gap: 8px; align-items: flex-start; font-size: 12px; }
  .news-src { color: var(--text-3); white-space: nowrap; }
  .news-text { color: var(--text-2); }
  .catalyst { display: flex; gap: 8px; padding: 10px 12px; border: 0.5px solid var(--border); border-radius: 8px; margin-bottom: 8px; align-items: center; font-size: 13px; background: white; }
  .catalyst-tag { padding: 2px 8px; border-radius: 6px; font-size: 11px; font-weight: 500; min-width: 56px; text-align: center; }
  .tag-earnings { background: var(--amber-bg); color: var(--amber-fg); }
  .tag-pdufa { background: var(--red-tag-bg); color: var(--red-tag-fg); }
  .tag-data { background: var(--purple-bg); color: var(--purple-fg); }
  .footer { margin-top: 40px; padding-top: 20px; border-top: 0.5px solid var(--border); font-size: 11px; color: var(--text-3); }
</style>
</head>
<body>
<div class="container">

  <p class="header-meta">Vanguard Roth IRA · {{ today_str }} · 5:00 PM ET</p>
  <h1>Daily close</h1>

  <div class="stats">
    <div class="stat">
      <div class="stat-label">Portfolio value</div>
      <div class="stat-value">${{ '{:,.0f}'.format(portfolio_value) }}</div>
    </div>
    <div class="stat">
      <div class="stat-label">Contributed</div>
      <div class="stat-value">${{ '{:,.0f}'.format(contributions) }}</div>
    </div>
    <div class="stat">
      <div class="stat-label">Investment returns</div>
      <div class="stat-value {{ returns_class }}">{{ returns_sign }}${{ '{:,.0f}'.format(returns_dollar|abs) }}</div>
      <div class="stat-sub {{ returns_class }}">{{ returns_sign }}{{ '%.2f'|format(returns_pct|abs) }}%</div>
    </div>
    <div class="stat">
      <div class="stat-label">Day</div>
      <div class="stat-value {{ day_class }}">{{ day_sign }}${{ '{:,.0f}'.format(day_dollar|abs) }}</div>
      <div class="stat-sub {{ day_class }}">{{ day_sign }}{{ '%.2f'|format(day_pct|abs) }}%</div>
    </div>
  </div>

  <div class="chart-row">
    <p class="section-label">Investment returns</p>
    <div class="toggle-group" data-target="returns">
      <button data-range="all" class="active">All</button>
      <button data-range="1y">1Y</button>
      <button data-range="30d">30D</button>
    </div>
  </div>
  <div class="chart-container">
    <canvas id="returnsChart"></canvas>
  </div>

  <h2 class="section-title">Holdings</h2>
  <div id="holdings-container"></div>

  {% if catalysts %}
  <h2 class="section-title">Catalysts (next 60 days)</h2>
  {% for c in catalysts %}
    <div class="catalyst">
      <div class="catalyst-tag tag-{{ c.kind if c.kind in ['earnings','pdufa','data'] else 'data' }}">{{ c.date_str }}</div>
      <div style="font-weight: 500; min-width: 50px;">{{ c.ticker }}</div>
      <div style="color: var(--text-2);">{{ c.desc }}</div>
    </div>
  {% endfor %}
  {% endif %}

  <div class="footer">
    Generated {{ generated_at }} · Prices via Yahoo Finance · News via Claude with web search.
  </div>
</div>

<script>
const RETURNS_DATA = {{ returns_data_json|safe }};
const HOLDINGS = {{ holdings_json|safe }};
const NEWS = {{ news_json|safe }};

// ---------- Returns chart ----------
function returnsForRange(range) {
  const all = RETURNS_DATA;
  if (range === 'all') return all;
  const cutoffDays = range === '1y' ? 365 : 30;
  const cutoff = new Date(); cutoff.setDate(cutoff.getDate() - cutoffDays);
  return all.filter(p => new Date(p.d) >= cutoff);
}

const breakEvenPlugin = {
  id: 'breakEven',
  afterDatasetsDraw: (chart) => {
    const y = chart.scales.y.getPixelForValue(0);
    const ctx = chart.ctx;
    ctx.save();
    ctx.beginPath();
    ctx.setLineDash([4, 3]);
    ctx.strokeStyle = 'rgba(0,0,0,0.4)';
    ctx.lineWidth = 1;
    ctx.moveTo(chart.chartArea.left, y);
    ctx.lineTo(chart.chartArea.right, y);
    ctx.stroke();
    ctx.restore();
  }
};
Chart.register(breakEvenPlugin);

let returnsChart;
function renderReturnsChart(range) {
  const data = returnsForRange(range);
  if (returnsChart) returnsChart.destroy();
  returnsChart = new Chart(document.getElementById('returnsChart'), {
    type: 'line',
    data: {
      labels: data.map(p => p.d),
      datasets: [{
        data: data.map(p => p.v),
        backgroundColor: (ctx) => {
          const chart = ctx.chart;
          const {ctx: c, chartArea} = chart;
          if (!chartArea) return 'rgba(163,45,45,0.15)';
          const zeroY = chart.scales.y.getPixelForValue(0);
          const top = chartArea.top, bottom = chartArea.bottom;
          const zeroPct = Math.max(0.001, Math.min(0.999, (zeroY - top) / (bottom - top)));
          const g = c.createLinearGradient(0, top, 0, bottom);
          g.addColorStop(0, 'rgba(29, 158, 117, 0.18)');
          g.addColorStop(zeroPct - 0.001, 'rgba(29, 158, 117, 0.18)');
          g.addColorStop(zeroPct + 0.001, 'rgba(163, 45, 45, 0.18)');
          g.addColorStop(1, 'rgba(163, 45, 45, 0.18)');
          return g;
        },
        segment: {
          borderColor: ctx => (ctx.p0.parsed.y < 0 || ctx.p1.parsed.y < 0) ? '#A32D2D' : '#1D9E75'
        },
        fill: { target: { value: 0 } },
        tension: 0.3,
        pointRadius: 0,
        borderWidth: 2,
        borderColor: '#1D9E75'
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      layout: { padding: { top: 16, bottom: 12 } },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: c => (c.parsed.y >= 0 ? '+$' : '−$') + Math.abs(c.parsed.y).toLocaleString(undefined, {maximumFractionDigits: 0})
          }
        }
      },
      scales: {
        x: {
          grid: { display: false },
          ticks: {
            font: { size: 10 },
            color: '#888',
            maxTicksLimit: 3,
            maxRotation: 0,
            autoSkip: true,
            callback: function(val, idx, ticks) {
              const lbl = this.getLabelForValue(val);
              const d = new Date(lbl);
              if (isNaN(d.getTime())) return lbl;
              const month = d.toLocaleString('en-US', { month: 'short' });
              const yr = String(d.getFullYear()).slice(2);
              return `${month} '${yr}`;
            }
          }
        },
        y: {
          grid: { color: 'rgba(0,0,0,0.06)' },
          ticks: {
            font: { size: 10 },
            color: '#888',
            maxTicksLimit: 4,
            callback: v => {
              const sign = v >= 0 ? '+$' : '−$';
              const abs = Math.abs(v);
              return sign + (abs >= 1000 ? (abs/1000).toFixed(1)+'k' : abs.toFixed(0));
            }
          }
        }
      }
    }
  });
}
renderReturnsChart('all');

// Toggle wiring for returns chart
document.querySelectorAll('.toggle-group[data-target="returns"] button').forEach(btn => {
  btn.addEventListener('click', () => {
    const grp = btn.parentElement;
    grp.querySelectorAll('button').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    renderReturnsChart(btn.dataset.range);
  });
});

// ---------- Holdings cards ----------
const verticalLinePlugin = {
  id: 'verticalLine',
  afterDatasetsDraw: (chart, args, opts) => {
    const xIdx = opts.xIndex;
    if (xIdx === undefined || xIdx === null || xIdx < 0) return;
    const x = chart.scales.x.getPixelForValue(xIdx);
    const ctx = chart.ctx;
    ctx.save();
    ctx.beginPath();
    ctx.setLineDash([3, 3]);
    ctx.strokeStyle = 'rgba(0,0,0,0.45)';
    ctx.lineWidth = 1;
    ctx.moveTo(x, chart.chartArea.top);
    ctx.lineTo(x, chart.chartArea.bottom);
    ctx.stroke();
    const data = chart.data.datasets[0].data;
    if (data[xIdx] !== undefined) {
      const y = chart.scales.y.getPixelForValue(data[xIdx]);
      ctx.setLineDash([]);
      ctx.beginPath();
      ctx.arc(x, y, 3.5, 0, 2*Math.PI);
      ctx.fillStyle = 'rgba(0,0,0,0.6)';
      ctx.fill();
    }
    ctx.restore();
  }
};
Chart.register(verticalLinePlugin);

function fmtMoney(n) {
  const sign = n >= 0 ? '+$' : '−$';
  return sign + Math.abs(n).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
}
function fmtPct(n) {
  const sign = n >= 0 ? '+' : '−';
  return sign + Math.abs(n).toFixed(2) + '%';
}

function holdingHTML(h) {
  const up = h.total_return >= 0;
  const dayClass = h.day_pct >= 0 ? 'green' : 'red';
  const totalClass = up ? 'green' : 'red';
  const news = NEWS[h.ticker];
  const newsHTML = news ? `<div class="news-row"><span class="news-src">${news.src}</span><span class="news-text">${news.text}</span></div>` : '';
  return `
    <div class="holding-card">
      <div class="holding-head">
        <div>
          <span class="holding-title">${h.ticker}</span>
          <span class="holding-shares">${h.shares.toLocaleString(undefined, {maximumFractionDigits: 2})} sh</span>
          <div class="holding-name">${h.name}</div>
        </div>
        <div style="text-align: right; display: flex; gap: 12px; align-items: flex-start;">
          <div>
            <div class="holding-price">$${h.price.toFixed(2)}</div>
            <div class="${dayClass}" style="font-size: 12px;">${fmtPct(h.day_pct)} today</div>
          </div>
          <div class="toggle-group" data-target="${h.ticker}" style="font-size: 10px;">
            <button data-range="all">All</button>
            <button data-range="1y" class="active">1Y</button>
            <button data-range="30d">30D</button>
          </div>
        </div>
      </div>
      <div class="holding-mini-chart"><canvas id="hc-${h.ticker}"></canvas></div>
      <div class="holding-line-note"><span class="dash"></span>Avg cost $${h.avg_cost.toFixed(2)} · entry marker</div>
      <div class="holding-stats">
        <div><div class="holding-stat-label">30-day</div><div class="holding-stat-val ${h.d30_pct >= 0 ? 'green' : 'red'}">${fmtPct(h.d30_pct)}</div></div>
        <div><div class="holding-stat-label">Total return</div><div class="holding-stat-val ${totalClass}">${fmtMoney(h.total_return)} (${fmtPct(h.total_pct)})</div></div>
        <div style="text-align: right;"><div class="holding-stat-label">Value</div><div class="holding-stat-val">$${h.value.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}</div></div>
      </div>
      ${newsHTML}
    </div>
  `;
}

document.getElementById('holdings-container').innerHTML = HOLDINGS.map(holdingHTML).join('');

// Find the index in the series closest to the avg-cost-weighted purchase date
function findEntryIdx(series, purchaseDate) {
  if (!series || series.length === 0 || !purchaseDate) return -1;
  // Find the index where the date is >= purchase_date (first chart point on or after entry)
  for (let i = 0; i < series.length; i++) {
    if (series[i].d >= purchaseDate) return i;
  }
  // Purchase happened after the chart window — return -1 so no line is drawn
  return -1;
}

const holdingCharts = {};
function renderHoldingChart(h, range) {
  const series = range === 'all' ? h.history_all : (range === '30d' ? h.history_30d : h.history_1y);
  const up = h.total_return >= 0;
  const color = up ? '#1D9E75' : '#A32D2D';
  const fillColor = up ? 'rgba(29,158,117,0.15)' : 'rgba(163,45,45,0.15)';
  const entryIdx = findEntryIdx(series, h.purchase_date);
  if (holdingCharts[h.ticker]) holdingCharts[h.ticker].destroy();
  holdingCharts[h.ticker] = new Chart(document.getElementById('hc-' + h.ticker), {
    type: 'line',
    data: { labels: series.map(p => p.d), datasets: [{ data: series.map(p => p.p), borderColor: color, backgroundColor: fillColor, fill: true, tension: 0.3, pointRadius: 0, borderWidth: 1.5 }] },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { display: false }, tooltip: { enabled: false }, verticalLine: { xIndex: entryIdx } },
      scales: {
        x: {
          display: true,
          grid: { display: false },
          border: { display: false },
          ticks: {
            font: { size: 9 },
            color: '#999',
            maxTicksLimit: 3,
            maxRotation: 0,
            autoSkip: true,
            callback: function(val) {
              const lbl = this.getLabelForValue(val);
              const d = new Date(lbl);
              if (isNaN(d.getTime())) return '';
              const month = d.toLocaleString('en-US', { month: 'short' });
              const yr = String(d.getFullYear()).slice(2);
              return `${month} '${yr}`;
            }
          }
        },
        y: {
          display: true,
          position: 'right',
          grid: { display: false },
          border: { display: false },
          ticks: {
            font: { size: 9 },
            color: '#999',
            maxTicksLimit: 3,
            callback: v => '$' + (v >= 100 ? v.toFixed(0) : v.toFixed(2))
          }
        }
      }
    }
  });
}

HOLDINGS.forEach(h => renderHoldingChart(h, '1y'));

document.querySelectorAll('.toggle-group:not([data-target="returns"]) button').forEach(btn => {
  btn.addEventListener('click', () => {
    const grp = btn.parentElement;
    const ticker = grp.dataset.target;
    grp.querySelectorAll('button').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    const h = HOLDINGS.find(x => x.ticker === ticker);
    renderHoldingChart(h, btn.dataset.range);
  });
});
</script>
</body>
</html>
"""


def render_dashboard(summary, holdings, returns_curve, news, catalysts):
    template = Template(DASHBOARD_TEMPLATE)
    html = template.render(
        today_str=TODAY.strftime("%a %b %-d, %Y"),
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M ET"),
        portfolio_value=summary["portfolio_value"],
        contributions=TOTAL_CONTRIBUTIONS,
        returns_dollar=summary["returns_dollar"],
        returns_pct=summary["returns_pct"],
        returns_sign=summary["returns_sign"],
        returns_class=summary["returns_class"],
        day_dollar=summary["day_dollar"],
        day_pct=summary["day_pct"],
        day_sign=summary["day_sign"],
        day_class=summary["day_class"],
        catalysts=catalysts,
        returns_data_json=json.dumps(returns_curve),
        holdings_json=json.dumps(holdings, default=str),
        news_json=json.dumps(news),
    )
    (OUT_DIR / "index.html").write_text(html)
    print(f"  Dashboard written to {OUT_DIR}/index.html")


# ---------------- Summary ----------------
def build_summary(held, returns_curve):
    portfolio_value = sum(h["value"] for h in held)
    day_dollar = sum(h["day_dollar"] for h in held)
    prev_value = portfolio_value - day_dollar
    day_pct = (day_dollar / prev_value) * 100 if prev_value else 0

    returns_dollar = portfolio_value - TOTAL_CONTRIBUTIONS
    returns_pct = (returns_dollar / TOTAL_CONTRIBUTIONS) * 100 if TOTAL_CONTRIBUTIONS else 0

    # 30-day: portfolio value 30 days ago vs now
    total_30d_ago = 0
    for h in held:
        if h["history_30d"]:
            total_30d_ago += h["shares"] * h["history_30d"][0]["p"]
    d30_dollar = portfolio_value - total_30d_ago if total_30d_ago else 0
    d30_pct = (d30_dollar / total_30d_ago) * 100 if total_30d_ago else 0

    # Top/worst movers
    top = max(held, key=lambda h: h["day_pct"]) if held else None
    worst = min(held, key=lambda h: h["day_pct"]) if held else None

    return {
        "portfolio_value": portfolio_value,
        "day_dollar": day_dollar,
        "day_pct": day_pct,
        "day_sign": "+" if day_dollar >= 0 else "−",
        "day_class": "green" if day_dollar >= 0 else "red",
        "d30_dollar": d30_dollar,
        "d30_pct": d30_pct,
        "d30_sign": "+" if d30_dollar >= 0 else "−",
        "returns_dollar": returns_dollar,
        "returns_pct": returns_pct,
        "returns_sign": "+" if returns_dollar >= 0 else "−",
        "returns_class": "green" if returns_dollar >= 0 else "red",
        "top_mover": f"{top['ticker']} {top['day_pct']:+.2f}%" if top else "—",
        "worst_mover": f"{worst['ticker']} {worst['day_pct']:+.2f}%" if worst else "—",
    }


# ---------------- Main ----------------
def main():
    print(f"Roth IRA Dashboard — {TODAY}")
    print("Fetching prices...")
    prices = fetch_prices()
    print("Computing holdings...")
    held = compute_held(prices)
    print("Building returns curve...")
    returns_curve = compute_returns_curve(held)
    print("Fetching news + macro...")
    news = fetch_news_and_macro(HELD_TICKERS)
    print("Building summary...")
    summary = build_summary(held, returns_curve)
    summary["news"] = news
    catalysts = upcoming_catalysts()
    summary["catalysts"] = catalysts
    print("Rendering dashboard...")
    render_dashboard(summary, held, returns_curve, news, catalysts)
    print("Sending email...")
    send_email(summary, DASHBOARD_URL)
    print("Done.")


if __name__ == "__main__":
    main()
