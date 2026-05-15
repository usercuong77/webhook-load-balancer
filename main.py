from concurrent.futures import ThreadPoolExecutor
import asyncio
import hashlib
import json
import os
from queue import Empty, Full, Queue
import threading
import time
from typing import Dict, List, Optional, Set, Tuple

import requests
from flask import Flask, Response, jsonify, request

try:
    import uid_checker_service as UID_CHECKER_SERVICE
except Exception as uid_checker_import_exc:
    UID_CHECKER_SERVICE = None
    UID_CHECKER_IMPORT_ERROR = str(uid_checker_import_exc)
else:
    UID_CHECKER_IMPORT_ERROR = ""

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


def _parse_csv_set(raw: str) -> Set[str]:
    return {item.strip().lower() for item in (raw or "").split(",") if item.strip()}


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
TELEGRAM_FORWARD_TIMEOUT_SEC = max(REQUEST_TIMEOUT_SEC, _env_int("TELEGRAM_FORWARD_TIMEOUT_SEC", 75))
WEBHOOK_SHARED_SECRET = os.getenv("WEBHOOK_SHARED_SECRET", "").strip()
UID_CHECKER_ENABLED = _env_bool("UID_CHECKER_ENABLED", True)
UID_CHECKER_API_KEY = (
    os.getenv("UID_CHECKER_API_KEY", "").strip()
    or os.getenv("EXTERNAL_CHECKER_API_KEY", "").strip()
    or "abc123"
)
CHECKER_CACHE_ENABLED = _env_bool("CHECKER_CACHE_ENABLED", True)
CHECKER_CACHE_MAX_ITEMS = max(100, _env_int("CHECKER_CACHE_MAX_ITEMS", 2000))
CHECKER_GET_UID_CACHE_TTL_SEC = max(60, _env_int("CHECKER_GET_UID_CACHE_TTL_SEC", 6 * 60 * 60))
CHECKER_CHECK_CACHE_TTL_SEC = max(0, _env_int("CHECKER_CHECK_CACHE_TTL_SEC", 45))
CHECKER_LATEST_POST_CACHE_TTL_SEC = max(0, _env_int("CHECKER_LATEST_POST_CACHE_TTL_SEC", 55))
CHECKER_BATCH_MAX_ITEMS = max(1, _env_int("CHECKER_BATCH_MAX_ITEMS", 25))
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
TELEGRAM_HEAVY_QUEUE_ENABLED = _env_bool("TELEGRAM_HEAVY_QUEUE_ENABLED", True)
TELEGRAM_HEAVY_QUEUE_WORKERS = max(1, _env_int("TELEGRAM_HEAVY_QUEUE_WORKERS", 2))
TELEGRAM_HEAVY_QUEUE_MAX_SIZE = max(10, _env_int("TELEGRAM_HEAVY_QUEUE_MAX_SIZE", 200))
TELEGRAM_HEAVY_QUEUE_NON_COMMANDS = _env_bool("TELEGRAM_HEAVY_QUEUE_NON_COMMANDS", True)
TELEGRAM_HEAVY_COMMANDS = _parse_csv_set(
    os.getenv(
        "TELEGRAM_HEAVY_COMMANDS",
        "/check,/checkpost,/viplike,/viplikeoff,/lammoiviplike,/lamoi,/refreshviplike,/capnhatmenu,/accgg",
    )
)
TELEGRAM_DURABLE_QUEUE_ENABLED = _env_bool("TELEGRAM_DURABLE_QUEUE_ENABLED", True)
UPSTASH_REDIS_REST_URL = os.getenv("UPSTASH_REDIS_REST_URL", "").strip().rstrip("/")
UPSTASH_REDIS_REST_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "").strip()
TELEGRAM_DURABLE_QUEUE_KEY = (
    os.getenv("TELEGRAM_DURABLE_QUEUE_KEY", "bot:telegram:heavy:queue").strip()
    or "bot:telegram:heavy:queue"
)
TELEGRAM_DURABLE_PROCESSING_KEY = (
    os.getenv("TELEGRAM_DURABLE_PROCESSING_KEY", "bot:telegram:heavy:processing").strip()
    or "bot:telegram:heavy:processing"
)
TELEGRAM_DURABLE_ACTIVE_QUEUES_KEY = (
    os.getenv("TELEGRAM_DURABLE_ACTIVE_QUEUES_KEY", "bot:telegram:heavy:active_queues").strip()
    or "bot:telegram:heavy:active_queues"
)
TELEGRAM_DURABLE_QUEUE_TIMEOUT_SEC = max(2, _env_int("TELEGRAM_DURABLE_QUEUE_TIMEOUT_SEC", 8))
TELEGRAM_DURABLE_QUEUE_IDLE_SEC = max(1, _env_int("TELEGRAM_DURABLE_QUEUE_IDLE_SEC", 2))
TELEGRAM_DURABLE_QUEUE_RECOVER_ON_STARTUP = _env_bool("TELEGRAM_DURABLE_QUEUE_RECOVER_ON_STARTUP", True)
TELEGRAM_DURABLE_QUEUE_RECOVER_LIMIT = max(0, _env_int("TELEGRAM_DURABLE_QUEUE_RECOVER_LIMIT", 100))
TELEGRAM_DURABLE_EMPTY_ACTIVE_FULL_SCAN_SEC = max(10, _env_int("TELEGRAM_DURABLE_EMPTY_ACTIVE_FULL_SCAN_SEC", 30))
TELEGRAM_HEAVY_QUEUE_NAMES = ("check", "checkpost", "viplike", "viplike_refresh", "misc")
TELEGRAM_HEAVY_QUEUE_SCAN_ORDER = ("check", "checkpost", "viplike", "viplike_refresh", "misc")
TELEGRAM_HEAVY_QUEUE_COMMAND_MAP = {
    "/check": "check",
    "/checkpost": "checkpost",
    "/viplike": "viplike",
    "/viplikeoff": "viplike",
    "/lammoiviplike": "viplike_refresh",
    "/lamoi": "viplike_refresh",
    "/refreshviplike": "viplike_refresh",
}
DEBUG_LOG_VERSION = "step36_uid_first_checkpost_batch_dedupe_2026-05-15"
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
TELEGRAM_HEAVY_QUEUE = Queue(maxsize=TELEGRAM_HEAVY_QUEUE_MAX_SIZE)
TELEGRAM_HEAVY_QUEUE_WAKE = threading.Event()
TELEGRAM_HEAVY_QUEUE_STARTED = False
TELEGRAM_HEAVY_QUEUE_START_LOCK = threading.Lock()
TELEGRAM_HEAVY_QUEUE_METRICS_LOCK = threading.Lock()
TELEGRAM_DURABLE_QUEUE_RECOVERED = False
TELEGRAM_DURABLE_QUEUE_RECOVER_LOCK = threading.Lock()
TELEGRAM_DURABLE_LAST_EMPTY_ACTIVE_FULL_SCAN_AT = 0.0
TELEGRAM_HEAVY_QUEUE_METRICS = {
    "enqueued": 0,
    "processed": 0,
    "failed": 0,
    "fallback_submitted": 0,
    "memory_enqueued": 0,
    "durable_enqueued": 0,
    "durable_claimed": 0,
    "durable_completed": 0,
    "durable_recovered": 0,
    "durable_errors": 0,
    "active": 0,
    "last_enqueued_at": "",
    "last_processed_at": "",
    "last_error": "",
}
TELEGRAM_DEDUP_CACHE: Dict[str, float] = {}
TELEGRAM_DEDUP_LOCK = threading.Lock()
CHECKER_CACHE: Dict[str, Dict] = {}
CHECKER_CACHE_LOCK = threading.Lock()
CHECKER_CACHE_METRICS = {
    "hits": 0,
    "misses": 0,
    "writes": 0,
    "evictions": 0,
}

if UID_CHECKER_SERVICE is not None and UID_CHECKER_API_KEY:
    UID_CHECKER_SERVICE.API_KEY = UID_CHECKER_API_KEY


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


def _post_json(
    url: str,
    payload: Dict,
    source: str,
    extra_params: Optional[Dict[str, str]] = None,
    timeout_sec: Optional[int] = None,
) -> requests.Response:
    target = _with_source(url, source)
    target = _append_query_params(target, extra_params or {})
    return requests.post(
        target,
        json=payload,
        headers=_forward_headers(),
        timeout=timeout_sec or REQUEST_TIMEOUT_SEC,
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
    if key:
        return TELEGRAM_BOT_TOKENS.get(key, "")
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


def _telegram_text_from_payload(payload: Dict) -> str:
    message = payload.get("message") if isinstance(payload, dict) else None
    if not isinstance(message, dict):
        return ""
    return str(message.get("text", "") or "").strip()


def _telegram_command_from_payload(payload: Dict) -> str:
    text = _telegram_text_from_payload(payload)
    if not text.startswith("/"):
        return ""
    first = text.split(None, 1)[0].split("@", 1)[0]
    return first.strip().lower()


def _telegram_heavy_queue_name_from_payload(payload: Dict) -> str:
    command = _telegram_command_from_payload(payload)
    if command:
        return TELEGRAM_HEAVY_QUEUE_COMMAND_MAP.get(command, "misc")
    return "check" if TELEGRAM_HEAVY_QUEUE_NON_COMMANDS else "misc"


def _normalize_telegram_heavy_queue_name(queue_name: str) -> str:
    name = str(queue_name or "").strip().lower()
    return name if name in TELEGRAM_HEAVY_QUEUE_NAMES else "misc"


def _is_heavy_telegram_update(payload: Dict) -> bool:
    if not TELEGRAM_HEAVY_QUEUE_ENABLED:
        return False
    if isinstance(payload, dict) and isinstance(payload.get("callback_query"), dict):
        return False
    text = _telegram_text_from_payload(payload)
    if not text:
        return False
    command = _telegram_command_from_payload(payload)
    if command:
        return command in TELEGRAM_HEAVY_COMMANDS
    return TELEGRAM_HEAVY_QUEUE_NON_COMMANDS


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
        resp = _post_json(target_url, payload, "telegram", params, TELEGRAM_FORWARD_TIMEOUT_SEC)
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


def _telegram_queue_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _durable_queue_configured() -> bool:
    return bool(
        TELEGRAM_DURABLE_QUEUE_ENABLED
        and UPSTASH_REDIS_REST_URL
        and UPSTASH_REDIS_REST_TOKEN
    )


def _upstash_redis_command(*parts):
    if not _durable_queue_configured():
        raise RuntimeError("durable_queue_not_configured")
    response = requests.post(
        UPSTASH_REDIS_REST_URL,
        headers={"Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}"},
        json=list(parts),
        timeout=TELEGRAM_DURABLE_QUEUE_TIMEOUT_SEC,
    )
    body_text = response.text or ""
    try:
        data = response.json() if body_text else {}
    except Exception as exc:
        raise RuntimeError(f"upstash_invalid_json:http_{response.status_code}:{exc}") from exc
    if not (200 <= int(response.status_code) < 300):
        raise RuntimeError(f"upstash_http_{response.status_code}:{body_text[:300]}")
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(f"upstash_error:{data.get('error')}")
    return data.get("result") if isinstance(data, dict) else data


def _telegram_durable_queue_key(queue_name: str) -> str:
    return f"{TELEGRAM_DURABLE_QUEUE_KEY}:{_normalize_telegram_heavy_queue_name(queue_name)}"


def _telegram_durable_processing_key(queue_name: str) -> str:
    return f"{TELEGRAM_DURABLE_PROCESSING_KEY}:{_normalize_telegram_heavy_queue_name(queue_name)}"


def _telegram_durable_queue_sizes() -> Dict[str, int]:
    if not _durable_queue_configured():
        return {}
    sizes: Dict[str, int] = {}
    for queue_name in TELEGRAM_HEAVY_QUEUE_NAMES:
        try:
            sizes[queue_name] = int(_upstash_redis_command("LLEN", _telegram_durable_queue_key(queue_name)) or 0)
        except Exception:
            sizes[queue_name] = -1
    return sizes


def _telegram_active_durable_queue_names() -> List[str]:
    global TELEGRAM_DURABLE_LAST_EMPTY_ACTIVE_FULL_SCAN_AT
    if not _durable_queue_configured():
        return []
    try:
        raw_names = _upstash_redis_command("SMEMBERS", TELEGRAM_DURABLE_ACTIVE_QUEUES_KEY) or []
    except Exception as exc:
        _durable_queue_error(
            "Durable queue active-set read failed; falling back to full scan",
            "telegram_durable_active_set_failed",
            error=str(exc),
        )
        return list(TELEGRAM_HEAVY_QUEUE_SCAN_ORDER)

    names_set = {
        _normalize_telegram_heavy_queue_name(str(item))
        for item in raw_names
        if str(item or "").strip()
    }
    if not names_set:
        now = time.time()
        if now - TELEGRAM_DURABLE_LAST_EMPTY_ACTIVE_FULL_SCAN_AT >= TELEGRAM_DURABLE_EMPTY_ACTIVE_FULL_SCAN_SEC:
            TELEGRAM_DURABLE_LAST_EMPTY_ACTIVE_FULL_SCAN_AT = now
            return list(TELEGRAM_HEAVY_QUEUE_SCAN_ORDER)
        return []
    return [name for name in TELEGRAM_HEAVY_QUEUE_SCAN_ORDER if name in names_set]


def _build_telegram_heavy_job(payload: Dict, forward_params: Dict[str, str], dedup_key: str) -> Dict:
    command = _telegram_command_from_payload(payload) or "__text__"
    queue_name = _telegram_heavy_queue_name_from_payload(payload)
    return {
        "payload": payload,
        "forward_params": dict(forward_params or {}),
        "dedup_key": dedup_key,
        "command": command,
        "queue_name": queue_name,
        "enqueued_at": _telegram_queue_now_iso(),
    }


def _serialize_telegram_heavy_job(job: Dict) -> str:
    return json.dumps(job, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _durable_queue_error(message: str, code: str, **fields) -> None:
    _telegram_queue_metric({"durable_errors": 1}, last_error=code)
    _log_event("warning", "telegram_queue", message, code=code, **fields)


def _telegram_queue_metric(delta: Optional[Dict] = None, **set_fields) -> Dict:
    with TELEGRAM_HEAVY_QUEUE_METRICS_LOCK:
        if delta:
            for key, value in delta.items():
                current = TELEGRAM_HEAVY_QUEUE_METRICS.get(key, 0)
                try:
                    TELEGRAM_HEAVY_QUEUE_METRICS[key] = int(current) + int(value)
                except Exception:
                    TELEGRAM_HEAVY_QUEUE_METRICS[key] = value
        for key, value in set_fields.items():
            TELEGRAM_HEAVY_QUEUE_METRICS[key] = value
        out = dict(TELEGRAM_HEAVY_QUEUE_METRICS)
    out["queue_size"] = TELEGRAM_HEAVY_QUEUE.qsize()
    out["queue_max_size"] = TELEGRAM_HEAVY_QUEUE_MAX_SIZE
    out["workers"] = TELEGRAM_HEAVY_QUEUE_WORKERS
    out["enabled"] = TELEGRAM_HEAVY_QUEUE_ENABLED
    out["started"] = TELEGRAM_HEAVY_QUEUE_STARTED
    out["mode"] = "redis" if _durable_queue_configured() else "memory"
    out["durable_enabled"] = TELEGRAM_DURABLE_QUEUE_ENABLED
    out["durable_configured"] = _durable_queue_configured()
    out["durable_queue_key"] = TELEGRAM_DURABLE_QUEUE_KEY
    out["durable_processing_key"] = TELEGRAM_DURABLE_PROCESSING_KEY
    out["durable_active_queues_key"] = TELEGRAM_DURABLE_ACTIVE_QUEUES_KEY
    out["durable_empty_active_full_scan_sec"] = TELEGRAM_DURABLE_EMPTY_ACTIVE_FULL_SCAN_SEC
    out["durable_queue_names"] = list(TELEGRAM_HEAVY_QUEUE_NAMES)
    out["durable_queue_sizes"] = _telegram_durable_queue_sizes() if not delta and not set_fields else {}
    out["durable_queue_total_size"] = sum(
        value for value in out["durable_queue_sizes"].values() if isinstance(value, int) and value > 0
    )
    out["durable_recover_on_startup"] = TELEGRAM_DURABLE_QUEUE_RECOVER_ON_STARTUP
    return out


def _enqueue_telegram_heavy_memory_job(job: Dict) -> bool:
    try:
        TELEGRAM_HEAVY_QUEUE.put_nowait(job)
        TELEGRAM_HEAVY_QUEUE_WAKE.set()
        _telegram_queue_metric(
            {"enqueued": 1, "memory_enqueued": 1},
            last_enqueued_at=job.get("enqueued_at", _telegram_queue_now_iso()),
        )
        return True
    except Full:
        _telegram_queue_metric({"fallback_submitted": 1}, last_error="telegram_heavy_queue_full")
        _log_event(
            "warning",
            "telegram_queue",
            "Heavy Telegram memory queue full; falling back to executor",
            code="telegram_heavy_queue_full_fallback",
            command=job.get("command", ""),
            dedup_key=job.get("dedup_key", ""),
            queue_size=TELEGRAM_HEAVY_QUEUE.qsize(),
            queue_max_size=TELEGRAM_HEAVY_QUEUE_MAX_SIZE,
        )
        TELEGRAM_EXECUTOR.submit(
            _forward_telegram_background,
            job.get("payload", {}),
            job.get("forward_params", {}),
        )
        return False


def _enqueue_telegram_heavy_durable_job(job: Dict) -> bool:
    queue_name = _normalize_telegram_heavy_queue_name(str(job.get("queue_name", "")))
    try:
        job["queue_name"] = queue_name
        _upstash_redis_command("LPUSH", _telegram_durable_queue_key(queue_name), _serialize_telegram_heavy_job(job))
        _upstash_redis_command("SADD", TELEGRAM_DURABLE_ACTIVE_QUEUES_KEY, queue_name)
        TELEGRAM_HEAVY_QUEUE_WAKE.set()
        _telegram_queue_metric(
            {"enqueued": 1, "durable_enqueued": 1},
            last_enqueued_at=job.get("enqueued_at", _telegram_queue_now_iso()),
            last_error="",
        )
        return True
    except Exception as exc:
        _durable_queue_error(
            "Durable queue enqueue failed; falling back to memory queue",
            "telegram_durable_enqueue_failed",
            queue=queue_name,
            command=job.get("command", ""),
            dedup_key=job.get("dedup_key", ""),
            error=str(exc),
        )
        return False


def _claim_telegram_heavy_durable_job(queue_name: str) -> Optional[Dict]:
    queue_name = _normalize_telegram_heavy_queue_name(queue_name)
    try:
        raw_job = _upstash_redis_command(
            "RPOPLPUSH",
            _telegram_durable_queue_key(queue_name),
            _telegram_durable_processing_key(queue_name),
        )
    except Exception as exc:
        _durable_queue_error(
            "Durable queue claim failed",
            "telegram_durable_claim_failed",
            queue=queue_name,
            error=str(exc),
        )
        return None

    if not raw_job:
        try:
            _upstash_redis_command("SREM", TELEGRAM_DURABLE_ACTIVE_QUEUES_KEY, queue_name)
        except Exception:
            pass
        return None

    try:
        job = json.loads(str(raw_job))
        if not isinstance(job, dict):
            raise ValueError("durable_job_not_object")
        job["_durable_raw"] = str(raw_job)
        job["_durable_queue_name"] = queue_name
        job["queue_name"] = _normalize_telegram_heavy_queue_name(str(job.get("queue_name", queue_name)))
        _telegram_queue_metric({"durable_claimed": 1})
        return job
    except Exception as exc:
        _durable_queue_error(
            "Durable queue job JSON is invalid; removing from processing queue",
            "telegram_durable_job_invalid",
            error=str(exc),
            raw=str(raw_job)[:300],
        )
        try:
            _upstash_redis_command("LREM", _telegram_durable_processing_key(queue_name), 1, str(raw_job))
        except Exception as remove_exc:
            _durable_queue_error(
                "Failed to remove invalid durable queue job",
                "telegram_durable_invalid_remove_failed",
                queue=queue_name,
                error=str(remove_exc),
            )
        return None


def _ack_telegram_heavy_durable_job(job: Dict) -> None:
    raw_job = str(job.get("_durable_raw", "") or "")
    if not raw_job:
        return
    queue_name = _normalize_telegram_heavy_queue_name(str(job.get("_durable_queue_name") or job.get("queue_name", "")))
    try:
        _upstash_redis_command("LREM", _telegram_durable_processing_key(queue_name), 1, raw_job)
        _telegram_queue_metric({"durable_completed": 1})
    except Exception as exc:
        _durable_queue_error(
            "Durable queue ack failed",
            "telegram_durable_ack_failed",
            queue=queue_name,
            command=job.get("command", ""),
            dedup_key=job.get("dedup_key", ""),
            error=str(exc),
        )


def _recover_telegram_heavy_durable_processing_once() -> None:
    global TELEGRAM_DURABLE_QUEUE_RECOVERED
    if (
        TELEGRAM_DURABLE_QUEUE_RECOVERED
        or not TELEGRAM_DURABLE_QUEUE_RECOVER_ON_STARTUP
        or not _durable_queue_configured()
        or TELEGRAM_DURABLE_QUEUE_RECOVER_LIMIT <= 0
    ):
        return
    with TELEGRAM_DURABLE_QUEUE_RECOVER_LOCK:
        if TELEGRAM_DURABLE_QUEUE_RECOVERED:
            return
        recovered = 0
        per_queue_limit = max(1, TELEGRAM_DURABLE_QUEUE_RECOVER_LIMIT)
        for queue_name in TELEGRAM_HEAVY_QUEUE_NAMES:
            for _ in range(per_queue_limit):
                try:
                    moved = _upstash_redis_command(
                        "RPOPLPUSH",
                        _telegram_durable_processing_key(queue_name),
                        _telegram_durable_queue_key(queue_name),
                    )
                except Exception as exc:
                    _durable_queue_error(
                        "Durable queue recovery failed",
                        "telegram_durable_recovery_failed",
                        queue=queue_name,
                        error=str(exc),
                    )
                    break
                if not moved:
                    break
                try:
                    _upstash_redis_command("SADD", TELEGRAM_DURABLE_ACTIVE_QUEUES_KEY, queue_name)
                except Exception:
                    pass
                recovered += 1
        TELEGRAM_DURABLE_QUEUE_RECOVERED = True
        if recovered:
            _telegram_queue_metric({"durable_recovered": recovered})
            _log_event(
                "warning",
                "telegram_queue",
                "Recovered durable queue jobs from processing back to queue",
                code="telegram_durable_jobs_recovered",
                recovered=recovered,
            )


def _next_telegram_heavy_queue_job() -> Tuple[str, Dict]:
    while True:
        try:
            return "memory", TELEGRAM_HEAVY_QUEUE.get_nowait()
        except Empty:
            pass

        if _durable_queue_configured():
            for queue_name in _telegram_active_durable_queue_names():
                durable_job = _claim_telegram_heavy_durable_job(queue_name)
                if durable_job:
                    return f"durable:{queue_name}", durable_job
            TELEGRAM_HEAVY_QUEUE_WAKE.wait(TELEGRAM_DURABLE_QUEUE_IDLE_SEC)
            TELEGRAM_HEAVY_QUEUE_WAKE.clear()
            continue

        return "memory", TELEGRAM_HEAVY_QUEUE.get()


def _telegram_heavy_queue_worker(worker_id: int) -> None:
    while True:
        source, job = _next_telegram_heavy_queue_job()
        _telegram_queue_metric({"active": 1})
        try:
            ok, attempts = _forward_telegram(job.get("payload", {}), job.get("forward_params", {}))
            if ok:
                _telegram_queue_metric({"processed": 1}, last_processed_at=_telegram_queue_now_iso(), last_error="")
            else:
                _telegram_queue_metric(
                    {"processed": 1, "failed": 1},
                    last_processed_at=_telegram_queue_now_iso(),
                    last_error="telegram_heavy_queue_forward_failed",
                )
                _log_event(
                    "error",
                    "telegram_queue",
                    "Heavy Telegram queued forward failed",
                    code="telegram_heavy_queue_forward_failed",
                    worker_id=worker_id,
                    queue_source=source,
                    command=job.get("command", ""),
                    dedup_key=job.get("dedup_key", ""),
                    attempts=attempts,
                )
        except Exception as exc:
            _telegram_queue_metric(
                {"processed": 1, "failed": 1},
                last_processed_at=_telegram_queue_now_iso(),
                last_error=str(exc),
            )
            _log_event(
                "error",
                "telegram_queue",
                "Heavy Telegram queue worker crashed on job",
                code="telegram_heavy_queue_job_exception",
                worker_id=worker_id,
                queue_source=source,
                command=job.get("command", ""),
                dedup_key=job.get("dedup_key", ""),
                error=str(exc),
            )
            app.logger.exception("Heavy Telegram queue job exception")
        finally:
            _telegram_queue_metric({"active": -1})
            if source.startswith("durable"):
                _ack_telegram_heavy_durable_job(job)
            else:
                TELEGRAM_HEAVY_QUEUE.task_done()


def _ensure_telegram_heavy_queue_started() -> None:
    global TELEGRAM_HEAVY_QUEUE_STARTED
    if TELEGRAM_HEAVY_QUEUE_STARTED or not TELEGRAM_HEAVY_QUEUE_ENABLED:
        return
    with TELEGRAM_HEAVY_QUEUE_START_LOCK:
        if TELEGRAM_HEAVY_QUEUE_STARTED:
            return
        _recover_telegram_heavy_durable_processing_once()
        for i in range(TELEGRAM_HEAVY_QUEUE_WORKERS):
            worker = threading.Thread(
                target=_telegram_heavy_queue_worker,
                args=(i + 1,),
                name=f"telegram-heavy-queue-{i + 1}",
                daemon=True,
            )
            worker.start()
        TELEGRAM_HEAVY_QUEUE_STARTED = True
        _log_event(
            "info",
            "telegram_queue",
            "Heavy Telegram queue started",
            code="telegram_heavy_queue_started",
            workers=TELEGRAM_HEAVY_QUEUE_WORKERS,
            max_size=TELEGRAM_HEAVY_QUEUE_MAX_SIZE,
            mode="redis" if _durable_queue_configured() else "memory",
            heavy_commands=sorted(TELEGRAM_HEAVY_COMMANDS),
        )


def _enqueue_telegram_heavy_job(payload: Dict, forward_params: Dict[str, str], dedup_key: str) -> bool:
    _ensure_telegram_heavy_queue_started()
    job = _build_telegram_heavy_job(payload, forward_params, dedup_key)
    if _durable_queue_configured() and _enqueue_telegram_heavy_durable_job(job):
        return True
    return _enqueue_telegram_heavy_memory_job(job)


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


def _checker_ready() -> bool:
    return bool(UID_CHECKER_ENABLED and UID_CHECKER_SERVICE is not None)


def _checker_api_key_header() -> str:
    return str(request.headers.get("X-Api-Key", "") or request.headers.get("x-api-key", "")).strip()


def _checker_payload() -> Dict:
    payload = _payload_dict()
    return payload if isinstance(payload, dict) else {}


def _run_checker_async(coro):
    try:
        return asyncio.run(coro)
    except RuntimeError as exc:
        if "asyncio.run() cannot be called from a running event loop" not in str(exc):
            raise
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()


def _checker_unavailable_response() -> Response:
    status = 503 if UID_CHECKER_ENABLED else 404
    return jsonify({
        "ok": False,
        "error": "uid_checker_unavailable",
        "enabled": UID_CHECKER_ENABLED,
        "imported": UID_CHECKER_SERVICE is not None,
        "importError": UID_CHECKER_IMPORT_ERROR,
    }), status


def _checker_response(result) -> Response:
    body = getattr(result, "body", None)
    if body is not None:
        status_code = int(getattr(result, "status_code", 200) or 200)
        media_type = str(getattr(result, "media_type", "") or "application/json")
        return Response(body, status=status_code, content_type=media_type)
    if isinstance(result, (dict, list)):
        return jsonify(result)
    return jsonify({"ok": True, "result": result})


def _checker_exception_response(exc: Exception) -> Response:
    status_code = int(getattr(exc, "status_code", 500) or 500)
    detail = getattr(exc, "detail", str(exc))
    if status_code >= 500:
        _log_event(
            "error",
            "uid_checker",
            "Integrated UID checker failed",
            code="integrated_uid_checker_exception",
            path=request.path,
            error=str(exc),
        )
    return jsonify({
        "ok": False,
        "error": "uid_checker_error" if status_code >= 500 else "uid_checker_rejected",
        "detail": detail,
    }), status_code


def _checker_cache_metric(name: str, delta: int = 1) -> None:
    try:
        CHECKER_CACHE_METRICS[name] = int(CHECKER_CACHE_METRICS.get(name, 0)) + int(delta)
    except Exception:
        CHECKER_CACHE_METRICS[name] = delta


def _checker_cache_key(namespace: str, source) -> str:
    try:
        raw = json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except Exception:
        raw = str(source)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"{namespace}:{digest}"


def _prune_checker_cache_locked(now: float) -> None:
    expired = [key for key, item in CHECKER_CACHE.items() if float(item.get("expires_at", 0)) <= now]
    for key in expired:
        CHECKER_CACHE.pop(key, None)
    if expired:
        _checker_cache_metric("evictions", len(expired))

    overflow = len(CHECKER_CACHE) - CHECKER_CACHE_MAX_ITEMS
    if overflow <= 0:
        return

    oldest = sorted(CHECKER_CACHE.items(), key=lambda pair: float(pair[1].get("expires_at", 0)))[:overflow]
    for key, _item in oldest:
        CHECKER_CACHE.pop(key, None)
    _checker_cache_metric("evictions", len(oldest))


def _checker_cache_get(cache_key: str) -> Optional[Dict]:
    if not CHECKER_CACHE_ENABLED or not cache_key:
        return None
    now = time.time()
    with CHECKER_CACHE_LOCK:
        _prune_checker_cache_locked(now)
        item = CHECKER_CACHE.get(cache_key)
        if not item or float(item.get("expires_at", 0)) <= now:
            CHECKER_CACHE.pop(cache_key, None)
            _checker_cache_metric("misses", 1)
            return None
        _checker_cache_metric("hits", 1)
        return dict(item)


def _checker_cache_set(cache_key: str, response: Response, ttl_seconds: int, namespace: str = "") -> None:
    if not CHECKER_CACHE_ENABLED or not cache_key or ttl_seconds <= 0:
        return
    status_code = int(response.status_code or 200)
    if status_code >= 400:
        return
    body = response.get_data(as_text=True)
    if len(body) > 250000:
        return
    try:
        parsed_body = json.loads(body) if body else {}
    except Exception:
        parsed_body = {}
    if isinstance(parsed_body, dict):
        reason = str(parsed_body.get("reason") or parsed_body.get("error") or parsed_body.get("detail") or "").strip().lower()
        method = str(parsed_body.get("method") or "").strip().lower()
        status = str(parsed_body.get("status") or "").strip().lower()
        post_id = str(parsed_body.get("postId") or parsed_body.get("post_id") or "").strip()
        if reason in {"invalid_uid", "latest_post_timeout"} or method == "latest_post_timeout":
            return
        if namespace.startswith("latest_post") and parsed_body.get("ok") is False and not post_id:
            return
        if namespace.startswith("check") and status == "unknown" and reason:
            return
    content_type = str(response.content_type or "application/json")
    now = time.time()
    with CHECKER_CACHE_LOCK:
        _prune_checker_cache_locked(now)
        CHECKER_CACHE[cache_key] = {
            "expires_at": now + ttl_seconds,
            "status": status_code,
            "body": body,
            "content_type": content_type,
            "cached_at": _telegram_queue_now_iso(),
        }
        _checker_cache_metric("writes", 1)


def _checker_cached_response(item: Dict) -> Response:
    response = Response(
        str(item.get("body", "") or ""),
        status=int(item.get("status", 200) or 200),
        content_type=str(item.get("content_type", "application/json") or "application/json"),
    )
    response.headers["X-Checker-Cache"] = "HIT"
    return response


def _checker_cache_status() -> Dict:
    with CHECKER_CACHE_LOCK:
        _prune_checker_cache_locked(time.time())
        out = dict(CHECKER_CACHE_METRICS)
        out["items"] = len(CHECKER_CACHE)
        out["enabled"] = CHECKER_CACHE_ENABLED
        out["maxItems"] = CHECKER_CACHE_MAX_ITEMS
        out["ttl"] = {
            "getUid": CHECKER_GET_UID_CACHE_TTL_SEC,
            "check": CHECKER_CHECK_CACHE_TTL_SEC,
            "latestPost": CHECKER_LATEST_POST_CACHE_TTL_SEC,
        }
        return out


def _call_checker(coro_factory) -> Response:
    if not _checker_ready():
        return _checker_unavailable_response()
    try:
        return _checker_response(_run_checker_async(coro_factory()))
    except Exception as exc:
        return _checker_exception_response(exc)


def _call_checker_cached(namespace: str, key_source, ttl_seconds: int, coro_factory) -> Response:
    cache_key = _checker_cache_key(namespace, key_source) if ttl_seconds > 0 else ""
    cached = _checker_cache_get(cache_key)
    if cached:
        return _checker_cached_response(cached)
    response = _call_checker(coro_factory)
    _checker_cache_set(cache_key, response, ttl_seconds, namespace)
    if cache_key:
        response.headers["X-Checker-Cache"] = "MISS"
    return response


def _checker_response_json(response: Response):
    raw = response.get_data(as_text=True) or ""
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {
            "ok": False,
            "error": "checker_non_json_response",
            "body": raw[:500],
        }


def _checker_auth_error_response() -> Optional[Response]:
    if UID_CHECKER_API_KEY and _checker_api_key_header() != UID_CHECKER_API_KEY:
        return jsonify({"ok": False, "error": "invalid_api_key"}), 401
    return None


def _normalize_checker_batch_payload(item_raw) -> Tuple[Dict, str, str]:
    item = item_raw if isinstance(item_raw, dict) else {"input": item_raw}
    payload = {}
    for key in ("uid", "url", "proxy", "cookies", "cookiesPool", "cookies_pool"):
        if key in item and item.get(key) not in (None, ""):
            payload[key] = item.get(key)

    raw_input = str(
        item.get("input")
        or item.get("target")
        or item.get("value")
        or item.get("raw")
        or payload.get("uid")
        or payload.get("url")
        or ""
    ).strip()
    if raw_input and not payload.get("uid") and not payload.get("url"):
        if raw_input.isdigit():
            payload["uid"] = raw_input
        else:
            payload["url"] = raw_input

    item_id = str(item.get("id") or item.get("watchId") or item.get("key") or raw_input or "").strip()
    return payload, item_id, raw_input


def _resolve_checker_batch_payload_uid(payload_raw: Dict) -> Dict:
    payload = dict(payload_raw or {})
    if str(payload.get("uid") or "").strip():
        return payload

    raw_url = str(payload.get("url") or "").strip()
    if not raw_url:
        return payload
    lowered = raw_url.lower()
    if "facebook.com/" not in lowered and "fb.com/" not in lowered:
        return payload
    if not _checker_ready() or not hasattr(UID_CHECKER_SERVICE, "resolve_uid_from_facebook_url"):
        return payload

    try:
        uid = str(_run_checker_async(
            UID_CHECKER_SERVICE.resolve_uid_from_facebook_url(
                raw_url,
                payload.get("proxy") or None,
            )
        ) or "").strip()
    except Exception:
        uid = ""

    if uid:
        payload["uid"] = uid
    return payload


def _checker_batch_items() -> Tuple[List, Optional[Response]]:
    body = _checker_payload()
    items = body.get("items") if isinstance(body, dict) else []
    if not isinstance(items, list):
        return [], (jsonify({"ok": False, "error": "invalid_items"}), 400)
    if len(items) > CHECKER_BATCH_MAX_ITEMS:
        return [], (
            jsonify({
                "ok": False,
                "error": "too_many_items",
                "maxItems": CHECKER_BATCH_MAX_ITEMS,
                "count": len(items),
            }),
            413,
        )
    return items, None


def _run_checker_cached_json(namespace: str, payload: Dict, ttl_seconds: int, coro_factory) -> Dict:
    response = _call_checker_cached(namespace, payload, ttl_seconds, coro_factory)
    parsed = _checker_response_json(response)
    if isinstance(parsed, dict):
        out = dict(parsed)
    else:
        out = {"ok": True, "value": parsed}
    out["_httpStatus"] = int(response.status_code or 200)
    out["_cache"] = str(response.headers.get("X-Checker-Cache", ""))
    return out


def _checker_batch_response(kind: str, ttl_seconds: int, runner) -> Response:
    if not _checker_ready():
        return _checker_unavailable_response()
    auth_error = _checker_auth_error_response()
    if auth_error:
        return auth_error

    items, error_response = _checker_batch_items()
    if error_response:
        return error_response

    started_at = time.time()
    results = []
    ok_count = 0
    fail_count = 0
    cache_hits = 0
    for index, item_raw in enumerate(items):
        payload, item_id, raw_input = _normalize_checker_batch_payload(item_raw)
        payload = _resolve_checker_batch_payload_uid(payload)
        try:
            result = _run_checker_cached_json(
                kind,
                payload,
                ttl_seconds,
                lambda payload=payload: runner(payload),
            )
            if result.get("_cache") == "HIT":
                cache_hits += 1
            if int(result.get("_httpStatus") or 200) < 400 and result.get("ok") is not False:
                ok_count += 1
            else:
                fail_count += 1
        except Exception as exc:
            fail_count += 1
            result = {
                "ok": False,
                "error": "checker_batch_item_failed",
                "detail": str(exc),
                "_httpStatus": int(getattr(exc, "status_code", 500) or 500),
                "_cache": "",
            }
        result["_index"] = index
        result["_itemId"] = item_id
        result["_input"] = raw_input
        results.append(result)

    return jsonify({
        "ok": fail_count == 0,
        "kind": kind,
        "count": len(results),
        "okCount": ok_count,
        "failCount": fail_count,
        "cacheHits": cache_hits,
        "elapsedMs": int((time.time() - started_at) * 1000),
        "results": results,
    })


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
            "integrated_uid_checker": {
                "enabled": UID_CHECKER_ENABLED,
                "ready": _checker_ready(),
                "importError": UID_CHECKER_IMPORT_ERROR,
                "apiKeyRequired": bool(UID_CHECKER_API_KEY),
                "batchMaxItems": CHECKER_BATCH_MAX_ITEMS,
                "cache": _checker_cache_status(),
            },
            "telegram_heavy_queue": _telegram_queue_metric(),
            "telegram_heavy_commands": sorted(TELEGRAM_HEAVY_COMMANDS),
            "telegram_heavy_queue_non_commands": TELEGRAM_HEAVY_QUEUE_NON_COMMANDS,
            "lead_backends": len(_lead_backends()),
            "sepay_backend": bool(PRIMARY_SCRIPT_URL),
        }
    )


@app.get("/health")
def health() -> Response:
    checker_health = {}
    if _checker_ready():
        try:
            checker_health = _run_checker_async(UID_CHECKER_SERVICE.health())
        except Exception as exc:
            checker_health = {"ok": False, "error": str(exc)}
    return jsonify({
        "ok": True,
        "service": "apps-script-webhook-load-balancer",
        "debug_log_version": DEBUG_LOG_VERSION,
        "integratedUidChecker": {
            "enabled": UID_CHECKER_ENABLED,
            "ready": _checker_ready(),
            "health": checker_health,
        },
    })


@app.get("/checker/health")
def checker_health() -> Response:
    return _call_checker(lambda: UID_CHECKER_SERVICE.health())


@app.get("/get-uid")
def checker_get_uid() -> Response:
    url = request.args.get("url", "")
    proxy = request.args.get("proxy", "")
    return _call_checker_cached(
        "get_uid_get",
        {"url": url, "proxy": proxy},
        CHECKER_GET_UID_CACHE_TTL_SEC,
        lambda: UID_CHECKER_SERVICE.get_uid(
            url=url,
            proxy=proxy,
            x_api_key=_checker_api_key_header() or None,
        ),
    )


@app.post("/get-uid")
def checker_get_uid_post() -> Response:
    payload = _checker_payload()
    def run():
        req = UID_CHECKER_SERVICE.CheckRequest(**payload)
        return UID_CHECKER_SERVICE.get_uid_post(req, x_api_key=_checker_api_key_header() or None)
    return _call_checker_cached("get_uid_post", payload, CHECKER_GET_UID_CACHE_TTL_SEC, run)


@app.get("/cookie-health")
@app.get("/cookie-health/")
def checker_cookie_health() -> Response:
    return _call_checker(lambda: UID_CHECKER_SERVICE.cookie_health(
        proxy=request.args.get("proxy", ""),
        x_api_key=_checker_api_key_header() or None,
    ))


@app.post("/cookie-health")
@app.post("/cookie-health/")
def checker_cookie_health_post() -> Response:
    def run():
        req = UID_CHECKER_SERVICE.CheckRequest(**_checker_payload())
        return UID_CHECKER_SERVICE.cookie_health_post(req, x_api_key=_checker_api_key_header() or None)
    return _call_checker(run)


@app.post("/check")
@app.post("/check/")
def checker_check() -> Response:
    payload = _checker_payload()
    def run():
        req = UID_CHECKER_SERVICE.CheckRequest(**payload)
        return UID_CHECKER_SERVICE.check(req, x_api_key=_checker_api_key_header() or None)
    return _call_checker_cached("check", payload, CHECKER_CHECK_CACHE_TTL_SEC, run)


@app.post("/batch-check")
@app.post("/check/batch")
def checker_batch_check() -> Response:
    return _checker_batch_response(
        "check",
        CHECKER_CHECK_CACHE_TTL_SEC,
        lambda payload: UID_CHECKER_SERVICE.check(
            UID_CHECKER_SERVICE.CheckRequest(**payload),
            x_api_key=UID_CHECKER_API_KEY or None,
        ),
    )


@app.post("/latest-post")
@app.post("/latest-post/")
@app.post("/checkpost")
@app.post("/checkpost/")
def checker_latest_post() -> Response:
    payload = _checker_payload()
    def run():
        req = UID_CHECKER_SERVICE.CheckRequest(**payload)
        return UID_CHECKER_SERVICE.latest_post(req, x_api_key=_checker_api_key_header() or None)
    return _call_checker_cached("latest_post", payload, CHECKER_LATEST_POST_CACHE_TTL_SEC, run)


@app.post("/batch-latest-post")
@app.post("/latest-post/batch")
@app.post("/checkpost/batch")
def checker_batch_latest_post() -> Response:
    return _checker_batch_response(
        "latest_post",
        CHECKER_LATEST_POST_CACHE_TTL_SEC,
        lambda payload: UID_CHECKER_SERVICE.latest_post(
            UID_CHECKER_SERVICE.CheckRequest(**payload),
            x_api_key=UID_CHECKER_API_KEY or None,
        ),
    )


@app.post("/live-check")
@app.post("/live-check/")
@app.post("/livecheck")
@app.post("/check-live")
def checker_live_check() -> Response:
    def run():
        req = UID_CHECKER_SERVICE.LiveCheckRequest(**_checker_payload())
        return UID_CHECKER_SERVICE.live_check(req, x_api_key=_checker_api_key_header() or None)
    return _call_checker(run)


@app.after_request
def add_cors_headers(response: Response) -> Response:
    allow_origin = _resolve_cors_allow_origin()
    if allow_origin:
        response.headers["Access-Control-Allow-Origin"] = allow_origin
        if allow_origin != "*":
            response.headers["Vary"] = "Origin"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
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

    use_heavy_queue = TELEGRAM_ASYNC_ENABLED and _is_heavy_telegram_update(payload)
    if TELEGRAM_ASYNC_ENABLED:
        # Ack Telegram immediately to avoid webhook timeouts on cold start or slow Apps Script runs.
        queued = False
        queue_name = ""
        if use_heavy_queue:
            queue_name = _telegram_heavy_queue_name_from_payload(payload)
            queued = _enqueue_telegram_heavy_job(payload, forward_params, dedup_key)
        else:
            TELEGRAM_EXECUTOR.submit(_forward_telegram_background, payload, forward_params)
        return jsonify({
            "ok": True,
            "accepted": True,
            "async": True,
            "source": "telegram",
            "dedup_key": dedup_key,
            "loading": bool(loading.get("ok")),
            "queued": bool(queued),
            "queueName": queue_name,
            "queue": (
                ("heavy_redis" if _durable_queue_configured() else "heavy_memory")
                if use_heavy_queue
                else "executor"
            ),
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
