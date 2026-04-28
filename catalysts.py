"""
Upcoming biotech catalysts — earnings dates, FDA PDUFA decisions, and clinical data readouts.
The script filters this list to show only events within the next 60 days.
Update this file when new catalysts are announced or after events pass.

Each entry: (date, ticker, kind, description)
  date: 'YYYY-MM-DD' for known dates, or 'YYYY-Qn' for quarter-only estimates
  kind: 'earnings' | 'pdufa' | 'data' | 'other'
"""

CATALYSTS = [
    # --- AQST Aquestive ---
    ("2026-05-11", "AQST", "earnings",
     "Q1 2026 earnings — first quarter post-CRL on Anaphylm; watch for resubmission timing and Libervant Q1 sales"),

    # --- IOVA Iovance ---
    ("2026-05-07", "IOVA", "earnings",
     "Q1 2026 earnings — Amtagvi sales ramp watch; conference call 8:30 AM ET"),

    # --- SGMO Sangamo ---
    ("2026-05-15", "SGMO", "earnings",
     "Q1 2026 earnings (estimated) — watch for Fabry BLA progress and partnership updates"),

    # --- STRO Sutro Biopharma ---
    ("2026-05-07", "STRO", "earnings",
     "Q1 2026 earnings (estimated) — luvelta out-licensing progress; cash runway"),

    # --- UNCY Unicycive ---
    ("2026-06-29", "UNCY", "pdufa",
     "PDUFA target date — oxylanthanum carbonate (OLC) for hyperphosphatemia in CKD on dialysis"),
    ("2026-05-15", "UNCY", "earnings",
     "Q1 2026 earnings (estimated) — pre-PDUFA commercial launch readiness"),
]
