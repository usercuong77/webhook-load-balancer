"""
Compatibility facade.

Legacy tests/imports may still call `app_modules.live_die`.
Keep this module thin and delegate to the new service layer.
"""

from typing import Dict, Optional

from app_modules.services import check_service


def check_live_die(raw_input: str, fetcher=None) -> Dict:
    return check_service.check_live_die(raw_input, fetcher)


def build_root_status() -> Dict:
    return check_service.build_root_status()


def health_status() -> Dict:
    return check_service.health_status()


def check_from_payload(payload: Optional[Dict]) -> Dict:
    return check_service.check_from_payload(payload)


def check_from_query(query: Dict) -> Dict:
    return check_service.check_from_query(query)


def get_uid_payload(url: str, debug_mode: bool = False) -> Dict:
    return check_service.get_uid_payload(url, debug_mode)


def relay_telegram_webhook(body: bytes, content_type: str) -> Dict:
    return check_service.relay_telegram_webhook(body, content_type)


def is_api_key_valid(provided_raw: Optional[str]) -> bool:
    return check_service.is_api_key_valid(provided_raw)
