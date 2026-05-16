import json
import os
import time
from typing import Callable, Dict, Optional
from urllib.parse import quote

from app_modules.config import FB_PUBLIC_APP_TOKEN, UID_CHECKER_FB_COOKIES_JSON
from app_modules.http_client import get_text, request_text
from app_modules.parsers.facebook_url import (
    build_facebook_probe_urls,
    build_uid_probe_header_candidates,
    extract_share_token,
    extract_uid_from_html,
    extract_uid_from_url,
    extract_username_from_login_next,
    extract_username_slug_from_url,
    normalize_url_input,
    normalize_uid,
)


def to_text(value) -> str:
    return "" if value is None else str(value)


UID_RESOLVE_CACHE_TTL_SEC = max(60, int(os.getenv("UID_RESOLVE_CACHE_TTL_SEC", "21600")))
UID_RESOLVE_CACHE_MAX_ITEMS = max(100, int(os.getenv("UID_RESOLVE_CACHE_MAX_ITEMS", "2000")))
UID_RESOLVE_CACHE: Dict[str, Dict[str, str]] = {}


def _normalize_cookie_map(cookies_raw) -> Dict[str, str]:
    if not isinstance(cookies_raw, dict):
        return {}
    out: Dict[str, str] = {}
    for key, value in cookies_raw.items():
        cookie_key = to_text(key).strip()
        cookie_value = to_text(value).strip()
        if cookie_key and cookie_value:
            out[cookie_key] = cookie_value
    return out


def _load_default_uid_probe_cookies() -> Dict[str, str]:
    raw = to_text(UID_CHECKER_FB_COOKIES_JSON).strip()
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


DEFAULT_UID_PROBE_COOKIES = _load_default_uid_probe_cookies()


def _uid_cache_get(cache_key_raw: Optional[str]) -> Optional[Dict[str, str]]:
    cache_key = to_text(cache_key_raw).strip().lower()
    if not cache_key:
        return None
    item = UID_RESOLVE_CACHE.get(cache_key)
    if not item:
        return None
    expires_at = int(item.get("expiresAt") or 0)
    if expires_at and expires_at < int(time.time()):
        UID_RESOLVE_CACHE.pop(cache_key, None)
        return None
    uid = normalize_uid(item.get("uid"))
    if not uid:
        UID_RESOLVE_CACHE.pop(cache_key, None)
        return None
    return {"uid": uid, "username": to_text(item.get("username")).strip()}


def _uid_cache_put(cache_key_raw: Optional[str], uid_raw: Optional[str], username_raw: Optional[str] = "") -> None:
    cache_key = to_text(cache_key_raw).strip().lower()
    uid = normalize_uid(uid_raw)
    if not cache_key or not uid:
        return
    UID_RESOLVE_CACHE[cache_key] = {
        "uid": uid,
        "username": to_text(username_raw).strip(),
        "expiresAt": str(int(time.time()) + UID_RESOLVE_CACHE_TTL_SEC),
    }
    if len(UID_RESOLVE_CACHE) > UID_RESOLVE_CACHE_MAX_ITEMS:
        for key in list(UID_RESOLVE_CACHE.keys())[: len(UID_RESOLVE_CACHE) - UID_RESOLVE_CACHE_MAX_ITEMS]:
            UID_RESOLVE_CACHE.pop(key, None)


def resolve_uid_from_graph_username(username_raw: Optional[str], fetcher: Optional[Callable] = None) -> str:
    username = to_text(username_raw).strip().lstrip("@").strip("/")
    if not username:
        return ""
    if not FB_PUBLIC_APP_TOKEN:
        return ""

    graph_url = (
        "https://graph.facebook.com/" + quote(username)
        + "?fields=id&access_token=" + quote(FB_PUBLIC_APP_TOKEN)
    )
    try:
        response = get_text(graph_url, fetcher)
        payload = json.loads(response.get("text") or "{}")
        return normalize_uid(payload.get("id")) if isinstance(payload, dict) else ""
    except Exception:
        return ""


def resolve_uid_from_facebook_url_debug(url_raw: Optional[str], fetcher: Optional[Callable] = None) -> Dict:
    normalized_url = normalize_url_input(url_raw)
    share_token = extract_share_token(normalized_url)
    if share_token:
        cached_share = _uid_cache_get("share:" + share_token.lower())
        if cached_share:
            cached_uid = normalize_uid(cached_share.get("uid"))
            cached_username = to_text(cached_share.get("username")).strip()
            return {
                "uid": cached_uid,
                "source": "share_cache",
                "attempts": [],
                "resolvedUsername": cached_username,
                "resolvedUrl": f"https://www.facebook.com/profile.php?id={cached_uid}",
            }

    slug_from_input = extract_username_slug_from_url(normalized_url)
    if slug_from_input:
        cached_username = _uid_cache_get("username:" + slug_from_input.lower())
        if cached_username:
            cached_uid = normalize_uid(cached_username.get("uid"))
            return {
                "uid": cached_uid,
                "source": "username_cache",
                "attempts": [],
                "resolvedUsername": slug_from_input,
                "resolvedUrl": f"https://www.facebook.com/profile.php?id={cached_uid}",
            }

    direct_uid = extract_uid_from_url(url_raw)
    if direct_uid:
        if share_token:
            _uid_cache_put("share:" + share_token.lower(), direct_uid)
        return {
            "uid": direct_uid,
            "source": "direct_url",
            "attempts": [],
            "resolvedUsername": extract_username_slug_from_url(url_raw),
            "resolvedUrl": f"https://www.facebook.com/profile.php?id={direct_uid}",
        }

    probe_urls = build_facebook_probe_urls(url_raw)
    if not probe_urls:
        return {"uid": "", "source": "no_probe_url", "attempts": [], "resolvedUsername": "", "resolvedUrl": ""}

    attempts = []
    derived_username = extract_username_slug_from_url(url_raw)
    cookie_rounds = [("no_cookie", {})]
    if DEFAULT_UID_PROBE_COOKIES:
        cookie_rounds.append(("with_cookie", DEFAULT_UID_PROBE_COOKIES))

    for cookie_source, cookie_map in cookie_rounds:
        for headers in build_uid_probe_header_candidates():
            for probe_url in probe_urls:
                try:
                    response = request_text(
                        "get",
                        probe_url,
                        fetcher=fetcher,
                        headers=headers,
                        cookies=cookie_map or None,
                    )
                except Exception:
                    attempts.append(
                        {
                            "url": probe_url,
                            "status": 0,
                            "ua": to_text(headers.get("User-Agent"))[:80],
                            "cookieSource": cookie_source,
                            "error": "request_exception",
                        }
                    )
                    continue

                final_url = to_text(response.get("url"))
                uid_html = extract_uid_from_html(response.get("text"))
                uid_final = extract_uid_from_url(final_url)
                resolved_username = extract_username_slug_from_url(final_url) or extract_username_from_login_next(final_url)
                if resolved_username and not derived_username:
                    derived_username = resolved_username
                attempts.append(
                    {
                        "url": probe_url,
                        "status": int(response.get("status_code") or 0),
                        "finalUrl": final_url,
                        "ua": to_text(headers.get("User-Agent"))[:80],
                        "cookieSource": cookie_source,
                        "uidFromHtml": uid_html,
                        "uidFromFinalUrl": uid_final,
                    }
                )
                if uid_html:
                    if share_token:
                        _uid_cache_put("share:" + share_token.lower(), uid_html, resolved_username)
                    if resolved_username:
                        _uid_cache_put("username:" + resolved_username.lower(), uid_html, resolved_username)
                    return {
                        "uid": uid_html,
                        "source": "html_pattern",
                        "attempts": attempts,
                        "resolvedUsername": resolved_username,
                        "resolvedUrl": f"https://www.facebook.com/profile.php?id={uid_html}",
                    }

                if uid_final:
                    if share_token:
                        _uid_cache_put("share:" + share_token.lower(), uid_final, resolved_username)
                    if resolved_username:
                        _uid_cache_put("username:" + resolved_username.lower(), uid_final, resolved_username)
                    return {
                        "uid": uid_final,
                        "source": "final_url",
                        "attempts": attempts,
                        "resolvedUsername": resolved_username,
                        "resolvedUrl": f"https://www.facebook.com/profile.php?id={uid_final}",
                    }

    if derived_username:
        username_probe_url = "https://www.facebook.com/" + quote(derived_username)
        username_cookie_rounds = [("no_cookie", {})]
        if DEFAULT_UID_PROBE_COOKIES:
            username_cookie_rounds.append(("with_cookie", DEFAULT_UID_PROBE_COOKIES))
        for cookie_source, cookie_map in username_cookie_rounds:
            try:
                response = request_text(
                    "get",
                    username_probe_url,
                    fetcher=fetcher,
                    cookies=cookie_map or None,
                )
                final_url = to_text(response.get("url"))
                uid_html = extract_uid_from_html(response.get("text"))
                uid_final = extract_uid_from_url(final_url)
                attempts.append(
                    {
                        "url": username_probe_url,
                        "status": int(response.get("status_code") or 0),
                        "finalUrl": final_url,
                        "ua": "username_direct_probe",
                        "cookieSource": cookie_source,
                        "uidFromHtml": uid_html,
                        "uidFromFinalUrl": uid_final,
                    }
                )
                chosen_uid = uid_html or uid_final
                if chosen_uid:
                    if share_token:
                        _uid_cache_put("share:" + share_token.lower(), chosen_uid, derived_username)
                    if derived_username:
                        _uid_cache_put("username:" + derived_username.lower(), chosen_uid, derived_username)
                    return {
                        "uid": chosen_uid,
                        "source": "username_direct_probe",
                        "attempts": attempts,
                        "resolvedUsername": derived_username,
                        "resolvedUrl": f"https://www.facebook.com/profile.php?id={chosen_uid}",
                    }
            except Exception:
                attempts.append(
                    {
                        "url": username_probe_url,
                        "status": 0,
                        "ua": "username_direct_probe",
                        "cookieSource": cookie_source,
                        "error": "request_exception",
                    }
                )

    return {
        "uid": "",
        "source": "not_found",
        "attempts": attempts,
        "resolvedUsername": derived_username,
        "resolvedUrl": normalize_url_input(url_raw),
    }


def resolve_uid_from_facebook_url(url_raw: Optional[str], fetcher: Optional[Callable] = None) -> str:
    return to_text(resolve_uid_from_facebook_url_debug(url_raw, fetcher).get("uid")).strip()


def resolve_uid_for_check(normalized: Dict, fetcher: Optional[Callable] = None) -> Dict:
    uid = normalize_uid(normalized.get("uid"))
    username = to_text(normalized.get("username")).strip()
    profile_url = to_text(normalized.get("profileUrl")).strip()
    share_token = extract_share_token(profile_url)

    if uid:
        derived_username = username
        canonical_url = f"https://www.facebook.com/profile.php?id={uid}"
        if not derived_username:
            debug_result = resolve_uid_from_facebook_url_debug(canonical_url, fetcher)
            derived_username = to_text(debug_result.get("resolvedUsername")).strip()
        if share_token:
            _uid_cache_put("share:" + share_token.lower(), uid, derived_username)
        if derived_username:
            _uid_cache_put("username:" + derived_username.lower(), uid, derived_username)
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
            debug_result = resolve_uid_from_facebook_url_debug(profile_url, fetcher)
            resolved_uid = to_text(debug_result.get("uid")).strip()
            if not resolved_username:
                resolved_username = to_text(debug_result.get("resolvedUsername")).strip()
            resolved_url = to_text(debug_result.get("resolvedUrl")).strip()
            if resolved_url:
                profile_url = resolved_url
            if resolved_uid:
                resolve_source = to_text(debug_result.get("source")).strip() or "url_probe"
                break

    username_candidate = username or resolved_username
    if not resolved_uid and username_candidate:
        resolved_uid = resolve_uid_from_graph_username(username_candidate, fetcher)
        if resolved_uid:
            resolve_source = "graph_username"

    if not resolved_uid and username_candidate:
        fallback_url = "https://www.facebook.com/" + quote(username_candidate)
        debug_result = resolve_uid_from_facebook_url_debug(fallback_url, fetcher)
        resolved_uid = to_text(debug_result.get("uid")).strip()
        if not resolved_username:
            resolved_username = to_text(debug_result.get("resolvedUsername")).strip()
        if resolved_uid:
            resolve_source = to_text(debug_result.get("source")).strip() or "username_probe"

    if resolved_uid:
        effective_username = username_candidate or resolved_username
        if share_token:
            _uid_cache_put("share:" + share_token.lower(), resolved_uid, effective_username)
        if effective_username:
            _uid_cache_put("username:" + effective_username.lower(), resolved_uid, effective_username)
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
