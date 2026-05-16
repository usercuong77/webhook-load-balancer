from flask import Flask, jsonify, request

from app_modules.services import check_service


app = Flask(__name__)


def _provided_api_key() -> str:
    return request.headers.get("X-Api-Key") or request.args.get("apiKey") or ""


def _unauthorized():
    return jsonify({"ok": False, "error": "unauthorized"}), 401


@app.get("/")
def root():
    return jsonify(check_service.build_root_status())


@app.get("/health")
def health():
    return jsonify(check_service.health_status())


@app.post("/check")
def check_post():
    if not check_service.is_api_key_valid(_provided_api_key()):
        return _unauthorized()
    payload = request.get_json(silent=True) or {}
    return jsonify(check_service.check_from_payload(payload))


@app.get("/check")
def check_get():
    if not check_service.is_api_key_valid(_provided_api_key()):
        return _unauthorized()
    return jsonify(check_service.check_from_query(request.args))


@app.get("/get-uid")
def get_uid():
    if not check_service.is_api_key_valid(_provided_api_key()):
        return _unauthorized()
    url = request.args.get("url") or request.args.get("input") or ""
    debug_mode = (request.args.get("debug") or "").strip().lower() in ("1", "true", "on", "yes")
    payload = check_service.get_uid_payload(url, debug_mode)
    return jsonify(payload), (200 if payload.get("ok") else 404)


@app.post("/webhook/telegram")
def webhook_telegram():
    body = request.get_data() or b"{}"
    content_type = request.headers.get("Content-Type", "application/json")
    result = check_service.relay_telegram_webhook(body, content_type)
    status_code = int(result.pop("statusCode", 200))
    return jsonify(result), status_code
