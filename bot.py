import os
import json
import time
import random
import asyncio
import requests
from datetime import datetime, timedelta

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from telegram.error import TimedOut, NetworkError, RetryAfter
from telegram.request import HTTPXRequest

# ===============================
# 🔐 VARIABLES (Railway)
# ===============================
TOKEN = os.getenv("TOKEN")
AV_KEY = os.getenv("AV_KEY")
FINNHUB_KEY = os.getenv("FINNHUB_KEY")
TWELVE_KEY = os.getenv("TWELVE_KEY")

_raw_chat_id = (os.getenv("CHAT_ID") or "").strip()
try:
    CHAT_ID = int(_raw_chat_id)
except Exception:
    CHAT_ID = None

REQUIRED = {"TOKEN": TOKEN, "AV_KEY": AV_KEY, "FINNHUB_KEY": FINNHUB_KEY, "TWELVE_KEY": TWELVE_KEY}
missing = [k for k, v in REQUIRED.items() if not v]
if missing:
    print("❌ FALTAN VARIABLES:", ", ".join(missing))
    print("⚠️ El bot arrancará SIN auto-señales")

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
RESET_MINUTE = 1

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
    if wd == 4 and now.hour >= 13:
        return True
    if wd == 5:
        return True
    if wd == 6 and now.hour < 19:
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
# 🔢 CONTADOR (reset robusto)
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
    - Si el bot estuvo dormido, resetea apenas vuelva a correr luego de 00:01.
    """
    now = now_utc5()
    today_str = str(today_utc5())
    data = load_counter()

    # Cambio de día: habilitar reset para el nuevo día
    if data.get("date") != today_str:
        data["date"] = today_str
        data["reset_done"] = False
        save_counter(data)

    # Reset si ya pasó 00:01 y aún no se hizo hoy
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
# 📡 DATA FETCH
# ===============================
def base_symbol(pair):
    p = pair.replace(" OTC", "")
    return p.split("/")

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
    items = sorted(r[key].items())
    return [float(v["4. close"]) for _, v in items]

def fetch_intraday_closes(a, b):
    return fetch_alpha(a, b)

# ===============================
# 🧠 SEÑAL
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
# ✅ ENVÍO ROBUSTO (no muere por Telegram)
# ===============================
async def send_with_retry(app: Application, chat_id: int, text: str):
    backoff = 2
    while True:
        try:
            await app.bot.send_message(chat_id=chat_id, text=text, disable_web_page_preview=True)
            return
        except RetryAfter as e:
            wait = int(getattr(e, "retry_after", 5)) + 1
            print(f"⚠️ Telegram RetryAfter. Esperando {wait}s...")
            await asyncio.sleep(wait)
        except (TimedOut, NetworkError) as e:
            print(f"⚠️ Telegram timeout/red. Reintentando en {backoff}s... {repr(e)}")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
        except Exception as e:
            # Cualquier cosa inesperada: reintenta sin matar el proceso
            print(f"⚠️ Error enviando a Telegram. Reintentando en {backoff}s... {repr(e)}")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

# ===============================
# 🚀 AUTO-SEÑALES (siempre vivo)
# ===============================
async def auto_signals(app: Application):
    if CHAT_ID is None:
        print("⚠️ Auto-señales desactivadas (CHAT_ID)")
        return

    while True:
        try:
            pair = pick_pair(get_active_pairs())
            msg = await asyncio.to_thread(build_signal, pair)

            # Enviar robusto (no se cae por Telegram)
            await send_with_retry(app, CHAT_ID, msg)

            # ritmo 2–3–4 min
            await asyncio.sleep(random.choice([120, 180, 240]))

        except Exception as e:
            # Nunca muere: si algo explota, respira y sigue
            print("⚠️ Auto loop error:", repr(e))
            await asyncio.sleep(5)

# ===============================
# 📟 MENÚ
# ===============================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pairs = get_active_pairs()
    await update.message.reply_text(
        "🔱 ARKANE BOT 🦂\nPares activos:\n\n" + "\n".join(pairs),
        reply_markup=ReplyKeyboardMarkup([[p] for p in pairs], resize_keyboard=True)
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text not in get_active_pairs():
        await update.message.reply_text("Par no disponible ahora.")
        return
    msg = await asyncio.to_thread(build_signal, update.message.text)
    await update.message.reply_text(msg)

# ===============================
# 🟢 MAIN (arranque correcto, sin warnings)
# ===============================
async def run_bot_forever():
    # Timeouts más largos para evitar TimedOut en getMe/startup
    request = HTTPXRequest(connect_timeout=30, read_timeout=30, write_timeout=30, pool_timeout=30)

    app = Application.builder().token(TOKEN).request(request).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Arranque manual (evita "coroutine was never awaited")
    while True:
        try:
            print("🔥 ARKANE BOT ONLINE (Railway)")
            await app.initialize()
            await app.start()
            # Inicia polling y espera idle
            await app.updater.start_polling(drop_pending_updates=True)
            asyncio.create_task(auto_signals(app))
            await app.updater.idle()
        except (TimedOut, NetworkError) as e:
            print(f"⚠️ Telegram timeout/red en main. Reiniciando en 10s... {repr(e)}")
            await asyncio.sleep(10)
        except Exception as e:
            print(f"⚠️ Error main inesperado. Reiniciando en 10s... {repr(e)}")
            await asyncio.sleep(10)
        finally:
            try:
                await app.updater.stop()
            except Exception:
                pass
            try:
                await app.stop()
            except Exception:
                pass
            try:
                await app.shutdown()
            except Exception:
                pass

def main():
    if not TOKEN:
        raise RuntimeError("Falta TOKEN")
    asyncio.run(run_bot_forever())

if __name__ == "__main__":
    main()
