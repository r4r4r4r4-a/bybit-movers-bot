import os
import json
import time
import urllib.parse
from datetime import datetime, timezone, timedelta

import requests

# ---------- НАСТРОЙКИ (через переменные окружения / GitHub Secrets) ----------
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
# Можно указать несколько адресов через запятую: "123456,-1001234567890"
# (например личный chat_id и id канала одновременно) — бот пошлёт в каждый.
TELEGRAM_CHAT_IDS = [
    c.strip() for c in os.environ["TELEGRAM_CHAT_ID"].split(",") if c.strip()
]

WINDOW_MINUTES = int(os.environ.get("WINDOW_MINUTES", "20"))  # окно поиска движения
THRESHOLD_PCT = float(os.environ.get("THRESHOLD_PCT", "12"))  # порог срабатывания, %
COOLDOWN_MINUTES = int(os.environ.get("COOLDOWN_MINUTES", "60"))
TZ_OFFSET_HOURS = int(os.environ.get("TZ_OFFSET_HOURS", "3"))

# Опциональный прокси — на случай если MEXC когда-нибудь тоже начнёт резать
# по IP (пока по опыту Replit-бота такого не наблюдалось, так что по
# умолчанию не используется). Формат такой же, как раньше: полный URL
# прокси, заканчивающийся на "...?url=".
PROXY_PREFIX = os.environ.get("MEXC_PROXY", "").strip()

STATE_FILE = "state.json"
BASE_URLS = ["https://contract.mexc.com", "https://api.mexc.com"]
RETRY_ATTEMPTS = int(os.environ.get("RETRY_ATTEMPTS", "2"))
RETRY_DELAY_SEC = float(os.environ.get("RETRY_DELAY_SEC", "3"))
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"history": {}, "alerted": {}}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def get_with_fallback(path, params=None):
    """Пробует запрос по очереди на всех известных доменах MEXC,
    при заданном PROXY_PREFIX — через прокси. Несколько попыток с паузой
    на случай точечных сетевых сбоев."""
    last_err = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        for base in BASE_URLS:
            target = f"{base}{path}"
            if params:
                target += "?" + urllib.parse.urlencode(params)
            try:
                if PROXY_PREFIX:
                    url = PROXY_PREFIX + urllib.parse.quote(target, safe="")
                    resp = requests.get(url, headers=HEADERS, timeout=25)
                else:
                    resp = requests.get(target, headers=HEADERS, timeout=15)
                resp.raise_for_status()
                return resp.json()
            except requests.exceptions.RequestException as e:
                last_err = e
                print(f"[warn] попытка {attempt}/{RETRY_ATTEMPTS}: {target} -> {e}")
                continue
        if attempt < RETRY_ATTEMPTS:
            time.sleep(RETRY_DELAY_SEC)
    raise last_err


def fetch_tickers():
    """Все тикеры фьючерсов MEXC одним запросом."""
    data = get_with_fallback("/api/v1/contract/ticker")
    if not data.get("success"):
        raise RuntimeError(f"MEXC API error (tickers): {data}")
    return {t["symbol"]: t for t in data["data"]}


def fetch_kline_window(symbol, minutes):
    """1-минутные свечи за последние `minutes` минут — для точных MAX/MIN."""
    now = int(time.time())
    start = now - (minutes + 2) * 60
    try:
        data = get_with_fallback(
            f"/api/v1/contract/kline/{symbol}",
            {"interval": "Min1", "start": start, "end": now},
        )
    except requests.exceptions.RequestException:
        return None
    if not data.get("success"):
        return None
    d = data.get("data") or {}
    highs = d.get("high") or []
    lows = d.get("low") or []
    if not highs or not lows:
        return None
    return {"high": highs, "low": lows}


def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for chat_id in TELEGRAM_CHAT_IDS:
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        r = requests.post(url, data=payload, timeout=15)
        if not r.ok:
            print(f"Telegram send error (chat_id={chat_id}):", r.text)


def fmt_price(p: float) -> str:
    return f"{p:.6g}"


def fmt_usd(x: float) -> str:
    if x >= 1_000_000:
        return f"${x/1_000_000:.2f}m"
    if x >= 1_000:
        return f"${x/1_000:.2f}k"
    return f"${x:.2f}"


def fmt_elapsed(minutes: float) -> str:
    return f"{minutes:.1f} мин"


def build_message(symbol, direction_up, change_pct, window_min, window_max,
                   last_price, fair_price, volume24h, elapsed_min):
    arrow = "🟢" if direction_up else "🔴"
    sign = "+" if change_pct > 0 else ""
    now = datetime.now(timezone.utc) + timedelta(hours=TZ_OFFSET_HOURS)
    display_name = symbol.replace("_USDT", "")

    lines = [
        f"splash {THRESHOLD_PCT:.0f}% MEXC",
        f"{arrow} ${display_name}",
        f"Изм.: <b>{sign}{change_pct:.2f}%</b> за {fmt_elapsed(elapsed_min)}",
        "",
        f"MAX: {fmt_price(window_max)}",
        f"MIN: {fmt_price(window_min)}",
        "",
        f"Now last price: ${fmt_price(last_price)}",
    ]
    if fair_price is not None:
        lines.append(f"Справедл. price: ${fmt_price(fair_price)}")
    lines += [
        "",
        f"🌊 Volume 24h: {fmt_usd(volume24h)}",
        f"🕐 {now.strftime('%H:%M:%S')} UTC+{TZ_OFFSET_HOURS}",
        "",
        f"🔗 https://www.mexc.com/futures/{symbol}",
    ]
    return "\n".join(lines)


def main():
    now = int(time.time())
    state = load_state()
    history = state.setdefault("history", {})
    alerted = state.setdefault("alerted", {})

    try:
        tickers = fetch_tickers()
    except requests.exceptions.RequestException as e:
        print(f"Не удалось получить тикеры MEXC: {e}")
        raise

    window_cutoff = now - WINDOW_MINUTES * 60
    prune_cutoff = now - (WINDOW_MINUTES + 5) * 60

    for symbol, t in tickers.items():
        # Фильтр: только USDT-маржинальные бессрочные фьючерсы с реальной ценой
        if not symbol.endswith("_USDT"):
            continue
        try:
            last_price = float(t["lastPrice"])
        except (KeyError, ValueError, TypeError):
            continue
        if last_price <= 0:
            continue

        sym_hist = history.setdefault(symbol, [])
        sym_hist.append({"t": now, "p": last_price})
        sym_hist[:] = [pt for pt in sym_hist if pt["t"] >= prune_cutoff]

        window_points = [pt for pt in sym_hist if pt["t"] >= window_cutoff]
        if len(window_points) < 2:
            continue  # ещё не набралась история на нужное окно

        baseline_price = window_points[0]["p"]
        baseline_ts = window_points[0]["t"]
        if baseline_price <= 0:
            continue

        window_prices = [pt["p"] for pt in window_points]
        w_max = max(window_prices)
        w_min = min(window_prices)

        up_pct = (w_max - baseline_price) / baseline_price * 100
        down_pct = (baseline_price - w_min) / baseline_price * 100

        if up_pct >= THRESHOLD_PCT:
            direction_up, change_pct = True, up_pct
        elif down_pct >= THRESHOLD_PCT:
            direction_up, change_pct = False, -down_pct
        else:
            continue

        last_alert_ts = alerted.get(symbol, 0)
        if now - last_alert_ts < COOLDOWN_MINUTES * 60:
            continue

        # уточняем MAX/MIN по 1-минутным свечам MEXC
        kl = fetch_kline_window(symbol, WINDOW_MINUTES)
        if kl:
            w_max = max(w_max, max(kl["high"]))
            w_min = min(w_min, min(kl["low"]))

        fair_price = None
        try:
            fair_price = float(t.get("fairPrice")) if t.get("fairPrice") else None
        except (ValueError, TypeError):
            fair_price = None

        try:
            volume24h_usd = float(t.get("amount24", 0))
        except (ValueError, TypeError):
            volume24h_usd = 0.0

        elapsed_min = (now - baseline_ts) / 60

        text = build_message(
            symbol, direction_up, change_pct, w_min, w_max,
            last_price, fair_price, volume24h_usd, elapsed_min,
        )
        send_telegram(text)
        alerted[symbol] = now

    alert_cutoff = now - COOLDOWN_MINUTES * 60 * 2
    for sym in list(alerted.keys()):
        if alerted[sym] < alert_cutoff:
            del alerted[sym]

    save_state(state)


if __name__ == "__main__":
    main()
