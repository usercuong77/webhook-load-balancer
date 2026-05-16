"""
Phase-1 split layer.

This module is the single service entrypoint used by controllers.
Current implementation delegates to legacy core (live_die) to keep
runtime behavior stable while we continue splitting internals.
"""

from typing import Dict, Optional

from app_modules import live_die as legacy_core


def build_root_status() -> Dict:
    payload = legacy_core.build_root_status()
    payload["serviceLayer"] = "check_service_v1"
    return payload


def health_status() -> Dict:
    return legacy_core.health_status()


def check_from_payload(payload: Optional[Dict]) -> Dict:
    return legacy_core.check_from_payload(payload)


def check_from_query(query: Dict) -> Dict:
    return legacy_core.check_from_query(query)


def get_uid_payload(url: str, debug_mode: bool = False) -> Dict:
    return legacy_core.get_uid_payload(url, debug_mode)


def relay_telegram_webhook(body: bytes, content_type: str) -> Dict:
    return legacy_core.relay_telegram_webhook(body, content_type)


def is_api_key_valid(provided_raw: Optional[str]) -> bool:
    return legacy_core.is_api_key_valid(provided_raw)
