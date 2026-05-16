from app_modules.controller import app
from app_modules.probes import live_die_probes as probe_core
from app_modules.services import check_service


# Compatibility exports for test/runtime scripts that import main.py directly.
EXTERNAL_CHECKER_URL = probe_core.EXTERNAL_CHECKER_URL


def check_live_die(raw_input, fetcher=None):
    # Allow callers to override external checker URL at runtime (legacy behavior).
    probe_core.EXTERNAL_CHECKER_URL = EXTERNAL_CHECKER_URL
    return check_service.check_live_die(raw_input, fetcher=fetcher)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
