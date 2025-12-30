import os
import json
import time
import random
import asyncio
import requests
from datetime import datetime, timedelta

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
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

# Intervalo normal entre señales
WAIT_CHOICES = [120, 180, 240]  # 2–3–4 min

# Watchdog: si por lo que sea no se envía nada en X segundos, forzamos otra vuelta
MAX_STALL_SECONDS = 8 * 60  # 8 minutos

# ===============================
# 📆 HORARIO OTC REAL
# Viernes 13:00 → Domingo 19:00
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
# 🔢 CONTADOR (RESET INTELIGENTE)
# - no depende de caer EXACTO en 00:01
# - resetea apenas se ejecute después de 00:01
# ===============================
def load_counter():
    if not os.path.exists(COUNTER_FILE):
        data = {"date": str(today_utc5()), "count": 0, "reset_done": False}
        with open(COUNTER_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
        return data

    try:
        with open(COUNTER_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        # si se corrompe por reinicio, lo recreamos
        data = {"date": str(today_utc5()), "count": 0, "reset_done": False}
        with open(COUNTER_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
        return data

def save_counter(data):
    with open(COUNTER_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)

def get_and_increment_counter():
    now = now_utc5()
    today_str = str(today_utc5())
    data = load_counter()

    # nuevo día => habilitar reset
    if data.get("date") != today_str:
        data["date"] = today_str
        data["reset_done"] = False
        save_counter(data)

    # si ya pasó 00:01 y aún no se reseteó hoy, resetea
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
# 📡 DATA FETCH (ALPHA)
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
    # timeout duro para no congelarse
    r = requests.get(url, timeout=(10, 20))
    data = r.json()
    key = "Time Series FX (1min)"
    if key not in data:
        raise ValueError("Alpha sin datos")
    items = sorted(data[key].items())
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
    if not pairs:
        return None
    opts = [p for p in pairs if p != LAST_PAIR_SENT] or pairs
    LAST_PAIR_SENT = random.choice(opts)
    return LAST_PAIR_SENT

# ===============================
# ✅ ENVÍO SEGURO (RETRY) - NO MANDA NADA EXTRA
# ===============================
async def safe_send_signal(app: Application, text: str):
    # Reintentos con backoff para timeouts/red
    delays = [1, 3, 7, 15]
    last_err = None

    for d in delays:
        try:
            # timeout duro para no colgarse
            await asyncio.wait_for(
                app.bot.send_message(chat_id=CHAT_ID, text=text, disable_web_page_preview=True),
                timeout=25
            )
            return True
        except RetryAfter as e:
            # Telegram pidió esperar
            wait_s = int(getattr(e, "retry_after", 5))
            await asyncio.sleep(min(wait_s, 30))
            last_err = e
        except (TimedOut, NetworkError, asyncio.TimeoutError) as e:
            last_err = e
            await asyncio.sleep(d)
        except Exception as e:
            # cualquier otra cosa: no matamos el loop, esperamos y seguimos
            last_err = e
            await asyncio.sleep(d)

    print("❌ No se pudo enviar señal (se reintentó):", repr(last_err))
    return False

# ===============================
# 🚀 AUTO-SEÑALES (ANTI-FREEZE)
# ===============================
async def auto_signals(app: Application):
    if CHAT_ID is None:
        print("⚠️ Auto-señales desactivadas (CHAT_ID inválido)")
        return

    last_sent_at = time.monotonic()

    while True:
        try:
            pair = pick_pair(get_active_pairs())
            if not pair:
                await asyncio.sleep(5)
                continue

            # timeout duro a build_signal para que nunca se quede colgado
            msg = await asyncio.wait_for(asyncio.to_thread(build_signal, pair), timeout=25)

            ok = await safe_send_signal(app, msg)
            if ok:
                last_sent_at = time.monotonic()

            # sleep normal, pero con watchdog: si algo se “pegó”, no esperamos infinito
            wait = random.choice(WAIT_CHOICES)
            start = time.monotonic()
            while True:
                await asyncio.sleep(2)
                # si ya cumplimos el wait, salimos
                if time.monotonic() - start >= wait:
                    break
                # watchdog: si llevamos demasiado sin enviar, rompemos el sleep y forzamos nueva señal
                if time.monotonic() - last_sent_at > MAX_STALL_SECONDS:
                    print("⚠️ Watchdog: demasiado tiempo sin enviar. Forzando nueva señal...")
                    break

        except Exception as e:
            # jamás dejamos que muera el loop
            print("Auto error:", repr(e))
            await asyncio.sleep(5)

# ===============================
# 📟 MENÚ (solo responde al usuario, NO al canal)
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
# 🟢 BOOT + SUPERVISOR (NO CRASHEA)
# ===============================
async def post_init(app: Application):
    # No enviamos mensajes al canal aquí, solo logs
    print("🔥 ARKANE BOT ONLINE (Railway)")
    if not missing and CHAT_ID is not None:
        asyncio.create_task(auto_signals(app))
    else:
        print("⚠️ Auto-señales no iniciadas (faltan variables o CHAT_ID inválido)")

async def run_forever():
    # Cliente Telegram con timeouts más tolerantes
    req = HTTPXRequest(
        connect_timeout=20,
        read_timeout=45,
        write_timeout=45,
        pool_timeout=45
    )

    while True:
        app = None
        try:
            app = (
                Application.builder()
                .token(TOKEN)
                .request(req)
                .post_init(post_init)
                .build()
            )

            app.add_handler(CommandHandler("start", start))
            app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

            # Arranque controlado (si Telegram falla, no muere el proceso)
            await app.initialize()
            await app.start()
            await app.updater.start_polling(drop_pending_updates=True)

            # mantener vivo
            await asyncio.Event().wait()

        except (TimedOut, NetworkError) as e:
            print("⚠️ Telegram timeout/red al iniciar. Reintentando en 10s...", repr(e))
            await asyncio.sleep(10)

        except Exception as e:
            print("❌ Error general. Reintentando en 10s...", repr(e))
            await asyncio.sleep(10)

        finally:
            # cerrar limpio si alcanzó a iniciar
            try:
                if app:
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
            except Exception:
                pass

def main():
    # si no hay TOKEN, no tiene sentido
    if not TOKEN:
        raise RuntimeError("Falta TOKEN en Railway Variables")
    asyncio.run(run_forever())

if __name__ == "__main__":
    main()
