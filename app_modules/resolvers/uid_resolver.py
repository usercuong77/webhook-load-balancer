import json
import os
import time
from typing import Callable, Dict, List, Optional
from urllib.parse import quote

import requests

from app_modules.config import FB_PUBLIC_APP_TOKEN, REQUEST_TIMEOUT_SEC, UID_CHECKER_FB_COOKIES_JSON
from app_modules.http_client import get_text, request_text
from app_modules.parsers.facebook_url import (
    build_facebook_probe_urls,
    build_uid_probe_header_candidates,
    extract_share_token,
    extract_uid_from_html,
    extract_uid_from_html_strict,
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


COOKIE_UID_USER_AGENTS = (
    (
        "Mozilla/5.0 (Linux; U; Android 4.0.3; en-us; Galaxy Nexus Build/IML74K) "
        "AppleWebKit/534.30 (KHTML, like Gecko) Version/4.0 Mobile Safari/534.30"
    ),
    (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1"
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
)


def _normalize_cookie_payload(payload) -> List[Dict[str, str]]:
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        if isinstance(payload.get("cookies"), list):
            items = payload.get("cookies") or []
        elif isinstance(payload.get("accounts"), list):
            items = payload.get("accounts") or []
        else:
            items = [payload]
    else:
        items = []

    accounts: List[Dict[str, str]] = []
    for item in items:
        normalized = _normalize_cookie_map(item)
        if normalized and normalized.get("c_user") and normalized.get("xs"):
            accounts.append(normalized)
    return accounts


def _load_uid_probe_cookie_accounts() -> List[Dict[str, str]]:
    raw = to_text(UID_CHECKER_FB_COOKIES_JSON).strip().lstrip("\ufeff")
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except Exception:
        return []
    return _normalize_cookie_payload(parsed)


DEFAULT_UID_PROBE_COOKIE_ACCOUNTS = _load_uid_probe_cookie_accounts()


def uid_cookie_pool_count() -> int:
    return len(DEFAULT_UID_PROBE_COOKIE_ACCOUNTS)


def _mask_cookie_id(cookie_map: Optional[Dict[str, str]]) -> str:
    c_user = to_text((cookie_map or {}).get("c_user")).strip()
    if len(c_user) <= 6:
        return "***" if c_user else ""
    return f"{c_user[:4]}***{c_user[-4:]}"


def _cookie_header(cookie_map: Optional[Dict[str, str]]) -> str:
    parts = []
    for key, value in (cookie_map or {}).items():
        clean_key = to_text(key).strip()
        clean_value = to_text(value).strip()
        if clean_key and clean_value:
            parts.append(f"{clean_key}={clean_value}")
    return "; ".join(parts)


def _cookie_uid_probe_header_candidates(cookie_map: Dict[str, str]) -> List[Dict[str, str]]:
    cookie_value = _cookie_header(cookie_map)
    if not cookie_value:
        return []
    return [
        {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,vi;q=0.8",
            "Cookie": cookie_value,
        }
        for user_agent in COOKIE_UID_USER_AGENTS
    ]


def _cookie_probe_url_priority(url_raw: Optional[str]):
    value = to_text(url_raw).lower()
    if "mbasic.facebook.com" in value:
        return (0, value)
    if "m.facebook.com" in value:
        return (1, value)
    return (2, value)


def _fetch_text_with_cookie_header(
    url: str,
    headers: Dict[str, str],
    fetcher: Optional[Callable] = None,
) -> Dict:
    if fetcher is not None:
        return request_text("get", url, fetcher=fetcher, headers=headers)
    try:
        response = requests.get(
            url,
            headers=dict(headers),
            timeout=max(5, REQUEST_TIMEOUT_SEC),
            allow_redirects=True,
        )
        return {
            "status_code": int(getattr(response, "status_code", 0) or 0),
            "url": to_text(getattr(response, "url", url)),
            "text": to_text(getattr(response, "text", "")),
        }
    except requests.RequestException:
        return {"status_code": 0, "url": url, "text": ""}


def _resolve_uid_with_cookie_pool(url_raw: Optional[str], fetcher: Optional[Callable] = None) -> Dict:
    probe_urls = sorted(build_facebook_probe_urls(url_raw), key=_cookie_probe_url_priority)
    attempts = []
    if not probe_urls:
        return {"uid": "", "source": "uid_cookie_pool", "attempts": attempts, "resolvedUsername": "", "resolvedUrl": ""}
    if not DEFAULT_UID_PROBE_COOKIE_ACCOUNTS:
        return {
            "uid": "",
            "source": "uid_cookie_pool",
            "attempts": attempts,
            "reason": "no_usable_cookie_accounts",
            "resolvedUsername": "",
            "resolvedUrl": normalize_url_input(url_raw),
        }

    for index, cookie_map in enumerate(DEFAULT_UID_PROBE_COOKIE_ACCOUNTS):
        account_uid = normalize_uid(cookie_map.get("c_user"))
        for probe_url in probe_urls:
            for headers in _cookie_uid_probe_header_candidates(cookie_map):
                response = _fetch_text_with_cookie_header(probe_url, headers, fetcher)
                final_url = to_text(response.get("url"))
                response_text = to_text(response.get("text"))
                uid_html = extract_uid_from_html(response_text)
                uid_final = extract_uid_from_url(final_url)
                if uid_html and uid_html == account_uid:
                    uid_html = ""
                if uid_final and uid_final == account_uid:
                    uid_final = ""
                resolved_username = extract_username_slug_from_url(final_url) or extract_username_from_login_next(final_url)
                attempt = {
                    "url": probe_url,
                    "status": int(response.get("status_code") or 0),
                    "finalUrl": final_url,
                    "ua": to_text(headers.get("User-Agent"))[:80],
                    "cookieSource": "cookie_pool",
                    "cookieIndex": index,
                    "cookieAccount": _mask_cookie_id(cookie_map),
                    "uidFromHtml": uid_html,
                    "uidFromFinalUrl": uid_final,
                }
                attempts.append(attempt)
                chosen_uid = uid_html or uid_final
                if chosen_uid:
                    return {
                        "uid": chosen_uid,
                        "source": "uid_cookie_pool",
                        "attempts": attempts,
                        "resolvedUsername": resolved_username,
                        "resolvedUrl": f"https://www.facebook.com/profile.php?id={chosen_uid}",
                    }

    return {
        "uid": "",
        "source": "uid_cookie_pool",
        "attempts": attempts,
        "reason": "uid_not_found_after_cookie_pool",
        "resolvedUsername": "",
        "resolvedUrl": normalize_url_input(url_raw),
    }


def _is_login_like_url(url_raw: Optional[str]) -> bool:
    url = to_text(url_raw).strip().lower()
    if not url:
        return True
    return (
        "/login" in url
        or "/login.php" in url
        or "checkpoint" in url
        or "recover" in url
    )


def _is_cookie_uid_candidate_safe(
    slug_raw: Optional[str],
    final_url_raw: Optional[str],
    response_html_raw: Optional[str],
    resolved_username_raw: Optional[str],
) -> bool:
    slug = to_text(slug_raw).strip().lower()
    if not slug:
        return True
    final_url = to_text(final_url_raw).strip().lower()
    resolved_username = to_text(resolved_username_raw).strip().lower()
    response_html = to_text(response_html_raw).strip().lower()

    if _is_login_like_url(final_url):
        return False
    if resolved_username and resolved_username == slug:
        return True
    if ("/" + slug) in final_url:
        return True
    if response_html and slug in response_html:
        # Accept when body still references the exact requested slug.
        return True
    return False


def _verify_uid_matches_slug_with_cookie(
    uid_raw: Optional[str],
    slug_raw: Optional[str],
    cookie_map: Optional[Dict[str, str]],
    fetcher: Optional[Callable] = None,
) -> bool:
    uid = normalize_uid(uid_raw)
    slug = to_text(slug_raw).strip().lower()
    cookies = cookie_map if isinstance(cookie_map, dict) else {}
    if not uid or not slug or not cookies:
        return False
    probe_url = f"https://www.facebook.com/profile.php?id={uid}"
    try:
        response = request_text(
            "get",
            probe_url,
            fetcher=fetcher,
            headers={"User-Agent": "Mozilla/5.0"},
            cookies=cookies,
        )
    except Exception:
        return False
    final_url = to_text(response.get("url"))
    if _is_login_like_url(final_url):
        return False
    canonical_slug = extract_username_slug_from_url(final_url) or extract_username_from_login_next(final_url)
    canonical_slug = to_text(canonical_slug).strip().lower()
    if canonical_slug:
        return canonical_slug == slug
    # If canonical URL doesn't expose slug, fallback to HTML reference check.
    body_lower = to_text(response.get("text")).lower()
    return ("/" + slug) in body_lower


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
                response_text = response.get("text")
                uid_html = extract_uid_from_html(response_text)
                if cookie_source == "with_cookie" and slug_from_input:
                    strict_uid = extract_uid_from_html_strict(response_text)
                    uid_html = strict_uid or ""
                uid_final = extract_uid_from_url(final_url)
                resolved_username = extract_username_slug_from_url(final_url) or extract_username_from_login_next(final_url)
                if slug_from_input and resolved_username:
                    if resolved_username.lower() != slug_from_input.lower():
                        resolved_username = ""
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
                    if cookie_source == "with_cookie" and slug_from_input:
                        if not _is_cookie_uid_candidate_safe(
                            slug_from_input,
                            final_url,
                            response_text,
                            resolved_username,
                        ):
                            continue
                        if not _verify_uid_matches_slug_with_cookie(
                            uid_html,
                            slug_from_input,
                            cookie_map,
                            fetcher,
                        ):
                            continue
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
                    if cookie_source == "with_cookie" and slug_from_input:
                        if not _is_cookie_uid_candidate_safe(
                            slug_from_input,
                            final_url,
                            response_text,
                            resolved_username,
                        ):
                            continue
                        if not _verify_uid_matches_slug_with_cookie(
                            uid_final,
                            slug_from_input,
                            cookie_map,
                            fetcher,
                        ):
                            continue
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

    cookie_pool_result = _resolve_uid_with_cookie_pool(url_raw, fetcher)
    cookie_pool_attempts = cookie_pool_result.get("attempts") or []
    if cookie_pool_attempts:
        attempts.extend(cookie_pool_attempts)
    cookie_pool_uid = normalize_uid(cookie_pool_result.get("uid"))
    if cookie_pool_uid:
        resolved_username = to_text(cookie_pool_result.get("resolvedUsername")).strip()
        if share_token:
            _uid_cache_put("share:" + share_token.lower(), cookie_pool_uid, resolved_username)
        if resolved_username:
            _uid_cache_put("username:" + resolved_username.lower(), cookie_pool_uid, resolved_username)
        return {
            "uid": cookie_pool_uid,
            "source": "uid_cookie_pool",
            "attempts": attempts,
            "resolvedUsername": resolved_username,
            "resolvedUrl": f"https://www.facebook.com/profile.php?id={cookie_pool_uid}",
        }

    if derived_username:
        username_probe_url = "https://www.facebook.com/" + quote(derived_username)
        username_cookie_rounds = [("no_cookie", {})]
        for cookie_source, cookie_map in username_cookie_rounds:
            try:
                response = request_text(
                    "get",
                    username_probe_url,
                    fetcher=fetcher,
                    cookies=cookie_map or None,
                )
                final_url = to_text(response.get("url"))
                response_text = response.get("text")
                uid_html = extract_uid_from_html(response_text)
                if cookie_source == "with_cookie" and slug_from_input:
                    strict_uid = extract_uid_from_html_strict(response_text)
                    uid_html = strict_uid or ""
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
                    if cookie_source == "with_cookie" and slug_from_input:
                        if not _is_cookie_uid_candidate_safe(
                            slug_from_input,
                            final_url,
                            response_text,
                            derived_username or slug_from_input,
                        ):
                            continue
                        if not _verify_uid_matches_slug_with_cookie(
                            chosen_uid,
                            slug_from_input,
                            cookie_map,
                            fetcher,
                        ):
                            continue
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
        debug_result = resolve_uid_from_facebook_url_debug(profile_url, fetcher)
        resolved_uid = to_text(debug_result.get("uid")).strip()
        if not resolved_username:
            resolved_username = to_text(debug_result.get("resolvedUsername")).strip()
        resolved_url = to_text(debug_result.get("resolvedUrl")).strip()
        if resolved_url:
            profile_url = resolved_url
        if resolved_uid:
            resolve_source = to_text(debug_result.get("source")).strip() or "url_probe"

    username_candidate = username or resolved_username
    if not resolved_uid and username_candidate:
        resolved_uid = resolve_uid_from_graph_username(username_candidate, fetcher)
        if resolved_uid:
            resolve_source = "graph_username"

    if not resolved_uid and username_candidate:
        fallback_url = "https://www.facebook.com/" + quote(username_candidate)
        if normalize_url_input(fallback_url).rstrip("/") != normalize_url_input(profile_url).rstrip("/"):
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
