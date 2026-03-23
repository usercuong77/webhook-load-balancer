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


PRIMARY_SCRIPT_URL = os.getenv("PRIMARY_SCRIPT_URL", "").strip()
TELEGRAM_SCRIPT_URLS = _parse_urls(os.getenv("TELEGRAM_SCRIPT_URLS", ""))
REQUEST_TIMEOUT_SEC = max(5, _env_int("REQUEST_TIMEOUT_SEC", 25))
WEBHOOK_SHARED_SECRET = os.getenv("WEBHOOK_SHARED_SECRET", "").strip()


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


def _telegram_backends() -> List[str]:
    if TELEGRAM_SCRIPT_URLS:
        return TELEGRAM_SCRIPT_URLS
    if PRIMARY_SCRIPT_URL:
        return [PRIMARY_SCRIPT_URL]
    return []


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
            "sepay_backend": bool(PRIMARY_SCRIPT_URL),
        }
    )


@app.post("/webhook/telegram")
def webhook_telegram() -> Response:
    payload = _payload_dict()
    ok, attempts = _forward_telegram(payload)
    # Telegram route always returns 200 to avoid aggressive retry storms.
    return jsonify({"ok": ok, "source": "telegram", "attempts": attempts}), 200


@app.post("/webhook/sepay")
def webhook_sepay() -> Response:
    payload = _payload_dict()
    ok, detail = _forward_single("sepay", payload)
    # Keep non-200 if upstream fails so SePay can retry.
    status = 200 if ok else 502
    return jsonify({"ok": ok, "source": "sepay", "detail": detail}), status


@app.post("/webhook/lead")
def webhook_lead() -> Response:
    payload = _payload_dict()
    ok, detail = _forward_single("lead", payload)
    status = 200 if ok else 502
    return jsonify({"ok": ok, "source": "lead", "detail": detail}), status


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
