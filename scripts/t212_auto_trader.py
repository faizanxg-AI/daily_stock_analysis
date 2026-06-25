#!/usr/bin/env python3
"""
T212 Autonomous Portfolio Manager
===================================
Reads DSA daily analysis, extracts buy/sell decisions with confidence scores,
and automatically manages a Trading212 portfolio:
- Buys stocks predicted to gain (confidence-weighted allocation)
- Sells stocks predicted to decline
- Adjusts holds based on updated confidence
- Tracks performance for next-day review

Usage:
    T212_API_KEY=*** T212_API_SECRET=*** python3 scripts/t212_auto_trader.py [--dry-run]
"""

from __future__ import annotations

import os
import sys
import json
import base64
import hashlib
import re
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ── Configuration ──────────────────────────────────────────────────
MAX_POSITIONS = int(os.environ.get("T212_MAX_POSITIONS", "5"))
MAX_ALLOCATION_PCT = float(os.environ.get("T212_MAX_ALLOCATION_PCT", "0.25"))
CASH_BUFFER_PCT = float(os.environ.get("T212_CASH_BUFFER_PCT", "0.10"))
MIN_SCORE_THRESHOLD = int(os.environ.get("T212_MIN_SCORE", "40"))
MIN_TRADE_VALUE = float(os.environ.get("T212_MIN_TRADE_VALUE", "2.0"))
PERFORMANCE_LOG = "data/t212_performance.json"
DRY_RUN = "--dry-run" in sys.argv

# ── T212 API helpers ───────────────────────────────────────────────

def get_auth() -> str:
    ak = os.environ.get("T212_API_KEY", "")
    sk = os.environ.get("T212_API_SECRET", "")
    if not ak or not sk:
        print("FATAL: T212_API_KEY and T212_API_SECRET required", file=sys.stderr)
        sys.exit(1)
    creds = f"{ak}:{sk}"
    return f"Basic {base64.b64encode(creds.encode()).decode()}"


def t212_get(path: str, auth: str) -> dict | list:
    """GET request to Trading212 API."""
    url = f"https://live.trading212.com/api/v0{path}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", auth)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429:  # Rate limit
                time.sleep(2 ** attempt)
                continue
            print(f"T212 GET {path}: HTTP {e.code}", file=sys.stderr)
            return {} if "portfolio" in path else []
        except Exception as e:
            print(f"T212 GET {path}: {e}", file=sys.stderr)
            return {} if "portfolio" in path else []
    return {}


def t212_post(path: str, auth: str, body: dict) -> dict:
    """POST request to Trading212 API (place orders)."""
    if DRY_RUN:
        print(f"  [DRY RUN] Would POST {path}: {json.dumps(body)}")
        return {"id": f"dry-{hashlib.md5(str(body).encode()).hexdigest()[:8]}"}

    url = f"https://live.trading212.com/api/v0{path}"
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", auth)
    req.add_header("Content-Type", "application/json")
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read().decode())
                return result
        except urllib.error.HTTPError as e:
            body_text = e.read().decode() if e.fp else ""
            print(f"T212 POST {path}: HTTP {e.code} — {body_text[:200]}", file=sys.stderr)
            if e.code == 429:
                time.sleep(2 ** attempt)
                continue
            return {"error": str(e.code), "body": body_text}
        except Exception as e:
            print(f"T212 POST {path}: {e}", file=sys.stderr)
            return {"error": str(e)}
    return {}


# ── DSA Report Parser ──────────────────────────────────────────────

def parse_dsa_report(report_path: str) -> list[dict]:
    """Parse the DSA markdown report into structured decisions."""
    if not os.path.exists(report_path):
        print(f"Report not found: {report_path}", file=sys.stderr)
        return []

    with open(report_path) as f:
        text = f.read()

    decisions = []

    # Parse the summary section — format:
    # 🟡 **Company Name(SYMBOL)**: Action text | Score NN | Trend
    summary_pattern = re.compile(
        r'\*\*[^*]+\((\w+)\)\*\*:\s*(.+?)\s*\|\s*Score\s+(\d+)\s*\|\s*(Bullish|Bearish|Neutral|Strongly Bullish|Strongly Bearish)',
        re.IGNORECASE
    )

    for match in summary_pattern.finditer(text):
        symbol = match.group(1)
        action_text = match.group(2).strip().lower()
        score = int(match.group(3))
        trend_raw = match.group(4).strip()
        # Normalize trend
        trend = trend_raw.replace("Strongly ", "").capitalize()

        # Normalize action
        if "sell" in action_text:
            action = "Sell"
        elif "reduce" in action_text:
            action = "Reduce"
        elif "buy" in action_text:
            action = "Buy"
        elif "watch" in action_text:
            action = "Watch"
        elif "hold" in action_text:
            action = "Hold"
        else:
            action = "Hold"

        # Override: if action is "Watch", treat as "Hold" for trading (still monitor)
        # But "Buy" and "Sell" are explicit

        decisions.append({
            "symbol": symbol,
            "action": action,
            "score": score,
            "trend": trend,
        })

    return decisions


# ── Ticker mapping ──────────────────────────────────────────────────

# Cache for instrument lookup
_instrument_cache: dict[str, str] | None = None


def load_instruments(auth: str) -> dict[str, str]:
    """Load all T212 instruments and build symbol→ticker map."""
    global _instrument_cache
    if _instrument_cache is not None:
        return _instrument_cache

    print("Loading T212 instrument catalog...", file=sys.stderr)
    instruments = t212_get("/equity/metadata/instruments", auth)
    if isinstance(instruments, dict):  # error
        instruments = []

    _instrument_cache = {}
    for inst in instruments:
        ticker = inst.get("ticker", "")
        # For US equities, map AAPL → AAPL_US_EQ
        if "_US_EQ" in ticker:
            symbol = ticker.split("_")[0]
            _instrument_cache[symbol] = ticker
        # Also store by full ticker for lookup
        _instrument_cache[ticker] = ticker

    print(f"  Loaded {len(_instrument_cache)} instruments", file=sys.stderr)
    return _instrument_cache


def symbol_to_ticker(symbol: str, auth: str) -> str | None:
    """Convert DSA symbol (AAPL) to T212 ticker (AAPL_US_EQ)."""
    instruments = load_instruments(auth)
    # Direct lookup
    if symbol in instruments:
        return instruments[symbol]
    # Try with _US_EQ suffix
    candidate = f"{symbol}_US_EQ"
    if candidate in instruments:
        return candidate
    print(f"  ⚠️  Ticker not found for {symbol}", file=sys.stderr)
    return None


# ── Position Sizing ─────────────────────────────────────────────────

def calculate_allocations(
    decisions: list[dict],
    current_positions: dict[str, dict],
    available_cash: float,
    total_portfolio_value: float,
) -> list[dict]:
    """
    Calculate target allocations based on confidence scores.

    Returns list of {symbol, ticker, target_pct, target_value, action, current_value}
    """
    # Filter: only buy/watch with score >= threshold
    buyable = [d for d in decisions if d["score"] >= MIN_SCORE_THRESHOLD
               and d["action"] in ("Buy", "Watch", "Hold")]

    # Sort by score descending
    buyable.sort(key=lambda d: d["score"], reverse=True)

    # Top N picks
    picks = buyable[:MAX_POSITIONS]
    if not picks:
        print("No stocks meet buy threshold.", file=sys.stderr)
        return []

    total_score = sum(p["score"] for p in picks)
    tradeable_cash = available_cash * (1 - CASH_BUFFER_PCT)

    allocations = []
    for pick in picks:
        symbol = pick["symbol"]
        weight = pick["score"] / total_score if total_score > 0 else 1.0 / len(picks)
        allocation_pct = min(weight, MAX_ALLOCATION_PCT)
        target_value = tradeable_cash * allocation_pct

        current = current_positions.get(symbol, {})
        current_value = current.get("value", 0.0)

        allocations.append({
            "symbol": symbol,
            "ticker": current.get("ticker", ""),  # Will be resolved later
            "score": pick["score"],
            "action": pick["action"],
            "trend": pick["trend"],
            "weight": round(weight, 3),
            "allocation_pct": round(allocation_pct * 100, 1),
            "target_value": round(target_value, 2),
            "current_value": round(current_value, 2),
            "delta": round(target_value - current_value, 2),
        })

    return allocations


# ── Order Execution ─────────────────────────────────────────────────

def execute_rebalance(
    allocations: list[dict],
    auth: str,
    positions: dict[str, dict],
) -> list[dict]:
    """Execute buy/sell orders to reach target allocations."""
    results = []
    instruments = load_instruments(auth)

    # Step 1: Sell stocks that DSA says to sell, or that are over-allocated
    sells = [d for d in allocations if d["action"] in ("Sell", "Reduce")]
    # Also sell positions not in the top picks
    held_symbols = set(positions.keys())
    target_symbols = {a["symbol"] for a in allocations}
    sells_extra = held_symbols - target_symbols

    for symbol in sells_extra:
        pos = positions.get(symbol, {})
        qty = pos.get("quantity", 0)
        if qty > 0:
            ticker = pos.get("ticker", f"{symbol}_US_EQ")
            print(f"  Selling {symbol} (not in top picks) — {qty} shares", file=sys.stderr)
            result = t212_post("/equity/orders/market", auth, {
                "ticker": ticker,
                "quantity": -float(qty)  # Negative for sell
            })
            results.append({"symbol": symbol, "type": "SELL", "quantity": qty, "result": result})
            time.sleep(0.3)  # Rate limit

    for alloc in allocations:
        symbol = alloc["symbol"]
        ticker = alloc["ticker"]

        if not ticker:
            # Resolve ticker
            ticker = symbol_to_ticker(symbol, auth)
            if not ticker:
                results.append({"symbol": symbol, "type": "SKIP", "reason": "ticker not found"})
                continue
            alloc["ticker"] = ticker

        delta = alloc["delta"]
        current_value = alloc["current_value"]

        # Determine action
        if alloc["action"] in ("Sell", "Reduce"):
            if current_value > 0 and symbol in positions:
                pos = positions[symbol]
                qty = pos.get("quantity", 0)
                sell_qty = qty if alloc["action"] == "Sell" else qty * 0.5
                if sell_qty > 0:
                    print(f"  {alloc['action']} {symbol} — {sell_qty} shares (${current_value:.2f})",
                          file=sys.stderr)
                    result = t212_post("/equity/orders/market", auth, {
                        "ticker": ticker,
                        "quantity": -float(sell_qty)
                    })
                    results.append({"symbol": symbol, "type": alloc["action"].upper(),
                                    "quantity": sell_qty, "value": current_value, "result": result})
                    time.sleep(0.3)
            continue

        # Buy if under-allocated
        if delta > MIN_TRADE_VALUE:
            print(f"  BUY {symbol} — target ${alloc['target_value']:.2f}, "
                  f"current ${current_value:.2f}, delta ${delta:.2f}",
                  file=sys.stderr)
            result = t212_post("/equity/orders/market", auth, {
                "ticker": ticker,
                "quantity": delta  # T212 accepts value-based orders for fractional shares
            })
            results.append({"symbol": symbol, "type": "BUY",
                            "value": delta, "result": result})
            time.sleep(0.3)

        elif delta < -MIN_TRADE_VALUE:
            # Over-allocated — trim
            trim_value = abs(delta)
            pos = positions.get(symbol, {})
            qty = pos.get("quantity", 0)
            if qty > 0:
                price = pos.get("avgPrice", 0) or (pos.get("value", 0) / qty if qty else 0)
                trim_qty = trim_value / price if price > 0 else 0
                if trim_qty > 0:
                    print(f"  TRIM {symbol} — reduce by ${trim_value:.2f} ({trim_qty:.4f} shares)",
                          file=sys.stderr)
                    result = t212_post("/equity/orders/market", auth, {
                        "ticker": ticker,
                        "quantity": -float(round(trim_qty, 6))
                    })
                    results.append({"symbol": symbol, "type": "TRIM",
                                    "quantity": trim_qty, "value": trim_value, "result": result})
                    time.sleep(0.3)

        else:
            print(f"  HOLD {symbol} — within target range (${current_value:.2f} ≈ ${alloc['target_value']:.2f})",
                  file=sys.stderr)
            results.append({"symbol": symbol, "type": "HOLD", "value": current_value})

    return results


# ── Performance Tracking ────────────────────────────────────────────

def save_performance(decisions: list[dict], allocations: list[dict],
                     results: list[dict], cash: dict):
    """Save today's decisions for next-day performance review."""
    log_path = PERFORMANCE_LOG
    os.makedirs(os.path.dirname(log_path) if os.path.dirname(log_path) else ".", exist_ok=True)

    entry = {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "cash": cash,
        "decisions": decisions,
        "allocations": allocations,
        "results": results,
    }

    # Load existing log
    history = []
    if os.path.exists(log_path):
        try:
            with open(log_path) as f:
                history = json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            history = []

    # Keep last 90 days
    history.append(entry)
    history = history[-90:]

    with open(log_path, "w") as f:
        json.dump(history, f, indent=2, default=str)

    print(f"\nPerformance log saved: {log_path} ({len(history)} entries)", file=sys.stderr)


def review_yesterday(decisions: list[dict], auth: str):
    """Check how yesterday's predictions performed."""
    log_path = PERFORMANCE_LOG
    if not os.path.exists(log_path):
        return

    with open(log_path) as f:
        history = json.load(f)

    if len(history) < 2:
        return

    yesterday = history[-2]
    today_decisions = {d["symbol"]: d for d in decisions}

    print("\n📊 === YESTERDAY'S PERFORMANCE REVIEW ===", file=sys.stderr)
    for alloc in yesterday.get("allocations", []):
        symbol = alloc["symbol"]
        yesterday_score = alloc["score"]
        yesterday_action = alloc.get("action", "?")
        today = today_decisions.get(symbol, {})
        today_score = today.get("score", 0)
        today_action = today.get("action", "?")

        score_delta = today_score - yesterday_score
        emoji = "📈" if score_delta > 5 else ("📉" if score_delta < -5 else "➡️")

        print(f"  {emoji} {symbol}: {yesterday_score}→{today_score} "
              f"({yesterday_action}→{today_action}) "
              f"Δ={score_delta:+d}",
              file=sys.stderr)


# ── Main ────────────────────────────────────────────────────────────

def main():
    print("=" * 60, file=sys.stderr)
    print("🤖 T212 Autonomous Portfolio Manager", file=sys.stderr)
    if DRY_RUN:
        print("⚠️  DRY RUN MODE — no real orders will be placed", file=sys.stderr)
    print("=" * 60, file=sys.stderr)

    auth = get_auth()

    # 1. Find latest DSA report
    reports_dir = "reports"
    report_files = sorted(Path(reports_dir).glob("report_*.md"), reverse=True)
    if not report_files:
        print("No DSA report found. Run DSA analysis first.", file=sys.stderr)
        sys.exit(1)
    report_path = str(report_files[0])
    print(f"\n📄 Report: {report_path}", file=sys.stderr)

    # 2. Parse decisions
    decisions = parse_dsa_report(report_path)
    if not decisions:
        print("Could not parse any decisions from report.", file=sys.stderr)
        sys.exit(1)

    print(f"\n📊 Parsed {len(decisions)} stock decisions:", file=sys.stderr)
    for d in decisions:
        emoji = {"Buy": "🟢", "Watch": "🟡", "Hold": "🟡", "Reduce": "🟠", "Sell": "🔴"}.get(d["action"], "⚪")
        print(f"  {emoji} {d['symbol']:6s} | Score {d['score']:3d} | {d['action']:6s} | {d['trend']}", file=sys.stderr)

    # 3. Get T212 state
    print("\n💰 Fetching T212 account state...", file=sys.stderr)
    cash = t212_get("/equity/account/cash", auth)
    if isinstance(cash, list):
        cash = {}
    portfolio = t212_get("/equity/portfolio", auth)
    if isinstance(portfolio, dict):
        portfolio = []

    available = float(cash.get("free", 0))
    invested = float(cash.get("invested", 0))
    total = float(cash.get("total", 0))
    ppl = float(cash.get("ppl", 0))

    print(f"  Available: ${available:.2f}", file=sys.stderr)
    print(f"  Invested:  ${invested:.2f}", file=sys.stderr)
    print(f"  Total:     ${total:.2f}", file=sys.stderr)
    print(f"  P&L:       ${ppl:.2f}", file=sys.stderr)

    # Build positions map
    positions: dict[str, dict] = {}
    for pos in portfolio:
        ticker = pos.get("ticker", "")
        symbol = ticker.split("_")[0] if "_" in ticker else ticker
        qty = float(pos.get("quantity", 0))
        avg_price = float(pos.get("averagePrice", 0))
        positions[symbol] = {
            "ticker": ticker,
            "quantity": qty,
            "avgPrice": avg_price,
            "value": qty * avg_price,
            "ppl": float(pos.get("ppl", 0)),
        }

    print(f"\n📦 Current positions: {len(positions)}", file=sys.stderr)
    for sym, pos in positions.items():
        print(f"  {sym}: {pos['quantity']:.4f} shares @ ${pos['avgPrice']:.2f} = ${pos['value']:.2f} (P&L: ${pos['ppl']:.2f})", file=sys.stderr)

    # 4. Review yesterday
    review_yesterday(decisions, auth)

    # 5. Calculate allocations
    print(f"\n🎯 Calculating target allocations...", file=sys.stderr)
    allocations = calculate_allocations(decisions, positions, available, total)
    if not allocations:
        print("No allocations to make.", file=sys.stderr)
        save_performance(decisions, allocations, [], cash)
        return

    print(f"\n📋 Target allocations:", file=sys.stderr)
    for a in allocations:
        delta_sign = "+" if a["delta"] > 0 else ""
        print(f"  {a['symbol']:6s} | Score {a['score']:3d} | {a['allocation_pct']:5.1f}% | "
              f"Target ${a['target_value']:7.2f} | Current ${a['current_value']:7.2f} | "
              f"Δ {delta_sign}${a['delta']:7.2f}",
              file=sys.stderr)

    # 6. Execute
    print(f"\n⚡ Executing orders...", file=sys.stderr)
    results = execute_rebalance(allocations, auth, positions)

    # 7. Summary
    buys = [r for r in results if r["type"] == "BUY"]
    sells = [r for r in results if r["type"] == "SELL"]
    trims = [r for r in results if r["type"] == "TRIM"]
    holds = [r for r in results if r["type"] == "HOLD"]

    print(f"\n✅ Done. Buys: {len(buys)}, Sells: {len(sells)}, "
          f"Trims: {len(trims)}, Holds: {len(holds)}",
          file=sys.stderr)

    for r in results:
        rtype = r["type"]
        if rtype in ("HOLD", "SKIP"):
            continue
        rid = r.get("result", {}).get("id", "?")
        emoji = {"BUY": "🟢", "SELL": "🔴", "TRIM": "🟠"}.get(rtype, "⚪")
        detail = f"{r.get('quantity', r.get('value', '?'))}"
        print(f"  {emoji} {r['symbol']:6s} {rtype:5s} | {detail} | ID: {rid}", file=sys.stderr)

    # 8. Save performance log
    save_performance(decisions, allocations, results, cash)

    # Output JSON for workflow consumption
    summary = {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "dry_run": DRY_RUN,
        "cash": cash,
        "allocations": allocations,
        "results_summary": {
            "buys": len(buys), "sells": len(sells),
            "trims": len(trims), "holds": len(holds)
        }
    }
    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
