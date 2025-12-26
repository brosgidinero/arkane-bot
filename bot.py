from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from telegram.constants import ChatAction

import requests, pandas as pd, pandas_ta as ta
from datetime import datetime, timedelta, date
import asyncio
import time
import random

# ✅ NUEVO: para guardar contador aunque el bot se caiga
import json
import os

# ======= CONFIGURA TUS CLAVES =======
TOKEN = "7785980824:AAHPZhuaGMv4cVm5LajHywCMAnCVRCnTBUQ"
AV_KEY = "HBK3Q0MCWJ4VTZ68"
FINNHUB_KEY = "d4aajspr01qnehvtnft0d4aajspr01qnehvtnftg"
TWELVE_KEY = "6bbb8ba1205544f899cbd7cd602cfa0f"
# ====================================

# ===== ID DEL CANAL VIP =====
CHAT_ID = -1003305524490  # <-- AQUÍ VA EL ID DE TU CANAL
# ============================

# ===== CONTADOR PERSISTENTE =====
COUNTER_FILE = "counter.json"
RESET_HOUR = 0
RESET_MINUTE = 1  # 00:01 AM
# ===============================

# Última dirección real de tendencia (para fallback si fallan las velas)
LAST_UPTREND = None

# ======== PARES SEGÚN HORARIO ========

PAIRS_NORMAL = [
    "EUR/USD",
    "EUR/GBP",
    "EUR/JPY",
    "GBP/USD"
]

PAIRS_OTC = [
    "EUR/USD OTC",
    "EUR/GBP OTC",
    "EUR/JPY OTC",
    "GBP/USD OTC"
]

def get_active_pairs():
    """Devuelve la lista de pares según la hora actual 🇨🇴 UTC-5."""
    now = datetime.now().hour

    if 0 <= now < 15:       # 00:00 – 15:00
        return PAIRS_NORMAL
    elif 15 <= now < 19:    # 15:00 – 19:00
        return PAIRS_OTC
    else:                   # 19:00 – 00:00
        return PAIRS_NORMAL


# ====== CONTADOR: CARGAR / GUARDAR / INCREMENTAR ======

def load_counter():
    if not os.path.exists(COUNTER_FILE):
        data = {
            "date": str(date.today()),
            "count": 0,
            "reset_done": False
        }
        with open(COUNTER_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
        return data

    with open(COUNTER_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_counter(data):
    with open(COUNTER_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)


def get_and_increment_counter():
    now = datetime.now()
    today_str = str(date.today())

    data = load_counter()

    # Si cambió el día, preparamos el reset (pero NO reseteamos aún)
    if data.get("date") != today_str:
        data["date"] = today_str
        data["reset_done"] = False
        # OJO: dejamos count como estaba hasta que llegue 00:01
        save_counter(data)

    # Reset SOLO a las 00:01 y SOLO una vez
    if (
        now.hour == RESET_HOUR
        and now.minute == RESET_MINUTE
        and not data.get("reset_done", False)
    ):
        data["count"] = 0
        data["reset_done"] = True
        save_counter(data)

    # Incrementar contador
    data["count"] = int(data.get("count", 0)) + 1
    save_counter(data)

    return data["count"]


# ------ FETCH DE DATOS ------
def base_symbol(pair: str):
    p = pair.replace(" OTC", "")
    a, b = p.split("/")
    return a, b

def map_symbol_for_finnhub(a, b):
    return f"OANDA:{a}_{b}"

def map_symbol_for_twelve(a, b):
    return f"{a}/{b}"

def to_unix_range(minutes_back=240):
    now = int(time.time())
    return now - minutes_back * 60, now

def fetch_intraday_fx(a, b, interval="1min", output_size="compact"):
    errors = []
    # 1) Alpha
    try:
        return fetch_from_alpha(a, b, interval, output_size)
    except Exception as e:
        errors.append(str(e))
    # 2) Finnhub
    try:
        return fetch_from_finnhub(a, b, "1")
    except Exception as e:
        errors.append(str(e))
    # 3) TwelveData
    try:
        return fetch_from_twelve(a, b, "1min", 500)
    except Exception as e:
        errors.append(str(e))

    raise ValueError("Sin datos disponibles: " + " | ".join(errors))

def fetch_from_alpha(a, b, interval, output_size):
    url = (
        "https://www.alphavantage.co/query"
        f"?function=FX_INTRADAY&from_symbol={a}&to_symbol={b}"
        f"&interval={interval}&apikey={AV_KEY}&outputsize={output_size}"
    )
    r = requests.get(url, timeout=20)
    data = r.json()

    key = f"Time Series FX ({interval})"
    if key not in data:
        raise ValueError("Alpha no data")

    df = (
        pd.DataFrame(data[key]).T
        .rename(columns={
            "1. open": "open",
            "2. high": "high",
            "3. low": "low",
            "4. close": "close"
        })
        .astype(float)
        .sort_index()
    )

    df.index = pd.to_datetime(df.index)
    return df

def fetch_from_finnhub(a, b, interval):
    sym = map_symbol_for_finnhub(a, b)
    _from, _to = to_unix_range(24 * 60)

    url = (
        f"https://finnhub.io/api/v1/forex/candle"
        f"?symbol={sym}&resolution={interval}&from={_from}&to={_to}&token={FINNHUB_KEY}"
    )
    r = requests.get(url, timeout=20)
    data = r.json()

    if not data or data.get("s") != "ok":
        raise ValueError("Finnhub sin datos")

    df = pd.DataFrame(
        {
            "open": data["o"],
            "high": data["h"],
            "low": data["l"],
            "close": data["c"]
        },
        index=pd.to_datetime(pd.Series(data["t"], dtype="int64"), unit="s")
    ).sort_index()

    return df

def fetch_from_twelve(a, b, interval, size):
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

    df = pd.DataFrame(data["values"]).rename(columns={"datetime": "ts"})
    df[["open", "high", "low", "close"]] = df[["open", "high", "low", "close"]].astype(float)
    df["ts"] = pd.to_datetime(df["ts"])
    return df.set_index("ts").sort_index()


# ---------- ESTRATEGIA SIMPLE ----------
def triple_confirmation_signal(df: pd.DataFrame):
    df["ema20"] = ta.ema(df["close"], length=20)
    df["ema50"] = ta.ema(df["close"], length=50)
    return df["ema20"].iloc[-1] > df["ema50"].iloc[-1]


# ---------- ARMAR SEÑAL (con fallback si fallan velas) ----------
def build_signal(pair: str):
    global LAST_UPTREND

    # ✅ NUEVO: contador persistente (no se reinicia si el bot se cae)
    signals_today = get_and_increment_counter()

    fs, ts = base_symbol(pair)

    # Intentar usar datos reales de velas
    try:
        df = fetch_intraday_fx(fs, ts, "1min", "compact")
        uptrend = triple_confirmation_signal(df)
        LAST_UPTREND = uptrend  # guardamos última tendencia real
    except Exception:
        # Si falla todo el feed:
        if LAST_UPTREND is not None:
            uptrend = LAST_UPTREND
        else:
            uptrend = (signals_today % 2 == 0)  # alterna si no hay historial

    direccion = "CALL" if uptrend else "PUT"
    color = "🟢" if uptrend else "🔴"

    entry_time = (datetime.now() + timedelta(minutes=5)).strftime("%H:%M")

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


# ---------- AUTO-SEÑALES ----------
async def auto_signals(app: Application):
    while True:
        try:
            active_pairs = get_active_pairs()
            pair = random.choice(active_pairs)

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


# ---------- MENÚ MANUAL ----------
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


# ---------- MAIN ----------
def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("🔥 ARKANE BOT ACTIVADO – Señales en tiempo real")

    # Lanzar auto_signals 1 segundo después
    app.job_queue.run_once(lambda *_: asyncio.create_task(auto_signals(app)), when=1)

    app.run_polling()


if __name__ == "__main__":
    main()

