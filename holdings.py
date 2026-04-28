"""
Roth IRA holdings — source of truth.
Update this file when buying/selling. Each entry: (ticker, shares, total_cost_basis).
Cost basis is total dollars invested in remaining shares (FIFO after any sales).

Closed positions stay listed with shares=0 so realized P&L can show on the returns chart.
"""

# Currently held positions
# Tuple format: (ticker, shares, cost_basis_total, name, avg_cost_weighted_purchase_date)
# avg_cost_weighted_purchase_date = the dollar-weighted center-of-mass of all purchases (post-FIFO sells, post-split)
HOLDINGS = [
    ("VOO",  9.129,   5693.35, "Vanguard S&P 500 ETF",         "2025-12-24"),
    ("SPY",  4.000,   2737.04, "SPDR S&P 500 ETF Trust",       "2026-01-21"),
    ("AQST", 386.000, 1324.88, "Aquestive Therapeutics",       "2025-08-12"),
    ("IOVA", 146.000,  783.40, "Iovance Biotherapeutics",      "2025-02-09"),
    ("SGMO", 89.000,    99.24, "Sangamo Therapeutics",         "2025-02-13"),
    ("STRO", 117.000, 2613.88, "Sutro Biopharma",              "2024-12-19"),
    ("UNCY", 263.000, 1611.23, "Unicycive Therapeutics",       "2026-02-05"),
]

# Closed positions — for the returns chart annotations
# (ticker, close_date, realized_pnl, note)
CLOSED_POSITIONS = [
    ("LAC",  "2024-12-12",    16.06, "Lithium Americas"),
    ("VKTX", "2026-02-02",  -283.10, "Viking Therapeutics"),
]

# Material partial closes (kept ticker but sold a chunk) — also annotated on chart
PARTIAL_CLOSES = [
    ("STRO", "2026-02-11", -3368.00, "Sutro partial sale post-split"),
    ("AQST", "2026-02-03",   134.25, "Aquestive partial sale"),
]

# Total contributions to the Roth IRA (used to compute investment returns vs. money in)
TOTAL_CONTRIBUTIONS = 18000.00

# Email destination
EMAIL_TO = "williamczec@gmail.com"
EMAIL_FROM = "Roth Bot <onboarding@resend.dev>"

# Dashboard URL — will be filled in after first deploy
# Format: https://zec13.github.io/roth-ira-dashboard/
DASHBOARD_URL = "https://zec13.github.io/roth-ira-dashboard/"
