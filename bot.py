import os
import sqlite3
import requests
import yfinance as yf
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

import redis as redis_lib

DB_PATH = os.path.join(os.environ.get("DATA_DIR", "."), "bot.db")

try:
    redis_client = redis_lib.from_url(os.environ["REDIS_URL"], decode_responses=True)
    redis_client.ping()
except Exception:
    redis_client = None

def send_heartbeat(bot_name: str):
    if redis_client is None:
        return
    try:
        redis_client.set(f"heartbeat:{bot_name}", datetime.now(ZoneInfo("UTC")).isoformat(), ex=600)
    except Exception:
        pass

def get_all_heartbeats():
    if redis_client is None:
        return {}
    result = {}
    for name in ["stock-bot", "shortbot", "traderbot", "portfoliobot"]:
        try:
            val = redis_client.get(f"heartbeat:{name}")
            result[name] = val
        except Exception:
            result[name] = None
    return result

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS watchlist (
        chat_id INTEGER, ticker TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS alerts (
        chat_id INTEGER, ticker TEXT, direction TEXT, price REAL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS pct_alerts (
        chat_id INTEGER, ticker TEXT, percent REAL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS sec_seen (
        ticker TEXT PRIMARY KEY, accession TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS user_settings (
        chat_id INTEGER PRIMARY KEY, default_pct REAL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS volume_alerted (
        ticker TEXT PRIMARY KEY, last_date TEXT)""")
    return conn
_CIK_CACHE = {}

def get_cik(ticker: str):
    global _CIK_CACHE
    if not _CIK_CACHE:
        try:
            resp = requests.get(
                "https://www.sec.gov/files/company_tickers.json",
                headers={"User-Agent": "stock-bot contact@example.com"},
                timeout=10,
            )
            data = resp.json()
            for entry in data.values():
                _CIK_CACHE[entry["ticker"].upper()] = str(entry["cik_str"]).zfill(10)
        except Exception:
            return None
    return _CIK_CACHE.get(ticker.upper())

def get_recent_filings(ticker: str, limit: int = 3):
    cik = get_cik(ticker)
    if not cik:
        return []
    try:
        resp = requests.get(
            f"https://data.sec.gov/submissions/CIK{cik}.json",
            headers={"User-Agent": "stock-bot contact@example.com"},
            timeout=10,
        )
        recent = resp.json().get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        accessions = recent.get("accessionNumber", [])
        docs = recent.get("primaryDocument", [])
        filings = []
        for i in range(min(limit, len(forms))):
            acc_nodash = accessions[i].replace("-", "")
            link = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_nodash}/{docs[i]}"
            filings.append({
                "form": forms[i], "date": dates[i],
                "accession": accessions[i], "link": link,
            })
        return filings
    except Exception:
        return []

def get_pct_change(ticker: str):
    data = yf.Ticker(ticker).history(period="2d")
    if len(data) < 2:
        return None, None
    prev_close = data['Close'].iloc[-2]
    last_close = data['Close'].iloc[-1]
    pct = round((last_close - prev_close) / prev_close * 100, 2)
    return pct, round(last_close, 2)

def get_price(ticker: str):
    data = yf.Ticker(ticker).history(period="1d")
    if data.empty:
        return None
    return round(data['Close'].iloc[-1], 2)

def get_volume_spike(ticker: str):
    data = yf.Ticker(ticker).history(period="20d")
    if len(data) < 6:
        return None
    today_volume = data['Volume'].iloc[-1]
    avg_volume = data['Volume'].iloc[:-1].mean()
    if avg_volume == 0:
        return None
    ratio = round(today_volume / avg_volume, 1)
    return ratio

def get_news(ticker: str, limit: int = 3):
    try:
        items = yf.Ticker(ticker).news or []
    except Exception:
        return []
    headlines = []
    for item in items[:limit]:
        content = item.get("content", item)
        title = content.get("title") or item.get("title")
        link = (content.get("canonicalUrl") or {}).get("url") or item.get("link")
        if title:
            headlines.append((title, link))
    return headlines

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Commands:\n"
        "/watch TICKER - add to watchlist\n"
        "/unwatch TICKER - remove from watchlist\n"
        "/list - show watchlist with current prices\n"
        "/alert TICKER above PRICE - alert when price rises above\n"
        "/alert TICKER below PRICE - alert when price falls below\n"
        "/pctalert TICKER PERCENT - alert on a daily move of that size, e.g. /pctalert EDBL 5\n"
        "/pctalertall PERCENT - apply that alert to your whole watchlist, and to every ticker you add from now on\n"
        "/alerts - show your active alerts\n"
        "/price TICKER - check a price on demand\n"
        "/news TICKER - latest headlines for a stock\n"
        "\nSEC filings for anything on your watchlist are sent automatically."
    )

async def pctalert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 2:
        await update.message.reply_text("Usage: /pctalert TICKER PERCENT   e.g. /pctalert EDBL 5")
        return
    ticker = context.args[0].upper()
    try:
        percent = abs(float(context.args[1]))
    except ValueError:
        await update.message.reply_text("Percent must be a number, e.g. /pctalert EDBL 5")
        return
    chat_id = update.effective_chat.id
    conn = get_conn()
    conn.execute("DELETE FROM pct_alerts WHERE chat_id=? AND ticker=?", (chat_id, ticker))
    conn.execute("INSERT INTO pct_alerts VALUES (?, ?, ?)", (chat_id, ticker, percent))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"Alert set: {ticker} moves \u00b1{percent}% in a day")

async def pctalertall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /pctalertall PERCENT   e.g. /pctalertall 5")
        return
    try:
        percent = abs(float(context.args[0]))
    except ValueError:
        await update.message.reply_text("Percent must be a number, e.g. /pctalertall 5")
        return
    chat_id = update.effective_chat.id
    conn = get_conn()
    conn.execute(
        "INSERT INTO user_settings (chat_id, default_pct) VALUES (?, ?) "
        "ON CONFLICT(chat_id) DO UPDATE SET default_pct=excluded.default_pct",
        (chat_id, percent),
    )
    tickers = conn.execute("SELECT ticker FROM watchlist WHERE chat_id=?", (chat_id,)).fetchall()
    for (ticker,) in tickers:
        conn.execute("DELETE FROM pct_alerts WHERE chat_id=? AND ticker=?", (chat_id, ticker))
        conn.execute("INSERT INTO pct_alerts VALUES (?, ?, ?)", (chat_id, ticker, percent))
    conn.commit()
    conn.close()
    await update.message.reply_text(
        f"Set \u00b1{percent}% alerts on all {len(tickers)} watchlist tickers. "
        f"New tickers you /watch from now on will get this automatically too."
    )

async def news(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /news TICKER")
        return
    ticker = context.args[0].upper()
    headlines = get_news(ticker)
    if not headlines:
        await update.message.reply_text(f"No recent news found for {ticker}")
        return
    lines = [f"{ticker} news:"]
    for title, link in headlines:
        lines.append(f"- {title}\n{link}" if link else f"- {title}")
    await update.message.reply_text("\n".join(lines), disable_web_page_preview=True)

async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /price TICKER")
        return
    ticker = context.args[0].upper()
    p = get_price(ticker)
    if p is None:
        await update.message.reply_text(f"Couldn't find {ticker}")
        return
    await update.message.reply_text(f"{ticker}: ${p}")

async def watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /watch TICKER [TICKER2 TICKER3 ...]")
        return
    chat_id = update.effective_chat.id
    conn = get_conn()
    default = conn.execute(
        "SELECT default_pct FROM user_settings WHERE chat_id=?", (chat_id,)
    ).fetchone()
    added, skipped = [], []
    for raw in context.args:
        ticker = raw.upper()
        existing = conn.execute(
            "SELECT 1 FROM watchlist WHERE chat_id=? AND ticker=?", (chat_id, ticker)
        ).fetchone()
        if existing:
            skipped.append(ticker)
            continue
        conn.execute("INSERT INTO watchlist VALUES (?, ?)", (chat_id, ticker))
        if default:
            conn.execute("DELETE FROM pct_alerts WHERE chat_id=? AND ticker=?", (chat_id, ticker))
            conn.execute("INSERT INTO pct_alerts VALUES (?, ?, ?)", (chat_id, ticker, default[0]))
        added.append(ticker)
    conn.commit()
    conn.close()
    lines = []
    if added:
        suffix = f" with \u00b1{default[0]}% alerts" if default else ""
        lines.append(f"Added: {', '.join(added)}{suffix}")
    if skipped:
        lines.append(f"Already watching: {', '.join(skipped)}")
    await update.message.reply_text("\n".join(lines))

async def unwatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /unwatch TICKER [TICKER2 TICKER3 ...]")
        return
    chat_id = update.effective_chat.id
    conn = get_conn()
    removed = []
    for raw in context.args:
        ticker = raw.upper()
        conn.execute("DELETE FROM watchlist WHERE chat_id=? AND ticker=?", (chat_id, ticker))
        conn.execute("DELETE FROM pct_alerts WHERE chat_id=? AND ticker=?", (chat_id, ticker))
        removed.append(ticker)
    conn.commit()
    conn.close()
    await update.message.reply_text(f"Removed: {', '.join(removed)}")
async def list_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    conn = get_conn()
    rows = conn.execute("SELECT ticker FROM watchlist WHERE chat_id=?", (chat_id,)).fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("Your watchlist is empty. Add one with /watch TICKER")
        return
    lines = []
    for (ticker,) in rows:
        p = get_price(ticker)
        lines.append(f"{ticker}: ${p}" if p is not None else f"{ticker}: unavailable")
    await update.message.reply_text("\n".join(lines))

async def alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 3 or context.args[1].lower() not in ("above", "below"):
        await update.message.reply_text("Usage: /alert TICKER above PRICE   or   /alert TICKER below PRICE")
        return
    ticker = context.args[0].upper()
    direction = context.args[1].lower()
    try:
        target_price = float(context.args[2])
    except ValueError:
        await update.message.reply_text("Price must be a number, e.g. /alert EDBL above 5.00")
        return
    chat_id = update.effective_chat.id
    conn = get_conn()
    conn.execute("INSERT INTO alerts VALUES (?, ?, ?, ?)", (chat_id, ticker, direction, target_price))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"Alert set: {ticker} {direction} ${target_price}")

async def list_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    conn = get_conn()
    rows = conn.execute(
        "SELECT ticker, direction, price FROM alerts WHERE chat_id=?", (chat_id,)
    ).fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("You have no active alerts.")
        return
    lines = [f"{t} {d} ${p}" for t, d, p in rows]
    await update.message.reply_text("\n".join(lines))

async def check_alerts(context: ContextTypes.DEFAULT_TYPE):
    conn = get_conn()
    rows = conn.execute("SELECT rowid, chat_id, ticker, direction, price FROM alerts").fetchall()
    for rowid, chat_id, ticker, direction, target_price in rows:
        current = get_price(ticker)
        if current is None:
            continue
        triggered = (direction == "above" and current >= target_price) or \
                    (direction == "below" and current <= target_price)
        if triggered:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"ALERT: {ticker} is {direction} ${target_price} (currently ${current})"
            )
            conn.execute("DELETE FROM alerts WHERE rowid=?", (rowid,))
            conn.commit()
    conn.close()

async def send_watchlist_updates(context: ContextTypes.DEFAULT_TYPE):
    conn = get_conn()
    chat_ids = conn.execute("SELECT DISTINCT chat_id FROM watchlist").fetchall()
    for (chat_id,) in chat_ids:
        rows = conn.execute("SELECT ticker FROM watchlist WHERE chat_id=?", (chat_id,)).fetchall()
        lines = []
        for (ticker,) in rows:
            p = get_price(ticker)
            line = f"{ticker}: ${p}" if p is not None else f"{ticker}: unavailable"
            headlines = get_news(ticker, limit=1)
            if headlines:
                line += f"\n  📰 {headlines[0][0]}"
            lines.append(line)
        if lines:
            await context.bot.send_message(chat_id=chat_id, text="Watchlist update:\n" + "\n".join(lines))
    conn.close()

async def check_pct_alerts(context: ContextTypes.DEFAULT_TYPE):
    conn = get_conn()
    rows = conn.execute("SELECT rowid, chat_id, ticker, percent FROM pct_alerts").fetchall()
    for rowid, chat_id, ticker, threshold in rows:
        pct, current = get_pct_change(ticker)
        if pct is None:
            continue
        if abs(pct) >= threshold:
            direction = "up" if pct > 0 else "down"
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"ALERT: {ticker} is {direction} {abs(pct)}% today (${current})"
            )
            conn.execute("DELETE FROM pct_alerts WHERE rowid=?", (rowid,))
            conn.commit()
    conn.close()

async def check_volume_spikes(context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    conn = get_conn()
    tickers = conn.execute("SELECT DISTINCT ticker FROM watchlist").fetchall()
    for (ticker,) in tickers:
        ratio = get_volume_spike(ticker)
        if ratio is None or ratio < 3:
            continue
        seen = conn.execute("SELECT last_date FROM volume_alerted WHERE ticker=?", (ticker,)).fetchone()
        if seen and seen[0] == today:
            continue
        conn.execute(
            "INSERT INTO volume_alerted (ticker, last_date) VALUES (?, ?) "
            "ON CONFLICT(ticker) DO UPDATE SET last_date=excluded.last_date",
            (ticker, today),
        )
        conn.commit()
        chat_ids = conn.execute("SELECT chat_id FROM watchlist WHERE ticker=?", (ticker,)).fetchall()
        for (chat_id,) in chat_ids:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"\U0001F4CA Volume alert: {ticker} is trading at {ratio}x its 20-day average volume"
            )
    conn.close()

async def check_sec_filings(context: ContextTypes.DEFAULT_TYPE):
    conn = get_conn()
    tickers = conn.execute("SELECT DISTINCT ticker FROM watchlist").fetchall()
    for (ticker,) in tickers:
        filings = get_recent_filings(ticker, limit=5)
        if not filings:
            continue
        seen = conn.execute("SELECT accession FROM sec_seen WHERE ticker=?", (ticker,)).fetchone()
        if seen is None:
            conn.execute("INSERT INTO sec_seen VALUES (?, ?)", (ticker, filings[0]["accession"]))
            conn.commit()
            continue
        new_filings = []
        for f in filings:
            if f["accession"] == seen[0]:
                break
            new_filings.append(f)
        if not new_filings:
            continue
        conn.execute(
            "UPDATE sec_seen SET accession=? WHERE ticker=?", (filings[0]["accession"], ticker)
        )
        conn.commit()
        chat_ids = conn.execute(
            "SELECT chat_id FROM watchlist WHERE ticker=?", (ticker,)
        ).fetchall()
        for f in reversed(new_filings):
            flag = "\U0001F6A8 Insider transaction: " if f["form"] == "4" else "New SEC filing: "
            for (chat_id,) in chat_ids:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"{flag}{ticker} filed a {f['form']} on {f['date']}\n{f['link']}"
                )
    conn.close()
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    heartbeats = get_all_heartbeats()
    now = datetime.now(ZoneInfo("UTC"))
    lines = ["Bot status:"]
    for name, val in heartbeats.items():
        if not val:
            lines.append(f"\u26aa {name}: no heartbeat seen")
            continue
        last_seen = datetime.fromisoformat(val)
        age = (now - last_seen).total_seconds()
        if age < 300:
            lines.append(f"\U0001F7E2 {name}: online ({int(age)}s ago)")
        else:
            lines.append(f"\U0001F534 {name}: stale ({int(age // 60)}m ago)")
    await update.message.reply_text("\n".join(lines))

async def heartbeat_job(context: ContextTypes.DEFAULT_TYPE):
    send_heartbeat("stock-bot")
app = Application.builder().token(os.environ["BOT_TOKEN"]).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("price", price))
app.add_handler(CommandHandler("watch", watch))
app.add_handler(CommandHandler("unwatch", unwatch))
app.add_handler(CommandHandler("list", list_watchlist))
app.add_handler(CommandHandler("alert", alert))
app.add_handler(CommandHandler("pctalert", pctalert))
app.add_handler(CommandHandler("pctalertall", pctalertall))
app.add_handler(CommandHandler("alerts", list_alerts))
app.add_handler(CommandHandler("news", news))
app.add_handler(CommandHandler("status", status))

app.job_queue.run_repeating(check_alerts, interval=300, first=10)
app.job_queue.run_repeating(heartbeat_job, interval=120, first=5)
app.job_queue.run_repeating(check_pct_alerts, interval=300, first=15)
app.job_queue.run_repeating(check_sec_filings, interval=1800, first=20)
app.job_queue.run_repeating(check_volume_spikes, interval=1800, first=25)
for hour in [8, 13, 15, 17]:
    app.job_queue.run_daily(send_watchlist_updates, time=dtime(hour=hour, tzinfo=ZoneInfo("America/New_York")))

app.run_polling()