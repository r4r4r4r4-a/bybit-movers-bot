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
# Если движение в ту же сторону усилилось ещё на столько % (сверх уже
# отправленного алерта) — шлём повторный алерт немедленно, не дожидаясь
# окончания кулдауна. По умолчанию — половина основного порога.
CONTINUATION_PCT = float(os.environ.get("CONTINUATION_PCT", str(THRESHOLD_PCT / 2)))
TZ_OFFSET_HOURS = int(os.environ.get("TZ_OFFSET_HOURS", "3"))

# Опциональный прокси — на случай если MEXC когда-нибудь тоже начнёт резать
# по IP (пока по опыту Replit-бота такого не наблюдалось, так что по
# умолчанию не используется). Формат такой же, как раньше: полный URL
# прокси, заканчивающийся на "...?url=".
PROXY_PREFIX = os.environ.get("MEXC_PROXY", "").strip()

# --- Фильтр "только монеты, которые есть на фьючерсах Bybit" ---
# Bybit блокирует частый опрос по IP, но список инструментов не нужно
# спрашивать каждые 1-2 минуты — обновляем редко (раз в N часов) через тот
# же Cloudflare Worker-прокси, что был поднят раньше для обхода блока.
ENABLE_BYBIT_FILTER = os.environ.get("ENABLE_BYBIT_FILTER", "true").strip().lower() != "false"
BYBIT_PROXY_PREFIX = os.environ.get("BYBIT_PROXY", "").strip()
BYBIT_SYMBOLS_REFRESH_HOURS = float(os.environ.get("BYBIT_SYMBOLS_REFRESH_HOURS", "12"))
BYBIT_BASE_URLS = ["https://api.bybit.com", "https://api.bytick.com"]

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


STATIC_BYBIT_SYMBOLS_FILE = "bybit_instruments_raw.json"


def load_static_bybit_symbols():
    """Резервный/основной источник списка Bybit — обычный JSON-файл в репо,
    сохранённый вручную (браузером, где нет гео-блока), в том же формате,
    что отдаёт сам Bybit API (result.list[].symbol)."""
    if not os.path.exists(STATIC_BYBIT_SYMBOLS_FILE):
        return None
    try:
        with open(STATIC_BYBIT_SYMBOLS_FILE, "r") as f:
            data = json.load(f)
        return {item["symbol"] for item in data["result"]["list"]}
    except Exception as e:
        print(f"[warn] не удалось прочитать {STATIC_BYBIT_SYMBOLS_FILE}: {e}")
        return None


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


def fetch_bybit_symbols():
    """Полный список торгующихся линейных USDT-перпетуалов Bybit — через
    Cloudflare Worker-прокси (BYBIT_PROXY). ВАЖНО: из GitHub Actions это,
    скорее всего, ВСЕГДА будет проваливаться — Anycast-сеть Cloudflare
    роутит запрос на ближайший к источнику дата-центр, а GitHub Actions
    физически сидит в США, откуда Bybit банит по гео систематически (не
    случайно). Поэтому не тратим много попыток — если не сработает, ниже по
    цепочке всё равно есть статический файл-резерв."""
    if not BYBIT_PROXY_PREFIX:
        return None
    for base in BYBIT_BASE_URLS:
        try:
            symbols = set()
            cursor = ""
            for _ in range(10):
                params = {"category": "linear", "status": "Trading", "limit": "1000"}
                if cursor:
                    params["cursor"] = cursor
                target = base + "/v5/market/instruments-info?" + urllib.parse.urlencode(params)
                url = BYBIT_PROXY_PREFIX + urllib.parse.quote(target, safe="")
                resp = requests.get(url, headers=HEADERS, timeout=25)
                resp.raise_for_status()
                data = resp.json()
                if data.get("retCode") != 0:
                    raise RuntimeError(str(data))
                for item in data["result"]["list"]:
                    symbols.add(item["symbol"])
                cursor = data["result"].get("nextPageCursor") or ""
                if not cursor:
                    break
            if symbols:
                print(f"[info] список Bybit получен живьём: {len(symbols)} символов ({base})")
                return symbols
        except Exception as e:
            print(f"[warn] fetch_bybit_symbols ({base}): {e}")
    return None


def get_bybit_symbol_set(state, now):
    """Список символов Bybit для фильтра. Порядок приоритета:
    1) свежий кэш в state.json (обновлялся не дольше BYBIT_SYMBOLS_REFRESH_HOURS назад)
    2) попытка обновить по сети через прокси (может не сработать — GitHub Actions
       физически не может пробить гео-блок Bybit из-за геопривязки Anycast, это
       системное ограничение, а не невезение — так что не рассчитываем на неё)
    3) статический файл bybit_instruments_raw.json, сохранённый вручную из браузера
    4) старый (просроченный) кэш в state.json, если он есть
    5) пусто — алерты этого цикла пропускаются, лучше молчать, чем прислать не ту монету"""
    if not ENABLE_BYBIT_FILTER:
        return None
    entry = state.get("bybit_symbols")
    stale = (
        not entry
        or now - entry.get("updated_at", 0) > BYBIT_SYMBOLS_REFRESH_HOURS * 3600
    )
    if stale:
        fetched = fetch_bybit_symbols()
        if fetched:
            state["bybit_symbols"] = {"list": sorted(fetched), "updated_at": now}
            return fetched
        static_syms = load_static_bybit_symbols()
        if static_syms:
            print(f"[info] использую статический список Bybit: {len(static_syms)} символов")
            return static_syms
        if entry:
            print("[warn] не удалось обновить список Bybit — использую старый кэш")
            return set(entry["list"])
        print("[warn] список Bybit недоступен ни живьём, ни статически — алерты этого цикла пропущены")
        return set()
    return set(entry["list"])


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
        f"{arrow} $<code>{display_name}</code>",
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

    bybit_symbol_set = get_bybit_symbol_set(state, now)

    for symbol, t in tickers.items():
        # Фильтр: только USDT-маржинальные бессрочные фьючерсы с реальной ценой
        if not symbol.endswith("_USDT"):
            continue

        # Фильтр: монета должна быть и на фьючерсах Bybit (если список доступен)
        if bybit_symbol_set is not None:
            bybit_style = symbol.replace("_", "")
            if bybit_style not in bybit_symbol_set:
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

        window_prices = [(pt["t"], pt["p"]) for pt in window_points]

        # Ищем лучший "свинг" внутри окна — не просто (окно_старт -> окно_конец),
        # а максимальное движение от любого локального минимума/максимума
        # до последующей точки. Так время движения отражает, когда РЕАЛЬНО
        # начался конкретный рывок, а не всегда "край окна" (~20 мин).
        best_up_pct = -1e18
        best_up_start_ts = best_up_end_ts = None
        best_up_lo = best_up_hi = None
        running_min_ts, running_min_p = window_prices[0]

        best_down_pct = -1e18
        best_down_start_ts = best_down_end_ts = None
        best_down_lo = best_down_hi = None
        running_max_ts, running_max_p = window_prices[0]

        for ts, p in window_prices[1:]:
            if running_min_p > 0:
                up = (p - running_min_p) / running_min_p * 100
                if up > best_up_pct:
                    best_up_pct = up
                    best_up_start_ts, best_up_end_ts = running_min_ts, ts
                    best_up_lo, best_up_hi = running_min_p, p
            if running_max_p > 0:
                down = (running_max_p - p) / running_max_p * 100
                if down > best_down_pct:
                    best_down_pct = down
                    best_down_start_ts, best_down_end_ts = running_max_ts, ts
                    best_down_lo, best_down_hi = p, running_max_p
            if p <= running_min_p:
                running_min_ts, running_min_p = ts, p
            if p >= running_max_p:
                running_max_ts, running_max_p = ts, p

        if best_up_pct >= THRESHOLD_PCT and best_up_pct >= best_down_pct:
            direction_up = True
            change_pct = best_up_pct
            swing_start_ts, swing_end_ts = best_up_start_ts, best_up_end_ts
            w_min, w_max = best_up_lo, best_up_hi
        elif best_down_pct >= THRESHOLD_PCT:
            direction_up = False
            change_pct = -best_down_pct
            swing_start_ts, swing_end_ts = best_down_start_ts, best_down_end_ts
            w_min, w_max = best_down_lo, best_down_hi
        else:
            continue

        # Кулдаун можно пробить, если движение в ту же сторону заметно
        # усилилось с прошлого алерта (хотя бы на CONTINUATION_PCT дальше) —
        # иначе можно пропустить, как памп ушёл с 12% до 18%+ без повторного сигнала.
        prev = alerted.get(symbol)
        if isinstance(prev, dict):
            prev_ts = prev.get("ts", 0)
            prev_pct = prev.get("change_pct", 0.0)
        else:
            prev_ts = prev or 0
            prev_pct = None

        in_cooldown = now - prev_ts < COOLDOWN_MINUTES * 60
        if in_cooldown:
            same_direction = prev_pct is not None and (
                (direction_up and prev_pct > 0) or (not direction_up and prev_pct < 0)
            )
            escalated = (
                same_direction
                and abs(change_pct) - abs(prev_pct) >= CONTINUATION_PCT
            )
            if not escalated:
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

        elapsed_min = max((swing_end_ts - swing_start_ts) / 60, 0.1)

        text = build_message(
            symbol, direction_up, change_pct, w_min, w_max,
            last_price, fair_price, volume24h_usd, elapsed_min,
        )
        send_telegram(text)
        alerted[symbol] = {"ts": now, "change_pct": change_pct}

    alert_cutoff = now - COOLDOWN_MINUTES * 60 * 2
    for sym in list(alerted.keys()):
        ts = alerted[sym]["ts"] if isinstance(alerted[sym], dict) else alerted[sym]
        if ts < alert_cutoff:
            del alerted[sym]

    save_state(state)


if __name__ == "__main__":
    main()
