import json
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional
from urllib.parse import quote, urlparse

from app_modules.config import EXTERNAL_CHECKER_API_KEY, EXTERNAL_CHECKER_URL, FB_PUBLIC_APP_TOKEN
from app_modules.http_client import get_text, request_text
from app_modules.parsers.facebook_url import normalize_uid, normalize_url_input, to_text
from app_modules.parsers.profile_name import (
    clean_profile_name_candidate,
    extract_profile_name_from_html,
    is_valid_profile_name,
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

HTML_PROBE_USER_AGENT = "Mozilla/5.0"


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


def public_profile_probe(
    name: str,
    url: str,
    fetcher: Optional[Callable] = None,
    cookies: Optional[Dict[str, str]] = None,
) -> ProbeResult:
    try:
        # Facebook can return HTTP 400 for modern Chrome-like UA on public profile HTML.
        # Use a conservative UA for HTML probes so we can still extract profile name/title.
        response = get_text(url, fetcher, headers={"User-Agent": HTML_PROBE_USER_AGENT}, cookies=cookies)
    except Exception as exc:
        return ProbeResult(name, "unknown", "weak", 0, f"fetch_error:{exc}", url)

    http_status = response["status_code"]
    body = response["text"].lower()
    profile_name = extract_profile_name_from_html(response["text"])

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


def graph_picture_primary_probe(uid: str, fetcher: Optional[Callable] = None) -> ProbeResult:
    if not uid:
        return ProbeResult("graph_picture_primary", "unknown", "weak", 0, "uid_missing", "")

    url = f"https://graph.facebook.com/{uid}/picture?type=normal&redirect=false"
    try:
        response = get_text(url, fetcher)
    except Exception as exc:
        return ProbeResult("graph_picture_primary", "unknown", "weak", 0, f"fetch_error:{exc}", url)

    http_status = response["status_code"]
    body = response["text"].lower()
    if "height" in body and "width" in body:
        return ProbeResult("graph_picture_primary", "live", "strong", http_status, "graph_primary_height_width", response["url"])
    return ProbeResult("graph_picture_primary", "unknown", "weak", http_status, "graph_primary_no_live_signal", response["url"])


def graph_picture_app_token_probe(uid: str, fetcher: Optional[Callable] = None) -> ProbeResult:
    if not uid:
        return ProbeResult("graph_picture_app_token", "unknown", "weak", 0, "uid_missing", "")

    url = f"https://graph.facebook.com/{uid}/picture?width=500&access_token={quote(FB_PUBLIC_APP_TOKEN)}&redirect=false"
    try:
        response = get_text(url, fetcher)
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


def graphql_node_probe(uid: str, fetcher: Optional[Callable] = None) -> ProbeResult:
    if not uid:
        return ProbeResult("graphql_node", "unknown", "weak", 0, "uid_missing", "")

    url = "https://www.facebook.com/api/graphql"
    data = "q=" + quote(f"node({uid}){{name}}")
    try:
        response = request_text(
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
        profile_name_raw = to_text(node.get("name"))
    elif isinstance(node, str):
        profile_name_raw = node
    profile_name = clean_profile_name_candidate(profile_name_raw) if is_valid_profile_name(profile_name_raw) else ""
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


def _normalize_external_status(value) -> str:
    text = to_text(value).strip().lower()
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
        if is_valid_profile_name(candidate):
            return clean_profile_name_candidate(candidate)
    return ""


def external_checker_probe(uid: str, profile_url: str, fetcher: Optional[Callable] = None) -> ProbeResult:
    if not EXTERNAL_CHECKER_URL:
        return ProbeResult("external_checker", "unknown", "weak", 0, "external_not_configured", "")
    if not uid:
        return ProbeResult("external_checker", "unknown", "weak", 0, "uid_missing", EXTERNAL_CHECKER_URL)

    headers = {}
    if EXTERNAL_CHECKER_API_KEY:
        headers["X-Api-Key"] = EXTERNAL_CHECKER_API_KEY
    try:
        response = request_text(
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
        "external:" + to_text(reason),
        response["url"],
        profile_name,
    )


def html_mobile_fallback_probe(profile_url: str, uid: str, username: str, fetcher: Optional[Callable] = None) -> ProbeResult:
    public_probe = public_profile_probe("html_public", profile_url, fetcher)
    if public_probe.status in ("live", "dead") and is_valid_profile_name(public_probe.profile_name):
        return ProbeResult(
            "html_mobile_fallback",
            public_probe.status,
            public_probe.confidence,
            public_probe.http_status,
            "public_name_signal:" + public_probe.reason,
            public_probe.url,
            public_probe.profile_name,
        )
    if public_probe.status == "dead" and public_probe.confidence == "strong":
        return ProbeResult(
            "html_mobile_fallback",
            public_probe.status,
            public_probe.confidence,
            public_probe.http_status,
            public_probe.reason,
            public_probe.url,
            public_probe.profile_name,
        )
    if public_probe.status == "live" and public_probe.confidence == "strong":
        return ProbeResult(
            "html_mobile_fallback",
            public_probe.status,
            public_probe.confidence,
            public_probe.http_status,
            public_probe.reason,
            public_probe.url,
            public_probe.profile_name,
        )

    mobile_probe = public_profile_probe("html_mobile", _mobile_url(profile_url, uid, username), fetcher)
    if mobile_probe.status in ("live", "dead") and is_valid_profile_name(mobile_probe.profile_name):
        return ProbeResult(
            "html_mobile_fallback",
            mobile_probe.status,
            mobile_probe.confidence,
            mobile_probe.http_status,
            "mobile_name_signal:" + mobile_probe.reason,
            mobile_probe.url,
            mobile_probe.profile_name,
        )
    if mobile_probe.status in ("live", "dead"):
        return ProbeResult(
            "html_mobile_fallback",
            mobile_probe.status,
            mobile_probe.confidence,
            mobile_probe.http_status,
            "mobile:" + mobile_probe.reason,
            mobile_probe.url,
            mobile_probe.profile_name,
        )
    if public_probe.status in ("live", "dead"):
        return ProbeResult(
            "html_mobile_fallback",
            public_probe.status,
            public_probe.confidence,
            public_probe.http_status,
            "public:" + public_probe.reason,
            public_probe.url,
            public_probe.profile_name,
        )
    return ProbeResult(
        "html_mobile_fallback",
        "unknown",
        "weak",
        public_probe.http_status or mobile_probe.http_status,
        "html_mobile_uncertain",
        profile_url,
        public_probe.profile_name or mobile_probe.profile_name,
    )


def choose_result(probes: List[ProbeResult]) -> Dict:
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


def pick_profile_name_from_probes(probes: List[ProbeResult], final_status: str) -> Dict[str, str]:
    if to_text(final_status).lower() != "live":
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
        if probe and is_valid_profile_name(probe.profile_name):
            return {
                "profileName": clean_profile_name_candidate(probe.profile_name),
                "profileNameSource": source,
            }

    for probe in probes:
        if is_valid_profile_name(probe.profile_name):
            return {
                "profileName": clean_profile_name_candidate(probe.profile_name),
                "profileNameSource": probe.name,
            }

    return {"profileName": "", "profileNameSource": ""}


def _resolve_profile_name_from_graph(uid_raw: Optional[str], fetcher: Optional[Callable] = None) -> str:
    uid = normalize_uid(uid_raw)
    if not uid or not FB_PUBLIC_APP_TOKEN:
        return ""
    url = (
        "https://graph.facebook.com/" + quote(uid)
        + "?fields=name&access_token=" + quote(FB_PUBLIC_APP_TOKEN)
    )
    try:
        response = get_text(url, fetcher)
        payload = json.loads(response.get("text") or "{}")
        if not isinstance(payload, dict):
            return ""
        name = payload.get("name")
        if is_valid_profile_name(name):
            return clean_profile_name_candidate(name)
        return ""
    except Exception:
        return ""


def enrich_profile_name_for_live_profile(
    profile_url: str,
    uid: str,
    username: str,
    default_name_probe_cookies: Optional[Dict[str, str]] = None,
    fetcher: Optional[Callable] = None,
) -> Dict[str, str]:
    graph_name = _resolve_profile_name_from_graph(uid, fetcher)
    if graph_name:
        return {"profileName": graph_name, "profileNameSource": "graph_name"}

    normalized_url = normalize_url_input(profile_url)
    candidates: List[str] = []
    if normalized_url:
        candidates.append(normalized_url)
    if uid:
        if username:
            safe_username = quote(to_text(username).strip().lstrip("@").strip("/"))
            candidates.append(f"https://www.facebook.com/{safe_username}")
            candidates.append(f"https://m.facebook.com/{safe_username}")
            candidates.append(f"https://touch.facebook.com/{safe_username}")
        candidates.append(f"https://m.facebook.com/profile.php?id={uid}")
        candidates.append(f"https://touch.facebook.com/profile.php?id={uid}")
        candidates.append(f"https://www.facebook.com/profile.php?id={uid}")
    elif username:
        safe_username = quote(to_text(username).strip().lstrip("@").strip("/"))
        candidates.append(f"https://m.facebook.com/{safe_username}")
        candidates.append(f"https://touch.facebook.com/{safe_username}")
        candidates.append(f"https://www.facebook.com/{safe_username}")

    seen = set()
    unique_candidates: List[str] = []
    for item in candidates:
        key = to_text(item).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        unique_candidates.append(key)

    default_cookies = default_name_probe_cookies or {}
    cookie_rounds: List[Dict[str, Dict[str, str]]] = [{"source": "no_cookie", "cookies": {}}]
    if default_cookies:
        cookie_rounds.append({"source": "with_cookie", "cookies": default_cookies})

    for cookie_round in cookie_rounds:
        cookies = cookie_round.get("cookies") or {}
        cookie_source = to_text(cookie_round.get("source")) or "no_cookie"
        for index, url in enumerate(unique_candidates[:3], start=1):
            probe = public_profile_probe(f"name_enrich_{index}", url, fetcher, cookies=cookies)
            if is_valid_profile_name(probe.profile_name):
                return {
                    "profileName": clean_profile_name_candidate(probe.profile_name),
                    "profileNameSource": f"name_enrich_{index}:{cookie_source}",
                }
    return {"profileName": "", "profileNameSource": ""}
