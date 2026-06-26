#!/usr/bin/env python3
"""Daily Briefing — compact CEO-level Telegram summary."""

from __future__ import annotations
import os, sys, json, re, base64
import urllib.request, urllib.error
from datetime import datetime, timezone
from pathlib import Path

BOT=os.environ.get
BOT_TOKEN = BOT("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = BOT("TELEGRAM_CHAT_ID", "")
PERFORMANCE_LOG = "data/t212_performance.json"
REPORTS_DIR = "reports"


def telegram_send(text: str) -> bool:
    if not BOT_TOKEN or not CHAT_ID:
        return False
    if len(text) > 4000:
        text = text[:4000] + "\n\n...(truncated)"
    url = "https://api.telegram.org/bot" + BOT_TOKEN + "/sendMessage"
    body = json.dumps({
        "chat_id": CHAT_ID, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": True
    }).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())
            return result.get("ok", False)
    except Exception as e:
        print(f"Telegram error: {e}", file=sys.stderr)
        return False


def load_performance() -> dict | None:
    if not os.path.exists(PERFORMANCE_LOG):
        return None
    with open(PERFORMANCE_LOG) as f:
        history = json.load(f)
    return history[-1] if history else None


def load_report_summary() -> dict:
    reports = sorted(Path(REPORTS_DIR).glob("report_*.md"), reverse=True)
    if not reports:
        return {}
    with open(reports[0]) as f:
        text = f.read()
    stats = {}
    m = re.search(r'Analyzed\s+\*?\*?(\d+)\*?\*?\s+stocks', text)
    if m: stats["total"] = int(m.group(1))
    m = re.search(r'Buy:\s*(\d+)', text)
    if m: stats["buys"] = int(m.group(1))
    m = re.search(r'Watch:\s*(\d+)', text)
    if m: stats["watches"] = int(m.group(1))
    m = re.search(r'Sell:\s*(\d+)', text)
    if m: stats["sells"] = int(m.group(1))

    stocks = []
    for match in re.finditer(
        r'\*\*([^*]+)\((\w+)\)\*\*:\s*(.+?)\s*\|\s*Score\s+(\d+)\s*\|\s*(.+?)$',
        text, re.MULTILINE
    ):
        stocks.append({
            "symbol": match.group(2),
            "company": match.group(1).strip(),
            "action": match.group(3).strip(),
            "score": int(match.group(4)),
            "trend": match.group(5).strip()
        })
    stats["stocks"] = stocks
    return stats


def load_t212_cash() -> dict:
    ak = os.environ.get("T212_API_KEY", "")
    sk = os.environ.get("T212_API_SECRET", "")
    if not ak or not sk:
        return {}
    creds = ak + ":" + sk
    auth = "Basic " + base64.b64encode(creds.encode()).decode()
    req = urllib.request.Request("https://live.trading212.com/api/v0/equity/account/cash")
    req.add_header("Authorization", auth)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return {}


def compose_briefing() -> str:
    today = datetime.now(timezone.utc).strftime("%B %d, %Y")
    weekday = datetime.now(timezone.utc).strftime("%A")
    lines = []
    lines.append("<b>DSA Daily Briefing \u2014 " + today + "</b>")
    lines.append("")

    # Portfolio
    cash = load_t212_cash()
    if cash:
        total = float(cash.get("total", 0))
        ppl = float(cash.get("ppl", 0))
        invested = float(cash.get("invested", 0))
        free = float(cash.get("free", 0))
        sign = "+" if ppl >= 0 else ""
        lines.append("<b>Portfolio:</b> ${:,.2f} ({}${:,.2f} P&amp;L)".format(total, sign, ppl))
        lines.append("   Invested: ${:,.2f} | Cash: ${:,.2f}".format(invested, free))
        lines.append("")

    # DSA Summary
    summary = load_report_summary()
    stocks = summary.get("stocks", [])
    if stocks:
        buys = [s for s in stocks if "buy" in s["action"].lower()]
        sells = [s for s in stocks if "sell" in s["action"].lower()]
        watches = [s for s in stocks if "watch" in s["action"].lower()]
        holds = [s for s in stocks if "hold" in s["action"].lower() and "watch" not in s["action"].lower()]

        lines.append("<b>AI Analysis:</b> {} stocks".format(len(stocks)))
        if buys:
            syms = ", ".join("{} ({})".format(s["symbol"], s["score"]) for s in buys)
            lines.append("   <b>Buy:</b> " + syms)
        if watches:
            syms = ", ".join("{} ({})".format(s["symbol"], s["score"]) for s in watches)
            lines.append("   <b>Watch:</b> " + syms)
        if holds:
            syms = ", ".join("{} ({})".format(s["symbol"], s["score"]) for s in holds)
            lines.append("   <b>Hold:</b> " + syms)
        if sells:
            syms = ", ".join("{} ({})".format(s["symbol"], s["score"]) for s in sells)
            lines.append("   <b>Sell:</b> " + syms)
        lines.append("")

    # Trades
    perf = load_performance()
    if perf:
        results = perf.get("results", [])
        buys = [r for r in results if r["type"] == "BUY"]
        sells = [r for r in results if r["type"] == "SELL"]
        trims = [r for r in results if r["type"] == "TRIM"]
        holds = [r for r in results if r["type"] == "HOLD"]

        if buys or sells or trims:
            lines.append("<b>Trades Executed:</b>")
            for r in buys:
                val = r.get("value", r.get("quantity", "?"))
                lines.append("   BUY  {}  ${}".format(r["symbol"], val))
            for r in sells:
                val = r.get("value", r.get("quantity", "?"))
                lines.append("   SELL {}  ${}".format(r["symbol"], val))
            for r in trims:
                val = r.get("value", r.get("quantity", "?"))
                lines.append("   TRIM {}  ${}".format(r["symbol"], val))
            if holds:
                lines.append("   HOLD " + ", ".join(r["symbol"] for r in holds))
            lines.append("")

        allocs = perf.get("allocations", [])
        if allocs:
            lines.append("<b>Allocations:</b>")
            for a in allocs[:5]:
                d_sign = "+" if a["delta"] > 0 else ""
                line = "   {}  {:5.1f}%  ${:7.2f}  (D {}{:7.2f})".format(
                    a["symbol"], a["allocation_pct"], a["target_value"], d_sign, a["delta"])
                lines.append(line)
            lines.append("")

    # Tomorrow
    if stocks:
        candidates = [s for s in stocks if s["action"].lower() in ("buy", "watch", "hold")]
        candidates.sort(key=lambda s: s["score"], reverse=True)
        if candidates:
            top = candidates[0]
            lines.append("<b>Tomorrow:</b>")
            lines.append("   Top pick: {} (Score {}, {})".format(top["symbol"], top["score"], top["trend"]))
            bullish = sum(1 for s in stocks if "bullish" in s["trend"].lower())
            bearish = sum(1 for s in stocks if "bearish" in s["trend"].lower())
            lines.append("   Sentiment: {} bullish / {} bearish".format(bullish, bearish))
            avg = sum(s["score"] for s in stocks) / len(stocks)
            if avg >= 50:
                bias = "Positive"
            elif avg >= 35:
                bias = "Neutral"
            else:
                bias = "Cautious"
            lines.append("   Bias: {} (avg score {:.0f}/100)".format(bias, avg))
        lines.append("")

    # Risk
    high_risk = [s for s in stocks if s["score"] < 30 and "sell" in s["action"].lower()]
    if high_risk:
        lines.append("<b>Risk Alert:</b> {} stocks flagged".format(len(high_risk)))
        lines.append("   " + ", ".join(s["symbol"] for s in high_risk[:5]))
        lines.append("")

    lines.append("<i>Next run: {} 18:00 Beijing</i>".format(weekday))
    return "\n".join(lines)


def main():
    briefing = compose_briefing()
    print(briefing)
    print("\n--- ({} chars) ---".format(len(briefing)))
    if "--send" in sys.argv:
        ok = telegram_send(briefing)
        print("Telegram: {}".format("SENT" if ok else "FAILED"))


if __name__ == "__main__":
    main()
