import re
from typing import Dict, List, Optional
from urllib.parse import parse_qs, quote, unquote, urlparse

from app_modules.config import FALLBACK_UID_PROBE_USER_AGENTS


FACEBOOK_RESERVED_PATH_PREFIXES = {
    "profile.php",
    "people",
    "share",
    "photo",
    "photos",
    "posts",
    "permalink.php",
    "story.php",
    "watch",
    "reel",
    "reels",
    "groups",
    "pages",
    "events",
    "marketplace",
    "login",
    "recover",
    "checkpoint",
    "dialog",
    "plugins",
    "settings",
    "messages",
    "notifications",
}

UID_SCRAPE_PATTERNS = (
    r'<meta[^>]+property=["\']al:ios:url["\'][^>]+content=["\']fb:\/\/profile\/(\d{8,20})["\']',
    r'<meta[^>]+property=["\']al:android:url["\'][^>]+content=["\']fb:\/\/profile\/(\d{8,20})["\']',
    r'<meta[^>]+property=["\']al:web:url["\'][^>]+content=["\']fb:\/\/profile\/(\d{8,20})["\']',
    r'<meta[^>]+property=["\']og:url["\'][^>]+content=["\']https?:\/\/(?:www\.)?facebook\.com\/profile\.php\?id=(\d{8,20})',
    r'"profile_owner"\s*:\s*"(\d{8,20})"',
    r'"owner"\s*:\s*\{\s*"id"\s*:\s*"(\d{8,20})"',
    r'"userID"\s*:\s*"(\d{8,20})"',
    r'"profile_id"\s*:\s*(\d{8,20})',
    r'"entity_id"\s*:\s*"(\d{8,20})"',
    r'"actorID"\s*:\s*"(\d{8,20})"',
    r'"subject_id"\s*:\s*"(\d{8,20})"',
    r'profile\.php\?id=(\d{8,20})',
    r"fb://profile/(\d{8,20})",
)

UID_SCRAPE_PATTERNS_STRICT = (
    r'<meta[^>]+property=["\']al:ios:url["\'][^>]+content=["\']fb:\/\/profile\/(\d{8,20})["\']',
    r'<meta[^>]+property=["\']al:android:url["\'][^>]+content=["\']fb:\/\/profile\/(\d{8,20})["\']',
    r'<meta[^>]+property=["\']al:web:url["\'][^>]+content=["\']fb:\/\/profile\/(\d{8,20})["\']',
    r'<meta[^>]+property=["\']og:url["\'][^>]+content=["\']https?:\/\/(?:www\.)?facebook\.com\/profile\.php\?id=(\d{8,20})',
    r'"profile_owner"\s*:\s*"(\d{8,20})"',
    r'"owner"\s*:\s*\{\s*"id"\s*:\s*"(\d{8,20})"',
    r'profile\.php\?id=(\d{8,20})',
    r"fb://profile/(\d{8,20})",
)


def to_text(value) -> str:
    return "" if value is None else str(value)


def normalize_url_input(raw: Optional[str]) -> str:
    value = to_text(raw).strip()
    if not value:
        return ""
    if value.startswith("http://") or value.startswith("https://"):
        return value
    return "https://" + value


def normalize_uid(uid_raw: Optional[str]) -> str:
    uid = to_text(uid_raw).strip()
    return uid if re.fullmatch(r"\d{8,}", uid) else ""


def extract_uid_from_url(url_raw: Optional[str]) -> str:
    url = normalize_url_input(url_raw)
    if not url:
        return ""
    try:
        parsed = urlparse(url)
    except Exception:
        return ""

    host = (parsed.netloc or "").lower().replace("www.", "")
    if "facebook.com" not in host and "fb.com" not in host:
        return ""

    qs = parse_qs(parsed.query or "")
    profile_id = to_text((qs.get("id", [""])[0] or "")).strip()
    if re.fullmatch(r"\d{8,}", profile_id):
        return profile_id

    path = to_text(parsed.path).strip("/")
    parts = [p for p in path.split("/") if p]
    if not parts:
        return ""

    if parts[0].lower() == "people" and len(parts) >= 3 and re.fullmatch(r"\d{8,}", parts[2]):
        return parts[2]
    if re.fullmatch(r"\d{8,}", parts[0]):
        return parts[0]
    return ""


def extract_username_slug_from_url(url_raw: Optional[str]) -> str:
    url = normalize_url_input(url_raw)
    if not url:
        return ""
    try:
        parsed = urlparse(url)
    except Exception:
        return ""

    host = (parsed.netloc or "").lower().replace("www.", "")
    if "facebook.com" not in host and "fb.com" not in host:
        return ""

    path = to_text(parsed.path).strip("/")
    parts = [part for part in path.split("/") if part]
    if not parts:
        return ""

    first_raw = unquote(to_text(parts[0]).strip())
    first = first_raw.lower()
    if not first:
        return ""
    if first in FACEBOOK_RESERVED_PATH_PREFIXES:
        return ""
    if re.fullmatch(r"\d{8,20}", first_raw):
        return ""
    return first_raw


def extract_username_from_login_next(url_raw: Optional[str]) -> str:
    url = normalize_url_input(url_raw)
    if not url:
        return ""
    try:
        parsed = urlparse(url)
    except Exception:
        return ""
    login_path = (parsed.path or "").lower().rstrip("/")
    if login_path not in ("/login.php", "/login"):
        return ""
    qs = parse_qs(parsed.query or "")
    next_url = to_text((qs.get("next", [""])[0] or "")).strip()
    if not next_url:
        return ""
    return extract_username_slug_from_url(next_url)


def extract_share_token(url_raw: Optional[str]) -> str:
    url = normalize_url_input(url_raw)
    if not url:
        return ""
    try:
        parsed = urlparse(url)
    except Exception:
        return ""
    path = to_text(parsed.path).strip("/")
    parts = [part for part in path.split("/") if part]
    if len(parts) < 2:
        return ""
    if parts[0].lower() != "share":
        return ""
    token = to_text(parts[1]).strip()
    return token if token else ""


def safe_percent_decode_text(value_raw: Optional[str], rounds_raw: int = 1) -> str:
    value = to_text(value_raw)
    if not value:
        return ""
    rounds = max(1, min(3, int(rounds_raw or 1)))
    for _ in range(rounds):
        next_value = re.sub(r"%([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), value)
        if next_value == value:
            break
        value = next_value
    return value


def normalize_facebook_payload_text(raw: Optional[str]) -> str:
    normalized = (
        to_text(raw)
        .replace("\\/", "/")
        .replace("\\u002f", "/")
        .replace("\\u003a", ":")
        .replace("\\u003d", "=")
        .replace("\\u0026", "&")
        .replace("\\u003f", "?")
        .replace("\\x2f", "/")
        .replace("\\x3a", ":")
        .replace("\\x3d", "=")
        .replace("\\x26", "&")
        .replace("\\x3f", "?")
        .replace("&#x2f;", "/")
        .replace("&#x3a;", ":")
        .replace("&#x3d;", "=")
        .replace("&#x26;", "&")
        .replace("&#x3f;", "?")
        .replace("&#47;", "/")
        .replace("&#58;", ":")
        .replace("&#61;", "=")
        .replace("&#38;", "&")
        .replace("&#63;", "?")
        .replace("&amp;", "&")
        .replace("%253d", "%3d")
        .replace("%253D", "%3D")
        .replace("%2526", "%26")
        .replace("%253f", "%3f")
        .replace("%253F", "%3F")
        .replace("%3d", "=")
        .replace("%3D", "=")
        .replace("%26", "&")
        .replace("%3f", "?")
        .replace("%3F", "?")
        .replace("&quot;", '"')
    )
    return safe_percent_decode_text(normalized, 2)


def extract_uid_from_html(html_raw: Optional[str]) -> str:
    html = to_text(html_raw)
    if not html:
        return ""
    normalized = normalize_facebook_payload_text(
        html.replace("\\/", "/")
        .replace("\\u002f", "/")
        .replace("\\u003a", ":")
        .replace("&quot;", '"')
    )
    for pattern in UID_SCRAPE_PATTERNS:
        match = re.search(pattern, normalized, flags=re.I)
        if not match:
            continue
        uid = to_text(match.group(1) if match.groups() else "").strip()
        if re.fullmatch(r"\d{8,20}", uid):
            return uid
    return ""


def extract_uid_from_html_strict(html_raw: Optional[str]) -> str:
    html = to_text(html_raw)
    if not html:
        return ""
    normalized = normalize_facebook_payload_text(
        html.replace("\\/", "/")
        .replace("\\u002f", "/")
        .replace("\\u003a", ":")
        .replace("&quot;", '"')
    )
    for pattern in UID_SCRAPE_PATTERNS_STRICT:
        match = re.search(pattern, normalized, flags=re.I)
        if not match:
            continue
        uid = to_text(match.group(1) if match.groups() else "").strip()
        if re.fullmatch(r"\d{8,20}", uid):
            return uid
    return ""


def build_facebook_navigation_hint_headers(user_agent_raw: Optional[str]) -> Dict[str, str]:
    user_agent = to_text(user_agent_raw).lower()
    platform = '"Windows"'
    mobile = "?0"
    if "android" in user_agent:
        platform = '"Android"'
        mobile = "?1"
    elif "iphone" in user_agent or "ipad" in user_agent or "ios" in user_agent:
        platform = '"iOS"'
        mobile = "?1"
    return {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "sec-ch-ua": '"Chromium";v="140", "Not.A/Brand";v="24", "Google Chrome";v="140"',
        "sec-ch-ua-mobile": mobile,
        "sec-ch-ua-platform": platform,
        "Cache-Control": "max-age=0",
        "Upgrade-Insecure-Requests": "1",
        "Referer": "https://www.facebook.com/",
    }


def build_facebook_probe_urls(url_raw: Optional[str]) -> List[str]:
    normalized = normalize_url_input(url_raw)
    if not normalized:
        return []
    urls: List[str] = [normalized]
    try:
        parsed = urlparse(normalized)
        host = (parsed.netloc or "").lower()
        if "facebook.com" in host or "fb.com" in host:
            path = parsed.path or "/"
            query = ("?" + parsed.query) if parsed.query else ""
            urls.append(f"https://m.facebook.com{path}{query}")
            urls.append(f"https://mbasic.facebook.com{path}{query}")
            urls.append(f"https://www.facebook.com{path}{query}")
    except Exception:
        pass

    out: List[str] = []
    seen = set()
    for item in urls:
        key = to_text(item).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def build_uid_probe_header_candidates() -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    seen = set()
    for ua in FALLBACK_UID_PROBE_USER_AGENTS:
        key = to_text(ua).strip()
        if not key:
            continue
        low = key.lower()
        if low in seen:
            continue
        seen.add(low)
        headers = {"User-Agent": key, "Accept-Language": "vi,en-US;q=0.9,en;q=0.8"}
        headers.update(build_facebook_navigation_hint_headers(key))
        out.append(headers)
    return out


def normalize_input(raw_input: str) -> Dict:
    text = to_text(raw_input).strip()
    if not text:
        return {"ok": False, "error": "empty_input"}

    if re.fullmatch(r"\d{8,}", text):
        uid = text
        return {
            "ok": True,
            "inputType": "uid",
            "uid": uid,
            "username": "",
            "profileUrl": f"https://www.facebook.com/profile.php?id={uid}",
        }

    url = text
    if "facebook.com/" in url.lower() or "fb.com/" in url.lower() or url.startswith("www."):
        if not re.match(r"^https?://", url, flags=re.I):
            url = "https://" + url
    else:
        username = text.lstrip("@").strip("/")
        return {
            "ok": True,
            "inputType": "username",
            "uid": "",
            "username": username,
            "profileUrl": "https://www.facebook.com/" + quote(username),
        }

    uid = extract_uid_from_url(url)
    username = extract_username_slug_from_url(url)
    profile_url = f"https://www.facebook.com/profile.php?id={uid}" if uid else url
    return {
        "ok": True,
        "inputType": "url",
        "uid": uid,
        "username": username,
        "profileUrl": profile_url,
    }
