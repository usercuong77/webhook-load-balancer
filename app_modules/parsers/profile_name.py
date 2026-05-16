import html as html_lib
import re
import unicodedata
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
)


def clean_profile_name_candidate(raw_name: Optional[str]) -> str:
    name = html_lib.unescape("" if raw_name is None else str(raw_name))
    if not name:
        return ""
    name = re.sub(r"<[^>]+>", " ", name)
    name = re.sub(r"\s+", " ", name).strip(" \t\r\n-â€“|")
    name = re.sub(r"\s+\|\s*facebook.*$", "", name, flags=re.I)
    name = re.sub(r"\s*-\s*facebook.*$", "", name, flags=re.I)
    name = re.sub(r"\s*Â·\s*facebook.*$", "", name, flags=re.I)
    return re.sub(r"\s+", " ", name).strip()


def _latin_fold(raw: Optional[str]) -> str:
    text = "" if raw is None else str(raw)
    text = text.replace("đ", "d").replace("Đ", "D")
    normalized = unicodedata.normalize("NFD", text)
    return "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")


def is_valid_profile_name(raw_name: Optional[str]) -> bool:
    name = clean_profile_name_candidate(raw_name)
    if len(name) < 2 or len(name) > 90:
        return False
    low = name.lower()
    low_folded = _latin_fold(low)
    for marker in PROFILE_NAME_BLOCKLIST:
        marker_low = marker.lower()
        marker_folded = _latin_fold(marker_low)
        if marker_low in low or marker_folded in low_folded:
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


def build_profile_name_from_username_slug(username_raw: Optional[str]) -> str:
    username = ("" if username_raw is None else str(username_raw)).strip().lstrip("@").strip("/")
    if not username:
        return ""
    if re.fullmatch(r"\d{8,20}", username):
        return ""
    normalized = re.sub(r"[._-]+", " ", username).strip()
    normalized = re.sub(r"\s+", " ", normalized)
    if len(normalized) < 2:
        return ""
    words = [word for word in normalized.split(" ") if word]
    candidate = " ".join(word[:1].upper() + word[1:] for word in words)
    return clean_profile_name_candidate(candidate) if is_valid_profile_name(candidate) else ""
