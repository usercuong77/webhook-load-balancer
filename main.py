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

VERSION = "step06_uid_resolve_before_probe_2026_05_16"
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

UID_SCRAPE_PATTERNS = (
    r'"userID"\s*:\s*"(\d{8,20})"',
    r'"profile_id"\s*:\s*(\d{8,20})',
    r'"entity_id"\s*:\s*"(\d{8,20})"',
    r'"actorID"\s*:\s*"(\d{8,20})"',
    r'"subject_id"\s*:\s*"(\d{8,20})"',
    r'profile\.php\?id=(\d{8,20})',
    r'fb://profile/(\d{8,20})',
)

FALLBACK_UID_PROBE_USER_AGENTS = (
    USER_AGENT,
    "Mozilla/5.0",
    "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
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


def _normalize_url_input(raw: Optional[str]) -> str:
    value = _to_text(raw).strip()
    if not value:
        return ""
    if value.startswith("http://") or value.startswith("https://"):
        return value
    return "https://" + value


def _normalize_uid(uid_raw: Optional[str]) -> str:
    uid = _to_text(uid_raw).strip()
    return uid if re.fullmatch(r"\d{8,}", uid) else ""


def _extract_uid_from_url(url_raw: Optional[str]) -> str:
    url = _normalize_url_input(url_raw)
    if not url:
        return ""
    try:
        parsed = urlparse(url)
    except Exception:
        return ""

    host = (parsed.netloc or "").lower().replace("www.", "")
    if "facebook.com" not in host and "fb.com" not in host:
        return ""

    qs = parse_qs(parsed.query or "")
    profile_id = _to_text((qs.get("id", [""])[0] or "")).strip()
    if re.fullmatch(r"\d{8,}", profile_id):
        return profile_id

    path = _to_text(parsed.path).strip("/")
    parts = [p for p in path.split("/") if p]
    if not parts:
        return ""

    if parts[0].lower() == "people" and len(parts) >= 3 and re.fullmatch(r"\d{8,}", parts[2]):
        return parts[2]
    if re.fullmatch(r"\d{8,}", parts[0]):
        return parts[0]
    return ""


def _extract_uid_from_html(html_raw: Optional[str]) -> str:
    html = _to_text(html_raw)
    if not html:
        return ""
    normalized = _normalize_facebook_payload_text(
        html.replace("\\/", "/")
        .replace("\\u002f", "/")
        .replace("\\u003a", ":")
        .replace("&quot;", '"')
    )
    for pattern in UID_SCRAPE_PATTERNS:
        match = re.search(pattern, normalized, flags=re.I)
        if not match:
            continue
        uid = _to_text(match.group(1) if match.groups() else "").strip()
        if re.fullmatch(r"\d{8,20}", uid):
            return uid
    return ""


def _safe_percent_decode_text(value_raw: Optional[str], rounds_raw: int = 1) -> str:
    value = _to_text(value_raw)
    if not value:
        return ""
    rounds = max(1, min(3, int(rounds_raw or 1)))
    for _ in range(rounds):
        next_value = re.sub(r"%([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), value)
        if next_value == value:
            break
        value = next_value
    return value


def _normalize_facebook_payload_text(raw: Optional[str]) -> str:
    normalized = (
        _to_text(raw)
        .replace("\\/", "/")
        .replace("\\u002f", "/")
        .replace("\\u003a", ":")
        .replace("\\u003d", "=")
        .replace("\\u0026", "&")
        .replace("\\u003f", "?")
        .replace("\\x2f", "/")
        .replace("\\x3a", ":")
        .replace("\\x3d", "=")
        .replace("\\x26", "&")
        .replace("\\x3f", "?")
        .replace("&#x2f;", "/")
        .replace("&#x3a;", ":")
        .replace("&#x3d;", "=")
        .replace("&#x26;", "&")
        .replace("&#x3f;", "?")
        .replace("&#47;", "/")
        .replace("&#58;", ":")
        .replace("&#61;", "=")
        .replace("&#38;", "&")
        .replace("&#63;", "?")
        .replace("&amp;", "&")
        .replace("%253d", "%3d")
        .replace("%253D", "%3D")
        .replace("%2526", "%26")
        .replace("%253f", "%3f")
        .replace("%253F", "%3F")
        .replace("%3d", "=")
        .replace("%3D", "=")
        .replace("%26", "&")
        .replace("%3f", "?")
        .replace("%3F", "?")
        .replace("&quot;", '"')
    )
    return _safe_percent_decode_text(normalized, 2)


def _build_facebook_navigation_hint_headers(user_agent_raw: Optional[str]) -> Dict[str, str]:
    user_agent = _to_text(user_agent_raw).lower()
    platform = '"Windows"'
    mobile = "?0"
    if "android" in user_agent:
        platform = '"Android"'
        mobile = "?1"
    elif "iphone" in user_agent or "ipad" in user_agent or "ios" in user_agent:
        platform = '"iOS"'
        mobile = "?1"
    return {
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "sec-ch-ua": '"Chromium";v="140", "Not.A/Brand";v="24", "Google Chrome";v="140"',
        "sec-ch-ua-mobile": mobile,
        "sec-ch-ua-platform": platform,
        "Cache-Control": "max-age=0",
    }


def _build_facebook_probe_urls(url_raw: Optional[str]) -> List[str]:
    normalized = _normalize_url_input(url_raw)
    if not normalized:
        return []
    urls: List[str] = [normalized]
    try:
        parsed = urlparse(normalized)
        host = (parsed.netloc or "").lower()
        if "facebook.com" in host or "fb.com" in host:
            path = parsed.path or "/"
            query = ("?" + parsed.query) if parsed.query else ""
            urls.append(f"https://m.facebook.com{path}{query}")
            urls.append(f"https://mbasic.facebook.com{path}{query}")
            urls.append(f"https://www.facebook.com{path}{query}")
    except Exception:
        pass

    out: List[str] = []
    seen = set()
    for item in urls:
        key = _to_text(item).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def _build_uid_probe_header_candidates() -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    seen = set()
    for ua in FALLBACK_UID_PROBE_USER_AGENTS:
        key = _to_text(ua).strip()
        if not key:
            continue
        low = key.lower()
        if low in seen:
            continue
        seen.add(low)
        headers = {"User-Agent": key, "Accept-Language": "vi,en-US;q=0.9,en;q=0.8"}
        headers.update(_build_facebook_navigation_hint_headers(key))
        out.append(headers)
    return out


def _resolve_uid_from_graph_username(username_raw: Optional[str], fetcher: Optional[Callable] = None) -> str:
    username = _to_text(username_raw).strip().lstrip("@").strip("/")
    if not username:
        return ""
    if not FB_PUBLIC_APP_TOKEN:
        return ""

    graph_url = (
        "https://graph.facebook.com/" + quote(username)
        + "?fields=id&access_token=" + quote(FB_PUBLIC_APP_TOKEN)
    )
    try:
        response = _get_text(graph_url, fetcher)
        payload = json.loads(response.get("text") or "{}")
        return _normalize_uid(payload.get("id")) if isinstance(payload, dict) else ""
    except Exception:
        return ""


def _resolve_uid_from_facebook_url_debug(url_raw: Optional[str], fetcher: Optional[Callable] = None) -> Dict:
    direct_uid = _extract_uid_from_url(url_raw)
    if direct_uid:
        return {"uid": direct_uid, "source": "direct_url", "attempts": []}

    probe_urls = _build_facebook_probe_urls(url_raw)
    if not probe_urls:
        return {"uid": "", "source": "no_probe_url", "attempts": []}

    attempts: List[Dict] = []
    for headers in _build_uid_probe_header_candidates():
        for probe_url in probe_urls:
            try:
                response = _request_text("get", probe_url, fetcher=fetcher, headers=headers)
            except Exception:
                attempts.append(
                    {
                        "url": probe_url,
                        "status": 0,
                        "ua": _to_text(headers.get("User-Agent"))[:80],
                        "error": "request_exception",
                    }
                )
                continue

            uid_html = _extract_uid_from_html(response.get("text"))
            uid_final = _extract_uid_from_url(response.get("url"))
            attempts.append(
                {
                    "url": probe_url,
                    "status": int(response.get("status_code") or 0),
                    "finalUrl": _to_text(response.get("url")),
                    "ua": _to_text(headers.get("User-Agent"))[:80],
                    "uidFromHtml": uid_html,
                    "uidFromFinalUrl": uid_final,
                }
            )
            if uid_html:
                return {"uid": uid_html, "source": "html_pattern", "attempts": attempts}

            if uid_final:
                return {"uid": uid_final, "source": "final_url", "attempts": attempts}

    return {"uid": "", "source": "not_found", "attempts": attempts}


def _resolve_uid_from_facebook_url(url_raw: Optional[str], fetcher: Optional[Callable] = None) -> str:
    return _to_text(_resolve_uid_from_facebook_url_debug(url_raw, fetcher).get("uid")).strip()


def _resolve_uid_for_check(normalized: Dict, fetcher: Optional[Callable] = None) -> Dict:
    uid = _normalize_uid(normalized.get("uid"))
    username = _to_text(normalized.get("username")).strip()
    profile_url = _to_text(normalized.get("profileUrl")).strip()
    input_type = _to_text(normalized.get("inputType")).strip().lower()

    if uid:
        return {"uid": uid, "source": "direct_uid", "profileUrl": f"https://www.facebook.com/profile.php?id={uid}"}

    resolved_uid = ""
    resolve_source = ""
    if profile_url:
        resolved_uid = _resolve_uid_from_facebook_url(profile_url, fetcher)
        if resolved_uid:
            resolve_source = "url_probe"

    if not resolved_uid and username:
        resolved_uid = _resolve_uid_from_graph_username(username, fetcher)
        if resolved_uid:
            resolve_source = "graph_username"

    if not resolved_uid and input_type == "username" and username:
        fallback_url = "https://www.facebook.com/" + quote(username)
        resolved_uid = _resolve_uid_from_facebook_url(fallback_url, fetcher)
        if resolved_uid:
            resolve_source = "username_probe"

    if resolved_uid:
        return {
            "uid": resolved_uid,
            "source": resolve_source or "resolved",
            "profileUrl": f"https://www.facebook.com/profile.php?id={resolved_uid}",
        }

    return {"uid": "", "source": "uid_not_resolved", "profileUrl": profile_url}


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

    username = normalized.get("username", "")
    resolved = _resolve_uid_for_check(normalized, fetcher)
    uid = _to_text(resolved.get("uid")).strip()
    uid_source = _to_text(resolved.get("source")).strip()
    profile_url = _to_text(resolved.get("profileUrl") or normalized.get("profileUrl")).strip()

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
        "uidResolved": bool(uid),
        "uidSource": uid_source,
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


@app.get("/get-uid")
def get_uid():
    auth_error = _require_api_key()
    if auth_error:
        return auth_error
    url = request.args.get("url") or request.args.get("input") or ""
    debug_mode = _to_text(request.args.get("debug")).strip() in ("1", "true", "on", "yes")
    result = _resolve_uid_from_facebook_url_debug(url)
    payload = {
        "ok": bool(result.get("uid")),
        "uid": _to_text(result.get("uid")),
        "source": _to_text(result.get("source")),
        "url": _normalize_url_input(url),
    }
    if not payload["ok"]:
        payload["error"] = "uid_not_found"
    if debug_mode:
        payload["attempts"] = result.get("attempts") or []
    return jsonify(payload), (200 if payload["ok"] else 404)


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
