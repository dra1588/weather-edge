"""
asia_edge.py — Melbourne-window Polymarket monitor + 2-week paper tally

WHAT THIS DOES
  1. Pulls news from feeds that actually work (verified, self-hosted, no auth)
  2. Snapshots Polymarket prices every run, builds a price history
  3. Flags markets that MOVED (repricing happened) or are STALE while
     related news broke (repricing hasn't happened yet — that's the edge)
  4. Logs paper trades you declare, scores them, reports a running tally

WHAT THIS DOES NOT DO
  Place a single real order. Entirely by design. The edge being tested here
  is your judgement during Asian hours. If a script makes the call, you are
  not testing the edge, you are testing a keyword matcher.

USAGE (GitHub Actions, hourly)
  python asia_edge.py                 # normal run: snapshot + alert
  python asia_edge.py --brief         # morning brief (run at 8am Melbourne)
  python asia_edge.py --tally         # print/send the 2-week scorecard

LOGGING A PAPER TRADE
  Add a line to paper_trades.json (or use the helper below), then commit.
  The scorer reads it, marks it to market each run, and reports.
"""

import os
import re
import sys
import json
import html
import time
import hashlib
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

MOVE_ALERT_PCT      = 5.0    # flag markets that moved this much (abs cents)
STALE_HOURS         = 12     # "hasn't repriced" window
PAPER_PERIOD_DAYS   = 14     # length of the evaluation period
MAX_MARKETS         = 400    # markets to snapshot per run
NEWS_LOOKBACK_HOURS = 18     # only consider headlines this fresh

SNAPSHOT_FILE = "price_history.json"
TRADES_FILE   = "paper_trades.json"
FEEDS_FILE    = "feed_health.json"
EXPORT_JSON   = "asia_edge.json"     # consumed by the OPS Centre panel
EXPORT_HTML   = "asia_edge.html"     # standalone snapshot, data baked in

GAMMA_BASE = "https://gamma-api.polymarket.com"
HEADERS    = {"User-Agent": "Mozilla/5.0 (compatible; AsiaEdge/1.0)"}

# ─────────────────────────────────────────────────────────────────────────────
# FEEDS
#
# Verified working as of build date. Reuters RSS is DEAD (killed, no
# replacement). Google News RSS is deliberately EXCLUDED: it works, but a
# July 2026 sampling found median item age ~6.6 days and only ~7.6% of items
# under six hours old. Stale news is worse than no news for this strategy —
# it looks like a signal and isn't one.
#
# SCMP and Nikkei are listed as KNOWN_BLOCKED for documentation, not used.
# ─────────────────────────────────────────────────────────────────────────────

FEEDS = {
    # Verified real-time, self-hosted, no auth
    "aljazeera_all":   "https://www.aljazeera.com/xml/rss/all.xml",

    # Well-established self-hosted feeds (script reports if any go dark)
    "bbc_asia":        "https://feeds.bbci.co.uk/news/world/asia/rss.xml",
    "bbc_world":       "https://feeds.bbci.co.uk/news/world/rss.xml",
    "abc_au_world":    "https://www.abc.net.au/news/feed/51120/rss.xml",
    "smh_world":       "https://www.smh.com.au/rss/world.xml",
    "channelnewsasia": "https://www.channelnewsasia.com/api/v1/rss-outbound-feed?_format=xml",
    "the_diplomat":    "https://thediplomat.com/feed/",
    "nhk_world":       "https://www3.nhk.or.jp/nhkworld/en/news/rss/all.xml",
    "straits_times":   "https://www.straitstimes.com/news/asia/rss.xml",
    "japan_times":     "https://www.japantimes.co.jp/feed/",
}

KNOWN_BLOCKED = {
    "reuters":  "RSS discontinued — no replacement",
    "scmp":     "Bot detection blocks automated requests",
    "nikkei":   "Subscriber-only",
    "gnews":    "Excluded: median item age ~6.6 days, too stale to trade on",
}

# Keyword themes → these drive both market selection and headline matching.
# Tune this list; it is the single biggest lever on signal quality.
THEMES = {
    "china_taiwan": ["china", "taiwan", "taipei", "beijing", "pla", "strait", "xi jinping"],
    "japan":        ["japan", "boj", "bank of japan", "yen", "tokyo", "nikkei"],
    "korea":        ["korea", "seoul", "pyongyang", "kim jong", "dprk"],
    "iran":         ["iran", "tehran", "irgc", "hormuz", "khamenei"],
    "india":        ["india", "modi", "delhi", "pakistan", "islamabad"],
    "russia":       ["russia", "putin", "kremlin", "moscow", "ukraine"],
    "middle_east":  ["israel", "gaza", "hezbollah", "lebanon", "syria", "houthi"],
    "trade":        ["tariff", "sanction", "export control", "chip", "semiconductor"],
}

# ─────────────────────────────────────────────────────────────────────────────
# STATE
# ─────────────────────────────────────────────────────────────────────────────

def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log(f"save error {path}: {e}")


def log(msg):
    print(f"[asia_edge] {msg}", flush=True)


def now():
    return datetime.now(timezone.utc)


def melbourne_now():
    # AEDT/AEST — approximate, no tz database dependency
    return now() + timedelta(hours=11)


# ─────────────────────────────────────────────────────────────────────────────
# NEWS
# ─────────────────────────────────────────────────────────────────────────────

def strip_tags(s):
    return re.sub(r"<[^>]+>", "", html.unescape(s or "")).strip()


def parse_feed(name, url):
    """Fetch and parse one RSS feed. Returns (items, error_or_None)."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            return [], f"HTTP {r.status_code}"
        root = ET.fromstring(r.content)
    except ET.ParseError as e:
        return [], f"parse error: {e}"
    except Exception as e:
        return [], f"{type(e).__name__}: {e}"

    items = []
    # RSS 2.0
    for it in root.iter("item"):
        title = strip_tags((it.findtext("title") or ""))
        link  = (it.findtext("link") or "").strip()
        pub   = (it.findtext("pubDate") or "").strip()
        desc  = strip_tags(it.findtext("description") or "")
        if title:
            items.append({"source": name, "title": title, "link": link,
                          "published": pub, "summary": desc[:300]})

    # Atom fallback
    if not items:
        ns = "{http://www.w3.org/2005/Atom}"
        for e in root.iter(f"{ns}entry"):
            title = strip_tags(e.findtext(f"{ns}title") or "")
            lnk_el = e.find(f"{ns}link")
            link = lnk_el.get("href") if lnk_el is not None else ""
            pub = (e.findtext(f"{ns}updated") or e.findtext(f"{ns}published") or "")
            if title:
                items.append({"source": name, "title": title, "link": link,
                              "published": pub, "summary": ""})

    return items, None


def fetch_all_news():
    """Pull every feed, track which ones are healthy."""
    health = load_json(FEEDS_FILE, {})
    all_items = []

    for name, url in FEEDS.items():
        items, err = parse_feed(name, url)
        prev = health.get(name, {})
        if err:
            fails = prev.get("consecutive_failures", 0) + 1
            health[name] = {"ok": False, "error": err,
                            "consecutive_failures": fails,
                            "last_check": now().isoformat()}
            log(f"FEED DOWN: {name} — {err} (fail #{fails})")
        else:
            health[name] = {"ok": True, "items": len(items),
                            "consecutive_failures": 0,
                            "last_check": now().isoformat()}
            log(f"feed ok: {name} — {len(items)} items")
            all_items.extend(items)
        time.sleep(0.3)

    save_json(FEEDS_FILE, health)
    return all_items, health


_THEME_RX = {
    theme: [re.compile(r"\b" + re.escape(w) + r"\b", re.I) for w in words]
    for theme, words in THEMES.items()
}


def theme_of(text):
    """
    Return set of themes a piece of text touches.

    Word-boundary matched, NOT substring. Naive `in` matching means "pla"
    (for PLA) fires on "play", "strait" fires on "straight", "iran" fires
    on "Tirana". Every one of those is a false signal that looks real.
    """
    t = text or ""
    return {theme for theme, rxs in _THEME_RX.items()
            if any(rx.search(t) for rx in rxs)}


# ─────────────────────────────────────────────────────────────────────────────
# POLYMARKET
# ─────────────────────────────────────────────────────────────────────────────

def fetch_markets(limit=MAX_MARKETS):
    out, offset = [], 0
    while len(out) < limit:
        try:
            r = requests.get(f"{GAMMA_BASE}/markets",
                             params={"active": "true", "closed": "false",
                                     "limit": 100, "offset": offset},
                             headers=HEADERS, timeout=20)
            r.raise_for_status()
            batch = r.json()
            if isinstance(batch, dict):
                batch = batch.get("markets", [])
            if not batch:
                break
            out.extend(batch)
            offset += 100
            time.sleep(0.2)
        except Exception as e:
            log(f"market fetch error: {e}")
            break
    return out[:limit]


def yes_price(m):
    """Extract YES price from a Gamma market dict, tolerating shape changes."""
    for key in ("outcomePrices", "outcome_prices"):
        v = m.get(key)
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except Exception:
                v = None
        if isinstance(v, list) and v:
            try:
                return float(v[0])
            except Exception:
                pass
    for key in ("lastTradePrice", "bestBid", "price"):
        if m.get(key) is not None:
            try:
                return float(m[key])
            except Exception:
                pass
    return None


def snapshot_markets():
    """Record current prices, return (markets, history)."""
    markets = fetch_markets()
    log(f"fetched {len(markets)} active markets")

    hist = load_json(SNAPSHOT_FILE, {})
    stamp = now().isoformat()
    tracked = 0

    for m in markets:
        q = m.get("question") or m.get("title") or ""
        if not q:
            continue
        themes = theme_of(q)
        if not themes:
            continue  # only track markets in our themes — keeps the file small

        p = yes_price(m)
        if p is None:
            continue

        mid = str(m.get("id") or m.get("conditionId") or
                  hashlib.md5(q.encode()).hexdigest()[:12])

        rec = hist.setdefault(mid, {"question": q,
                                    "slug": m.get("slug", ""),
                                    "themes": sorted(themes),
                                    "points": []})
        rec["question"] = q
        rec["themes"] = sorted(themes)
        rec["points"].append({"t": stamp, "p": round(p, 4)})
        # keep 14 days of hourly points
        rec["points"] = rec["points"][-400:]
        tracked += 1

    save_json(SNAPSHOT_FILE, hist)
    log(f"tracking {tracked} themed markets")
    return markets, hist


def price_move(rec, hours):
    """Return (old_price, new_price, delta_pts) over the window, or None."""
    pts = rec.get("points", [])
    if len(pts) < 2:
        return None
    cutoff = now() - timedelta(hours=hours)
    old = None
    for pt in pts:
        try:
            t = datetime.fromisoformat(pt["t"])
        except Exception:
            continue
        if t <= cutoff:
            old = pt
    if old is None:
        old = pts[0]
    new = pts[-1]
    return old["p"], new["p"], round((new["p"] - old["p"]) * 100, 2)


# ─────────────────────────────────────────────────────────────────────────────
# THE ACTUAL SIGNAL
# ─────────────────────────────────────────────────────────────────────────────

def find_opportunities(news, hist):
    """
    Two categories, and the second is the one that matters:

      MOVED  — market repriced sharply. Informational: what does the market
               already know? Usually too late to trade.

      STALE  — news broke in a theme, but the market hasn't moved.
               This is the Melbourne window. Requires your judgement to
               decide whether the news is actually material.
    """
    cutoff = now() - timedelta(hours=NEWS_LOOKBACK_HOURS)
    fresh_by_theme = {}

    for item in news:
        # parse pubDate loosely; if unparseable, treat as fresh
        keep = True
        p = item.get("published", "")
        for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z"):
            try:
                dt = datetime.strptime(p, fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                keep = dt >= cutoff
                item["_dt"] = dt
                break
            except Exception:
                continue
        if not keep:
            continue
        for th in theme_of(item["title"] + " " + item.get("summary", "")):
            fresh_by_theme.setdefault(th, []).append(item)

    moved, stale = [], []

    for mid, rec in hist.items():
        mv = price_move(rec, STALE_HOURS)
        if not mv:
            continue
        old_p, new_p, delta = mv
        themes = set(rec.get("themes", []))
        related = []
        for th in themes:
            related.extend(fresh_by_theme.get(th, []))

        entry = {
            "id": mid,
            "question": rec["question"],
            "slug": rec.get("slug", ""),
            "old": old_p, "new": new_p, "delta": delta,
            "themes": sorted(themes),
            "headlines": related[:3],
            "n_headlines": len(related),
        }

        if abs(delta) >= MOVE_ALERT_PCT:
            moved.append(entry)
        elif related and abs(delta) < 2.0:
            stale.append(entry)

    moved.sort(key=lambda x: -abs(x["delta"]))
    stale.sort(key=lambda x: -x["n_headlines"])
    return moved, stale


# ─────────────────────────────────────────────────────────────────────────────
# PAPER TRADE LEDGER
# ─────────────────────────────────────────────────────────────────────────────

def new_ledger():
    return {"started": now().isoformat(), "trades": []}


def log_paper_trade(question, side, entry_price, thesis, market_id=""):
    """
    Call this to record a decision. side = "YES" or "NO".
    Thesis is mandatory and it is the whole point — an unexplained trade
    teaches you nothing when you review it in two weeks.
    """
    led = load_json(TRADES_FILE, new_ledger())
    led["trades"].append({
        "id": hashlib.md5(f"{question}{now()}".encode()).hexdigest()[:8],
        "market_id": market_id,
        "question": question,
        "side": side.upper(),
        "entry_price": round(float(entry_price), 4),
        "thesis": thesis,
        "opened": now().isoformat(),
        "opened_melb": melbourne_now().strftime("%Y-%m-%d %H:%M"),
        "status": "open",
        "exit_price": None,
        "closed": None,
        "pnl_pct": None,
    })
    save_json(TRADES_FILE, led)
    log(f"paper trade logged: {side} {question[:50]} @ {entry_price}")
    return led


def mark_to_market(hist):
    """Update open paper trades with current prices."""
    led = load_json(TRADES_FILE, new_ledger())
    changed = False

    for tr in led["trades"]:
        if tr["status"] != "open":
            continue
        rec = hist.get(tr.get("market_id", ""))
        if not rec:
            # fall back to matching on question text
            rec = next((r for r in hist.values()
                        if r["question"] == tr["question"]), None)
        if not rec or not rec.get("points"):
            continue
        cur = rec["points"][-1]["p"]
        if tr["side"] == "NO":
            cur_eff, ent_eff = 1 - cur, 1 - tr["entry_price"]
        else:
            cur_eff, ent_eff = cur, tr["entry_price"]
        if ent_eff > 0:
            tr["current_price"] = round(cur, 4)
            tr["pnl_pct"] = round((cur_eff - ent_eff) / ent_eff * 100, 2)
            changed = True

    if changed:
        save_json(TRADES_FILE, led)
    return led


def tally(led):
    """Score the paper period."""
    trades = led.get("trades", [])
    if not trades:
        return None

    started = datetime.fromisoformat(led["started"])
    elapsed = (now() - started).days
    remaining = max(0, PAPER_PERIOD_DAYS - elapsed)

    closed = [t for t in trades if t["status"] == "closed" and t.get("pnl_pct") is not None]
    open_t = [t for t in trades if t["status"] == "open"]
    scored = closed + [t for t in open_t if t.get("pnl_pct") is not None]

    wins = [t for t in scored if t["pnl_pct"] > 0]
    losses = [t for t in scored if t["pnl_pct"] <= 0]

    avg = sum(t["pnl_pct"] for t in scored) / len(scored) if scored else 0
    win_rate = len(wins) / len(scored) * 100 if scored else 0
    avg_win = sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0

    return {
        "elapsed_days": elapsed,
        "remaining_days": remaining,
        "total": len(trades),
        "open": len(open_t),
        "closed": len(closed),
        "win_rate": round(win_rate, 1),
        "avg_pnl_pct": round(avg, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "best": max(scored, key=lambda t: t["pnl_pct"]) if scored else None,
        "worst": min(scored, key=lambda t: t["pnl_pct"]) if scored else None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────────────────────────────────────

def send(msg):
    token = os.environ.get("BOT_TOKEN")
    chat = os.environ.get("CHAT_ID")
    if not token or not chat:
        log("no telegram config — printing instead:\n" + msg)
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": msg[:4000],
                                "parse_mode": "HTML",
                                "disable_web_page_preview": True},
                          timeout=15)
        return r.status_code == 200
    except Exception as e:
        log(f"telegram error: {e}")
        return False


def esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_brief(moved, stale, health, tal):
    mel = melbourne_now()
    L = [f"🌏 <b>Melbourne Brief</b> — {mel.strftime('%a %d %b, %H:%M')} AEDT", ""]

    down = [k for k, v in health.items() if not v.get("ok")]
    if down:
        L.append(f"⚠️ Feeds down: {', '.join(down)}")
        L.append("")

    if stale:
        L.append(f"🎯 <b>UNPRICED — news broke, market flat ({len(stale)})</b>")
        L.append("<i>The window. Read the resolution rules before acting.</i>")
        for s in stale[:5]:
            L.append(f"\n• <b>{esc(s['question'][:70])}</b>")
            L.append(f"  {s['new']*100:.0f}¢ (flat {s['delta']:+.1f}pts) | {s['n_headlines']} headlines")
            for h in s["headlines"][:2]:
                L.append(f"  ↳ {esc(h['title'][:75])}")
            if s.get("slug"):
                L.append(f"  https://polymarket.com/event/{s['slug']}")
        L.append("")

    if moved:
        L.append(f"📈 <b>ALREADY REPRICED ({len(moved)})</b>")
        L.append("<i>Informational — the market knows. Usually too late.</i>")
        for m in moved[:4]:
            arrow = "▲" if m["delta"] > 0 else "▼"
            L.append(f"• {arrow} {m['delta']:+.1f}pts → {m['new']*100:.0f}¢ | {esc(m['question'][:55])}")
        L.append("")

    if not stale and not moved:
        L.append("Nothing actionable. Most days look like this.")
        L.append("")

    if tal:
        L.append(f"📊 <b>Paper tally</b> — day {tal['elapsed_days']}/{PAPER_PERIOD_DAYS}")
        if tal["total"]:
            L.append(f"{tal['total']} trades | {tal['win_rate']:.0f}% win | avg {tal['avg_pnl_pct']:+.1f}%")
        else:
            L.append("No trades logged yet.")

    return "\n".join(L)


def format_tally(tal):
    if not tal:
        return "📊 No paper trades logged yet.\n\nLog one with log_paper_trade() — thesis is mandatory."

    L = [f"📊 <b>Paper Trading Scorecard</b>",
         f"Day {tal['elapsed_days']} of {PAPER_PERIOD_DAYS} ({tal['remaining_days']} remaining)", ""]
    L.append(f"Trades: {tal['total']} ({tal['open']} open, {tal['closed']} closed)")
    L.append(f"Win rate: <b>{tal['win_rate']:.1f}%</b>")
    L.append(f"Avg P&L: <b>{tal['avg_pnl_pct']:+.2f}%</b>")
    L.append(f"Avg win: {tal['avg_win']:+.2f}% | Avg loss: {tal['avg_loss']:+.2f}%")

    if tal["best"]:
        L.append(f"\n✅ Best: {esc(tal['best']['question'][:50])} {tal['best']['pnl_pct']:+.1f}%")
    if tal["worst"]:
        L.append(f"❌ Worst: {esc(tal['worst']['question'][:50])} {tal['worst']['pnl_pct']:+.1f}%")

    if tal["remaining_days"] == 0:
        L.append("\n<b>Period complete.</b>")
        if tal["total"] < 10:
            L.append("Fewer than 10 trades — not enough to conclude anything.")
        elif tal["win_rate"] > 55 and tal["avg_pnl_pct"] > 0:
            L.append("Edge looks real. Consider small live size.")
        else:
            L.append("No demonstrated edge. Do not go live yet.")

    return "\n".join(L)


# ─────────────────────────────────────────────────────────────────────────────
# DASHBOARD EXPORT
# ─────────────────────────────────────────────────────────────────────────────

def build_export(moved, stale, health, tal, led):
    """Compact payload for the OPS Centre panel."""
    def slim(e):
        return {
            "question": e["question"],
            "slug": e.get("slug", ""),
            "price": round(e["new"] * 100, 1),
            "delta": e["delta"],
            "themes": e["themes"],
            "n_headlines": e.get("n_headlines", 0),
            "headlines": [{"title": h["title"], "source": h["source"]}
                          for h in e.get("headlines", [])[:3]],
        }

    open_trades = [
        {"question": t["question"], "side": t["side"],
         "entry": round(t["entry_price"] * 100, 1),
         "current": round(t.get("current_price", t["entry_price"]) * 100, 1),
         "pnl_pct": t.get("pnl_pct"), "thesis": t.get("thesis", ""),
         "opened": t.get("opened_melb", "")}
        for t in led.get("trades", []) if t["status"] == "open"
    ]

    return {
        "generated": now().isoformat(),
        "generated_melb": melbourne_now().strftime("%a %d %b %H:%M"),
        "unpriced": [slim(e) for e in stale[:8]],
        "repriced": [slim(e) for e in moved[:6]],
        "feeds": {
            "live": sorted(k for k, v in health.items() if v.get("ok")),
            "down": sorted(k for k, v in health.items() if not v.get("ok")),
        },
        "tally": tal or {},
        "open_trades": open_trades,
        "period_days": PAPER_PERIOD_DAYS,
    }


def write_exports(payload):
    save_json(EXPORT_JSON, payload)

    data = json.dumps(payload, indent=2)
    html_doc = _PANEL_HTML.replace("__DATA__", data)
    try:
        with open(EXPORT_HTML, "w") as f:
            f.write(html_doc)
        log(f"wrote {EXPORT_JSON} + {EXPORT_HTML}")
    except Exception as e:
        log(f"export error: {e}")


# Standalone page: same markup + renderer as asia_panel.html, data baked in.
_PANEL_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Asia Edge — Melbourne Window</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@300;400;500;600&display=swap');
:root{--bg:#0a0c10;--surface:#111318;--border:#1e2330;--dim:#2a3040;
--muted:#4a5568;--text:#e2e8f0;--sub:#8892a4;--green:#00d084;--red:#ff4757;
--amber:#ffa502;--blue:#3b82f6;--purple:#a78bfa}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:'IBM Plex Sans',sans-serif;font-size:14px;padding:24px}
.ae-wrap{max-width:1100px;margin:0 auto}
block into your existing <style> tag (or leave as-is)
     2. Paste the <section> where you want the panel to appear
     3. Paste the <script> before </body>
     4. Set ASIA_FEED_URL below to wherever asia_edge.json is served

     Inherits your existing CSS variables (--bg, --surface, --green, etc).
     If a variable is missing it falls back to a sane value.
     ═══════════════════════════════════════════════════════════════════ -->

<style>
/* ── Asia Edge panel ── */
.ae-panel{
  background:var(--surface,#111318);
  border:1px solid var(--border,#1e2330);
  border-radius:8px;
  overflow:hidden;
  margin-bottom:20px;
}
.ae-head{
  display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;
  padding:14px 18px;
  border-bottom:1px solid var(--border,#1e2330);
}
.ae-title{
  font-family:'IBM Plex Mono',monospace;font-size:12px;font-weight:600;
  letter-spacing:.12em;text-transform:uppercase;color:var(--text,#e2e8f0);
}
.ae-title em{color:var(--purple,#a78bfa);font-style:normal}
.ae-stamp{
  font-family:'IBM Plex Mono',monospace;font-size:11px;
  color:var(--sub,#8892a4);margin-left:auto;
}
.ae-feeds{font-family:'IBM Plex Mono',monospace;font-size:10px;color:var(--muted,#4a5568)}
.ae-feeds b{color:var(--green,#00d084);font-weight:500}
.ae-feeds .down{color:var(--red,#ff4757)}

/* The split is the whole point: two columns, weighted */
.ae-body{display:grid;grid-template-columns:1.35fr 1fr;gap:0}
@media(max-width:840px){.ae-body{grid-template-columns:1fr}}
.ae-col{padding:14px 18px}
.ae-col+.ae-col{border-left:1px solid var(--border,#1e2330)}
@media(max-width:840px){.ae-col+.ae-col{border-left:0;border-top:1px solid var(--border,#1e2330)}}

.ae-lbl{
  font-family:'IBM Plex Mono',monospace;font-size:10px;font-weight:600;
  letter-spacing:.14em;text-transform:uppercase;margin-bottom:3px;
}
.ae-lbl.live{color:var(--green,#00d084)}
.ae-lbl.late{color:var(--muted,#4a5568)}
.ae-sub{font-size:11px;color:var(--sub,#8892a4);margin-bottom:12px;line-height:1.4}

.ae-row{
  padding:10px 0;border-bottom:1px solid var(--dim,#2a3040);
}
.ae-row:last-child{border-bottom:0}
.ae-q{font-size:13px;line-height:1.35;margin-bottom:5px;color:var(--text,#e2e8f0)}
.ae-q a{color:inherit;text-decoration:none;border-bottom:1px solid transparent}
.ae-q a:hover{border-bottom-color:var(--sub,#8892a4)}
.ae-meta{display:flex;align-items:center;gap:8px;flex-wrap:wrap;
  font-family:'IBM Plex Mono',monospace;font-size:11px}
.ae-px{color:var(--text,#e2e8f0);font-weight:500}
.ae-flat{color:var(--green,#00d084)}
.ae-up{color:var(--green,#00d084)}
.ae-dn{color:var(--red,#ff4757)}
.ae-tag{
  font-size:9px;letter-spacing:.06em;text-transform:uppercase;
  color:var(--sub,#8892a4);background:var(--dim,#2a3040);
  padding:1px 6px;border-radius:3px;
}
.ae-hl{font-size:11px;color:var(--sub,#8892a4);margin-top:5px;line-height:1.45;padding-left:10px;
  border-left:2px solid var(--dim,#2a3040)}
.ae-hl span{color:var(--muted,#4a5568);font-family:'IBM Plex Mono',monospace;font-size:9px}

.ae-empty{font-size:12px;color:var(--muted,#4a5568);padding:14px 0;line-height:1.5}

/* Tally strip */
.ae-tally{
  display:flex;gap:22px;flex-wrap:wrap;align-items:baseline;
  padding:12px 18px;border-top:1px solid var(--border,#1e2330);
  background:rgba(0,0,0,.2);
}
.ae-stat{display:flex;flex-direction:column;gap:2px}
.ae-stat .v{font-family:'IBM Plex Mono',monospace;font-size:15px;font-weight:600;color:var(--text,#e2e8f0)}
.ae-stat .v.pos{color:var(--green,#00d084)}
.ae-stat .v.neg{color:var(--red,#ff4757)}
.ae-stat .k{font-size:9px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted,#4a5568)}
.ae-prog{flex:1;min-width:120px;display:flex;flex-direction:column;gap:4px;justify-content:center}
.ae-bar{height:3px;background:var(--dim,#2a3040);border-radius:2px;overflow:hidden}
.ae-bar i{display:block;height:100%;background:var(--purple,#a78bfa)}
.ae-verdict{font-size:11px;color:var(--amber,#ffa502);padding:0 18px 12px}
</style></head><body><div class="ae-wrap">
<section class="ae-panel" id="asia-edge-panel">
  <div class="ae-head">
    <span class="ae-title">Asia Edge · <em>Melbourne Window</em></span>
    <span class="ae-feeds" id="ae-feeds"></span>
    <span class="ae-stamp" id="ae-stamp">loading…</span>
  </div>
  <div class="ae-body">
    <div class="ae-col">
      <div class="ae-lbl live">Unpriced</div>
      <div class="ae-sub">News broke. Market hasn't moved. Read the resolution rules before acting.</div>
      <div id="ae-unpriced"></div>
    </div>
    <div class="ae-col">
      <div class="ae-lbl late">Already repriced</div>
      <div class="ae-sub">The market knows. Informational only.</div>
      <div id="ae-repriced"></div>
    </div>
  </div>
  <div class="ae-tally" id="ae-tally"></div>
  <div class="ae-verdict" id="ae-verdict"></div>
</section>
</div>
<script>const ASIA_DATA = __DATA__;</script>
<script>
/* ═══ Asia Edge panel renderer ═══
   Set this to wherever asia_edge.json is reachable from the browser.
   See the deployment notes — a private repo needs one of the three options. */
const ASIA_FEED_URL = "asia_edge.json";

(function () {
  const $ = id => document.getElementById(id);
  const esc = s => (s || "").replace(/[&<>"]/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  function marketRow(m, mode) {
    const link = m.slug
      ? `<a href="https://polymarket.com/event/${esc(m.slug)}" target="_blank" rel="noopener">${esc(m.question)}</a>`
      : esc(m.question);

    let move;
    if (mode === "unpriced") {
      move = `<span class="ae-flat">flat ${m.delta >= 0 ? "+" : ""}${m.delta.toFixed(1)}pts</span>`;
    } else {
      const cls = m.delta > 0 ? "ae-up" : "ae-dn";
      const arrow = m.delta > 0 ? "▲" : "▼";
      move = `<span class="${cls}">${arrow} ${m.delta >= 0 ? "+" : ""}${m.delta.toFixed(1)}pts</span>`;
    }

    const tags = (m.themes || []).slice(0, 2)
      .map(t => `<span class="ae-tag">${esc(t.replace(/_/g, " "))}</span>`).join("");

    const heads = mode === "unpriced"
      ? (m.headlines || []).slice(0, 2).map(h =>
          `<div class="ae-hl">${esc(h.title)} <span>${esc(h.source)}</span></div>`).join("")
      : "";

    return `<div class="ae-row">
      <div class="ae-q">${link}</div>
      <div class="ae-meta">
        <span class="ae-px">${m.price.toFixed(0)}¢</span>
        ${move}
        ${mode === "unpriced" ? `<span class="ae-tag">${m.n_headlines} headline${m.n_headlines === 1 ? "" : "s"}</span>` : ""}
        ${tags}
      </div>${heads}</div>`;
  }

  function render(d) {
    $("ae-stamp").textContent = d.generated_melb + " AEDT";

    const live = (d.feeds && d.feeds.live) || [];
    const down = (d.feeds && d.feeds.down) || [];
    $("ae-feeds").innerHTML =
      `<b>${live.length}</b> feeds live` +
      (down.length ? ` · <span class="down">${down.length} down: ${down.map(esc).join(", ")}</span>` : "");

    $("ae-unpriced").innerHTML = (d.unpriced || []).length
      ? d.unpriced.map(m => marketRow(m, "unpriced")).join("")
      : `<div class="ae-empty">Nothing unpriced right now. Most days look like this — the window is narrow by nature.</div>`;

    $("ae-repriced").innerHTML = (d.repriced || []).length
      ? d.repriced.map(m => marketRow(m, "repriced")).join("")
      : `<div class="ae-empty">No sharp moves in the last 12 hours.</div>`;

    // Tally
    const t = d.tally || {};
    const days = t.elapsed_days ?? 0, total = t.period_days || d.period_days || 14;
    const pct = Math.min(100, (days / total) * 100);
    const sign = v => (v > 0 ? "pos" : v < 0 ? "neg" : "");

    if (t.total) {
      $("ae-tally").innerHTML = `
        <div class="ae-stat"><span class="v">${t.total}</span><span class="k">Trades</span></div>
        <div class="ae-stat"><span class="v">${t.win_rate.toFixed(0)}%</span><span class="k">Win rate</span></div>
        <div class="ae-stat"><span class="v ${sign(t.avg_pnl_pct)}">${t.avg_pnl_pct >= 0 ? "+" : ""}${t.avg_pnl_pct.toFixed(1)}%</span><span class="k">Avg P&amp;L</span></div>
        <div class="ae-stat"><span class="v">${t.open ?? 0}</span><span class="k">Open</span></div>
        <div class="ae-prog">
          <div class="ae-bar"><i style="width:${pct}%"></i></div>
          <span class="k" style="font-size:9px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted,#4a5568)">Paper day ${days}/${total}</span>
        </div>`;
    } else {
      $("ae-tally").innerHTML = `
        <div class="ae-stat"><span class="v">0</span><span class="k">Trades logged</span></div>
        <div class="ae-prog">
          <div class="ae-bar"><i style="width:${pct}%"></i></div>
          <span class="k" style="font-size:9px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted,#4a5568)">Paper day ${days}/${total} — log a call to start scoring</span>
        </div>`;
    }

    // Verdict only at period end
    const v = $("ae-verdict");
    if (t.remaining_days === 0 && t.total !== undefined) {
      v.textContent = t.total < 10
        ? `Period complete — ${t.total} trades is too few to conclude anything. Extend or accept the result is unproven.`
        : (t.win_rate > 55 && t.avg_pnl_pct > 0)
          ? "Period complete — edge looks real. Consider small live size."
          : "Period complete — no demonstrated edge. Do not go live.";
    } else { v.textContent = ""; }
  }

  // Baked-in data (standalone page) wins; otherwise fetch.
  if (typeof ASIA_DATA !== "undefined") { render(ASIA_DATA); return; }

  fetch(ASIA_FEED_URL, { cache: "no-store" })
    .then(r => { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
    .then(render)
    .catch(e => {
      $("ae-stamp").textContent = "unavailable";
      $("ae-unpriced").innerHTML =
        `<div class="ae-empty">Can't reach <code>${esc(ASIA_FEED_URL)}</code> — ${esc(e.message)}.<br>
         Check ASIA_FEED_URL in the panel script.</div>`;
    });
})();
</script>
</body></html>"""




def main():
    mode = "run"
    if "--brief" in sys.argv:
        mode = "brief"
    elif "--tally" in sys.argv:
        mode = "tally"

    log(f"start — mode={mode} | Melbourne {melbourne_now().strftime('%H:%M')}")

    _, hist = snapshot_markets()
    led = mark_to_market(hist)
    tal = tally(led)

    if mode == "tally":
        send(format_tally(tal))
        return

    news, health = fetch_all_news()
    log(f"{len(news)} headlines from {sum(1 for v in health.values() if v.get('ok'))} live feeds")

    moved, stale = find_opportunities(news, hist)
    log(f"{len(moved)} moved | {len(stale)} stale-with-news")

    # Always write the dashboard export, even on quiet runs — the panel
    # should show a current timestamp rather than going stale silently.
    write_exports(build_export(moved, stale, health, tal, led))

    # Only push a full brief in the morning, or when something is genuinely
    # unpriced. Hourly spam trains you to ignore the alerts.
    if mode == "brief" or stale:
        send(format_brief(moved, stale, health, tal))
    else:
        log("nothing worth interrupting for — no alert sent")

    log("done")


if __name__ == "__main__":
    main()
