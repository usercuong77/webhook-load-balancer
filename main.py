import hashlib
import json
import os
from typing import Dict, List, Optional, Tuple

import requests
from flask import Flask, Response, jsonify, request

app = Flask(__name__)


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


PRIMARY_SCRIPT_URL = os.getenv("PRIMARY_SCRIPT_URL", "").strip()
TELEGRAM_SCRIPT_URLS = _parse_urls(os.getenv("TELEGRAM_SCRIPT_URLS", ""))
SCRIPT_BACKEND_URLS = _parse_urls(os.getenv("SCRIPT_BACKEND_URLS", ""))
LEAD_SCRIPT_URLS = _parse_urls(os.getenv("LEAD_SCRIPT_URLS", ""))
SEPAY_SCRIPT_URLS = _parse_urls(os.getenv("SEPAY_SCRIPT_URLS", ""))
SEPAY_FAILOVER_ENABLED = _env_bool("SEPAY_FAILOVER_ENABLED", False)
REQUEST_TIMEOUT_SEC = max(5, _env_int("REQUEST_TIMEOUT_SEC", 25))
WEBHOOK_SHARED_SECRET = os.getenv("WEBHOOK_SHARED_SECRET", "").strip()
CORS_ALLOWED_ORIGINS = _parse_urls(os.getenv("CORS_ALLOWED_ORIGINS", "*")) or ["*"]
CORS_ALLOW_HEADERS = (
    os.getenv(
        "CORS_ALLOW_HEADERS",
        "Content-Type, X-Webhook-Secret, Authorization, X-Telegram-Bot-Api-Secret-Token",
    ).strip()
    or "Content-Type"
)
CORS_MAX_AGE_SEC = max(60, _env_int("CORS_MAX_AGE_SEC", 600))


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


def _ordered_telegram_urls(payload: Dict) -> List[str]:
    urls = _telegram_backends()
    if not urls:
        return []
    update_id = str(payload.get("update_id", "")).strip()
    if not update_id:
        return urls

    hash_val = int(hashlib.sha256(update_id.encode("utf-8")).hexdigest(), 16)
    start = hash_val % len(urls)
    return urls[start:] + urls[:start]


def _ordered_urls(urls: List[str], payload: Dict, source: str) -> List[str]:
    if len(urls) <= 1:
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


def _telegram_forward_params() -> Dict[str, str]:
    # Preserve the bot/profile hint so one Render endpoint can serve main, buff, and uid bots.
    for key in ("bot", "tg_bot", "profile"):
        value = request.args.get(key, "").strip()
        if value:
            return {"bot": value}
    return {}


def _forward_telegram(payload: Dict) -> Tuple[bool, List[Dict]]:
    forward_params = _telegram_forward_params()
    attempts = []
    for url in _ordered_telegram_urls(payload):
        try:
            resp = _post_json(url, payload, "telegram", forward_params)
            attempts.append({"url": _append_query_params(url, forward_params), "status": resp.status_code})
            if 200 <= resp.status_code < 300:
                return True, attempts
        except Exception as exc:
            attempts.append({"url": _append_query_params(url, forward_params), "error": str(exc)})
    return False, attempts


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


def _response_is_retryable_failure(resp: requests.Response, body_text: str) -> bool:
    status = int(resp.status_code)
    if status in (408, 425, 429, 500, 502, 503, 504):
        return True
    if status < 200 or status >= 300:
        return False
    return _response_body_contains_quota_error(body_text)


def _forward_with_failover(
    source: str,
    payload: Dict,
    urls: List[str],
    extra_params: Optional[Dict[str, str]] = None,
) -> Tuple[bool, List[Dict]]:
    if not urls:
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
            if not _response_is_retryable_failure(resp, body_text):
                return 200 <= int(resp.status_code) < 300, attempts
        except Exception as exc:
            attempts.append({"url": _append_query_params(url, extra_params or {}), "error": str(exc)})
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
            "telegram_backends": len(_telegram_backends()),
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
    ok, attempts = _forward_telegram(payload)
    # Telegram route always returns 200 to avoid aggressive retry storms.
    return jsonify({"ok": ok, "source": "telegram", "attempts": attempts}), 200


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
