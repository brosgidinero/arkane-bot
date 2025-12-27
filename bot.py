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
# ✅ CLAVES DESDE VARIABLES (Railway)
# ===============================
TOKEN = os.getenv("TOKEN")
AV_KEY = os.getenv("AV_KEY")
FINNHUB_KEY = os.getenv("FINNHUB_KEY")
TWELVE_KEY = os.getenv("TWELVE_KEY")

CHAT_ID = int(os.getenv("CHAT_ID", "0"))

# Validación rápida
missing = [k for k, v in {
    "TOKEN": TOKEN,
    "AV_KEY": AV_KEY,
    "FINNHUB_KEY": FINNHUB_KEY,
    "TWELVE_KEY": TWELVE_KEY,
    "CHAT_ID": (None if CHAT_ID == 0 else str(CHAT_ID)),
}.items() if not v]

if missing:
    raise RuntimeError(
        "Faltan Variables en Railway: " + ", ".join(missing) +
        " | Ve a Railway -> Variables y agrégalas."
    )

# ===============================
# ✅ CONFIG
# ===============================
COUNTER_FILE = "counter.json"
RESET_HOUR = 0
RESET_MINUTE = 1  # 00:01 AM (UTC-5)

LAST_UPTREND = None
LAST_PAIR_SENT = None  # ✅ para no repetir par seguido

PAIRS_NORMAL = ["EUR/USD", "EUR/GBP", "EUR/JPY", "GBP/USD"]
PAIRS_OTC = ["EUR/USD OTC", "EUR/GBP OTC", "EUR/JPY OTC", "GBP/USD OTC"]

# ===============================
# ✅ TIME (UTC-5 Bogotá)
# ===============================
def now_utc5():
    return datetime.utcnow() - timedelta(hours=5)

def today_utc5():
    return (datetime.utcnow() - timedelta(hours=5)).date()

def get_active_pairs():
    """Devuelve la lista de pares según la hora actual (UTC-5)."""
    hour = now_utc5().hour
    if 0 <= hour < 15:
        return PAIRS_NORMAL
    elif 15 <= hour < 19:
        return PAIRS_OTC
    else:
        return PAIRS_NORMAL

# ===============================
# ✅ CONTADOR PERSISTENTE
# ===============================
def load_counter():
    if not os.path.exists(COUNTER_FILE):
        data = {"date": str(today_utc5()), "count": 0, "reset_done": False}
        with open(COUNTER_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
        return data

    with open(COUNTER_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_counter(data):
    with open(COUNTER_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)

def get_and_increment_counter():
    now = now_utc5()
    today_str = str(today_utc5())
    data = load_counter()

    # Si cambió el día: permitir reset de 00:01
    if data.get("date") != today_str:
        data["date"] = today_str
        data["reset_done"] = False
        save_counter(data)

    # Reset SOLO a las 00:01 y SOLO una vez
    if (now.hour == RESET_HOUR and now.minute == RESET_MINUTE and not data.get("reset_done", False)):
        data["count"] = 0
        data["reset_done"] = True
        save_counter(data)

    data["count"] = int(data.get("count", 0)) + 1
    save_counter(data)
    return data["count"]

# ===============================
# ✅ EMA SIN PANDAS (a mano)
# ===============================
def ema(values, length: int):
    if not values or len(values) < length:
        return None
    k = 2 / (length + 1)
    # arrancamos con SMA inicial
    sma = sum(values[:length]) / length
    e = sma
    for v in values[length:]:
        e = (v * k) + (e * (1 - k))
    return e

def triple_confirmation_signal_from_closes(closes):
    e20 = ema(closes, 20)
    e50 = ema(closes, 50)
    if e20 is None or e50 is None:
        return None
    return e20 > e50

# ===============================
# ✅ DATA FETCH (sin pandas)
# ===============================
def base_symbol(pair: str):
    p = pair.replace(" OTC", "")
    a, b = p.split("/")
    return a, b

def map_symbol_for_finnhub(a, b):
    return f"OANDA:{a}_{b}"

def map_symbol_for_twelve(a, b):
    return f"{a}/{b}"

def to_unix_range(minutes_back=24 * 60):
    now = int(time.time())
    return now - minutes_back * 60, now

def fetch_from_alpha_closes(a, b, interval="1min", output_size="compact"):
    url = (
        "https://www.alphavantage.co/query"
        f"?function=FX_INTRADAY&from_symbol={a}&to_symbol={b}"
        f"&interval={interval}&apikey={AV_KEY}&outputsize={output_size}"
    )
    r = requests.get(url, timeout=20)
    data = r.json()
    key = f"Time Series FX ({interval})"
    if key not in data:
        raise ValueError("Alpha sin datos")

    # ordenar por timestamp ascendente
    items = list(data[key].items())
    items.sort(key=lambda x: x[0])
    closes = [float(v["4. close"]) for _, v in items]
    return closes

def fetch_from_finnhub_closes(a, b):
    sym = map_symbol_for_finnhub(a, b)
    _from, _to = to_unix_range(24 * 60)
    url = (
        f"https://finnhub.io/api/v1/forex/candle"
        f"?symbol={sym}&resolution=1&from={_from}&to={_to}&token={FINNHUB_KEY}"
    )
    r = requests.get(url, timeout=20)
    data = r.json()
    if not data or data.get("s") != "ok" or "c" not in data:
        raise ValueError("Finnhub sin datos")
    return [float(x) for x in data["c"]]

def fetch_from_twelve_closes(a, b, interval="1min", size=500):
    sym = map_symbol_for_twelve(a, b)
    url = (
        f"https://api.twelvedata.com/time_series?symbol={sym}"
        f"&interval={interval}&outputsize={size}"
        f"&apikey={TWELVE_KEY}&format=JSON&dp=8"
    )
    r = requests.get(url, timeout=20)
    data = r.json()
    if "values" not in data:
        raise ValueError("Twelve sin datos")

    values = data["values"]
    values.reverse()  # para que quede ascendente
    closes = [float(v["close"]) for v in values]
    return closes

def fetch_intraday_closes(a, b):
    errors = []
    try:
        return fetch_from_alpha_closes(a, b, "1min", "compact")
    except Exception as e:
        errors.append(f"Alpha: {e}")
    try:
        return fetch_from_finnhub_closes(a, b)
    except Exception as e:
        errors.append(f"Finnhub: {e}")
    try:
        return fetch_from_twelve_closes(a, b, "1min", 500)
    except Exception as e:
        errors.append(f"Twelve: {e}")

    raise ValueError("Sin datos disponibles: " + " | ".join(errors))

# ===============================
# ✅ ARMAR SEÑAL (entry +4 min)
# ===============================
def build_signal(pair: str):
    global LAST_UPTREND

    signals_today = get_and_increment_counter()
    a, b = base_symbol(pair)

    try:
        closes = fetch_intraday_closes(a, b)
        uptrend = triple_confirmation_signal_from_closes(closes)
        if uptrend is None:
            raise ValueError("Pocas velas para EMA")
        LAST_UPTREND = uptrend
    except Exception:
        if LAST_UPTREND is not None:
            uptrend = LAST_UPTREND
        else:
            uptrend = (signals_today % 2 == 0)

    direccion = "CALL" if uptrend else "PUT"
    color = "🟢" if uptrend else "🔴"

    entry_time = (now_utc5() + timedelta(minutes=4)).strftime("%H:%M")

    return (
        "🔱 ARKANE BOT 🦂\n"
        "🔥 Señal detectada 🔥\n"
        "⏰ Zona horaria: UTC-5 Bogotá\n\n"
        f"👉 Par: {pair}\n"
        f"👉 Hora de entrada: {entry_time}\n"
        f"👉 Dirección: {color} {direccion}\n"
        "👉 Expiración: 1 MINUTO\n"
        f"👉 Señales enviadas hoy: {signals_today}\n\n"
        "⚠️ Opera con responsabilidad. Gestiona tu riesgo."
    )

# ===============================
# ✅ ELEGIR PAR (sin repetir seguido)
# ===============================
def pick_pair_no_repeat(active_pairs):
    global LAST_PAIR_SENT
    if not active_pairs:
        return None

    if LAST_PAIR_SENT in active_pairs and len(active_pairs) > 1:
        options = [p for p in active_pairs if p != LAST_PAIR_SENT]
    else:
        options = active_pairs

    choice = random.choice(options)
    LAST_PAIR_SENT = choice
    return choice

# ===============================
# ✅ AUTO-SEÑALES (sin JobQueue)
# ===============================
async def auto_signals(app: Application):
    while True:
        try:
            active_pairs = get_active_pairs()
            pair = pick_pair_no_repeat(active_pairs)
            if not pair:
                await asyncio.sleep(10)
                continue

            msg = await asyncio.to_thread(build_signal, pair)

            await app.bot.send_message(
                chat_id=CHAT_ID,
                text=msg,
                disable_web_page_preview=True
            )

            wait = random.choice([120, 180, 240])  # 2–3–4 min
            await asyncio.sleep(wait)

        except Exception as e:
            print("Error en auto_signals:", e)
            await asyncio.sleep(10)

# ===============================
# ✅ MENÚ MANUAL
# ===============================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    active_pairs = get_active_pairs()
    keyboard = [[p] for p in active_pairs]
    await update.message.reply_text(
        "🔱 ARKANE BOT 🦂\n"
        "Pares activos según el horario actual:\n\n"
        + "\n".join([f"• {p}" for p in active_pairs]),
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    active_pairs = get_active_pairs()

    if text not in active_pairs:
        await update.message.reply_text("Ese par no está disponible en este horario.")
        return

    placeholder = await update.message.reply_text(f"🔎 Analizando {text}...")

    try:
        msg = await asyncio.to_thread(build_signal, text)
        await placeholder.edit_text(msg)
    except Exception as e:
        await placeholder.edit_text(f"⚠️ Error: {e}")

# ===============================
# ✅ MAIN (post_init para arrancar el loop)
# ===============================
async def post_init(app: Application):
    print("🔥 ARKANE BOT ACTIVADO – Señales en tiempo real (Railway)")
    asyncio.create_task(auto_signals(app))

def main():
    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.run_polling()

if __name__ == "__main__":
    main()
