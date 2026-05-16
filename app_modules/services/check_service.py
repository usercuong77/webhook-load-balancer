import json
import logging
import time
from typing import Dict, Optional

import requests

from app_modules.config import (
    TELEGRAM_RELAY_TARGET_URL,
    TELEGRAM_RELAY_TIMEOUT_SEC,
    UID_CHECKER_API_KEY,
    UID_CHECKER_FB_COOKIES_JSON,
)
from app_modules.parsers.facebook_url import normalize_input as parser_normalize_input
from app_modules.parsers.facebook_url import to_text
from app_modules.parsers.profile_name import build_profile_name_from_username_slug
from app_modules.probes import live_die_probes as probe_core
from app_modules.resolvers.uid_resolver import (
    resolve_uid_for_check as resolver_resolve_uid_for_check,
    resolve_uid_from_facebook_url_debug as resolver_resolve_uid_from_facebook_url_debug,
)


VERSION = "step18_username_cookie_strict_uid_2026_05_17"

LOGGER = logging.getLogger("checker.check_service")
if not LOGGER.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

PROBE_MODE_TO_NAME = {
    "1": "graph_picture_primary",
    "2": "graph_picture_app_token",
    "3": "graphql_node",
    "4": "external_checker",
    "5": "html_mobile_fallback",
}
PROBE_NAME_TO_MODE = dict((name, mode) for mode, name in PROBE_MODE_TO_NAME.items())
ALL_PROBE_MODES = ("1", "2", "3", "4", "5")
ALL_PROBE_NAMES = tuple(PROBE_MODE_TO_NAME[mode] for mode in ALL_PROBE_MODES)
PROBE_MODE_SYNTAX = "1|2|3|4|5|all"
PROFILE_NAME_CACHE_TTL_SEC = 3 * 24 * 3600
PROFILE_NAME_CACHE_MAX_ITEMS = 2000
PROFILE_NAME_CACHE = {}


def _now_ms() -> int:
    return int(time.time() * 1000)


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


def _log_probe_diagnostics(raw_input: str, uid: str, status: str, chosen_source: str, probes, elapsed_ms: int):
    error_reasons = []
    for probe in probes:
        reason = to_text(getattr(probe, "reason", ""))
        http_status = int(getattr(probe, "http_status", 0) or 0)
        if "fetch_error" in reason or http_status >= 500:
            error_reasons.append(f"{probe.name}:{reason}:{http_status}")
    if status == "unknown" or error_reasons:
        payload = {
            "event": "check_probe_diag",
            "status": status,
            "source": chosen_source,
            "uid": uid,
            "elapsedMs": elapsed_ms,
            "input": to_text(raw_input)[:160],
            "errors": error_reasons[:6],
        }
        LOGGER.warning(json.dumps(payload, ensure_ascii=False))


def _normalize_probe_mode(mode_raw: Optional[str]) -> str:
    mode = to_text(mode_raw).strip().lower()
    if not mode or mode == "all" or mode == "*":
        return "all"
    if mode in PROBE_MODE_TO_NAME:
        return mode
    if mode in PROBE_NAME_TO_MODE:
        return PROBE_NAME_TO_MODE[mode]
    return "all"


def _probe_names_for_mode(mode_key: str):
    if mode_key == "all":
        return list(ALL_PROBE_NAMES)
    probe_name = PROBE_MODE_TO_NAME.get(mode_key)
    return [probe_name] if probe_name else list(ALL_PROBE_NAMES)


def _run_probe_by_name(probe_name: str, uid: str, profile_url: str, username: str, fetcher=None):
    if probe_name == "graph_picture_primary":
        return probe_core.graph_picture_primary_probe(uid, fetcher)
    if probe_name == "graph_picture_app_token":
        return probe_core.graph_picture_app_token_probe(uid, fetcher)
    if probe_name == "graphql_node":
        return probe_core.graphql_node_probe(uid, fetcher)
    if probe_name == "external_checker":
        return probe_core.external_checker_probe(uid, profile_url, fetcher)
    if probe_name == "html_mobile_fallback":
        return probe_core.html_mobile_fallback_probe(profile_url, uid, username, fetcher)
    return probe_core.html_mobile_fallback_probe(profile_url, uid, username, fetcher)


def _force_binary_status(
    chosen_raw: Dict,
    uid: str,
    profile_url: str,
    username: str,
    fetcher=None,
) -> Dict:
    chosen = chosen_raw if isinstance(chosen_raw, dict) else {}
    current_status = to_text(chosen.get("status")).strip().lower()
    if current_status in ("live", "dead"):
        return chosen

    fallback_probe = probe_core.html_mobile_fallback_probe(profile_url, uid, username, fetcher)
    if fallback_probe.status in ("live", "dead"):
        return {
            "status": fallback_probe.status,
            "confidence": fallback_probe.confidence or "weak",
            "source": "binary_fallback_html_mobile",
            "httpStatus": fallback_probe.http_status,
            "reason": "binary_fallback:" + to_text(fallback_probe.reason or "html_mobile"),
        }

    if uid:
        graph_probe = probe_core.graph_picture_app_token_probe(uid, fetcher)
        if graph_probe.status in ("live", "dead"):
            return {
                "status": graph_probe.status,
                "confidence": graph_probe.confidence or "weak",
                "source": "binary_fallback_graph_app",
                "httpStatus": graph_probe.http_status,
                "reason": "binary_fallback:" + to_text(graph_probe.reason or "graph_app"),
            }

    return {
        "status": "dead",
        "confidence": "weak",
        "source": to_text(chosen.get("source")) or "binary_fallback_forced_dead",
        "httpStatus": int(chosen.get("httpStatus") or 0),
        "reason": "binary_forced_dead:" + to_text(chosen.get("reason") or "no_stable_signal"),
    }


def _profile_name_cache_get(uid_raw: Optional[str]) -> str:
    uid = to_text(uid_raw).strip()
    if not uid:
        return ""
    item = PROFILE_NAME_CACHE.get(uid)
    if not item:
        return ""
    expires_at = int(item.get("expiresAt") or 0)
    if expires_at and expires_at < int(time.time()):
        PROFILE_NAME_CACHE.pop(uid, None)
        return ""
    name = to_text(item.get("name")).strip()
    return name if name else ""


def _profile_name_cache_put(uid_raw: Optional[str], profile_name_raw: Optional[str]) -> None:
    uid = to_text(uid_raw).strip()
    profile_name = to_text(profile_name_raw).strip()
    if not uid or not profile_name:
        return
    PROFILE_NAME_CACHE[uid] = {
        "name": profile_name,
        "expiresAt": str(int(time.time()) + PROFILE_NAME_CACHE_TTL_SEC),
    }
    if len(PROFILE_NAME_CACHE) > PROFILE_NAME_CACHE_MAX_ITEMS:
        keys = list(PROFILE_NAME_CACHE.keys())
        trim = len(PROFILE_NAME_CACHE) - PROFILE_NAME_CACHE_MAX_ITEMS
        for key in keys[:trim]:
            PROFILE_NAME_CACHE.pop(key, None)


def _run_check_pipeline(raw_input: str, fetcher=None, probe_mode_raw: Optional[str] = "all") -> Dict:
    started = _now_ms()
    mode_key = _normalize_probe_mode(probe_mode_raw)
    selected_probe_names = _probe_names_for_mode(mode_key)
    normalized = parser_normalize_input(raw_input)
    if not normalized.get("ok"):
        return {
            "ok": False,
            "error": normalized.get("error", "invalid_input"),
            "status": "unknown",
            "confidence": "weak",
            "input": raw_input,
            "requestedProbeMode": to_text(probe_mode_raw).strip().lower() or "all",
            "appliedProbeMode": mode_key,
            "enabledProbes": selected_probe_names,
            "elapsedMs": _now_ms() - started,
        }

    username = normalized.get("username", "")
    resolved = resolver_resolve_uid_for_check(normalized, fetcher)
    uid = to_text(resolved.get("uid")).strip()
    uid_source = to_text(resolved.get("source")).strip()
    profile_url = to_text(resolved.get("profileUrl") or normalized.get("profileUrl")).strip()
    if not username:
        username = to_text(resolved.get("username")).strip()

    probes = []
    for probe_name in selected_probe_names:
        probes.append(_run_probe_by_name(probe_name, uid, profile_url, username, fetcher))

    chosen = probe_core.choose_result(probes)
    chosen = _force_binary_status(chosen, uid, profile_url, username, fetcher)
    profile_name_pick = probe_core.pick_profile_name_from_probes(probes, chosen["status"])
    if not profile_name_pick["profileName"] and to_text(chosen["status"]).lower() == "live":
        enriched_name = probe_core.enrich_profile_name_for_live_profile(
            profile_url,
            uid,
            username,
            DEFAULT_NAME_PROBE_COOKIES,
            fetcher,
        )
        if enriched_name["profileName"]:
            profile_name_pick = enriched_name

    if not profile_name_pick["profileName"] and to_text(chosen["status"]).lower() == "live":
        fallback_name = build_profile_name_from_username_slug(username)
        if fallback_name:
            profile_name_pick = {"profileName": fallback_name, "profileNameSource": "username_slug"}

    if to_text(chosen["status"]).lower() == "live":
        if profile_name_pick["profileName"] and profile_name_pick.get("profileNameSource") != "username_slug":
            _profile_name_cache_put(uid, profile_name_pick["profileName"])
        elif uid:
            cached_name = _profile_name_cache_get(uid)
            if cached_name:
                profile_name_pick = {"profileName": cached_name, "profileNameSource": "uid_name_cache"}

    elapsed_ms = _now_ms() - started
    _log_probe_diagnostics(raw_input, uid, chosen["status"], chosen["source"], probes, elapsed_ms)

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
        "requestedProbeMode": to_text(probe_mode_raw).strip().lower() or "all",
        "appliedProbeMode": mode_key,
        "enabledProbes": selected_probe_names,
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
        "elapsedMs": elapsed_ms,
    }


def check_live_die(raw_input: str, fetcher=None, probe_mode: Optional[str] = "all") -> Dict:
    return _run_check_pipeline(raw_input, fetcher, probe_mode_raw=probe_mode)


def build_root_status() -> Dict:
    return {
        "ok": True,
        "service": "bot-new-scratch-checker",
        "version": VERSION,
        "architecture": "modular",
        "features": ["/check", "/get-uid", "/webhook/telegram"],
        "pipeline": ["parse_input", "resolve_uid", "probe_live_die", "choose_result", "enrich_name"],
        "liveDieProbeCount": 5,
        "liveDieProbes": [
            "graph_picture_primary",
            "graph_picture_app_token",
            "graphql_node",
            "external_checker",
            "html_mobile_fallback",
        ],
        "probeModeSyntax": PROBE_MODE_SYNTAX,
        "nameProbeCookieConfigured": bool(DEFAULT_NAME_PROBE_COOKIES),
        "telegramRelayConfigured": bool(TELEGRAM_RELAY_TARGET_URL),
    }


def health_status() -> Dict:
    return {"ok": True, "version": VERSION}


def check_from_payload(payload: Optional[Dict]) -> Dict:
    safe_payload = payload if isinstance(payload, dict) else {}
    raw_input = safe_payload.get("input") or safe_payload.get("url") or safe_payload.get("uid") or ""
    probe_mode = safe_payload.get("probeMode") or safe_payload.get("mode") or "all"
    return _run_check_pipeline(raw_input, probe_mode_raw=probe_mode)


def check_from_query(query: Dict) -> Dict:
    raw_input = query.get("input") or query.get("url") or query.get("uid") or ""
    probe_mode = query.get("probeMode") or query.get("mode") or "all"
    return _run_check_pipeline(raw_input, probe_mode_raw=probe_mode)


def get_uid_payload(url: str, debug_mode: bool = False) -> Dict:
    result = resolver_resolve_uid_from_facebook_url_debug(url)
    payload = {
        "ok": bool(result.get("uid")),
        "uid": to_text(result.get("uid")),
        "source": to_text(result.get("source")),
        "url": to_text(url).strip(),
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
        LOGGER.warning("telegram_relay_exception: %s", exc)
        return {"ok": False, "error": f"telegram_relay_exception:{exc}", "statusCode": 502}

    status_code = int(getattr(upstream, "status_code", 0) or 0)
    if 200 <= status_code < 300:
        return {"ok": True, "statusCode": status_code}
    return {
        "ok": False,
        "error": "telegram_relay_http_error",
        "statusCode": status_code or 502,
        "upstreamBody": to_text(getattr(upstream, "text", ""))[:500],
    }


def is_api_key_valid(provided_raw: Optional[str]) -> bool:
    if not UID_CHECKER_API_KEY:
        return True
    provided = to_text(provided_raw).strip()
    return provided == UID_CHECKER_API_KEY
