import json
import os
import re
import time
import html as html_lib
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional
from urllib.parse import parse_qs, quote, unquote, urlparse

import requests
from app_modules.parsers.facebook_url import normalize_input as parser_normalize_input
from app_modules.resolvers.uid_resolver import (
    resolve_uid_for_check as resolver_resolve_uid_for_check,
    resolve_uid_from_facebook_url_debug as resolver_resolve_uid_from_facebook_url_debug,
)

VERSION = "step09_module_split_phase1_2026_05_16"
REQUEST_TIMEOUT_SEC = 8
FB_PUBLIC_APP_TOKEN = os.getenv("FB_PUBLIC_APP_TOKEN", "6628568379|c1e620fa708a1d5696fb991c1bde5662")
EXTERNAL_CHECKER_URL = os.getenv("EXTERNAL_CHECKER_URL", "").strip()
EXTERNAL_CHECKER_API_KEY = os.getenv("EXTERNAL_CHECKER_API_KEY", "").strip()
UID_CHECKER_API_KEY = os.getenv("UID_CHECKER_API_KEY", "").strip()
UID_CHECKER_FB_COOKIES_JSON = os.getenv("UID_CHECKER_FB_COOKIES_JSON", "").strip()
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

PROFILE_NAME_BLOCKLIST = (
    "facebook",
    "error",
    "sorry",
    "log in",
    "login",
    "login or sign up",
    "join facebook",
    "sign up",
    "unsupported browser",
    "trinh duyet nay khong duoc ho tro",
    "dang nhap",
    "dang ky",
    "message",
    "friend",
    "notifications",
    "watch",
    "marketplace",
    "meta",
)

FACEBOOK_RESERVED_PATH_PREFIXES = {
    "profile.php",
    "people",
    "share",
    "photo",
    "photos",
    "posts",
    "permalink.php",
    "story.php",
    "watch",
    "reel",
    "reels",
    "groups",
    "pages",
    "events",
    "marketplace",
    "login",
    "recover",
    "checkpoint",
    "dialog",
    "plugins",
    "settings",
    "messages",
    "notifications",
}

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
    profile_name: str = ""

    def to_dict(self) -> Dict:
        return {
            "name": self.name,
            "status": self.status,
            "confidence": self.confidence,
            "httpStatus": self.http_status,
            "reason": self.reason,
            "url": self.url,
            "profileName": self.profile_name,
        }


def _now_ms() -> int:
    return int(time.time() * 1000)


def _to_text(value) -> str:
    return "" if value is None else str(value)


def _normalize_cookie_map(cookies_raw) -> Dict[str, str]:
    if not isinstance(cookies_raw, dict):
        return {}
    out: Dict[str, str] = {}
    for key, value in cookies_raw.items():
        cookie_key = _to_text(key).strip()
        cookie_value = _to_text(value).strip()
        if cookie_key and cookie_value:
            out[cookie_key] = cookie_value
    return out


def _load_default_name_probe_cookies() -> Dict[str, str]:
    raw = UID_CHECKER_FB_COOKIES_JSON
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    if isinstance(parsed, list):
        for item in parsed:
            normalized = _normalize_cookie_map(item)
            if normalized:
                return normalized
        return {}
    return _normalize_cookie_map(parsed)


DEFAULT_NAME_PROBE_COOKIES = _load_default_name_probe_cookies()


def _clean_profile_name_candidate(raw_name: Optional[str]) -> str:
    name = html_lib.unescape(_to_text(raw_name))
    if not name:
        return ""
    name = re.sub(r"<[^>]+>", " ", name)
    name = re.sub(r"\s+", " ", name).strip(" \t\r\n-–|")
    name = re.sub(r"\s+\|\s*facebook.*$", "", name, flags=re.I)
    name = re.sub(r"\s*-\s*facebook.*$", "", name, flags=re.I)
    name = re.sub(r"\s*·\s*facebook.*$", "", name, flags=re.I)
    return re.sub(r"\s+", " ", name).strip()


def _is_valid_profile_name(raw_name: Optional[str]) -> bool:
    name = _clean_profile_name_candidate(raw_name)
    if len(name) < 2 or len(name) > 90:
        return False
    low = name.lower()
    for marker in PROFILE_NAME_BLOCKLIST:
        if marker in low:
            return False
    return any(ch.isalpha() for ch in name)


def _extract_profile_name_from_html(html_raw: Optional[str]) -> str:
    html = _to_text(html_raw)
    if not html:
        return ""

    patterns = (
        r'<meta[^>]+\bproperty=["\']og:title["\'][^>]+\bcontent=["\']([^"\']+)["\']',
        r'<meta[^>]+\bcontent=["\']([^"\']+)["\'][^>]+\bproperty=["\']og:title["\']',
        r"<title[^>]*>(.*?)</title>",
        r"<h1[^>]*>(.*?)</h1>",
    )
    for pattern in patterns:
        match = re.search(pattern, html, flags=re.I | re.S)
        if not match:
            continue
        candidate = _clean_profile_name_candidate(match.group(1))
        if _is_valid_profile_name(candidate):
            return candidate
    return ""


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

    uid = _extract_uid_from_url(url)
    username = _extract_username_slug_from_url(url)

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


def _extract_username_slug_from_url(url_raw: Optional[str]) -> str:
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

    path = _to_text(parsed.path).strip("/")
    parts = [part for part in path.split("/") if part]
    if not parts:
        return ""

    first_raw = unquote(_to_text(parts[0]).strip())
    first = first_raw.lower()
    if not first:
        return ""
    if first in FACEBOOK_RESERVED_PATH_PREFIXES:
        return ""
    if re.fullmatch(r"\d{8,20}", first_raw):
        return ""
    return first_raw


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
        return {
            "uid": direct_uid,
            "source": "direct_url",
            "attempts": [],
            "resolvedUsername": _extract_username_slug_from_url(url_raw),
            "resolvedUrl": f"https://www.facebook.com/profile.php?id={direct_uid}",
        }

    probe_urls = _build_facebook_probe_urls(url_raw)
    if not probe_urls:
        return {"uid": "", "source": "no_probe_url", "attempts": [], "resolvedUsername": "", "resolvedUrl": ""}

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

            final_url = _to_text(response.get("url"))
            uid_html = _extract_uid_from_html(response.get("text"))
            uid_final = _extract_uid_from_url(final_url)
            resolved_username = _extract_username_slug_from_url(final_url)
            attempts.append(
                {
                    "url": probe_url,
                    "status": int(response.get("status_code") or 0),
                    "finalUrl": final_url,
                    "ua": _to_text(headers.get("User-Agent"))[:80],
                    "uidFromHtml": uid_html,
                    "uidFromFinalUrl": uid_final,
                }
            )
            if uid_html:
                return {
                    "uid": uid_html,
                    "source": "html_pattern",
                    "attempts": attempts,
                    "resolvedUsername": resolved_username,
                    "resolvedUrl": f"https://www.facebook.com/profile.php?id={uid_html}",
                }

            if uid_final:
                return {
                    "uid": uid_final,
                    "source": "final_url",
                    "attempts": attempts,
                    "resolvedUsername": resolved_username,
                    "resolvedUrl": f"https://www.facebook.com/profile.php?id={uid_final}",
                }

    return {
        "uid": "",
        "source": "not_found",
        "attempts": attempts,
        "resolvedUsername": _extract_username_slug_from_url(url_raw),
        "resolvedUrl": _normalize_url_input(url_raw),
    }


def _resolve_uid_from_facebook_url(url_raw: Optional[str], fetcher: Optional[Callable] = None) -> str:
    return _to_text(_resolve_uid_from_facebook_url_debug(url_raw, fetcher).get("uid")).strip()


def _resolve_uid_for_check(normalized: Dict, fetcher: Optional[Callable] = None) -> Dict:
    uid = _normalize_uid(normalized.get("uid"))
    username = _to_text(normalized.get("username")).strip()
    profile_url = _to_text(normalized.get("profileUrl")).strip()
    input_type = _to_text(normalized.get("inputType")).strip().lower()

    if uid:
        derived_username = username
        canonical_url = f"https://www.facebook.com/profile.php?id={uid}"
        if not derived_username:
            debug_result = _resolve_uid_from_facebook_url_debug(canonical_url, fetcher)
            derived_username = _to_text(debug_result.get("resolvedUsername")).strip()
        return {
            "uid": uid,
            "source": "direct_uid",
            "profileUrl": canonical_url,
            "username": derived_username,
        }

    resolved_uid = ""
    resolved_username = ""
    resolve_source = ""
    if profile_url:
        for _ in range(2):
            debug_result = _resolve_uid_from_facebook_url_debug(profile_url, fetcher)
            resolved_uid = _to_text(debug_result.get("uid")).strip()
            if not resolved_username:
                resolved_username = _to_text(debug_result.get("resolvedUsername")).strip()
            resolved_url = _to_text(debug_result.get("resolvedUrl")).strip()
            if resolved_url:
                profile_url = resolved_url
            if resolved_uid:
                resolve_source = _to_text(debug_result.get("source")).strip() or "url_probe"
                break

    if not resolved_uid and username:
        resolved_uid = _resolve_uid_from_graph_username(username, fetcher)
        if resolved_uid:
            resolve_source = "graph_username"

    if not resolved_uid and username:
        fallback_url = "https://www.facebook.com/" + quote(username)
        debug_result = _resolve_uid_from_facebook_url_debug(fallback_url, fetcher)
        resolved_uid = _to_text(debug_result.get("uid")).strip()
        if not resolved_username:
            resolved_username = _to_text(debug_result.get("resolvedUsername")).strip()
        if resolved_uid:
            resolve_source = _to_text(debug_result.get("source")).strip() or "username_probe"

    if resolved_uid:
        effective_username = username or resolved_username
        return {
            "uid": resolved_uid,
            "source": resolve_source or "resolved",
            "profileUrl": f"https://www.facebook.com/profile.php?id={resolved_uid}",
            "username": effective_username,
        }

    return {
        "uid": "",
        "source": "uid_not_resolved",
        "profileUrl": profile_url,
        "username": username or resolved_username,
    }


def _get_text(
    url: str,
    fetcher: Optional[Callable] = None,
    headers: Optional[Dict] = None,
    cookies: Optional[Dict[str, str]] = None,
) -> Dict:
    return _request_text("get", url, fetcher=fetcher, headers=headers, cookies=cookies)


def _request_text(
    method: str,
    url: str,
    fetcher: Optional[Callable] = None,
    json_payload: Optional[Dict] = None,
    data: Optional[str] = None,
    headers: Optional[Dict] = None,
    cookies: Optional[Dict[str, str]] = None,
) -> Dict:
    request_headers = {"User-Agent": USER_AGENT, "Accept-Language": "vi,en-US;q=0.9,en;q=0.8"}
    if headers:
        request_headers.update(headers)
    request_cookies = _normalize_cookie_map(cookies)
    if fetcher is None:
        response = requests.request(
            method.upper(),
            url,
            timeout=REQUEST_TIMEOUT_SEC,
            allow_redirects=True,
            headers=request_headers,
            json=json_payload,
            data=data,
            cookies=request_cookies or None,
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
            cookies=request_cookies or None,
        )
    return {
        "status_code": int(getattr(response, "status_code", 0) or 0),
        "url": str(getattr(response, "url", url) or url),
        "text": _to_text(getattr(response, "text", ""))[:400000],
        "headers": dict(getattr(response, "headers", {}) or {}),
    }


def _public_profile_probe(
    name: str,
    url: str,
    fetcher: Optional[Callable] = None,
    cookies: Optional[Dict[str, str]] = None,
) -> ProbeResult:
    try:
        response = _get_text(url, fetcher, cookies=cookies)
    except Exception as exc:
        return ProbeResult(name, "unknown", "weak", 0, f"fetch_error:{exc}", url)

    http_status = response["status_code"]
    body = response["text"].lower()
    profile_name = _extract_profile_name_from_html(response["text"])

    if any(marker in body for marker in DEAD_MARKERS):
        return ProbeResult(name, "dead", "strong", http_status, "dead_marker", response["url"], profile_name)

    if http_status in (404, 410):
        return ProbeResult(name, "unknown", "weak", http_status, "http_not_found_without_dead_marker", response["url"], profile_name)

    if http_status >= 500:
        return ProbeResult(name, "unknown", "weak", http_status, "server_error", response["url"], profile_name)

    if http_status in (200, 301, 302):
        if any(marker in body for marker in LIVE_MARKERS):
            return ProbeResult(name, "live", "weak", http_status, "live_marker_or_auth_wall", response["url"], profile_name)
        return ProbeResult(name, "live", "weak", http_status, "http_ok_no_dead_marker", response["url"], profile_name)

    return ProbeResult(name, "unknown", "weak", http_status, "unclassified_http", response["url"], profile_name)


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
    profile_name_raw = ""
    if isinstance(node, dict):
        profile_name_raw = _to_text(node.get("name"))
    elif isinstance(node, str):
        profile_name_raw = node
    profile_name = _clean_profile_name_candidate(profile_name_raw) if _is_valid_profile_name(profile_name_raw) else ""
    is_dead = node is None or node == ""
    return ProbeResult(
        "graphql_node",
        "dead" if is_dead else "live",
        "strong",
        http_status,
        "graphql_node_empty" if is_dead else "graphql_node_found",
        response["url"],
        profile_name,
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
    profile_name = _pick_profile_name_from_external_payload(payload)
    return ProbeResult(
        "external_checker",
        status,
        "weak" if status == "unknown" else "strong",
        int(payload.get("httpCode") or payload.get("http_code") or http_status or 0),
        "external:" + _to_text(reason),
        response["url"],
        profile_name,
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


def _pick_profile_name_from_external_payload(payload: Dict) -> str:
    if not isinstance(payload, dict):
        return ""
    candidates = [
        payload.get("profileName"),
        payload.get("profile_name"),
        payload.get("name"),
        payload.get("fullName"),
        payload.get("displayName"),
    ]
    signals = payload.get("signals")
    if isinstance(signals, dict):
        for key in ("m", "touch", "www", "mbasic", "public", "url"):
            signal = signals.get(key)
            if isinstance(signal, dict):
                candidates.append(signal.get("name"))

    for candidate in candidates:
        if _is_valid_profile_name(candidate):
            return _clean_profile_name_candidate(candidate)
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


def _pick_profile_name_from_probes(probes: List[ProbeResult], final_status: str) -> Dict[str, str]:
    if _to_text(final_status).lower() != "live":
        return {"profileName": "", "profileNameSource": ""}

    by_name = {probe.name: probe for probe in probes}
    preferred_order = (
        "external_checker",
        "html_mobile_fallback",
        "graphql_node",
        "graph_picture_primary",
        "graph_picture_app_token",
    )
    for source in preferred_order:
        probe = by_name.get(source)
        if probe and _is_valid_profile_name(probe.profile_name):
            return {
                "profileName": _clean_profile_name_candidate(probe.profile_name),
                "profileNameSource": source,
            }

    for probe in probes:
        if _is_valid_profile_name(probe.profile_name):
            return {
                "profileName": _clean_profile_name_candidate(probe.profile_name),
                "profileNameSource": probe.name,
            }

    return {"profileName": "", "profileNameSource": ""}


def _resolve_profile_name_from_graph(uid_raw: Optional[str], fetcher: Optional[Callable] = None) -> str:
    uid = _normalize_uid(uid_raw)
    if not uid or not FB_PUBLIC_APP_TOKEN:
        return ""
    url = (
        "https://graph.facebook.com/" + quote(uid)
        + "?fields=name&access_token=" + quote(FB_PUBLIC_APP_TOKEN)
    )
    try:
        response = _get_text(url, fetcher)
        payload = json.loads(response.get("text") or "{}")
        if not isinstance(payload, dict):
            return ""
        name = payload.get("name")
        if _is_valid_profile_name(name):
            return _clean_profile_name_candidate(name)
        return ""
    except Exception:
        return ""


def _enrich_profile_name_for_live_profile(
    profile_url: str,
    uid: str,
    username: str,
    fetcher: Optional[Callable] = None,
) -> Dict[str, str]:
    graph_name = _resolve_profile_name_from_graph(uid, fetcher)
    if graph_name:
        return {"profileName": graph_name, "profileNameSource": "graph_name"}

    normalized_url = _normalize_url_input(profile_url)
    candidates: List[str] = []
    if normalized_url:
        candidates.append(normalized_url)
    if uid:
        candidates.append(f"https://m.facebook.com/profile.php?id={uid}")
        candidates.append(f"https://touch.facebook.com/profile.php?id={uid}")
        candidates.append(f"https://www.facebook.com/profile.php?id={uid}")
    elif username:
        safe_username = quote(_to_text(username).strip().lstrip("@").strip("/"))
        candidates.append(f"https://m.facebook.com/{safe_username}")
        candidates.append(f"https://touch.facebook.com/{safe_username}")
        candidates.append(f"https://www.facebook.com/{safe_username}")

    seen = set()
    unique_candidates: List[str] = []
    for item in candidates:
        key = _to_text(item).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        unique_candidates.append(key)

    cookie_rounds: List[Dict[str, Dict[str, str]]] = [{"source": "no_cookie", "cookies": {}}]
    if DEFAULT_NAME_PROBE_COOKIES:
        cookie_rounds.append({"source": "with_cookie", "cookies": DEFAULT_NAME_PROBE_COOKIES})

    for cookie_round in cookie_rounds:
        cookies = cookie_round.get("cookies") or {}
        cookie_source = _to_text(cookie_round.get("source")) or "no_cookie"
        for index, url in enumerate(unique_candidates[:3], start=1):
            probe = _public_profile_probe(f"name_enrich_{index}", url, fetcher, cookies=cookies)
            if _is_valid_profile_name(probe.profile_name):
                return {
                    "profileName": _clean_profile_name_candidate(probe.profile_name),
                    "profileNameSource": f"name_enrich_{index}:{cookie_source}",
                }
    return {"profileName": "", "profileNameSource": ""}


def _build_profile_name_from_username_slug(username_raw: Optional[str]) -> str:
    username = _to_text(username_raw).strip().lstrip("@").strip("/")
    if not username:
        return ""
    if re.fullmatch(r"\d{8,20}", username):
        return ""
    normalized = re.sub(r"[._-]+", " ", username).strip()
    normalized = re.sub(r"\s+", " ", normalized)
    if len(normalized) < 2:
        return ""
    words = [word for word in normalized.split(" ") if word]
    candidate = " ".join(word[:1].upper() + word[1:] for word in words)
    return _clean_profile_name_candidate(candidate) if _is_valid_profile_name(candidate) else ""


def check_live_die(raw_input: str, fetcher: Optional[Callable] = None) -> Dict:
    started = _now_ms()
    normalized = parser_normalize_input(raw_input)
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
    resolved = resolver_resolve_uid_for_check(normalized, fetcher)
    uid = _to_text(resolved.get("uid")).strip()
    uid_source = _to_text(resolved.get("source")).strip()
    profile_url = _to_text(resolved.get("profileUrl") or normalized.get("profileUrl")).strip()
    if not username:
        username = _to_text(resolved.get("username")).strip()

    probes = [
        _graph_picture_primary_probe(uid, fetcher),
        _graph_picture_app_token_probe(uid, fetcher),
        _graphql_node_probe(uid, fetcher),
        _external_checker_probe(uid, profile_url, fetcher),
        _html_mobile_fallback_probe(profile_url, uid, username, fetcher),
    ]

    chosen = _choose_result(probes)
    profile_name_pick = _pick_profile_name_from_probes(probes, chosen["status"])
    if not profile_name_pick["profileName"] and _to_text(chosen["status"]).lower() == "live":
        enriched_name = _enrich_profile_name_for_live_profile(profile_url, uid, username, fetcher)
        if enriched_name["profileName"]:
            profile_name_pick = enriched_name
    if not profile_name_pick["profileName"] and _to_text(chosen["status"]).lower() == "live":
        fallback_name = _build_profile_name_from_username_slug(username)
        if fallback_name:
            profile_name_pick = {"profileName": fallback_name, "profileNameSource": "username_slug"}
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
        "profileName": profile_name_pick["profileName"],
        "profileNameSource": profile_name_pick["profileNameSource"],
        "nameProbeCookieConfigured": bool(DEFAULT_NAME_PROBE_COOKIES),
        "probeCount": len(probes),
        "probes": [probe.to_dict() for probe in probes],
        "elapsedMs": _now_ms() - started,
    }

def build_root_status() -> Dict:
    return {
        "ok": True,
        "service": "bot-new-scratch-checker",
        "version": VERSION,
        "features": ["/check", "/get-uid", "/webhook/telegram"],
        "nameProbeCookieConfigured": bool(DEFAULT_NAME_PROBE_COOKIES),
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


def health_status() -> Dict:
    return {"ok": True, "version": VERSION}


def check_from_payload(payload: Optional[Dict]) -> Dict:
    safe_payload = payload if isinstance(payload, dict) else {}
    raw_input = safe_payload.get("input") or safe_payload.get("url") or safe_payload.get("uid") or ""
    return check_live_die(raw_input)


def check_from_query(query: Dict) -> Dict:
    raw_input = query.get("input") or query.get("url") or query.get("uid") or ""
    return check_live_die(raw_input)


def get_uid_payload(url: str, debug_mode: bool = False) -> Dict:
    result = resolver_resolve_uid_from_facebook_url_debug(url)
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
    return payload


def relay_telegram_webhook(body: bytes, content_type: str) -> Dict:
    if not TELEGRAM_RELAY_TARGET_URL:
        return {"ok": False, "error": "telegram_relay_target_missing", "statusCode": 500}

    try:
        upstream = requests.post(
            TELEGRAM_RELAY_TARGET_URL,
            data=body,
            headers={"Content-Type": content_type},
            timeout=TELEGRAM_RELAY_TIMEOUT_SEC,
            allow_redirects=True,
        )
    except Exception as exc:
        return {"ok": False, "error": f"telegram_relay_exception:{exc}", "statusCode": 502}

    if 200 <= upstream.status_code < 300:
        return {
            "ok": True,
            "accepted": True,
            "upstreamStatus": upstream.status_code,
            "statusCode": 200,
        }

    return {
        "ok": False,
        "error": "telegram_relay_upstream_failed",
        "upstreamStatus": upstream.status_code,
        "upstreamBody": (upstream.text or "")[:500],
        "statusCode": 502,
    }


def is_api_key_valid(provided_raw: Optional[str]) -> bool:
    if not UID_CHECKER_API_KEY:
        return True
    provided = _to_text(provided_raw).strip()
    return provided == UID_CHECKER_API_KEY
