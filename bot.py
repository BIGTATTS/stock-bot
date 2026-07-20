import os
import sqlite3
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
    return conn

def get_price(ticker: str):
    data = yf.Ticker(ticker).history(period="1d")
    if data.empty:
        return None
    return round(data['Close'].iloc[-1], 2)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Commands:\n"
        "/watch TICKER - add to watchlist\n"
        "/unwatch TICKER - remove from watchlist\n"
        "/list - show watchlist with current prices\n"
        "/alert TICKER above PRICE - alert when price rises above\n"
        "/alert TICKER below PRICE - alert when price falls below\n"
        "/alerts - show your active alerts\n"
        "/price TICKER - check a price on demand"
    )

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
            lines.append(f"{ticker}: ${p}" if p is not None else f"{ticker}: unavailable")
        if lines:
            await context.bot.send_message(chat_id=chat_id, text="Watchlist update:\n" + "\n".join(lines))
    conn.close()

app = Application.builder().token(os.environ["BOT_TOKEN"]).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("price", price))
app.add_handler(CommandHandler("watch", watch))
app.add_handler(CommandHandler("unwatch", unwatch))
app.add_handler(CommandHandler("list", list_watchlist))
app.add_handler(CommandHandler("alert", alert))
app.add_handler(CommandHandler("alerts", list_alerts))

app.job_queue.run_repeating(check_alerts, interval=300, first=10)
app.job_queue.run_repeating(send_watchlist_updates, interval=3600, first=30)

app.run_polling()