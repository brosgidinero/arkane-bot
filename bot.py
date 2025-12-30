import os
import json
import time
import random
import asyncio
import requests
from datetime import datetime, timedelta

from telegram.ext import Application

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

# Si falta algo CRÍTICO, crashea para que Railway reinicie (NO manda nada a Telegram)
if not TOKEN or CHAT_ID is None:
    raise RuntimeError("Missing TOKEN or CHAT_ID environment variables.")

# ===============================
# ⏰ TIME UTC-5 (Bogotá)
# ===============================
def now_utc5():
    return datetime.utcnow() - timedelta(hours=5)

def today_utc5_str():
    return str(now_utc5().date())

# ===============================
# 📊 CONFIG
# ===============================
COUNTER_FILE = "counter.json"
RESET_HOUR = 0
RESET_MINUTE = 1  # 00:01

LAST_UPTREND = None
LAST_PAIR_SENT = None

PAIRS_NORMAL = ["EUR/USD", "EUR/GBP", "EUR/JPY", "GBP/USD"]
PAIRS_OTC = ["EUR/USD OTC", "EUR/GBP OTC", "EUR/JPY OTC", "GBP/USD OTC"]

# Señales cada 2–3–4 min (tú ya lo tenías)
WAIT_OPTIONS = [120, 180, 240]

# ✅ Watchdog anti-freeze:
# Si pasa más de este tiempo sin una señal ENVIADA, forzamos reinicio (silencioso)
MAX_SILENCE_SECONDS = 9 * 60  # 9 minutos (seguro con waits de 4 min)

# Timeouts para evitar bloqueos
BUILD_SIGNAL_TIMEOUT = 45  # seg
SEND_TIMEOUT = 25          # seg
FETCH_TIMEOUT = 18         # seg (requests)

# Track de última señal enviada (para watchdog)
LAST_SIGNAL_TS = time.time()

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
# 🔢 CONTADOR (Reset inteligente)
# ===============================
def load_counter():
    if not os.path.exists(COUNTER_FILE):
        data = {"date": today_utc5_str(), "count": 0, "reset_done": False}
        with open(COUNTER_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
        return data

    try:
        with open(COUNTER_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        # si se corrompe por un corte, lo reiniciamos seguro
        data = {"date": today_utc5_str(), "count": 0, "reset_done": False}
        with open(COUNTER_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
        return data

def save_counter(data):
    with open(COUNTER_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)

def get_and_increment_counter():
    """
    ✅ Reset inteligente:
    - NO depende de ejecutarse EXACTO en 00:01.
    - Si ya pasó 00:01 y hoy aún no se reseteó, resetea en la siguiente ejecución.
    """
    now = now_utc5()
    today_str = today_utc5_str()
    data = load_counter()

    # Si cambió el día, habilitar reset
    if data.get("date") != today_str:
        data["date"] = today_str
        data["reset_done"] = False
        save_counter(data)

    reset_time_reached = (now.hour > RESET_HOUR) or (now.hour == RESET_HOUR and now.minute >= RESET_MINUTE)

    # Reset una sola vez al día cuando ya pasó 00:01
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
# 📡 DATA FETCH (con timeout)
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
    r = requests.get(url, timeout=FETCH_TIMEOUT).json()
    key = "Time Series FX (1min)"
    if key not in r:
        raise ValueError("Alpha sin datos")
    items = sorted(r[key].items())
    return [float(v["4. close"]) for _, v in items]

def fetch_intraday_closes(a, b):
    # (Mantengo Alpha como fuente principal porque así lo tienes estable)
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
# 🛡️ WATCHDOG (SILENCIOSO)
# Si pasan > MAX_SILENCE_SECONDS sin enviar señal,
# forzamos reinicio para que Railway lo levante otra vez.
# NO manda nada a Telegram.
# ===============================
async def watchdog():
    global LAST_SIGNAL_TS
    while True:
        await asyncio.sleep(60)
        if (time.time() - LAST_SIGNAL_TS) > MAX_SILENCE_SECONDS:
            os._exit(1)  # reinicio silencioso (Railway lo re-lanza)

# ===============================
# 🚀 AUTO-SEÑALES (robusto, silencioso)
# ===============================
async def auto_signals(app: Application):
    global LAST_SIGNAL_TS

    while True:
        try:
            active_pairs = get_active_pairs()
            pair = pick_pair(active_pairs)

            # build_signal con timeout (para que nunca se quede pegado)
            msg = await asyncio.wait_for(asyncio.to_thread(build_signal, pair), timeout=BUILD_SIGNAL_TIMEOUT)

            # send_message con timeout
            await asyncio.wait_for(
                app.bot.send_message(chat_id=CHAT_ID, text=msg, disable_web_page_preview=True),
                timeout=SEND_TIMEOUT
            )

            # ✅ Solo cuando se envía de verdad
            LAST_SIGNAL_TS = time.time()

            await asyncio.sleep(random.choice(WAIT_OPTIONS))

        except Exception:
            # Silencioso: no prints, no mensajes. Solo espera un poco y reintenta.
            await asyncio.sleep(8)

# ===============================
# 🟢 MAIN (solo señales, nada más)
# ===============================
async def post_init(app: Application):
    # Lanzar auto señales + watchdog (ambos silenciosos)
    asyncio.create_task(auto_signals(app))
    asyncio.create_task(watchdog())

def main():
    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .build()
    )

    # ✅ NO handlers: no /start, no respuestas, nada.
    # ✅ Solo auto-señales al canal.

    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()

