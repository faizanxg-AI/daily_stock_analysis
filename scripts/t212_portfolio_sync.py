#!/usr/bin/env python3
"""
Trading212 Portfolio → DSA Stock List Sync
Fetches live portfolio positions from Trading212 API and converts tickers
to Daily Stock Analysis format. Falls back to a default watchlist when
the portfolio is empty.

Usage:
    T212_API_KEY=*** T212_API_SECRET=*** python3 t212_portfolio_sync.py

Output: comma-separated stock codes (e.g. "AAPL,MSFT,NVDA")
"""

from __future__ import annotations

import os
import sys
import json
import base64
import urllib.request
import urllib.error

# ── Default fallback watchlist (used when portfolio is empty) ──
DEFAULT_STOCKS = [
    "AAPL", "MSFT", "NVDA", "TSLA", "GOOGL",
    "AMZN", "META", "AMD", "NFLX", "PLTR"
]

# ── Ticker conversion: Trading212 format → DSA format ──
# Trading212 uses: SYMBOL_COUNTRY_TYPE (e.g. AAPL_US_EQ)
# DSA expects: plain symbol for US, hk00XXX for HK, etc.
def t212_to_dsa(ticker: str) -> str | None:
    """Convert Trading212 ticker to DSA-compatible stock code."""
    parts = ticker.split("_")
    if len(parts) < 3:
        return None

    symbol, country, instrument_type = parts[0], parts[1], parts[2]

    # US equities: just the symbol
    if country == "US" and instrument_type == "EQ":
        return symbol

    # UK equities: symbol with .L suffix (not fully supported by DSA but kept)
    if country == "UK" and instrument_type == "EQ":
        return f"{symbol}.L"

    # European equities: skip (DSA doesn't support most EU exchanges)
    if country in ("DE", "FR", "NL", "ES", "IT", "SE", "CH"):
        # Return None — DSA doesn't handle these well
        return None

    # ETFs: keep as-is with _US_EQ suffix stripped
    if instrument_type == "ETF":
        return symbol

    # Unknown — skip
    return None


def get_auth_header() -> str:
    """Build Basic auth header from env vars."""
    api_key = os.environ.get("T212_API_KEY", "")
    api_secret = os.environ.get("T212_API_SECRET", "")
    if not api_key or not api_secret:
        print("ERROR: T212_API_KEY and T212_API_SECRET must be set", file=sys.stderr)
        sys.exit(1)
    credentials = f"{api_key}:{api_secret}"
    encoded = base64.b64encode(credentials.encode("utf-8")).decode("utf-8")
    return f"Basic {encoded}"


def api_get(path: str, auth: str) -> dict | list:
    """Make an authenticated GET request to Trading212 API."""
    url = f"https://live.trading212.com/api/v0{path}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", auth)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        print(f"API error {e.code} on {path}: {e.reason}", file=sys.stderr)
        return {} if "portfolio" not in path else []
    except Exception as e:
        print(f"Request failed on {path}: {e}", file=sys.stderr)
        return {} if "portfolio" not in path else []


def get_portfolio_tickers(auth: str) -> list[str]:
    """Fetch all tickers from open positions and pies."""
    tickers = set()

    # 1. Open positions (direct portfolio)
    positions = api_get("/equity/portfolio", auth)
    if isinstance(positions, list):
        for pos in positions:
            ticker = pos.get("ticker", "")
            if ticker:
                tickers.add(ticker)

    # 2. Pie holdings (T212 portfolio buckets)
    try:
        pies = api_get("/equity/pies", auth)
        if isinstance(pies, list):
            for pie in pies:
                pie_id = pie.get("id")
                if not pie_id:
                    continue
                # Get detailed pie info with holdings
                pie_detail = api_get(f"/equity/pies/{pie_id}", auth)
                if isinstance(pie_detail, dict):
                    instruments = pie_detail.get("instrumentShares", {})
                    for ticker in instruments:
                        if instruments[ticker] > 0:  # Only include if holding > 0
                            tickers.add(ticker)
    except Exception as e:
        print(f"Pie fetch warning: {e}", file=sys.stderr)

    return sorted(tickers)


def main():
    auth = get_auth_header()

    # Fetch portfolio
    raw_tickers = get_portfolio_tickers(auth)

    # Convert to DSA format
    dsa_codes = []
    skipped = []
    for ticker in raw_tickers:
        code = t212_to_dsa(ticker)
        if code:
            dsa_codes.append(code)
        else:
            skipped.append(ticker)

    if skipped:
        print(f"Skipped unsupported tickers: {', '.join(skipped)}", file=sys.stderr)

    # Fall back to defaults if nothing to track
    if not dsa_codes:
        print(f"Portfolio empty — using default watchlist", file=sys.stderr)
        dsa_codes = DEFAULT_STOCKS
    else:
        print(f"Synced {len(dsa_codes)} stocks from T212 portfolio", file=sys.stderr)

    # Output the stock list
    print(",".join(dsa_codes))


if __name__ == "__main__":
    main()
