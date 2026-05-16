import json
import os
import re
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional
from urllib.parse import parse_qs, quote, urlparse

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

VERSION = "step02_five_live_die_modes_2026_05_16"
REQUEST_TIMEOUT_SEC = 8
FB_PUBLIC_APP_TOKEN = os.getenv("FB_PUBLIC_APP_TOKEN", "6628568379|c1e620fa708a1d5696fb991c1bde5662")
EXTERNAL_CHECKER_URL = os.getenv("EXTERNAL_CHECKER_URL", "").strip()
EXTERNAL_CHECKER_API_KEY = os.getenv("EXTERNAL_CHECKER_API_KEY", "").strip()
UID_CHECKER_API_KEY = os.getenv("UID_CHECKER_API_KEY", "").strip()
TELEGRAM_RELAY_TARGET_URL = os.getenv(
    "TELEGRAM_RELAY_TARGET_URL",
    "https://script.google.com/macros/s/AKfycbyfgY-Dt5vmus2nbCROMIsNOWN0ddDKDnYTaYrQY2SdeUdlMrsCjOnLujB4h7OK3x8/exec",
).strip()
TELEGRAM_RELAY_TIMEOUT_SEC = float(os.getenv("TELEGRAM_RELAY_TIMEOUT_SEC", "25"))
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)

DEAD_MARKERS = (
    "this content isn't available",
    "this page isn't available",
    "content isn't available right now",
    "page not found",
    "profile unavailable",
    "the link may be broken",
    "object does not exist",
    "unsupported get request",
)

LIVE_MARKERS = (
    "profile.php?id=",
    "timeline",
    "about",
    "friends",
    "photos",
    "log in to facebook",
    "join facebook to connect",
)

DEFAULT_AVATAR_MARKERS = (
    "static.xx.fbcdn.net/rsrc.php",
    "profile/default",
    "silhouette",
    "q_silhouette",
)


@dataclass
class ProbeResult:
    name: str
    status: str
    confidence: str
    http_status: int
    reason: str
    url: str

    def to_dict(self) -> Dict:
        return {
            "name": self.name,
            "status": self.status,
            "confidence": self.confidence,
            "httpStatus": self.http_status,
            "reason": self.reason,
            "url": self.url,
        }


def _now_ms() -> int:
    return int(time.time() * 1000)


def _to_text(value) -> str:
    return "" if value is None else str(value)


def _normalize_input(raw_input: str) -> Dict:
    text = _to_text(raw_input).strip()
    if not text:
        return {"ok": False, "error": "empty_input"}

    if re.fullmatch(r"\d{8,}", text):
        uid = text
        return {
            "ok": True,
            "inputType": "uid",
            "uid": uid,
            "username": "",
            "profileUrl": f"https://www.facebook.com/profile.php?id={uid}",
        }

    url = text
    if "facebook.com/" in url.lower() or "fb.com/" in url.lower() or url.startswith("www."):
        if not re.match(r"^https?://", url, flags=re.I):
            url = "https://" + url
    else:
        return {
            "ok": True,
            "inputType": "username",
            "uid": "",
            "username": text.lstrip("@").strip("/"),
            "profileUrl": "https://www.facebook.com/" + quote(text.lstrip("@").strip("/")),
        }

    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    uid = (qs.get("id") or [""])[0]
    path_parts = [part for part in parsed.path.split("/") if part]
    username = ""
    if path_parts:
        first = path_parts[0]
        if first.lower() == "profile.php":
            username = ""
        elif re.fullmatch(r"\d{8,}", first):
            uid = first
        else:
            username = first

    profile_url = f"https://www.facebook.com/profile.php?id={uid}" if uid else url
    return {
        "ok": True,
        "inputType": "url",
        "uid": uid,
        "username": username,
        "profileUrl": profile_url,
    }


def _get_text(url: str, fetcher: Optional[Callable] = None) -> Dict:
    return _request_text("get", url, fetcher=fetcher)


def _request_text(
    method: str,
    url: str,
    fetcher: Optional[Callable] = None,
    json_payload: Optional[Dict] = None,
    data: Optional[str] = None,
    headers: Optional[Dict] = None,
) -> Dict:
    request_headers = {"User-Agent": USER_AGENT, "Accept-Language": "vi,en-US;q=0.9,en;q=0.8"}
    if headers:
        request_headers.update(headers)
    if fetcher is None:
        response = requests.request(
            method.upper(),
            url,
            timeout=REQUEST_TIMEOUT_SEC,
            allow_redirects=True,
            headers=request_headers,
            json=json_payload,
            data=data,
        )
    else:
        response = fetcher(
            url,
            timeout=REQUEST_TIMEOUT_SEC,
            allow_redirects=True,
            headers=request_headers,
            json=json_payload,
            data=data,
            method=method,
        )
    return {
        "status_code": int(getattr(response, "status_code", 0) or 0),
        "url": str(getattr(response, "url", url) or url),
        "text": _to_text(getattr(response, "text", ""))[:400000],
        "headers": dict(getattr(response, "headers", {}) or {}),
    }


def _public_profile_probe(name: str, url: str, fetcher: Optional[Callable] = None) -> ProbeResult:
    try:
        response = _get_text(url, fetcher)
    except Exception as exc:
        return ProbeResult(name, "unknown", "weak", 0, f"fetch_error:{exc}", url)

    http_status = response["status_code"]
    body = response["text"].lower()

    if any(marker in body for marker in DEAD_MARKERS):
        return ProbeResult(name, "dead", "strong", http_status, "dead_marker", response["url"])

    if http_status in (404, 410):
        return ProbeResult(name, "unknown", "weak", http_status, "http_not_found_without_dead_marker", response["url"])

    if http_status >= 500:
        return ProbeResult(name, "unknown", "weak", http_status, "server_error", response["url"])

    if http_status in (200, 301, 302):
        if any(marker in body for marker in LIVE_MARKERS):
            return ProbeResult(name, "live", "weak", http_status, "live_marker_or_auth_wall", response["url"])
        return ProbeResult(name, "live", "weak", http_status, "http_ok_no_dead_marker", response["url"])

    return ProbeResult(name, "unknown", "weak", http_status, "unclassified_http", response["url"])


def _mobile_url(profile_url: str, uid: str, username: str) -> str:
    if uid:
        return f"https://m.facebook.com/profile.php?id={uid}"
    if username:
        return "https://m.facebook.com/" + quote(username)
    parsed = urlparse(profile_url)
    path = parsed.path or "/"
    query = ("?" + parsed.query) if parsed.query else ""
    return "https://m.facebook.com" + path + query


def _graph_picture_primary_probe(uid: str, fetcher: Optional[Callable] = None) -> ProbeResult:
    if not uid:
        return ProbeResult("graph_picture_primary", "unknown", "weak", 0, "uid_missing", "")

    url = f"https://graph.facebook.com/{uid}/picture?type=normal&redirect=false"
    try:
        response = _get_text(url, fetcher)
    except Exception as exc:
        return ProbeResult("graph_picture_primary", "unknown", "weak", 0, f"fetch_error:{exc}", url)

    http_status = response["status_code"]
    body = response["text"].lower()

    if "height" in body and "width" in body:
        return ProbeResult("graph_picture_primary", "live", "strong", http_status, "graph_primary_height_width", response["url"])

    return ProbeResult("graph_picture_primary", "unknown", "weak", http_status, "graph_primary_no_live_signal", response["url"])


def _graph_picture_app_token_probe(uid: str, fetcher: Optional[Callable] = None) -> ProbeResult:
    if not uid:
        return ProbeResult("graph_picture_app_token", "unknown", "weak", 0, "uid_missing", "")

    url = f"https://graph.facebook.com/{uid}/picture?width=500&access_token={quote(FB_PUBLIC_APP_TOKEN)}&redirect=false"
    try:
        response = _get_text(url, fetcher)
    except Exception as exc:
        return ProbeResult("graph_picture_app_token", "unknown", "weak", 0, f"fetch_error:{exc}", url)

    http_status = response["status_code"]
    body = response["text"].lower()
    is_dead = ".gif" in body or "error" in body
    return ProbeResult(
        "graph_picture_app_token",
        "dead" if is_dead else "live",
        "strong",
        http_status,
        "graph_app_token_gif_or_error" if is_dead else "graph_app_token_live",
        response["url"],
    )


def _graphql_node_probe(uid: str, fetcher: Optional[Callable] = None) -> ProbeResult:
    if not uid:
        return ProbeResult("graphql_node", "unknown", "weak", 0, "uid_missing", "")

    url = "https://www.facebook.com/api/graphql"
    data = "q=" + quote(f"node({uid}){{name}}")
    try:
        response = _request_text(
            "post",
            url,
            fetcher=fetcher,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
        )
    except Exception as exc:
        return ProbeResult("graphql_node", "unknown", "weak", 0, f"fetch_error:{exc}", url)

    http_status = response["status_code"]
    try:
        payload = json.loads(response["text"] or "{}")
    except Exception:
        payload = {}
    node = payload.get(uid) if isinstance(payload, dict) else None
    is_dead = node is None or node == ""
    return ProbeResult(
        "graphql_node",
        "dead" if is_dead else "live",
        "strong",
        http_status,
        "graphql_node_empty" if is_dead else "graphql_node_found",
        response["url"],
    )


def _external_checker_probe(uid: str, profile_url: str, fetcher: Optional[Callable] = None) -> ProbeResult:
    if not EXTERNAL_CHECKER_URL:
        return ProbeResult("external_checker", "unknown", "weak", 0, "external_not_configured", "")
    if not uid:
        return ProbeResult("external_checker", "unknown", "weak", 0, "uid_missing", EXTERNAL_CHECKER_URL)

    headers = {}
    if EXTERNAL_CHECKER_API_KEY:
        headers["X-Api-Key"] = EXTERNAL_CHECKER_API_KEY
    try:
        response = _request_text(
            "post",
            EXTERNAL_CHECKER_URL,
            fetcher=fetcher,
            json_payload={"uid": uid, "url": profile_url},
            headers=headers,
        )
    except Exception as exc:
        return ProbeResult("external_checker", "unknown", "weak", 0, f"fetch_error:{exc}", EXTERNAL_CHECKER_URL)

    http_status = response["status_code"]
    try:
        payload = json.loads(response["text"] or "{}")
    except Exception:
        payload = {}
    status = _normalize_external_status(payload.get("status") or payload.get("result") or payload.get("state") or "")
    if not status:
        status = "unknown"
    reason = payload.get("reason") or payload.get("message") or payload.get("detail") or "-"
    return ProbeResult(
        "external_checker",
        status,
        "weak" if status == "unknown" else "strong",
        int(payload.get("httpCode") or payload.get("http_code") or http_status or 0),
        "external:" + _to_text(reason),
        response["url"],
    )


def _normalize_external_status(value) -> str:
    text = _to_text(value).strip().lower()
    if text in ("live", "ok", "active", "success"):
        return "live"
    if text in ("die", "dead", "disabled", "locked", "suspended", "ban", "banned"):
        return "dead"
    if text in ("unknown", "error", "timeout", "checkpoint"):
        return "unknown"
    return ""


def _html_mobile_fallback_probe(profile_url: str, uid: str, username: str, fetcher: Optional[Callable] = None) -> ProbeResult:
    public_probe = _public_profile_probe("html_public", profile_url, fetcher)
    if public_probe.status == "dead" and public_probe.confidence == "strong":
        return ProbeResult("html_mobile_fallback", public_probe.status, public_probe.confidence, public_probe.http_status, public_probe.reason, public_probe.url)
    if public_probe.status == "live" and public_probe.confidence == "strong":
        return ProbeResult("html_mobile_fallback", public_probe.status, public_probe.confidence, public_probe.http_status, public_probe.reason, public_probe.url)

    mobile_probe = _public_profile_probe("html_mobile", _mobile_url(profile_url, uid, username), fetcher)
    if mobile_probe.status in ("live", "dead"):
        return ProbeResult("html_mobile_fallback", mobile_probe.status, mobile_probe.confidence, mobile_probe.http_status, "mobile:" + mobile_probe.reason, mobile_probe.url)
    if public_probe.status in ("live", "dead"):
        return ProbeResult("html_mobile_fallback", public_probe.status, public_probe.confidence, public_probe.http_status, "public:" + public_probe.reason, public_probe.url)
    return ProbeResult("html_mobile_fallback", "unknown", "weak", public_probe.http_status or mobile_probe.http_status, "html_mobile_uncertain", profile_url)


def _choose_result(probes: List[ProbeResult]) -> Dict:
    by_name = {probe.name: probe for probe in probes}
    ordered_authoritative = (
        "graph_picture_primary",
        "graph_picture_app_token",
        "graphql_node",
        "external_checker",
        "html_mobile_fallback",
    )
    for name in ordered_authoritative:
        probe = by_name.get(name)
        if probe and probe.status in ("live", "dead") and probe.confidence == "strong":
            return {
                "status": probe.status,
                "confidence": probe.confidence,
                "source": probe.name,
                "httpStatus": probe.http_status,
                "reason": probe.reason,
            }

    live_strong = [probe for probe in probes if probe.status == "live" and probe.confidence == "strong"]
    live_any = [probe for probe in probes if probe.status == "live"]
    dead_strong = [probe for probe in probes if probe.status == "dead" and probe.confidence == "strong"]
    dead_any = [probe for probe in probes if probe.status == "dead"]

    if live_strong:
        winner = live_strong[0]
    elif live_any and not dead_strong:
        winner = live_any[0]
    elif dead_strong and not live_any:
        winner = dead_strong[0]
    elif dead_any and not live_any:
        winner = dead_any[0]
    else:
        winner = ProbeResult("combined", "unknown", "weak", 0, "no_stable_signal", "")

    return {
        "status": winner.status,
        "confidence": winner.confidence,
        "source": winner.name,
        "httpStatus": winner.http_status,
        "reason": winner.reason,
    }


def check_live_die(raw_input: str, fetcher: Optional[Callable] = None) -> Dict:
    started = _now_ms()
    normalized = _normalize_input(raw_input)
    if not normalized.get("ok"):
        return {
            "ok": False,
            "error": normalized.get("error", "invalid_input"),
            "status": "unknown",
            "confidence": "weak",
            "input": raw_input,
            "elapsedMs": _now_ms() - started,
        }

    uid = normalized.get("uid", "")
    username = normalized.get("username", "")
    profile_url = normalized.get("profileUrl", "")
    probes = [
        _graph_picture_primary_probe(uid, fetcher),
        _graph_picture_app_token_probe(uid, fetcher),
        _graphql_node_probe(uid, fetcher),
        _external_checker_probe(uid, profile_url, fetcher),
        _html_mobile_fallback_probe(profile_url, uid, username, fetcher),
    ]

    chosen = _choose_result(probes)
    return {
        "ok": True,
        "version": VERSION,
        "input": raw_input,
        "inputType": normalized.get("inputType", ""),
        "uid": uid,
        "username": username,
        "profileUrl": profile_url,
        "status": chosen["status"],
        "confidence": chosen["confidence"],
        "source": chosen["source"],
        "httpStatus": chosen["httpStatus"],
        "reason": chosen["reason"],
        "probeCount": len(probes),
        "probes": [probe.to_dict() for probe in probes],
        "elapsedMs": _now_ms() - started,
    }


@app.get("/")
def root():
    return jsonify(
        {
            "ok": True,
            "service": "bot-new-scratch-checker",
            "version": VERSION,
            "features": ["/check", "/webhook/telegram"],
            "liveDieProbeCount": 5,
            "liveDieProbes": [
                "graph_picture_primary",
                "graph_picture_app_token",
                "graphql_node",
                "external_checker",
                "html_mobile_fallback",
            ],
            "telegramRelayConfigured": bool(TELEGRAM_RELAY_TARGET_URL),
        }
    )


@app.get("/health")
def health():
    return jsonify({"ok": True, "version": VERSION})


@app.post("/check")
def check():
    auth_error = _require_api_key()
    if auth_error:
        return auth_error
    payload = request.get_json(silent=True) or {}
    raw_input = payload.get("input") or payload.get("url") or payload.get("uid") or ""
    return jsonify(check_live_die(raw_input))


@app.get("/check")
def check_get():
    auth_error = _require_api_key()
    if auth_error:
        return auth_error
    raw_input = request.args.get("input") or request.args.get("url") or request.args.get("uid") or ""
    return jsonify(check_live_die(raw_input))


@app.post("/webhook/telegram")
def webhook_telegram():
    if not TELEGRAM_RELAY_TARGET_URL:
        return jsonify({"ok": False, "error": "telegram_relay_target_missing"}), 500

    body = request.get_data() or b"{}"
    content_type = request.headers.get("Content-Type", "application/json")
    try:
        upstream = requests.post(
            TELEGRAM_RELAY_TARGET_URL,
            data=body,
            headers={"Content-Type": content_type},
            timeout=TELEGRAM_RELAY_TIMEOUT_SEC,
            allow_redirects=True,
        )
    except Exception as exc:
        return jsonify({"ok": False, "error": f"telegram_relay_exception:{exc}"}), 502

    if 200 <= upstream.status_code < 300:
        return jsonify({"ok": True, "accepted": True, "upstreamStatus": upstream.status_code})
    return (
        jsonify(
            {
                "ok": False,
                "error": "telegram_relay_upstream_failed",
                "upstreamStatus": upstream.status_code,
                "upstreamBody": (upstream.text or "")[:500],
            }
        ),
        502,
    )


def _require_api_key():
    if not UID_CHECKER_API_KEY:
        return None
    provided = request.headers.get("X-Api-Key") or request.args.get("apiKey") or ""
    if provided == UID_CHECKER_API_KEY:
        return None
    return jsonify({"ok": False, "error": "unauthorized"}), 401


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
