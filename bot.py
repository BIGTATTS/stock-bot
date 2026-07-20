import os
import sqlite3
import requests
import yfinance as yf
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

DB_PATH = os.path.join(os.environ.get("DATA_DIR", "."), "bot.db")

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
        await update.message.reply_text("Usage: /watch TICKER")
        return
    ticker = context.args[0].upper()
    chat_id = update.effective_chat.id
    conn = get_conn()
    existing = conn.execute(
        "SELECT 1 FROM watchlist WHERE chat_id=? AND ticker=?", (chat_id, ticker)
    ).fetchone()
    if existing:
        await update.message.reply_text(f"{ticker} is already on your watchlist.")
    else:
        conn.execute("INSERT INTO watchlist VALUES (?, ?)", (chat_id, ticker))
        conn.commit()
        await update.message.reply_text(f"Added {ticker} to your watchlist.")
    conn.close()

async def unwatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /unwatch TICKER")
        return
    ticker = context.args[0].upper()
    chat_id = update.effective_chat.id
    conn = get_conn()
    conn.execute("DELETE FROM watchlist WHERE chat_id=? AND ticker=?", (chat_id, ticker))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"Removed {ticker} from your watchlist.")

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

async def check_sec_filings(context: ContextTypes.DEFAULT_TYPE):
    conn = get_conn()
    tickers = conn.execute("SELECT DISTINCT ticker FROM watchlist").fetchall()
    for (ticker,) in tickers:
        filings = get_recent_filings(ticker, limit=1)
        if not filings:
            continue
        latest = filings[0]
        seen = conn.execute("SELECT accession FROM sec_seen WHERE ticker=?", (ticker,)).fetchone()
        if seen is None:
            conn.execute("INSERT INTO sec_seen VALUES (?, ?)", (ticker, latest["accession"]))
            conn.commit()
            continue
        if seen[0] != latest["accession"]:
            conn.execute(
                "UPDATE sec_seen SET accession=? WHERE ticker=?", (latest["accession"], ticker)
            )
            conn.commit()
            chat_ids = conn.execute(
                "SELECT chat_id FROM watchlist WHERE ticker=?", (ticker,)
            ).fetchall()
            for (chat_id,) in chat_ids:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"New SEC filing: {ticker} filed a {latest['form']} on {latest['date']}\n{latest['link']}"
                )
    conn.close()

app = Application.builder().token(os.environ["BOT_TOKEN"]).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("price", price))
app.add_handler(CommandHandler("watch", watch))
app.add_handler(CommandHandler("unwatch", unwatch))
app.add_handler(CommandHandler("list", list_watchlist))
app.add_handler(CommandHandler("alert", alert))
app.add_handler(CommandHandler("pctalert", pctalert))
app.add_handler(CommandHandler("alerts", list_alerts))
app.add_handler(CommandHandler("news", news))

app.job_queue.run_repeating(check_alerts, interval=300, first=10)
app.job_queue.run_repeating(check_pct_alerts, interval=300, first=15)
app.job_queue.run_repeating(check_sec_filings, interval=1800, first=20)
app.job_queue.run_repeating(send_watchlist_updates, interval=3600, first=30)

app.run_polling()