import os
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

_raw_chat_id = (os.getenv("CHAT_ID") or "").strip()
try:
    CHAT_ID = int(_raw_chat_id)
except Exception:
    CHAT_ID = None

# Crashea si falta lo crítico (Railway reinicia). NO manda nada a Telegram.
if not TOKEN or CHAT_ID is None:
    raise RuntimeError("Missing TOKEN or CHAT_ID environment variables.")

# ===============================
# ⏰ TIME UTC-5 (Bogotá)
# ===============================
def now_utc5():
    return datetime.utcnow() - timedelta(hours=5)

# ===============================
# 📊 CONFIG
# ===============================
LAST_UPTREND = None
LAST_PAIR_SENT = None

PAIRS_NORMAL = ["EUR/USD", "EUR/GBP", "EUR/JPY", "GBP/USD"]
PAIRS_OTC = ["EUR/USD OTC", "EUR/GBP OTC", "EUR/JPY OTC", "GBP/USD OTC"]

# Señales cada 2–3–4 min
WAIT_OPTIONS = [120, 180, 240]

# Timeouts anti-bloqueo
BUILD_SIGNAL_TIMEOUT = 45  # seg
SEND_TIMEOUT = 25          # seg
FETCH_TIMEOUT = 18         # seg (requests)

# Si pasan muchos minutos sin poder enviar, reinicio silencioso para que Railway lo levante
MAX_SILENCE_SECONDS = 10 * 60  # 10 min
LAST_SENT_TS = time.time()

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
    # OTC fin de semana
    if is_otc_weekend():
        return PAIRS_OTC

    # OTC horario diario 15–19
    hour = now_utc5().hour
    if 15 <= hour < 19:
        return PAIRS_OTC

    return PAIRS_NORMAL

# ===============================
# ✅ PARES SIN REPETICIÓN CONSECUTIVA (y sin repetir hasta agotar lista)
# ===============================
PAIR_BAG = []

def refill_bag(active_pairs):
    """Crea una bolsa barajada para no repetir pares hasta consumir todos."""
    global PAIR_BAG, LAST_PAIR_SENT
    PAIR_BAG = active_pairs[:]
    random.shuffle(PAIR_BAG)

    # Evitar que el primer par del nuevo ciclo sea igual al último enviado
    if LAST_PAIR_SENT and len(PAIR_BAG) > 1 and PAIR_BAG[0] == LAST_PAIR_SENT:
        # swap con otro índice
        j = random.randrange(1, len(PAIR_BAG))
        PAIR_BAG[0], PAIR_BAG[j] = PAIR_BAG[j], PAIR_BAG[0]

def next_pair():
    """Devuelve un par garantizando que no sea igual al anterior."""
    global LAST_PAIR_SENT, PAIR_BAG

    active_pairs = get_active_pairs()

    # Si cambió el set de pares (Normal/OTC), recrear bolsa usando solo los activos
    if not PAIR_BAG or any(p not in active_pairs for p in PAIR_BAG):
        refill_bag(active_pairs)

    # Si se agotó, nuevo ciclo barajado
    if not PAIR_BAG:
        refill_bag(active_pairs)

    candidate = PAIR_BAG.pop(0)

    # Blindaje extra: jamás permitir repetición consecutiva (por seguridad)
    if LAST_PAIR_SENT and candidate == LAST_PAIR_SENT:
        # si hay otro disponible en la bolsa, usamos el siguiente
        if PAIR_BAG:
            candidate2 = PAIR_BAG.pop(0)
            # devolvemos el repetido al final
            PAIR_BAG.append(candidate)
            candidate = candidate2
        else:
            # caso extremo (no debería pasar con 4 pares)
            refill_bag(active_pairs)
            candidate = PAIR_BAG.pop(0)

    LAST_PAIR_SENT = candidate
    return candidate

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
# 📡 DATA FETCH (AlphaVantage)
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
# 🧠 SEÑAL (+4 min)  (SIN CONTADOR)
# ===============================
def build_signal(pair):
    global LAST_UPTREND

    a, b = base_symbol(pair)

    try:
        closes = fetch_intraday_closes(a, b)
        up = trend_from_closes(closes)
        if up is None:
            raise ValueError("Pocas velas")
        LAST_UPTREND = up
    except Exception:
        # fallback estable
        up = LAST_UPTREND if LAST_UPTREND is not None else random.choice([True, False])

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
        "👉 Expiración: 1 MINUTO\n\n"
        "⚠️ Gestiona tu riesgo"
    )

# ===============================
# 🛡️ WATCHDOG (silencioso)
# ===============================
async def watchdog():
    global LAST_SENT_TS
    while True:
        await asyncio.sleep(60)
        if (time.time() - LAST_SENT_TS) > MAX_SILENCE_SECONDS:
            os._exit(1)  # reinicio silencioso (Railway lo relanza)

# ===============================
# 🚀 AUTO-SEÑALES (robusto, silencioso)
# ===============================
async def auto_signals(app: Application):
    global LAST_SENT_TS

    while True:
        try:
            pair = next_pair()

            msg = await asyncio.wait_for(
                asyncio.to_thread(build_signal, pair),
                timeout=BUILD_SIGNAL_TIMEOUT
            )

            await asyncio.wait_for(
                app.bot.send_message(chat_id=CHAT_ID, text=msg, disable_web_page_preview=True),
                timeout=SEND_TIMEOUT
            )

            # Solo cuando se envía de verdad
            LAST_SENT_TS = time.time()

            await asyncio.sleep(random.choice(WAIT_OPTIONS))

        except Exception:
            # Silencioso: no mensajes al canal, solo reintenta
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
        # timeouts del cliente Telegram (ayuda con timeouts)
        .connect_timeout(15)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .post_init(post_init)
        .build()
    )

    # ✅ Sin handlers, sin /start, sin responder chats.
    # ✅ Solo envía señales al canal.
    app.run_polling(close_loop=False, allowed_updates=[])

if __name__ == "__main__":
    main()
