import os
import json
import time
import urllib.parse
from datetime import datetime, timezone, timedelta

import requests

# ---------- НАСТРОЙКИ (через переменные окружения / GitHub Secrets) ----------
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

CATEGORY = "linear"  # только USDT-перпетуальные фьючерсы Bybit — спот не смотрим,
                      # так как движения на споте и на фьючерсах не всегда совпадают
WINDOW_MINUTES = int(os.environ.get("WINDOW_MINUTES", "20"))  # окно поиска движения
THRESHOLD_PCT = float(os.environ.get("THRESHOLD_PCT", "12"))  # порог срабатывания, %
COOLDOWN_MINUTES = int(os.environ.get("COOLDOWN_MINUTES", "60"))
TZ_OFFSET_HOURS = int(os.environ.get("TZ_OFFSET_HOURS", "3"))
# Опциональный прокси, чтобы обойти гео-блокировку Bybit по IP раннера
# (Bybit блокирует US/China IP — GitHub Actions часто сидит в США).
# Формат: полный URL прокси-эндпоинта, заканчивающийся на параметр вида "...?url="
# Пример для теста: https://api.allorigins.win/raw?url=
# Пример для своего Cloudflare Worker: https://<имя>.workers.dev/?url=
# Если не задано (пусто) — запросы идут напрямую, как раньше.
PROXY_PREFIX = os.environ.get("BYBIT_PROXY", "").strip()

STATE_FILE = "state.json"
# mainnet + зеркало + региональные домены Bybit (публичные тикеры отдают одинаковые
# данные независимо от региона, так что это просто больше шансов пробиться)
BASE_URLS = [
    "https://api.bybit.com",
    "https://api.bytick.com",
    "https://api.bybit.nl",
    "https://api.bybit.tr",
    "https://api.bybit.kz",
    "https://api.bybitgeorgia.ge",
    "https://api.bybit.ae",
    "https://api.bybit.id",
]
RETRY_ATTEMPTS = int(os.environ.get("RETRY_ATTEMPTS", "3"))
RETRY_DELAY_SEC = float(os.environ.get("RETRY_DELAY_SEC", "4"))
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


def get_with_fallback(path, params):
    """Пробует запрос по очереди на всех известных доменах Bybit,
    при заданном PROXY_PREFIX — через прокси. Каждый полный круг по доменам
    повторяется несколько раз с паузой — Cloudflare Anycast может каждый раз
    выходить на Bybit с другого edge-сервера, так что повтор часто помогает
    обойти точечную блокировку конкретного датацентра."""
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
            except requests.exceptions.HTTPError as e:
                last_err = e
                print(f"[warn] попытка {attempt}/{RETRY_ATTEMPTS}: {target} -> {e}")
                continue
            except requests.exceptions.RequestException as e:
                last_err = e
                print(f"[warn] попытка {attempt}/{RETRY_ATTEMPTS}: {target} -> {e}")
                continue
        if attempt < RETRY_ATTEMPTS:
            time.sleep(RETRY_DELAY_SEC)
    raise last_err


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"history": {}, "alerted": {}}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def fetch_tickers(category):
    data = get_with_fallback("/v5/market/tickers", {"category": category})
    if data.get("retCode") != 0:
        raise RuntimeError(f"Bybit API error (tickers): {data}")
    return {t["symbol"]: t for t in data["result"]["list"]}


def fetch_kline_window(symbol, category, minutes):
    """1-минутные свечи за последние `minutes` минут — для точных MAX/MIN и объёма."""
    try:
        data = get_with_fallback(
            "/v5/market/kline",
            {
                "category": category,
                "symbol": symbol,
                "interval": "1",
                "limit": min(minutes + 2, 200),
            },
        )
    except requests.exceptions.HTTPError:
        return None
    if data.get("retCode") != 0:
        return None
    # каждая свеча: [start, open, high, low, close, volume, turnover]
    return data["result"]["list"]


def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    r = requests.post(url, data=payload, timeout=15)
    if not r.ok:
        print("Telegram send error:", r.text)


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
                   last_price, mark_price, volume24h, elapsed_min):
    arrow = "🟢" if direction_up else "🔴"
    sign = "+" if change_pct > 0 else ""
    now = datetime.now(timezone.utc) + timedelta(hours=TZ_OFFSET_HOURS)

    lines = [
        f"splash {THRESHOLD_PCT:.0f}% BYBIT",
        f"{arrow} ${symbol.replace('USDT', '')}",
        f"Изм.: <b>{sign}{change_pct:.2f}%</b> за {fmt_elapsed(elapsed_min)}",
        "",
        f"MAX: {fmt_price(window_max)}",
        f"MIN: {fmt_price(window_min)}",
        "",
        f"Now last price: ${fmt_price(last_price)}",
    ]
    if mark_price is not None:
        lines.append(f"Справедл. price: ${fmt_price(mark_price)}")
    lines += [
        "",
        f"🌊 Volume 24h: {fmt_usd(volume24h)}",
        f"🕐 {now.strftime('%H:%M:%S')} UTC+{TZ_OFFSET_HOURS}",
        "",
        f"🔗 https://www.bybit.com/trade/usdt/{symbol}",
    ]
    return "\n".join(lines)


def main():
    now = int(time.time())
    state = load_state()
    history = state.setdefault("history", {})
    alerted = state.setdefault("alerted", {})

    try:
        tickers = fetch_tickers(CATEGORY)
    except requests.exceptions.HTTPError as e:
        if "403" in str(e):
            print(
                "Bybit вернул 403 на всех доменах (api.bybit.com, api.bytick.com). "
                "Это похоже на гео-блокировку IP раннера GitHub Actions "
                "(Bybit блокирует US/China IP). Попробуй перезапустить ран — "
                "раннеры поднимаются на разных серверах, может повезти со следующим."
            )
        raise

    window_cutoff = now - WINDOW_MINUTES * 60
    prune_cutoff = now - (WINDOW_MINUTES + 5) * 60

    for symbol, t in tickers.items():
        if not symbol.endswith("USDT"):
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

        # ищем максимальное отклонение от базовой цены внутри окна,
        # а не только "было -> стало", чтобы не пропустить откатившийся пик
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

        # уточняем MAX/MIN и объём по 1-минутным свечам (точнее, чем наши снапшоты раз в 2 мин)
        kl = fetch_kline_window(symbol, CATEGORY, WINDOW_MINUTES)
        if kl:
            highs = [float(c[2]) for c in kl]
            lows = [float(c[3]) for c in kl]
            w_max = max(w_max, max(highs))
            w_min = min(w_min, min(lows))

        mark_price = None
        try:
            mark_price = float(t.get("markPrice")) if t.get("markPrice") else None
        except (ValueError, TypeError):
            mark_price = None

        try:
            volume24h_usd = float(t.get("turnover24h", 0))
        except (ValueError, TypeError):
            volume24h_usd = 0.0

        elapsed_min = (now - baseline_ts) / 60

        text = build_message(
            symbol, direction_up, change_pct, w_min, w_max,
            last_price, mark_price, volume24h_usd, elapsed_min,
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
