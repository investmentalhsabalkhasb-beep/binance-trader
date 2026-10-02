import os
import re
import json
import time
import hmac
import hashlib
import threading
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

import requests
from flask import Flask, jsonify, request, Response

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HTML_PATH = os.path.join(BASE_DIR, "index.html")
KEYS_FILE = os.path.join(BASE_DIR, "keys.json")
LOCK = threading.Lock()
_cache_lock = threading.Lock()

BOT_TOKEN = os.environ.get("BOT_TOKEN", "8661997948:AAH8Xsq141EirxRhBRk1ljL8B2JtIfBDguc")
OWNER_ID = int(os.environ.get("OWNER_ID", "7168766467"))
SITE_URL = os.environ.get("SITE_URL", "")
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "5000"))

BASES = {"test": "https://testnet.binance.vision", "live": "https://api.binance.com"}
PUBLIC_BASE = "https://api.binance.com"
SYMBOL_RE = re.compile(r"^[A-Z0-9]{5,20}$")
INTERVALS = {"1m", "5m", "15m", "1h", "4h", "1d"}

app = Flask(__name__)


def _empty_state():
    return {"mode": "test", "test": {"key": "", "secret": ""}, "live": {"key": "", "secret": ""}}


def load_state():
    with LOCK:
        if not os.path.exists(KEYS_FILE):
            return _empty_state()
        try:
            with open(KEYS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            st = _empty_state()
            st.update({k: data[k] for k in ("mode", "test", "live") if k in data})
            return st
        except (OSError, ValueError):
            return _empty_state()


def save_state(st):
    with LOCK:
        with open(KEYS_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f)
        try:
            os.chmod(KEYS_FILE, 0o600)
        except OSError:
            pass


class ApiError(Exception):
    def __init__(self, message, status=400, code="error"):
        super().__init__(message)
        self.message, self.status, self.code = message, status, code


def binance_public(path, params=None):
    try:
        r = requests.get(PUBLIC_BASE + path, params=params or {}, timeout=10)
        data = r.json()
    except (requests.RequestException, ValueError):
        raise ApiError("Cannot reach Binance", 502, "network")
    if r.status_code != 200:
        raise ApiError(data.get("msg", "Binance error"), r.status_code)
    return data


def binance_signed(method, path, params, key, secret, base):
    if not key or not secret:
        raise ApiError("API keys are not set for this mode", 400, "no_keys")
    p = dict(params or {})
    p["timestamp"] = int(time.time() * 1000)
    p["recvWindow"] = 10000
    qs = urlencode(p)
    sig = hmac.new(secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
    try:
        r = requests.request(method, base + path + "?" + qs + "&signature=" + sig,
                             headers={"X-MBX-APIKEY": key}, timeout=10)
        data = r.json()
    except (requests.RequestException, ValueError):
        raise ApiError("Cannot reach Binance", 502, "network")
    if r.status_code != 200:
        raise ApiError(data.get("msg", "Binance error"), r.status_code, "binance")
    return data


def active_creds():
    st = load_state()
    mode = st["mode"] if st["mode"] in BASES else "test"
    return mode, st[mode].get("key", ""), st[mode].get("secret", "")


_cache = {}


def cached(name, ttl, fn):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(name)
        if hit and now - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _cache_lock:
        _cache[name] = (now, val)
    return val


def account_summary():
    mode, key, secret = active_creds()
    acc = binance_signed("GET", "/api/v3/account", {}, key, secret, BASES[mode])
    prices = {p["symbol"]: float(p["price"]) for p in cached(
        "prices", 5, lambda: binance_public("/api/v3/ticker/price"))}
    rows, total = [], 0.0
    for b in acc.get("balances", []):
        free, locked = float(b["free"]), float(b["locked"])
        if free + locked <= 0:
            continue
        asset = b["asset"]
        price = 1.0 if asset in ("USDT", "USDC", "FDUSD", "BUSD") else prices.get(asset + "USDT", 0.0)
        value = (free + locked) * price
        total += value
        rows.append({"asset": asset, "free": free, "locked": locked, "value": value})
    rows.sort(key=lambda r: r["value"], reverse=True)
    return {"mode": mode, "total": total, "balances": rows}


@app.after_request
def cors(resp):
    origin = request.headers.get("Origin", "")
    resp.headers["Access-Control-Allow-Origin"] = origin or "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Init-Data"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Vary"] = "Origin"
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.errorhandler(ApiError)
def handle_api_error(e):
    return jsonify({"error": e.message, "code": e.code}), e.status


@app.route("/")
def index():
    with open(HTML_PATH, "r", encoding="utf-8") as f:
        html = f.read()
    return Response(html, mimetype="text/html")


def state_public():
    st = load_state()
    hint = lambda k: ("••••" + k[-4:]) if len(k) >= 4 else ""
    return {"mode": st["mode"],
            "configured": {m: bool(st[m]["key"] and st[m]["secret"]) for m in ("test", "live")},
            "hints": {m: hint(st[m]["key"]) for m in ("test", "live")}}


@app.route("/api/state")
def api_state():
    return jsonify(state_public())


@app.route("/api/mode", methods=["POST"])
def api_mode():
    mode = (request.get_json(silent=True) or {}).get("mode")
    if mode not in BASES:
        raise ApiError("Invalid mode")
    st = load_state()
    st["mode"] = mode
    save_state(st)
    with _cache_lock:
        _cache.clear()
    return jsonify(state_public())


@app.route("/api/keys", methods=["POST"])
def api_keys():
    body = request.get_json(silent=True) or {}
    mode = body.get("mode")
    key = str(body.get("key", "")).strip()
    secret = str(body.get("secret", "")).strip()
    if mode not in BASES:
        raise ApiError("Invalid mode")
    if not key or not secret:
        raise ApiError("Key and secret are required", 400, "missing")
    binance_signed("GET", "/api/v3/account", {}, key, secret, BASES[mode])
    st = load_state()
    st[mode] = {"key": key, "secret": secret}
    st["mode"] = mode
    save_state(st)
    with _cache_lock:
        _cache.clear()
    return jsonify(state_public())


@app.route("/api/tickers")
def api_tickers():
    def fetch():
        out = []
        for t in binance_public("/api/v3/ticker/24hr"):
            s = t.get("symbol", "")
            if not s.endswith("USDT") or float(t.get("quoteVolume", 0) or 0) <= 0:
                continue
            out.append({"s": s, "p": t["lastPrice"], "c": t["priceChangePercent"],
                        "h": t["highPrice"], "l": t["lowPrice"], "v": t["quoteVolume"]})
        out.sort(key=lambda x: float(x["v"]), reverse=True)
        return out
    return jsonify(cached("tickers", 3, fetch))


@app.route("/api/klines")
def api_klines():
    symbol = request.args.get("symbol", "BTCUSDT").upper()
    interval = request.args.get("interval", "15m")
    if not SYMBOL_RE.match(symbol) or interval not in INTERVALS:
        raise ApiError("Invalid parameters")
    data = binance_public("/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": 100})
    return jsonify([[c[0], c[1], c[2], c[3], c[4], c[5]] for c in data])


@app.route("/api/account")
def api_account():
    return jsonify(account_summary())


@app.route("/api/order", methods=["POST"])
def api_order():
    body = request.get_json(silent=True) or {}
    symbol = str(body.get("symbol", "")).upper()
    side = str(body.get("side", "")).upper()
    if not SYMBOL_RE.match(symbol) or side not in ("BUY", "SELL"):
        raise ApiError("Invalid order")
    try:
        amount = Decimal(str(body.get("quote_amount")))
    except (InvalidOperation, TypeError):
        raise ApiError("Invalid amount")
    if amount <= 0:
        raise ApiError("Invalid amount")
    mode, key, secret = active_creds()
    res = binance_signed("POST", "/api/v3/order", {
        "symbol": symbol, "side": side, "type": "MARKET",
        "quoteOrderQty": format(amount, "f")}, key, secret, BASES[mode])
    return jsonify({"mode": mode, "orderId": res.get("orderId"), "symbol": res.get("symbol"),
                    "side": res.get("side"), "status": res.get("status"),
                    "executedQty": res.get("executedQty"), "quoteQty": res.get("cummulativeQuoteQty")})


TG_API = "https://api.telegram.org/bot" + BOT_TOKEN


def tg(method, **params):
    try:
        return requests.post(TG_API + "/" + method, json=params, timeout=45).json()
    except (requests.RequestException, ValueError) as e:
        print("tg error [" + method + "]:", e)
        return {}


def send(chat_id, text, markup=None):
    limit = 4000
    chunks = [text[i:i+limit] for i in range(0, len(text), limit)] or [""]
    for i, chunk in enumerate(chunks):
        p = {"chat_id": chat_id, "text": chunk}
        if markup and i == len(chunks) - 1:
            p["reply_markup"] = markup
        tg("sendMessage", **p)


def app_button():
    if not SITE_URL or not SITE_URL.startswith("https://"):
        return None
    return {"inline_keyboard": [[{"text": "📈 فتح التداول", "web_app": {"url": SITE_URL}}]]}


def handle(msg):
    global SITE_URL
    chat_id = msg["chat"]["id"]
    uid = msg["from"]["id"]
    parts = (msg.get("text") or "").strip().split()
    cmd = parts[0].split("@")[0].lower() if parts else ""

    if cmd == "/id":
        return send(chat_id, "معرّفك: " + str(uid))
    if uid != OWNER_ID:
        return send(chat_id, "غير مصرح لك باستخدام هذا البوت.\nمعرّفك: " + str(uid))

    if cmd in ("/start", "/app"):
        send(chat_id,
             "أهلاً بك 👋\n"
             "افتح التطبيق من زر القائمة أسفل الشاشة.\n\n"
             "الأوامر:\n"
             "/price BTC — سعر عملة\n"
             "/balance — الرصيد\n"
             "/mode — النمط الحالي\n"
             "/seturl <url> — تحديث رابط التطبيق",
             app_button())
    elif cmd == "/seturl":
        if len(parts) < 2 or not parts[1].startswith("https://"):
            return send(chat_id, "أرسل: /seturl https://your-app-url.com")
        SITE_URL = parts[1].rstrip("/")
        tg("setChatMenuButton", menu_button={"type": "web_app", "text": "التداول", "web_app": {"url": SITE_URL}})
        send(chat_id, "✅ تم تحديث الرابط:\n" + SITE_URL, app_button())
    elif cmd == "/price":
        sym = (parts[1] if len(parts) > 1 else "BTC").upper()
        sym = sym if sym.endswith("USDT") else sym + "USDT"
        try:
            t = binance_public("/api/v3/ticker/24hr", {"symbol": sym})
            send(chat_id, "%s\nالسعر: %g\nالتغير 24س: %+.2f%%" % (
                sym, float(t["lastPrice"]), float(t["priceChangePercent"])))
        except ApiError as e:
            send(chat_id, "تعذّر جلب السعر: " + e.message)
    elif cmd == "/balance":
        try:
            s = account_summary()
            top = "\n".join("%s: %g" % (b["asset"], b["free"] + b["locked"]) for b in s["balances"][:8])
            send(chat_id, "النمط: %s\nالإجمالي: $%s\n%s" % (
                "تجريبي" if s["mode"] == "test" else "حقيقي", format(s["total"], ",.2f"), top))
        except ApiError as e:
            send(chat_id, "أضف المفاتيح أولاً من التطبيق." if e.code == "no_keys" else "خطأ: " + e.message)
    elif cmd == "/mode":
        st = state_public()
        send(chat_id, "النمط الحالي: %s\nمفاتيح تجريبي: %s\nمفاتيح حقيقي: %s" % (
            "تجريبي" if st["mode"] == "test" else "حقيقي",
            "✅" if st["configured"]["test"] else "❌",
            "✅" if st["configured"]["live"] else "❌"))
    else:
        send(chat_id, "أرسل /start لعرض الأوامر.")


def poll_loop():
    tg("deleteWebhook")
    if SITE_URL and SITE_URL.startswith("https://"):
        tg("setChatMenuButton", menu_button={"type": "web_app", "text": "التداول", "web_app": {"url": SITE_URL}})
    offset = None
    while True:
        res = tg("getUpdates", offset=offset, timeout=30, allowed_updates=["message"])
        if not res.get("ok"):
            time.sleep(3)
            continue
        for u in res.get("result", []):
            offset = u["update_id"] + 1
            if "message" in u:
                try:
                    handle(u["message"])
                except Exception as e:
                    print("handler error:", e)


threading.Thread(target=poll_loop, daemon=True).start()
print("=" * 60)
print("Bot started.")
print("Server: http://%s:%s" % (HOST, PORT))
print("Site URL: %s" % (SITE_URL or "(not set)"))
print("=" * 60)
app.run(host=HOST, port=PORT, debug=False, threaded=True, use_reloader=False)
