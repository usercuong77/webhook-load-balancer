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


VERSION = "step13_modular_core_extract_share_cache_2026_05_16"

LOGGER = logging.getLogger("checker.check_service")
if not LOGGER.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


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


def _run_check_pipeline(raw_input: str, fetcher=None) -> Dict:
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
    uid = to_text(resolved.get("uid")).strip()
    uid_source = to_text(resolved.get("source")).strip()
    profile_url = to_text(resolved.get("profileUrl") or normalized.get("profileUrl")).strip()
    if not username:
        username = to_text(resolved.get("username")).strip()

    probes = [
        probe_core.graph_picture_primary_probe(uid, fetcher),
        probe_core.graph_picture_app_token_probe(uid, fetcher),
        probe_core.graphql_node_probe(uid, fetcher),
        probe_core.external_checker_probe(uid, profile_url, fetcher),
        probe_core.html_mobile_fallback_probe(profile_url, uid, username, fetcher),
    ]

    chosen = probe_core.choose_result(probes)
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


def check_live_die(raw_input: str, fetcher=None) -> Dict:
    return _run_check_pipeline(raw_input, fetcher)


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
        "nameProbeCookieConfigured": bool(DEFAULT_NAME_PROBE_COOKIES),
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
