import json
from typing import Callable, Dict, Optional
from urllib.parse import quote

from app_modules.config import FB_PUBLIC_APP_TOKEN
from app_modules.http_client import get_text, request_text
from app_modules.parsers.facebook_url import (
    build_facebook_probe_urls,
    build_uid_probe_header_candidates,
    extract_uid_from_html,
    extract_uid_from_url,
    extract_username_slug_from_url,
    normalize_uid,
)


def to_text(value) -> str:
    return "" if value is None else str(value)


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
    direct_uid = extract_uid_from_url(url_raw)
    if direct_uid:
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
    for headers in build_uid_probe_header_candidates():
        for probe_url in probe_urls:
            try:
                response = request_text("get", probe_url, fetcher=fetcher, headers=headers)
            except Exception:
                attempts.append(
                    {
                        "url": probe_url,
                        "status": 0,
                        "ua": to_text(headers.get("User-Agent"))[:80],
                        "error": "request_exception",
                    }
                )
                continue

            final_url = to_text(response.get("url"))
            uid_html = extract_uid_from_html(response.get("text"))
            uid_final = extract_uid_from_url(final_url)
            resolved_username = extract_username_slug_from_url(final_url)
            attempts.append(
                {
                    "url": probe_url,
                    "status": int(response.get("status_code") or 0),
                    "finalUrl": final_url,
                    "ua": to_text(headers.get("User-Agent"))[:80],
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
        "resolvedUsername": extract_username_slug_from_url(url_raw),
        "resolvedUrl": to_text(url_raw).strip(),
    }


def resolve_uid_from_facebook_url(url_raw: Optional[str], fetcher: Optional[Callable] = None) -> str:
    return to_text(resolve_uid_from_facebook_url_debug(url_raw, fetcher).get("uid")).strip()


def resolve_uid_for_check(normalized: Dict, fetcher: Optional[Callable] = None) -> Dict:
    uid = normalize_uid(normalized.get("uid"))
    username = to_text(normalized.get("username")).strip()
    profile_url = to_text(normalized.get("profileUrl")).strip()

    if uid:
        derived_username = username
        canonical_url = f"https://www.facebook.com/profile.php?id={uid}"
        if not derived_username:
            debug_result = resolve_uid_from_facebook_url_debug(canonical_url, fetcher)
            derived_username = to_text(debug_result.get("resolvedUsername")).strip()
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

    if not resolved_uid and username:
        resolved_uid = resolve_uid_from_graph_username(username, fetcher)
        if resolved_uid:
            resolve_source = "graph_username"

    if not resolved_uid and username:
        fallback_url = "https://www.facebook.com/" + quote(username)
        debug_result = resolve_uid_from_facebook_url_debug(fallback_url, fetcher)
        resolved_uid = to_text(debug_result.get("uid")).strip()
        if not resolved_username:
            resolved_username = to_text(debug_result.get("resolvedUsername")).strip()
        if resolved_uid:
            resolve_source = to_text(debug_result.get("source")).strip() or "username_probe"

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
