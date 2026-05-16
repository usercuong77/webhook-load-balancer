import html as html_lib
import re
from typing import Optional


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


def clean_profile_name_candidate(raw_name: Optional[str]) -> str:
    name = html_lib.unescape("" if raw_name is None else str(raw_name))
    if not name:
        return ""
    name = re.sub(r"<[^>]+>", " ", name)
    name = re.sub(r"\s+", " ", name).strip(" \t\r\n-–|")
    name = re.sub(r"\s+\|\s*facebook.*$", "", name, flags=re.I)
    name = re.sub(r"\s*-\s*facebook.*$", "", name, flags=re.I)
    name = re.sub(r"\s*·\s*facebook.*$", "", name, flags=re.I)
    return re.sub(r"\s+", " ", name).strip()


def is_valid_profile_name(raw_name: Optional[str]) -> bool:
    name = clean_profile_name_candidate(raw_name)
    if len(name) < 2 or len(name) > 90:
        return False
    low = name.lower()
    for marker in PROFILE_NAME_BLOCKLIST:
        if marker in low:
            return False
    return any(ch.isalpha() for ch in name)


def extract_profile_name_from_html(html_raw: Optional[str]) -> str:
    html = "" if html_raw is None else str(html_raw)
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
        candidate = clean_profile_name_candidate(match.group(1))
        if is_valid_profile_name(candidate):
            return candidate
    return ""
