import os


VERSION = "step09_module_split_phase1_2026_05_16"
REQUEST_TIMEOUT_SEC = 8

FB_PUBLIC_APP_TOKEN = os.getenv(
    "FB_PUBLIC_APP_TOKEN",
    "6628568379|c1e620fa708a1d5696fb991c1bde5662",
)
EXTERNAL_CHECKER_URL = os.getenv("EXTERNAL_CHECKER_URL", "").strip()
EXTERNAL_CHECKER_API_KEY = os.getenv("EXTERNAL_CHECKER_API_KEY", "").strip()
UID_CHECKER_API_KEY = os.getenv("UID_CHECKER_API_KEY", "").strip()
UID_CHECKER_FB_COOKIES_JSON = os.getenv("UID_CHECKER_FB_COOKIES_JSON", "").strip()
TELEGRAM_RELAY_TARGET_URL = os.getenv(
    "TELEGRAM_RELAY_TARGET_URL",
    "https://script.google.com/macros/s/AKfycbyfgY-Dt5vmus2nbCROMIsNOWN0ddDKDnYTaYrQY2SdeUdlMrsCjOnLujB4h7OK3x8/exec",
).strip()
TELEGRAM_RELAY_TIMEOUT_SEC = float(os.getenv("TELEGRAM_RELAY_TIMEOUT_SEC", "25"))

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)

FALLBACK_UID_PROBE_USER_AGENTS = (
    USER_AGENT,
    "Mozilla/5.0",
    "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
)
