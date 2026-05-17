import time

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


@app.post("/realtime/check-bulk")
@app.post("/realtime/check-bulk/")
def realtime_check_bulk():
    if not check_service.is_api_key_valid(_provided_api_key()):
        return _unauthorized()

    started = time.perf_counter()
    payload = request.get_json(silent=True) or {}
    raw_jobs = payload.get("jobs") if isinstance(payload, dict) else []
    jobs = raw_jobs if isinstance(raw_jobs, list) else []
    results = []

    for index, job in enumerate(jobs):
        item = job if isinstance(job, dict) else {}
        job_id = str(item.get("id") or f"job_{index + 1}").strip()
        job_type = str(item.get("type") or "uid").strip().lower()
        if job_type != "uid":
            results.append({
                "id": job_id,
                "type": job_type,
                "ok": False,
                "status": "UNKNOWN",
                "uid": "",
                "reason": "unsupported_job_type",
            })
            continue

        raw_input = str(item.get("input") or item.get("uid") or "").strip()
        if not raw_input:
            results.append({
                "id": job_id,
                "type": "uid",
                "ok": False,
                "status": "UNKNOWN",
                "uid": "",
                "reason": "empty_input",
            })
            continue

        try:
            result = check_service.check_from_payload({
                "input": raw_input,
                "mode": item.get("mode") or item.get("probeMode") or "all",
            })
            result["id"] = job_id
            result["type"] = "uid"
            if "httpCode" not in result and "httpStatus" in result:
                result["httpCode"] = result.get("httpStatus")
            if "name" not in result and "profileName" in result:
                result["name"] = result.get("profileName")
            results.append(result)
        except Exception as exc:
            results.append({
                "id": job_id,
                "type": "uid",
                "ok": False,
                "status": "UNKNOWN",
                "uid": "",
                "reason": f"job_error:{type(exc).__name__}",
                "httpCode": 0,
                "elapsedMs": 0,
            })

    return jsonify({
        "ok": True,
        "results": results,
        "jobCount": len(jobs),
        "elapsedMs": int((time.perf_counter() - started) * 1000),
    })


@app.post("/webhook/telegram")
def webhook_telegram():
    body = request.get_data() or b"{}"
    content_type = request.headers.get("Content-Type", "application/json")
    result = check_service.relay_telegram_webhook(body, content_type)
    status_code = int(result.pop("statusCode", 200))
    return jsonify(result), status_code
