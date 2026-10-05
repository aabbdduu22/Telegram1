import os
import time
import threading
import statistics
from datetime import datetime
import requests

CHECK_INTERVAL = 60
SURGE_MULTIPLIER = 5.0
REVERSAL_MULTIPLIER = 2.0
MIN_CONFIRMATION_VOL = 300
MIN_RANGE_PCT = 0.05
MIN_BASE_VOL = 5

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

MEXC_CONTRACT_DETAIL_URL = "https://contract.mexc.com/api/v1/contract/detail"
MEXC_KLINE_URL = "https://contract.mexc.com/api/v1/contract/kline"

session_x1 = requests.Session()
session_x2 = requests.Session()

def validate_environment():
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN غير موجود في Environment Variables")
    if not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_CHAT_ID غير موجود في Environment Variables")

def send_telegram(session, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        response = session.post(url, json=payload, timeout=10)
        if response.status_code != 200:
            print(f"Telegram HTTP {response.status_code}: {response.text[:300]}")
            return False
        result = response.json()
        if not result.get("ok"):
            print(f"Telegram error: {result.get('description', 'Unknown error')}")
            return False
        return True
    except Exception as e:
        print(f"Telegram connection error: {e}")
        return False

def clean_symbol(symbol):
    return symbol.replace("STOCK", "").replace("_USDT", "")

def get_stock_symbols(session):
    try:
        response = session.get(MEXC_CONTRACT_DETAIL_URL, timeout=10)
        if response.status_code != 200:
            print(f"MEXC detail HTTP {response.status_code}")
            return []
        contracts = response.json().get("data", [])
        return [
            c.get("symbol")
            for c in contracts
            if "STOCK" in c.get("symbol", "") and "USDT" in c.get("symbol", "")
        ]
    except Exception as e:
        print(f"MEXC symbols error: {e}")
        return []

def get_klines(session, symbol, limit=30):
    url = f"{MEXC_KLINE_URL}/{symbol}?interval=Min1"
    try:
        response = session.get(url, timeout=10)
        if response.status_code != 200:
            return []
        data = response.json().get("data", {})
        times = data.get("time", [])
        opens = data.get("open", [])
        highs = data.get("high", [])
        lows = data.get("low", [])
        closes = data.get("close", [])
        volumes = data.get("vol", [])
        if len(times) < limit:
            return []
        result = []
        for i in range(len(times) - limit, len(times)):
            try:
                result.append({
                    "time": int(times[i]),
                    "open": float(opens[i]),
                    "high": float(highs[i]),
                    "low": float(lows[i]),
                    "close": float(closes[i]),
                    "vol": float(volumes[i]),
                })
            except (ValueError, TypeError, IndexError):
                continue
        return result
    except Exception:
        return []

def calculate_baseline(historical):
    vols = [k["vol"] for k in historical if k["vol"] > 0]
    if len(vols) < 5:
        return None
    baseline = max(statistics.median(vols), statistics.mean(vols) * 0.5)
    return max(baseline, MIN_BASE_VOL)

# ---------------- X1 ----------------

def x1_detect_early_surge(klines):
    if len(klines) < 10:
        return None
    baseline = calculate_baseline(klines[:-2])
    if not baseline:
        return None
    surge = klines[-2]
    if surge["open"] <= 0 or surge["vol"] < baseline * SURGE_MULTIPLIER:
        return None
    range_pct = ((surge["high"] - surge["low"]) / surge["open"]) * 100
    if range_pct < MIN_RANGE_PCT:
        return None
    direction = "صاعدة 🟢" if surge["close"] > surge["open"] else "هابطة 🔴"
    return {"ratio": round(surge["vol"] / baseline, 1), "direction": direction}

def x1_detect_opportunity(klines):
    if len(klines) < 10:
        return None
    baseline = calculate_baseline(klines[:-3])
    if not baseline:
        return None
    surge, reversal = klines[-3], klines[-2]
    if surge["open"] <= 0 or reversal["open"] <= 0:
        return None

    surge_range = ((surge["high"] - surge["low"]) / surge["open"]) * 100
    reversal_range = ((reversal["high"] - reversal["low"]) / reversal["open"]) * 100

    if surge["vol"] < baseline * SURGE_MULTIPLIER or surge_range < MIN_RANGE_PCT:
        return None
    if reversal["vol"] < MIN_CONFIRMATION_VOL or reversal["vol"] < baseline * REVERSAL_MULTIPLIER:
        return None
    if reversal_range < MIN_RANGE_PCT:
        return None

    surge_up = surge["close"] > surge["open"]
    reversal_up = reversal["close"] > reversal["open"]
    if surge_up == reversal_up:
        return None

    if not surge_up and reversal_up:
        direction, action = "🟢 ارتداد صاعد", "شراء"
        entry = reversal["close"]
        stop_loss = min(surge["low"], reversal["low"])
        target = surge["open"]
    else:
        direction, action = "🔴 ارتداد هابط", "بيع"
        entry = reversal["close"]
        stop_loss = max(surge["high"], reversal["high"])
        target = surge["open"]

    risk = abs(entry - stop_loss)
    reward = abs(target - entry)
    rr = round(reward / risk, 2) if risk > 0 else 0

    return {
        "direction": direction, "action": action,
        "surge_vol": surge["vol"], "reversal_vol": reversal["vol"],
        "surge_ratio": round(surge["vol"] / baseline, 1),
        "reversal_ratio": round(reversal["vol"] / baseline, 1),
        "surge_range": round(surge_range, 2),
        "reversal_range": round(reversal_range, 2),
        "entry": round(entry, 4), "target": round(target, 4),
        "stop_loss": round(stop_loss, 4),
        "risk_pct": round((risk / entry) * 100, 2),
        "reward_pct": round((reward / entry) * 100, 2),
        "rr": rr,
    }

def format_alert(symbol, r):
    name = clean_symbol(symbol)
    stars = "⭐⭐⭐" if r["surge_ratio"] >= 50 else "⭐⭐" if r["surge_ratio"] >= 20 else "⭐"
    return (
        f"<b>{r['direction']} - {name}</b> {stars}\n"
        f"<code>━━━━━━━━━━━━━━━━━━━━</code>\n"
        f"💥 انفجار: <b>{r['surge_vol']:,.0f}</b> عقد ({r['surge_ratio']}x)\n"
        f"🔄 ارتداد: <b>{r['reversal_vol']:,.0f}</b> عقد ({r['reversal_ratio']}x)\n"
        f"📊 النطاق: {r['surge_range']}% → {r['reversal_range']}%\n"
        f"<code>━━━━━━━━━━━━━━━━━━━━</code>\n"
        f"💰 دخول: <b>${r['entry']:.4f}</b>\n"
        f"🎯 هدف: ${r['target']:.4f} (+{r['reward_pct']}%)\n"
        f"🛑 وقف: ${r['stop_loss']:.4f} (-{r['risk_pct']}%)\n"
        f"⚡ R/R: <b>{r['rr']}:1</b>\n"
        f"<code>━━━━━━━━━━━━━━━━━━━━</code>\n"
        f"🚀 <b>{r['action']} الآن!</b>"
    )

def x1_scan_and_alert():
    print("🔵 X1 بدأ العمل")
    send_telegram(session_x1, "🔵 <b>X1 يعمل بنجاح!</b>\n🔍 جاري مراقبة STOCK...")
    symbols = get_stock_symbols(session_x1)
    if not symbols:
        print("❌ X1: لا توجد عقود STOCK.")
        return
    print(f"🔵 X1: {len(symbols)} عقد للمراقبة")
    completed_history, early_history = {}, {}

    while True:
        try:
            print(f"🔵 X1 | فحص {datetime.now().strftime('%H:%M:%S')}")
            for symbol in symbols:
                klines = get_klines(session_x1, symbol, 15)
                if len(klines) < 10:
                    time.sleep(0.1)
                    continue
                now = time.time()
                name = clean_symbol(symbol)

                early = x1_detect_early_surge(klines)
                if early and now - early_history.get(symbol, 0) > 180:
                    early_history[symbol] = now
                    send_telegram(
                        session_x1,
                        f"👀 <b>مراقبة مبكرة - {name}</b>\n"
                        f"⚡️ انفجار فوليوم ({early['ratio']}x) {early['direction']}\n"
                        f"⏳ ننتظر التأكيد..."
                    )

                result = x1_detect_opportunity(klines)
                if result and now - completed_history.get(symbol, 0) > 300:
                    completed_history[symbol] = now
                    send_telegram(session_x1, format_alert(symbol, result))
                    print(f"🚨 X1: {name} | R/R {result['rr']}:1")
                time.sleep(0.1)

            time.sleep(CHECK_INTERVAL)
        except Exception as e:
            print(f"❌ X1 cycle error: {e}")
            time.sleep(10)

# ---------------- X2 ----------------

def x2_detect_early_surge(klines):
    if len(klines) < 10:
        return None
    baseline = calculate_baseline(klines[:-1])
    if not baseline:
        return None
    surge = klines[-1]
    if surge["open"] <= 0 or surge["vol"] < baseline * SURGE_MULTIPLIER:
        return None
    range_pct = ((surge["high"] - surge["low"]) / surge["open"]) * 100
    if range_pct < MIN_RANGE_PCT:
        return None
    direction = "صاعدة 🟢" if surge["close"] > surge["open"] else "هابطة 🔴"
    return {"ratio": round(surge["vol"] / baseline, 1), "direction": direction}

def x2_detect_opportunity(klines):
    if len(klines) < 10:
        return None
    baseline = calculate_baseline(klines)
    if not baseline:
        return None
    surge, reversal = klines[-2], klines[-1]
    if surge["open"] <= 0 or reversal["open"] <= 0:
        return None

    surge_range = ((surge["high"] - surge["low"]) / surge["open"]) * 100
    reversal_range = ((reversal["high"] - reversal["low"]) / reversal["open"]) * 100

    if surge["vol"] < baseline * SURGE_MULTIPLIER or surge_range < MIN_RANGE_PCT:
        return None
    if reversal["vol"] < MIN_CONFIRMATION_VOL or reversal["vol"] < baseline * REVERSAL_MULTIPLIER:
        return None
    if reversal_range < MIN_RANGE_PCT:
        return None

    surge_up = surge["close"] > surge["open"]
    reversal_up = reversal["close"] > reversal["open"]
    if surge_up == reversal_up:
        return None

    if not surge_up and reversal_up:
        direction, action = "🟢 ارتداد صاعد", "شراء"
        entry = reversal["close"]
        stop_loss = min(surge["low"], reversal["low"])
        target = surge["open"]
    else:
        direction, action = "🔴 ارتداد هابط", "بيع"
        entry = reversal["close"]
        stop_loss = max(surge["high"], reversal["high"])
        target = surge["open"]

    risk = abs(entry - stop_loss)
    reward = abs(target - entry)
    rr = round(reward / risk, 2) if risk > 0 else 0

    return {
        "direction": direction, "action": action,
        "surge_vol": surge["vol"], "reversal_vol": reversal["vol"],
        "surge_ratio": round(surge["vol"] / baseline, 1),
        "reversal_ratio": round(reversal["vol"] / baseline, 1),
        "surge_range": round(surge_range, 2),
        "reversal_range": round(reversal_range, 2),
        "entry": round(entry, 4), "target": round(target, 4),
        "stop_loss": round(stop_loss, 4),
        "risk_pct": round((risk / entry) * 100, 2),
        "reward_pct": round((reward / entry) * 100, 2),
        "rr": rr,
    }

def x2_scan_and_alert():
    print("🟢 X2 بدأ العمل")
    send_telegram(session_x2, "🟢 <b>X2 يعمل بنجاح!</b>\n🔍 جاري مراقبة STOCK...")
    symbols = get_stock_symbols(session_x2)
    if not symbols:
        print("❌ X2: لا توجد عقود STOCK.")
        return
    print(f"🟢 X2: {len(symbols)} عقد للمراقبة")
    completed_history, early_history = {}, {}

    while True:
        try:
            print(f"🟢 X2 | فحص {datetime.now().strftime('%H:%M:%S')}")
            found = 0
            for symbol in symbols:
                klines = get_klines(session_x2, symbol, 30)
                if len(klines) < 10:
                    time.sleep(0.05)
                    continue
                now = time.time()
                name = clean_symbol(symbol)

                early = x2_detect_early_surge(klines)
                if early and now - early_history.get(symbol, 0) > 180:
                    early_history[symbol] = now
                    send_telegram(
                        session_x2,
                        f"👀 <b>مراقبة مبكرة - {name}</b>\n"
                        f"⚡️ فوليوم غير طبيعي ({early['ratio']}x) {early['direction']}\n"
                        f"⏳ ننتظر التأكيد..."
                    )

                result = x2_detect_opportunity(klines)
                if result and now - completed_history.get(symbol, 0) >= 300:
                    completed_history[symbol] = now
                    found += 1
                    send_telegram(session_x2, format_alert(symbol, result))
                    print(f"🚨 X2: {name} | R/R {result['rr']}:1")
                time.sleep(0.05)

            print(f"🟢 X2 | فرص جديدة: {found}")
            time.sleep(CHECK_INTERVAL)
        except Exception as e:
            print(f"❌ X2 cycle error: {e}")
            time.sleep(10)

def main():
    validate_environment()
    print("=" * 60)
    print("🚀 تشغيل X1 + X2")
    print("=" * 60)

    t1 = threading.Thread(target=x1_scan_and_alert, name="X1", daemon=False)
    t2 = threading.Thread(target=x2_scan_and_alert, name="X2", daemon=False)

    t1.start()
    t2.start()

    print("✅ X1 يعمل")
    print("✅ X2 يعمل")

    t1.join()
    t2.join()

if __name__ == "__main__":
    main()
