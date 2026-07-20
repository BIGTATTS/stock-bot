from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import yfinance as yf

async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /price TICKER")
        return
    ticker = context.args[0].upper()
    data = yf.Ticker(ticker).history(period="1d")
    if data.empty:
        await update.message.reply_text(f"Couldn't find {ticker}")
        return
    last = data['Close'].iloc[-1]
    await update.message.reply_text(f"{ticker}: ${last:.2f}")

import os
app = Application.builder().token(os.environ["BOT_TOKEN"]).build()
app.add_handler(CommandHandler("price", price))
app.run_polling()

