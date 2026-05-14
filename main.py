from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import threading
import time
from typing import Dict, List, Optional, Tuple

import requests
from flask import Flask, Response, jsonify, request

app = Flask(__name__)


def _sanitize_log_value(value, depth: int = 0):
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        text = " ".join(value.split())
        text = text.replace(WEBHOOK_SHARED_SECRET, "***") if WEBHOOK_SHARED_SECRET else text
        if "secret=" in text:
            parts = text.split("secret=")
            text = parts[0] + "secret=***"
            if "&" in parts[-1]:
                text += "&" + parts[-1].split("&", 1)[1]
        return text[:600]
    if depth >= 2:
        try:
            return json.dumps(value, ensure_ascii=False)[:600]
        except Exception:
            return "[unserializable]"
    if isinstance(value, list):
        out = [_sanitize_log_value(item, depth + 1) for item in value[:8]]
        if len(value) > 8:
            out.append(f"... +{len(value) - 8}")
        return out
    if isinstance(value, dict):
        out = {}
        for key, item in list(value.items())[:24]:
            key_text = str(key)
            if any(part in key_text.lower() for part in ("token", "secret", "password", "api_key", "apikey")):
                out[key_text] = "***" if item else ""
            else:
                out[key_text] = _sanitize_log_value(item, depth + 1)
        if len(value) > 24:
            out["_truncated_keys"] = len(value) - 24
        return out
    return str(value)[:600]


def _log_event(level: str, category: str, message: str, **fields) -> None:
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": "render",
        "category": category,
        "severity": level,
        "message": message,
    }
    for key, value in fields.items():
        entry[key] = _sanitize_log_value(value)
    line = "[bot-debug] " + json.dumps(entry, ensure_ascii=False, sort_keys=True)
    if level == "error":
        app.logger.error(line)
    elif level == "warning":
        app.logger.warning(line)
    else:
        app.logger.info(line)


def _parse_urls(raw: str) -> List[str]:
    if not raw:
        return []
    parts = [item.strip() for item in raw.split(",")]
    return [item for item in parts if item]


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = str(os.getenv(name, "1" if default else "0")).strip().lower()
    return raw in ("1", "true", "yes", "on")


def _env_json_dict(name: str) -> Dict:
    raw = os.getenv(name, "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _load_telegram_bot_token_map() -> Dict[str, str]:
    mapping = {}
    for key, value in _env_json_dict("TELEGRAM_BOT_TOKEN_MAP").items():
        token = str(value or "").strip()
        if token:
            mapping[str(key).strip().lower()] = token

    aliases = {
        "main": ["TELEGRAM_BOT_TOKEN_MAIN", "TELEGRAM_BOT_TOKEN"],
        "buff": ["TELEGRAM_BOT_TOKEN_BUFF", "CHILD_TELEGRAM_BOT_TOKEN", "BUFF_TELEGRAM_BOT_TOKEN"],
        "uid": ["TELEGRAM_BOT_TOKEN_UID", "UID_TELEGRAM_BOT_TOKEN"],
    }
    for hint, env_names in aliases.items():
        for env_name in env_names:
            token = os.getenv(env_name, "").strip()
            if token:
                mapping[hint] = token
                break
    return mapping


PRIMARY_SCRIPT_URL = os.getenv("PRIMARY_SCRIPT_URL", "").strip()
TELEGRAM_SCRIPT_URLS = _parse_urls(os.getenv("TELEGRAM_SCRIPT_URLS", ""))
SCRIPT_BACKEND_URLS = _parse_urls(os.getenv("SCRIPT_BACKEND_URLS", ""))
LEAD_SCRIPT_URLS = _parse_urls(os.getenv("LEAD_SCRIPT_URLS", ""))
SEPAY_SCRIPT_URLS = _parse_urls(os.getenv("SEPAY_SCRIPT_URLS", ""))
SEPAY_FAILOVER_ENABLED = _env_bool("SEPAY_FAILOVER_ENABLED", False)
REQUEST_TIMEOUT_SEC = max(5, _env_int("REQUEST_TIMEOUT_SEC", 25))
WEBHOOK_SHARED_SECRET = os.getenv("WEBHOOK_SHARED_SECRET", "").strip()
TELEGRAM_ASYNC_ENABLED = _env_bool("TELEGRAM_ASYNC_ENABLED", True)
TELEGRAM_ASYNC_WORKERS = max(2, _env_int("TELEGRAM_ASYNC_WORKERS", 8))
TELEGRAM_FAILOVER_STRATEGY = str(os.getenv("TELEGRAM_FAILOVER_STRATEGY", "priority")).strip().lower()
if TELEGRAM_FAILOVER_STRATEGY not in ("priority", "hash"):
    TELEGRAM_FAILOVER_STRATEGY = "priority"
TELEGRAM_DEDUP_ENABLED = _env_bool("TELEGRAM_DEDUP_ENABLED", True)
TELEGRAM_DEDUP_TTL_SEC = max(60, _env_int("TELEGRAM_DEDUP_TTL_SEC", 6 * 60 * 60))
TELEGRAM_DEDUP_MAX_ITEMS = max(100, _env_int("TELEGRAM_DEDUP_MAX_ITEMS", 5000))
TELEGRAM_LOADING_ENABLED = _env_bool("TELEGRAM_LOADING_ENABLED", True)
TELEGRAM_LOADING_TEXT = os.getenv("TELEGRAM_LOADING_TEXT", "Đang chạy...").strip() or "Đang chạy..."
TELEGRAM_LOADING_TIMEOUT_SEC = max(1, _env_int("TELEGRAM_LOADING_TIMEOUT_SEC", 4))
TELEGRAM_BOT_TOKENS = _load_telegram_bot_token_map()
DEBUG_LOG_VERSION = "step25_render_immediate_loading_2026-05-14"
CORS_ALLOWED_ORIGINS = _parse_urls(os.getenv("CORS_ALLOWED_ORIGINS", "*")) or ["*"]
CORS_ALLOW_HEADERS = (
    os.getenv(
        "CORS_ALLOW_HEADERS",
        "Content-Type, X-Webhook-Secret, Authorization, X-Telegram-Bot-Api-Secret-Token",
    ).strip()
    or "Content-Type"
)
CORS_MAX_AGE_SEC = max(60, _env_int("CORS_MAX_AGE_SEC", 600))
TELEGRAM_EXECUTOR = ThreadPoolExecutor(max_workers=TELEGRAM_ASYNC_WORKERS)
TELEGRAM_DEDUP_CACHE: Dict[str, float] = {}
TELEGRAM_DEDUP_LOCK = threading.Lock()


def _payload_dict() -> Dict:
    parsed = request.get_json(silent=True)
    if isinstance(parsed, dict):
        return parsed

    raw = request.get_data(as_text=True) or ""
    if raw:
        try:
            decoded = json.loads(raw)
            if isinstance(decoded, dict):
                return decoded
        except Exception:
            pass

    return {}


def _is_telegram_payload(payload: Dict) -> bool:
    return isinstance(payload, dict) and (
        "update_id" in payload
        or "message" in payload
        or "callback_query" in payload
        or "edited_message" in payload
    )


def _telegram_message_identity(payload: Dict) -> Tuple[str, str]:
    for field in ("message", "edited_message", "channel_post", "edited_channel_post"):
        message = payload.get(field)
        if not isinstance(message, dict):
            continue
        message_id = str(message.get("message_id", "")).strip()
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        chat_id = str(chat.get("id", "")).strip()
        if chat_id and message_id:
            return field, f"{chat_id}:{message_id}"
    return "", ""


def _telegram_dedup_key(payload: Dict) -> str:
    update_id = str(payload.get("update_id", "")).strip()
    if update_id:
        return f"upd:{update_id}"

    callback_query = payload.get("callback_query")
    if isinstance(callback_query, dict):
        callback_id = str(callback_query.get("id", "")).strip()
        if callback_id:
            return f"cb:{callback_id}"

    message_type, message_key = _telegram_message_identity(payload)
    if message_key:
        return f"{message_type}:{message_key}"

    try:
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    except Exception:
        raw = str(payload)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"hash:{digest}"


def _prune_telegram_dedup_cache(now: float) -> None:
    expired = [key for key, expires_at in TELEGRAM_DEDUP_CACHE.items() if expires_at <= now]
    for key in expired:
        TELEGRAM_DEDUP_CACHE.pop(key, None)

    overflow = len(TELEGRAM_DEDUP_CACHE) - TELEGRAM_DEDUP_MAX_ITEMS
    if overflow <= 0:
        return

    oldest = sorted(TELEGRAM_DEDUP_CACHE.items(), key=lambda item: item[1])[:overflow]
    for key, _expires_at in oldest:
        TELEGRAM_DEDUP_CACHE.pop(key, None)


def _mark_telegram_update_seen(payload: Dict) -> Tuple[bool, str]:
    if not TELEGRAM_DEDUP_ENABLED:
        return False, ""

    key = _telegram_dedup_key(payload)
    now = time.time()
    with TELEGRAM_DEDUP_LOCK:
        _prune_telegram_dedup_cache(now)
        expires_at = TELEGRAM_DEDUP_CACHE.get(key, 0)
        if expires_at > now:
            return True, key
        TELEGRAM_DEDUP_CACHE[key] = now + TELEGRAM_DEDUP_TTL_SEC
        return False, key


def _is_sepay_payload(payload: Dict) -> bool:
    if not isinstance(payload, dict):
        return False
    transfer_type = str(payload.get("transferType", payload.get("transfer_type", ""))).strip().lower()
    has_tx_id = any(
        str(payload.get(k, "")).strip()
        for k in ["id", "transaction_id", "transactionId", "referenceCode", "reference_code"]
    )
    try:
        amount = float(payload.get("transferAmount", payload.get("transfer_amount", payload.get("amount", 0))) or 0)
    except Exception:
        amount = 0
    has_content = any(
        str(payload.get(k, "")).strip()
        for k in ["content", "description", "transferContent", "referenceCode", "reference_code"]
    )
    return bool(transfer_type or (has_tx_id and amount > 0) or (has_tx_id and has_content))


def _with_source(url: str, source: str) -> str:
    joiner = "&" if "?" in url else "?"
    return f"{url}{joiner}source={source}"


def _append_query_params(url: str, params: Dict[str, str]) -> str:
    out = url
    for key, value in params.items():
        if not value:
            continue
        joiner = "&" if "?" in out else "?"
        out = f"{out}{joiner}{key}={value}"
    return out


def _forward_headers() -> Dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if WEBHOOK_SHARED_SECRET:
        headers["X-Webhook-Secret"] = WEBHOOK_SHARED_SECRET
    return headers


def _post_json(url: str, payload: Dict, source: str, extra_params: Optional[Dict[str, str]] = None) -> requests.Response:
    target = _with_source(url, source)
    target = _append_query_params(target, extra_params or {})
    return requests.post(
        target,
        json=payload,
        headers=_forward_headers(),
        timeout=REQUEST_TIMEOUT_SEC,
    )


def _resolve_cors_allow_origin() -> str:
    origin = str(request.headers.get("Origin", "")).strip()
    if "*" in CORS_ALLOWED_ORIGINS:
        return "*"
    if origin and origin in CORS_ALLOWED_ORIGINS:
        return origin
    if not origin and CORS_ALLOWED_ORIGINS:
        return CORS_ALLOWED_ORIGINS[0]
    return ""


def _unique_urls(urls: List[str]) -> List[str]:
    out = []
    seen = set()
    for item in urls:
        val = str(item or "").strip()
        if not val or val in seen:
            continue
        seen.add(val)
        out.append(val)
    return out


def _default_script_backends() -> List[str]:
    if SCRIPT_BACKEND_URLS:
        return _unique_urls(SCRIPT_BACKEND_URLS)
    if PRIMARY_SCRIPT_URL:
        return [PRIMARY_SCRIPT_URL]
    return []


def _telegram_backends() -> List[str]:
    if TELEGRAM_SCRIPT_URLS:
        return _unique_urls(TELEGRAM_SCRIPT_URLS)
    return _default_script_backends()


def _lead_backends() -> List[str]:
    if LEAD_SCRIPT_URLS:
        return _unique_urls(LEAD_SCRIPT_URLS)
    return _default_script_backends()


def _sepay_backends() -> List[str]:
    if SEPAY_SCRIPT_URLS:
        return _unique_urls(SEPAY_SCRIPT_URLS)
    if PRIMARY_SCRIPT_URL:
        return [PRIMARY_SCRIPT_URL]
    return _default_script_backends()


def _ordered_urls(urls: List[str], payload: Dict, source: str) -> List[str]:
    if len(urls) <= 1:
        return urls

    if source == "telegram" and TELEGRAM_FAILOVER_STRATEGY == "priority":
        # Strict ordered failover: backend 1 -> backend 2 -> backend 3
        return urls

    key_candidates = [
        payload.get("update_id"),
        payload.get("id"),
        payload.get("transaction_id"),
        payload.get("transactionId"),
        payload.get("referenceCode"),
        payload.get("reference_code"),
        payload.get("phone"),
        payload.get("chatId"),
    ]
    key = ""
    for item in key_candidates:
        val = str(item or "").strip()
        if val:
            key = val
            break
    if not key:
        key = json.dumps(payload, ensure_ascii=False, sort_keys=True)[:400]

    hash_val = int(hashlib.sha256(f"{source}:{key}".encode("utf-8")).hexdigest(), 16)
    start = hash_val % len(urls)
    return urls[start:] + urls[:start]


def _telegram_forward_params_from_request() -> Dict[str, str]:
    # Preserve the bot/profile hint so one Render endpoint can serve main, buff, and uid bots.
    hint = _telegram_bot_hint_from_request()
    return {"bot": hint} if hint else {}


def _telegram_bot_hint_from_request() -> str:
    for key in ("bot", "tg_bot", "profile"):
        value = request.args.get(key, "").strip().lower()
        if value:
            return value
    return ""


def _telegram_bot_token_for_hint(hint: str) -> str:
    key = str(hint or "").strip().lower()
    if key and key in TELEGRAM_BOT_TOKENS:
        return TELEGRAM_BOT_TOKENS[key]
    return TELEGRAM_BOT_TOKENS.get("main", "") or os.getenv("TELEGRAM_BOT_TOKEN", "").strip()


def _telegram_text_message_target(payload: Dict) -> Tuple[str, str]:
    message = payload.get("message") if isinstance(payload, dict) else None
    if not isinstance(message, dict):
        return "", ""
    text = str(message.get("text", "") or "").strip()
    chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    chat_id = str(chat.get("id", "") or "").strip()
    if not chat_id or not text:
        return "", ""
    return chat_id, text


def _send_telegram_loading_message(payload: Dict, bot_hint: str) -> Dict:
    if not TELEGRAM_LOADING_ENABLED:
        return {"ok": False, "skipped": True, "reason": "loading_disabled"}

    chat_id, text = _telegram_text_message_target(payload)
    if not chat_id or not text:
        return {"ok": False, "skipped": True, "reason": "not_text_message"}

    token = _telegram_bot_token_for_hint(bot_hint)
    if not token:
        return {"ok": False, "skipped": True, "reason": "telegram_token_missing"}

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": f"<i>{TELEGRAM_LOADING_TEXT}</i>",
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=TELEGRAM_LOADING_TIMEOUT_SEC,
        )
        body = resp.json() if resp.text else {}
        message_id = 0
        if isinstance(body, dict) and isinstance(body.get("result"), dict):
            try:
                message_id = int(body["result"].get("message_id") or 0)
            except Exception:
                message_id = 0
        ok = 200 <= int(resp.status_code) < 300 and bool(body.get("ok", False))
        if not ok:
            _log_event(
                "warning",
                "telegram_loading",
                "Telegram loading message failed",
                code="telegram_loading_failed",
                bot=bot_hint or "main",
                http=int(resp.status_code),
                body=(resp.text or "")[:600],
            )
        return {
            "ok": ok,
            "chat_id": chat_id,
            "message_id": message_id,
            "http": int(resp.status_code),
        }
    except Exception as exc:
        _log_event(
            "warning",
            "telegram_loading",
            "Telegram loading message exception",
            code="telegram_loading_exception",
            bot=bot_hint or "main",
            error=str(exc),
        )
        return {"ok": False, "error": str(exc)}


def _forward_telegram(payload: Dict, forward_params: Optional[Dict[str, str]] = None) -> Tuple[bool, List[Dict]]:
    urls = _ordered_urls(_telegram_backends(), payload, "telegram")
    if not urls:
        _log_event("error", "telegram_retry", "No Telegram backend URL configured", code="telegram_backend_missing")
        return False, [{"error": "No backend URL configured"}]

    # Telegram commands are not safe to retry across Apps Script backends:
    # Apps Script can send Telegram messages before Render sees a timeout/error.
    # Retrying would duplicate menus/orders, so one update is delivered to one backend only.
    target_url = urls[0]
    params = forward_params or {}
    try:
        resp = _post_json(target_url, payload, "telegram", params)
        body_text = (resp.text or "")[:1200]
        return (
            200 <= int(resp.status_code) < 300,
            [{
                "url": _append_query_params(target_url, params),
                "status": int(resp.status_code),
                "body": body_text,
                "single_delivery": True,
            }],
        )
    except Exception as exc:
        _log_event(
            "error",
            "telegram_retry",
            "Telegram forward request failed",
            code="telegram_forward_exception",
            url=_append_query_params(target_url, params),
            error=str(exc),
        )
        return False, [{
            "url": _append_query_params(target_url, params),
            "error": str(exc),
            "single_delivery": True,
        }]


def _forward_telegram_background(payload: Dict, forward_params: Dict[str, str]) -> None:
    try:
        ok, attempts = _forward_telegram(payload, forward_params)
        if not ok:
            _log_event(
                "error",
                "telegram_retry",
                "Telegram async forward failed",
                code="telegram_async_forward_failed",
                attempts=attempts,
            )
    except Exception as exc:
        _log_event(
            "error",
            "telegram_retry",
            "Telegram async forward crashed",
            code="telegram_async_forward_crashed",
            error=str(exc),
        )
        app.logger.exception("Telegram async forward crashed")


def _response_body_contains_quota_error(body_text: str) -> bool:
    text = (body_text or "").lower()
    if not text:
        return False
    return (
        "service invoked too many times for one day: urlfetch" in text
        or "too many times for one day: urlfetch" in text
        or "limit exceeded: url fetch" in text
        or "quota exceeded" in text
    )


def _response_body_has_json_ok_true(body_text: str) -> bool:
    text = (body_text or "").strip()
    if not text:
        return False
    try:
        data = json.loads(text)
    except Exception:
        return False
    return isinstance(data, dict) and data.get("ok") is True


def _response_is_retryable_failure(
    resp: requests.Response,
    body_text: str,
    retry_on_any_non_2xx: bool = False,
    require_json_ok_true: bool = False,
) -> bool:
    status = int(resp.status_code)
    if status in (408, 425, 429, 500, 502, 503, 504):
        return True
    if status < 200 or status >= 300:
        return retry_on_any_non_2xx
    if _response_body_contains_quota_error(body_text):
        return True
    if require_json_ok_true and not _response_body_has_json_ok_true(body_text):
        return True
    return False


def _forward_with_failover(
    source: str,
    payload: Dict,
    urls: List[str],
    extra_params: Optional[Dict[str, str]] = None,
    retry_on_any_non_2xx: bool = False,
    require_json_ok_true: bool = False,
) -> Tuple[bool, List[Dict]]:
    if not urls:
        _log_event("error", "render_forward_error", "No backend URL configured", code="backend_missing", route=source)
        return False, [{"error": "No backend URL configured"}]

    attempts = []
    for url in _ordered_urls(urls, payload, source):
        try:
            resp = _post_json(url, payload, source, extra_params)
            body_text = (resp.text or "")[:1200]
            attempt = {
                "url": _append_query_params(url, extra_params or {}),
                "status": int(resp.status_code),
                "body": body_text,
            }
            attempts.append(attempt)
            if not _response_is_retryable_failure(
                resp,
                body_text,
                retry_on_any_non_2xx=retry_on_any_non_2xx,
                require_json_ok_true=require_json_ok_true,
            ):
                return 200 <= int(resp.status_code) < 300, attempts
        except Exception as exc:
            _log_event(
                "error",
                "render_forward_error",
                "Backend forward request failed",
                code="backend_forward_exception",
                route=source,
                url=_append_query_params(url, extra_params or {}),
                error=str(exc),
            )
            attempts.append({"url": _append_query_params(url, extra_params or {}), "error": str(exc)})
    _log_event(
        "error",
        "render_forward_error",
        "All backend forward attempts failed",
        code="backend_forward_all_failed",
        route=source,
        attempts=attempts,
    )
    return False, attempts


def _forward_single(source: str, payload: Dict) -> Tuple[bool, Dict]:
    if not PRIMARY_SCRIPT_URL:
        return False, {"error": "PRIMARY_SCRIPT_URL is not configured"}

    try:
        resp = _post_json(PRIMARY_SCRIPT_URL, payload, source)
        body_text = resp.text[:1200] if resp.text else ""
        return (
            200 <= resp.status_code < 300,
            {"status": resp.status_code, "body": body_text},
        )
    except Exception as exc:
        return False, {"error": str(exc)}


@app.get("/")
def home() -> Response:
    return jsonify(
        {
            "ok": True,
            "service": "apps-script-webhook-load-balancer",
            "debug_log_version": DEBUG_LOG_VERSION,
            "telegram_backends": len(_telegram_backends()),
            "telegram_async": TELEGRAM_ASYNC_ENABLED,
            "telegram_failover_strategy": TELEGRAM_FAILOVER_STRATEGY,
            "telegram_delivery_mode": "single_backend_no_retry",
            "telegram_retry_on_json_ok_false": False,
            "telegram_dedup_enabled": TELEGRAM_DEDUP_ENABLED,
            "telegram_dedup_ttl_sec": TELEGRAM_DEDUP_TTL_SEC,
            "telegram_dedup_cache_items": len(TELEGRAM_DEDUP_CACHE),
            "telegram_loading_enabled": TELEGRAM_LOADING_ENABLED,
            "telegram_loading_tokens": sorted(TELEGRAM_BOT_TOKENS.keys()),
            "lead_backends": len(_lead_backends()),
            "sepay_backend": bool(PRIMARY_SCRIPT_URL),
        }
    )


@app.after_request
def add_cors_headers(response: Response) -> Response:
    allow_origin = _resolve_cors_allow_origin()
    if allow_origin:
        response.headers["Access-Control-Allow-Origin"] = allow_origin
        if allow_origin != "*":
            response.headers["Vary"] = "Origin"
    response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = CORS_ALLOW_HEADERS
    response.headers["Access-Control-Max-Age"] = str(CORS_MAX_AGE_SEC)
    return response


@app.route("/webhook/telegram", methods=["OPTIONS"])
@app.route("/webhook/sepay", methods=["OPTIONS"])
@app.route("/webhook/lead", methods=["OPTIONS"])
@app.route("/webhook", methods=["OPTIONS"])
def webhook_options() -> Response:
    return Response(status=204)


@app.post("/webhook/telegram")
def webhook_telegram() -> Response:
    payload = _payload_dict()
    duplicate, dedup_key = _mark_telegram_update_seen(payload)
    if duplicate:
        _log_event(
            "info",
            "telegram_retry",
            "Duplicate Telegram update skipped by Render",
            code="duplicate_update_skipped",
            route="telegram",
            dedup_key=dedup_key,
            update_id=payload.get("update_id") if isinstance(payload, dict) else "",
        )
        return jsonify({
            "ok": True,
            "accepted": False,
            "duplicate": True,
            "dedup_key": dedup_key,
            "source": "telegram",
        }), 200

    bot_hint = _telegram_bot_hint_from_request()
    forward_params = _telegram_forward_params_from_request()
    loading = _send_telegram_loading_message(payload, bot_hint)
    if loading.get("ok") and loading.get("message_id"):
        forward_params["loading_message_id"] = str(loading.get("message_id"))
        forward_params["loading_source"] = "render"

    if TELEGRAM_ASYNC_ENABLED:
        # Ack Telegram immediately to avoid webhook timeouts on cold start or slow Apps Script runs.
        TELEGRAM_EXECUTOR.submit(_forward_telegram_background, payload, forward_params)
        return jsonify({
            "ok": True,
            "accepted": True,
            "async": True,
            "source": "telegram",
            "dedup_key": dedup_key,
            "loading": bool(loading.get("ok")),
        }), 200

    ok, attempts = _forward_telegram(payload, forward_params)
    # Telegram route always returns 200 to avoid aggressive retry storms.
    return jsonify({
        "ok": ok,
        "source": "telegram",
        "dedup_key": dedup_key,
        "loading": bool(loading.get("ok")),
        "attempts": attempts,
    }), 200


@app.post("/webhook/sepay")
def webhook_sepay() -> Response:
    payload = _payload_dict()
    if SEPAY_FAILOVER_ENABLED:
        ok, attempts = _forward_with_failover("sepay", payload, _sepay_backends())
        status = 200 if ok else 502
        return jsonify({"ok": ok, "source": "sepay", "attempts": attempts}), status

    ok, detail = _forward_single("sepay", payload)
    # Keep non-200 if upstream fails so SePay can retry.
    status = 200 if ok else 502
    return jsonify({"ok": ok, "source": "sepay", "detail": detail}), status


@app.post("/webhook/lead")
def webhook_lead() -> Response:
    payload = _payload_dict()
    ok, attempts = _forward_with_failover("lead", payload, _lead_backends())
    status = 200 if ok else 502
    return jsonify({"ok": ok, "source": "lead", "attempts": attempts}), status


@app.post("/webhook")
def webhook_auto() -> Response:
    payload = _payload_dict()
    if _is_telegram_payload(payload):
        return webhook_telegram()
    if _is_sepay_payload(payload):
        return webhook_sepay()
    return webhook_lead()


if __name__ == "__main__":
    port = _env_int("PORT", 10000)
    app.run(host="0.0.0.0", port=port)
