import os
import json
import time
import random
import asyncio
import requests
from datetime import datetime, timedelta, date

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# ===============================
# 🔑 VARIABLES DE ENTORNO (Railway)
# ===============================
TOKEN = os.getenv("TOKEN")
AV_KEY = os.getenv("AV_KEY")
FINNHUB_KEY = os.getenv("FINNHUB_KEY")
TWELVE_KEY = os.getenv("TWELVE_KEY")
CHAT_ID = int(os.getenv("CHAT_ID", "0"))

missing = [k for k, v in {
    "TOKEN": TOKEN,
    "AV_KEY": AV_KEY,
    "FINNHUB_KEY": FINNHUB_KEY,
    "TWELVE_KEY": TWELVE_KEY,
    "CHAT_ID": None if CHAT_ID != 0 else None,
}.items() if v is None]

if missing:
    raise RuntimeError(
        "Faltan variables en Railway: " + ", ".join(missing)
    )

# ===============================
# ⏰ TIEMPO UTC-5 (Bogotá)
# ===============================
def now_utc5():
    return datetime.utcnow() - timedelta(hours=5)

def today_utc5():
    return now_utc5().date()

# ===============================
# 📊 CONTADOR PERSISTENTE
# ===============================
COUNTER_FILE = "counter.json"
RESET_HOUR = 0
RESET_MINUTE = 1

def load_counter():
    if not os.path.exists(COUNTER_FILE):
        data = {"date": str(today_utc5()), "count": 0, "reset_done": False}
        with open(COUNTER_FILE, "w") as f:
            json.dump(data, f)
        return data
    with open(COUNTER_FILE, "r") as f:
        return json.load(f)

def save_counter(data):
    with open(COUNTER_FILE, "w") as f:
        json.dump(data, f)

def get_and_increment_counter():
    now = now_utc5()
    today = str(today_utc5())
    data = load_counter()

    if data["date"] != today:
        data["date"] = today
        data["reset_done"] = False

    if (
        now.hour == RESET_HOUR
        and now.minute == RESET_MINUTE
        and not data["reset_done"]
    ):
        data["count"] = 0
        data["reset_done"] = True

    data["count"] += 1
    save_counter(data)
    return data["count"]

# ===============================
# 💱 PARES
# ===============================
PAIRS_NORMAL = ["EUR/USD", "EUR/GBP", "EUR/JPY", "GBP/USD"]
PAIRS_OTC = ["EUR/USD OTC", "EUR/GBP OTC", "EUR/JPY OTC", "GBP/USD OTC"]

def is_otc_weekend(now):
    wd = now.weekday()  # lunes=0
    h = now.hour
    if wd == 4 and h >= 13:  # viernes 13:00+
        return True
    if wd == 5:             # sábado completo
        return True
    if wd == 6 and h < 19:  # domingo <19:00
        return True
    return False

def get_active_pairs():
    now = now_utc5()
    h = now.hour

    if is_otc_weekend(now):
        return PAIRS_OTC

    if 0 <= h < 15:
        return PAIRS_NORMAL
    elif 15 <= h < 19:
        return PAIRS_OTC
    else:
        return PAIRS_NORMAL

# ===============================
# 📈 EMA MANUAL (SIN PANDAS)
# ===============================
def ema(values, length):
    if len(values) < length:
        return None
    k = 2 / (length + 1)
    e = sum(values[:length]) / length
    for v in values[length:]:
        e = v * k + e * (1 - k)
    return e

def trend_from_closes(closes):
    e20 = ema(closes, 20)
    e50 = ema(closes, 50)
    if e20 is None or e50 is None:
        return None
    return e20 > e50

# ===============================
# 🌐 DATA FETCH
# ===============================
def base_symbol(pair):
    return pair.replace(" OTC", "").split("/")

def fetch_closes(a, b):
    url = (
        "https://www.alphavantage.co/query"
        f"?function=FX_INTRADAY&from_symbol={a}&to_symbol={b}"
        f"&interval=1min&apikey={AV_KEY}&outputsize=compact"
    )
    r = requests.get(url, timeout=20)
    data = r.json()
    key = "Time Series FX (1min)"
    if key not in data:
        raise ValueError("Alpha sin datos")

    items = sorted(data[key].items())
    return [float(v["4. close"]) for _, v in items]

# ===============================
# 🔔 SEÑAL
# ===============================
LAST_UPTREND = None
LAST_PAIR = None

def build_signal(pair):
    global LAST_UPTREND

    count = get_and_increment_counter()
    a, b = base_symbol(pair)

    try:
        closes = fetch_closes(a, b)
        up = trend_from_closes(closes)
        if up is None:
            raise ValueError()
        LAST_UPTREND = up
    except Exception:
        up = LAST_UPTREND if LAST_UPTREND is not None else count % 2 == 0

    direction = "CALL" if up else "PUT"
    color = "🟢" if up else "🔴"
    entry = (now_utc5() + timedelta(minutes=4)).strftime("%H:%M")

    return (
        "🔱 ARKANE BOT 🦂\n"
        "🔥 Señal detectada 🔥\n"
        "⏰ Zona horaria: UTC-5 Bogotá\n\n"
        f"👉 Par: {pair}\n"
        f"👉 Hora de entrada: {entry}\n"
        f"👉 Dirección: {color} {direction}\n"
        "👉 Expiración: 1 MINUTO\n"
        f"👉 Señales enviadas hoy: {count}\n\n"
        "⚠️ Opera con responsabilidad."
    )

def pick_pair(pairs):
    global LAST_PAIR
    options = [p for p in pairs if p != LAST_PAIR] or pairs
    choice = random.choice(options)
    LAST_PAIR = choice
    return choice

# ===============================
# 🤖 AUTO SEÑALES
# ===============================
async def auto_signals(app):
    while True:
        try:
            pairs = get_active_pairs()
            pair = pick_pair(pairs)
            msg = await asyncio.to_thread(build_signal, pair)
            await app.bot.send_message(CHAT_ID, msg)
            await asyncio.sleep(random.choice([120, 180, 240]))
        except Exception as e:
            print("Error:", e)
            await asyncio.sleep(10)

# ===============================
# 📲 TELEGRAM
# ===============================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pairs = get_active_pairs()
    await update.message.reply_text(
        "🔱 ARKANE BOT 🦂\n\nPares activos:\n" + "\n".join(pairs),
        reply_markup=ReplyKeyboardMarkup([[p] for p in pairs], resize_keyboard=True)
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pair = update.message.text
    if pair not in get_active_pairs():
        await update.message.reply_text("Par no disponible.")
        return
    msg = await asyncio.to_thread(build_signal, pair)
    await update.message.reply_text(msg)

# ===============================
# 🚀 MAIN
# ===============================
async def post_init(app):
    asyncio.create_task(auto_signals(app))

def main():
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()

if __name__ == "__main__":
    main()
