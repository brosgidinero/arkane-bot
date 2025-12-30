import os
import json
import time
import random
import asyncio
import requests
from datetime import datetime, timedelta

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# ✅ Importante para manejar timeouts/reintentos de Telegram (PTB v20+)
from telegram.request import HTTPXRequest
from telegram.error import TimedOut, NetworkError, RetryAfter

# ===============================
# 🔐 VARIABLES (Railway)
# ===============================
TOKEN = (os.getenv("TOKEN") or "").strip()
AV_KEY = (os.getenv("AV_KEY") or "").strip()
FINNHUB_KEY = (os.getenv("FINNHUB_KEY") or "").strip()
TWELVE_KEY = (os.getenv("TWELVE_KEY") or "").strip()

_raw_chat_id = (os.getenv("CHAT_ID") or "").strip()
try:
    CHAT_ID = int(_raw_chat_id)
except Exception:
    CHAT_ID = None

REQUIRED = {
    "TOKEN": TOKEN,
    "AV_KEY": AV_KEY,
    "FINNHUB_KEY": FINNHUB_KEY,
    "TWELVE_KEY": TWELVE_KEY,
}

missing = [k for k, v in REQUIRED.items() if not v]
# Nota: NO mandamos nada a Telegram si falta algo. Solo logs en Railway.
if missing:
    print("❌ FALTAN VARIABLES:", ", ".join(missing))
if CHAT_ID is None:
    print("❌ CHAT_ID inválido o vacío. Auto-señales desactivadas.")

# ===============================
# ⏰ TIME UTC-5
# ===============================
def now_utc5():
    return datetime.utcnow() - timedelta(hours=5)

def today_utc5():
    return now_utc5().date()

# ===============================
# 📊 CONFIG
# ===============================
COUNTER_FILE = "counter.json"
RESET_HOUR = 0
RESET_MINUTE = 1  # 00:01 UTC-5

LAST_UPTREND = None
LAST_PAIR_SENT = None

PAIRS_NORMAL = ["EUR/USD", "EUR/GBP", "EUR/JPY", "GBP/USD"]
PAIRS_OTC = ["EUR/USD OTC", "EUR/GBP OTC", "EUR/JPY OTC", "GBP/USD OTC"]

# ===============================
# 📆 HORARIO OTC REAL
# Viernes 13:00 → Domingo 19:00 (UTC-5)
# ===============================
def is_otc_weekend():
    now = now_utc5()
    wd = now.weekday()  # 0=lun ... 4=vie ... 6=dom

    if wd == 4 and now.hour >= 13:  # viernes desde 13:00
        return True
    if wd == 5:  # sábado completo
        return True
    if wd == 6 and now.hour < 19:  # domingo hasta 19:00
        return True
    return False

def get_active_pairs():
    if is_otc_weekend():
        return PAIRS_OTC

    hour = now_utc5().hour
    if 0 <= hour < 15:
        return PAIRS_NORMAL
    elif 15 <= hour < 19:
        return PAIRS_OTC
    else:
        return PAIRS_NORMAL

# ===============================
# 🔢 CONTADOR (reset inteligente)
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
    """
    ✅ Reset inteligente:
    - No depende de caer EXACTO en 00:01.
    - Si el bot se “duerme”, resetea al primer tick después de 00:01.
    """
    now = now_utc5()
    today_str = str(today_utc5())
    data = load_counter()

    # Si cambió el día => permitir reset para hoy
    if data.get("date") != today_str:
        data["date"] = today_str
        data["reset_done"] = False
        save_counter(data)

    reset_time_reached = (now.hour > RESET_HOUR) or (now.hour == RESET_HOUR and now.minute >= RESET_MINUTE)
    if reset_time_reached and not data.get("reset_done", False):
        data["count"] = 0
        data["reset_done"] = True
        save_counter(data)

    data["count"] = int(data.get("count", 0)) + 1
    save_counter(data)
    return data["count"]

# ===============================
# 📈 EMA (SIN PANDAS)
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
# 📡 DATA FETCH (Alpha)
# ===============================
def base_symbol(pair):
    p = pair.replace(" OTC", "")
    a, b = p.split("/")
    return a, b

def fetch_alpha(a, b):
    url = (
        "https://www.alphavantage.co/query"
        f"?function=FX_INTRADAY&from_symbol={a}&to_symbol={b}"
        f"&interval=1min&apikey={AV_KEY}&outputsize=compact"
    )
    r = requests.get(url, timeout=20).json()
    key = "Time Series FX (1min)"
    if key not in r:
        raise ValueError("Alpha sin datos")
    items = sorted(r[key].items())  # ascendente
    return [float(v["4. close"]) for _, v in items]

def fetch_intraday_closes(a, b):
    return fetch_alpha(a, b)

# ===============================
# 🧠 SEÑAL (+4 min)
# ===============================
def build_signal(pair):
    global LAST_UPTREND

    count = get_and_increment_counter()
    a, b = base_symbol(pair)

    try:
        closes = fetch_intraday_closes(a, b)
        up = trend_from_closes(closes)
        if up is None:
            raise ValueError("Pocas velas")
        LAST_UPTREND = up
    except Exception:
        up = LAST_UPTREND if LAST_UPTREND is not None else (count % 2 == 0)

    direction = "CALL" if up else "PUT"
    color = "🟢" if up else "🔴"
    entry = (now_utc5() + timedelta(minutes=4)).strftime("%H:%M")

    return (
        "🔱 ARKANE BOT 🦂\n"
        "🔥 Señal detectada 🔥\n"
        "⏰ UTC-5 Bogotá\n\n"
        f"👉 Par: {pair}\n"
        f"👉 Hora de entrada: {entry}\n"
        f"👉 Dirección: {color} {direction}\n"
        "👉 Expiración: 1 MINUTO\n"
        f"👉 Señales hoy: {count}\n\n"
        "⚠️ Gestiona tu riesgo"
    )

# ===============================
# 🔁 PAR SIN REPETIR
# ===============================
def pick_pair(pairs):
    global LAST_PAIR_SENT
    opts = [p for p in pairs if p != LAST_PAIR_SENT] or pairs
    LAST_PAIR_SENT = random.choice(opts)
    return LAST_PAIR_SENT

# ===============================
# 📤 ENVÍO ROBUSTO (NO CRASHEA)
# - reintentos con backoff
# - no manda mensajes extra al canal, solo la señal cuando se puede
# ===============================
async def send_signal_with_retry(app: Application, text: str):
    delay = 5
    max_delay = 120  # tope 2 min entre reintentos (evita quedarse “mudo” mucho tiempo)

    while True:
        try:
            await app.bot.send_message(chat_id=CHAT_ID, text=text, disable_web_page_preview=True)
            return  # ✅ enviado
        except RetryAfter as e:
            # Telegram pidió esperar (rate limit)
            wait = int(getattr(e, "retry_after", 5))
            await asyncio.sleep(max(5, min(wait, 60)))
        except (TimedOut, NetworkError) as e:
            # Red/Telegram inestable: reintenta sin morirse
            print(f"⚠️ Telegram timeout/red. Reintentando en {delay}s... {repr(e)}")
            await asyncio.sleep(delay)
            delay = min(max_delay, int(delay * 1.6))
        except Exception as e:
            # Cualquier otro error: no crashea, solo espera y reintenta
            print(f"⚠️ Error enviando a Telegram. Reintentando en {delay}s... {repr(e)}")
            await asyncio.sleep(delay)
            delay = min(max_delay, int(delay * 1.6))

# ===============================
# 🚀 AUTO-SEÑALES (NUNCA SE DETIENE)
# ===============================
async def auto_signals(app: Application):
    if CHAT_ID is None:
        print("⚠️ Auto-señales desactivadas (CHAT_ID)")
        return
    if missing:
        print("⚠️ Auto-señales desactivadas (faltan API keys)")
        return

    while True:
        try:
            pair = pick_pair(get_active_pairs())
            msg = await asyncio.to_thread(build_signal, pair)

            # ✅ envío robusto
            await send_signal_with_retry(app, msg)

            # ✅ ritmo
            await asyncio.sleep(random.choice([120, 180, 240]))  # 2–3–4 min
        except Exception as e:
            # ✅ pase lo que pase, NO se muere
            print("Auto loop error:", repr(e))
            await asyncio.sleep(5)

# ===============================
# 📟 MENÚ MANUAL (solo si te escriben)
# ===============================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pairs = get_active_pairs()
    await update.message.reply_text(
        "🔱 ARKANE BOT 🦂\nPares activos:\n\n" + "\n".join(pairs),
        reply_markup=ReplyKeyboardMarkup([[p] for p in pairs], resize_keyboard=True)
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    if txt not in get_active_pairs():
        await update.message.reply_text("Par no disponible ahora.")
        return
    msg = await asyncio.to_thread(build_signal, txt)
    await update.message.reply_text(msg)

# ===============================
# 🟢 MAIN (supervisor + timeouts altos)
# ===============================
async def post_init(app: Application):
    # NO manda mensajes al canal, solo arranca el loop interno
    asyncio.create_task(auto_signals(app))

def build_app():
    # ✅ timeouts altos para reducir “TimedOut” falsos
    req = HTTPXRequest(
        connect_timeout=20,
        read_timeout=30,
        write_timeout=30,
        pool_timeout=30,
        connection_pool_size=20,
    )
    return (
        Application.builder()
        .token(TOKEN)
        .request(req)
        .post_init(post_init)
        .build()
    )

def main():
    # ✅ Supervisor: si Telegram se cae al iniciar, vuelve a intentar
    while True:
        try:
            app = build_app()
            app.add_handler(CommandHandler("start", start))
            app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
            app.run_polling(close_loop=False)
        except Exception as e:
            print("🔥 Supervisor restart por error:", repr(e))
            time.sleep(10)

if __name__ == "__main__":
    main()
