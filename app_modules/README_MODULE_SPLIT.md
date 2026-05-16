## Module Split - Phase 1

Muc tieu phase 1: tach layer de de mo rong/bao tri, nhung giu hanh vi runtime on dinh.

### Cau truc hien tai

- `app_modules/controller.py`
  - Flask routes (`/`, `/health`, `/check`, `/get-uid`, `/webhook/telegram`)
  - Chi goi service layer.

- `app_modules/services/check_service.py`
  - Service entrypoint cho controller.
  - Phase 1 dang delegate ve `live_die` de tranh gay vo luong.

- `app_modules/live_die.py`
  - Legacy core logic (tam giu nguyen de on dinh).
  - Da bat dau goi module tach rieng cho:
    - parse input URL (`parsers/facebook_url.py`)
    - resolve UID (`resolvers/uid_resolver.py`)

- `app_modules/config.py`
  - Tap trung env/config constants.

- `app_modules/http_client.py`
  - Wrapper request HTTP + cookie normalization.

- `app_modules/parsers/facebook_url.py`
  - Parse/normalize input Facebook, extract UID/username/share handling.

- `app_modules/parsers/profile_name.py`
  - Parse/clean/validate ten profile.

- `app_modules/resolvers/uid_resolver.py`
  - Resolve UID tu URL/username.

### Ke hoach Phase 2

1. Tach toan bo `*_probe` ra `probes/live_die_probes.py`.
2. Tach `name enrich` ra `services/profile_name_service.py`.
3. Giam `live_die.py` thanh facade + compatibility layer.
4. Bo sung regression tests cho:
   - username URL
   - share URL
   - direct UID
   - dead samples

