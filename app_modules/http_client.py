from typing import Callable, Dict, Optional

import requests

from app_modules.config import REQUEST_TIMEOUT_SEC, USER_AGENT


def to_text(value) -> str:
    return "" if value is None else str(value)


def normalize_cookie_map(cookies_raw) -> Dict[str, str]:
    if not isinstance(cookies_raw, dict):
        return {}
    out: Dict[str, str] = {}
    for key, value in cookies_raw.items():
        cookie_key = to_text(key).strip()
        cookie_value = to_text(value).strip()
        if cookie_key and cookie_value:
            out[cookie_key] = cookie_value
    return out


def request_text(
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
    request_cookies = normalize_cookie_map(cookies)
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
        "text": to_text(getattr(response, "text", ""))[:400000],
        "headers": dict(getattr(response, "headers", {}) or {}),
    }


def get_text(
    url: str,
    fetcher: Optional[Callable] = None,
    headers: Optional[Dict] = None,
    cookies: Optional[Dict[str, str]] = None,
) -> Dict:
    return request_text("get", url, fetcher=fetcher, headers=headers, cookies=cookies)
