import os
import json
import time
import random
import asyncio
import requests
from datetime import datetime, timedelta

from telegram.ext import Application
from telegram.error import TimedOut, NetworkError, RetryAfter, TelegramError

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

WAIT_OPTIONS = [120, 180, 240]  # 2–3–4 min

# ✅ Anti-ráfaga persistente (aunque reinicie Railway)
MIN_SECONDS_BETWEEN_SIGNALS = 120  # recomendado >= 120s (2 min)

# ✅ Watchdog anti-freeze:
MAX_SILENCE_SECONDS = 9 * 60  # 9 minutos (seguro con waits de 4 min)

# Timeouts para evitar bloqueos
BUILD_SIGNAL_TIMEOUT = 45  # seg
SEND_TIMEOUT = 25          # seg
FETCH_TIMEOUT = 18         # seg (requests)

# Track de última señal enviada (en memoria, pero sincronizada con archivo)
LAST_SIGNAL_TS = 0

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
# 🔢 CONTADOR + LAST_SIGNAL_TS (PERSISTENTE)
# - NO incrementa si el envío NO fue exitoso
# - Reset inteligente sin depender exacto 00:01
# - Guarda last_signal_ts para anti-ráfaga incluso con reinicios
# ===============================
def _default_counter():
    return {
        "date": today_utc5_str(),
        "count": 0,
        "reset_done": False,
        "last_signal_ts": 0
    }

def load_counter():
    if not os.path.exists(COUNTER_FILE):
        data = _default_counter()
        with open(COUNTER_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
        return data

    try:
        with open(COUNTER_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # compatibilidad si el archivo es viejo
        if "last_signal_ts" not in data:
            data["last_signal_ts"] = 0
        if "reset_done" not in data:
            data["reset_done"] = False
        if "count" not in data:
            data["count"] = 0
        if "date" not in data:
            data["date"] = today_utc5_str()
        return data
    except Exception:
        # si se corrompe por corte, reinicio seguro
        data = _default_counter()
        with open(COUNTER_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
        return data

def save_counter(data):
    with open(COUNTER_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)

def compute_next_counter_state(data):
    """
    Devuelve (new_data, next_count) SIN guardar.
    Solo se guarda si el envío a Telegram fue exitoso.
    """
    now = now_utc5()
    today_str = today_utc5_str()

    # Si cambió el día, habilitar reset para el nuevo día
    if data.get("date") != today_str:
        data["date"] = today_str
        data["reset_done"] = False

    # Reset una sola vez al día cuando ya pasó 00:01
    reset_time_reached = (now.hour > RESET_HOUR) or (now.hour == RESET_HOUR and now.minute >= RESET_MINUTE)
    if reset_time_reached and not data.get("reset_done", False):
        data["count"] = 0
        data["reset_done"] = True

    next_count = int(data.get("count", 0)) + 1
    data["count"] = next_count
    data["last_signal_ts"] = int(time.time())
    return data, next_count

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
    return fetch_alpha(a, b)

# ===============================
# 🧠 SEÑAL (+4 min)
# (ahora recibe "count" ya calculado para mostrar correcto)
# ===============================
def build_signal(pair, count):
    global LAST_UPTREND

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
# Si pasan > MAX_SILENCE_SECONDS sin enviar señal, forzamos reinicio.
# ===============================
async def watchdog():
    global LAST_SIGNAL_TS
    while True:
        await asyncio.sleep(60)
        if LAST_SIGNAL_TS and (time.time() - LAST_SIGNAL_TS) > MAX_SILENCE_SECONDS:
            os._exit(1)

# ===============================
# 🚀 AUTO-SEÑALES (robusto, silencioso)
# - Anti-ráfaga persistente con counter.json
# - Backoff inteligente en timeouts/rate limits
# ===============================
async def auto_signals(app: Application):
    global LAST_SIGNAL_TS

    # sincroniza último envío desde archivo (para no “volverse loco” tras reinicio)
    data0 = load_counter()
    LAST_SIGNAL_TS = int(data0.get("last_signal_ts", 0)) or int(time.time())

    backoff = 2  # aumenta si Telegram falla
    while True:
        try:
            # ✅ Anti-ráfaga persistente
            data = load_counter()
            last_ts = int(data.get("last_signal_ts", 0)) or 0
            now_ts = int(time.time())

            if last_ts and (now_ts - last_ts) < MIN_SECONDS_BETWEEN_SIGNALS:
                # duerme lo necesario para respetar mínimo entre señales
                await asyncio.sleep(10)
                continue

            active_pairs = get_active_pairs()
            pair = pick_pair(active_pairs)

            # Pre-calcular contador (pero NO guardar aún)
            data = load_counter()
            data_next = dict(data)  # copia
            data_next, next_count = compute_next_counter_state(data_next)

            # construir señal con timeout
            msg = await asyncio.wait_for(
                asyncio.to_thread(build_signal, pair, next_count),
                timeout=BUILD_SIGNAL_TIMEOUT
            )

            # enviar con timeout
            await asyncio.wait_for(
                app.bot.send_message(chat_id=CHAT_ID, text=msg, disable_web_page_preview=True),
                timeout=SEND_TIMEOUT
            )

            # ✅ Solo si se envió, guardamos contador+timestamp
            save_counter(data_next)
            LAST_SIGNAL_TS = int(data_next["last_signal_ts"])

            # reset backoff si todo va bien
            backoff = 2

            await asyncio.sleep(random.choice(WAIT_OPTIONS))

        except RetryAfter as e:
            # rate limit: esperamos lo que diga Telegram (sin mandar nada extra)
            wait_s = int(getattr(e, "retry_after", 5)) + random.randint(1, 3)
            await asyncio.sleep(wait_s)

        except (TimedOut, NetworkError):
            # problema red/telegram: backoff progresivo para no reiniciar sin parar
            await asyncio.sleep(min(60, backoff))
            backoff = min(60, backoff * 2)

        except TelegramError:
            # otros errores telegram: pausa corta
            await asyncio.sleep(min(30, backoff))
            backoff = min(60, backoff * 2)

        except Exception:
            # silencioso, reintento
            await asyncio.sleep(8)

# ===============================
# 🟢 MAIN (solo señales, nada más)
# ===============================
async def post_init(app: Application):
    asyncio.create_task(auto_signals(app))
    asyncio.create_task(watchdog())

def main():
    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .build()
    )

    # ✅ Sin handlers: no /start, no respuestas, nada. Solo auto-señales.
    app.run_polling()

if __name__ == "__main__":
    main()
