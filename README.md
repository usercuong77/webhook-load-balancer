# Apps Script Webhook Load Balancer

Service nay dung cho luong webhook cua bot:

- Telegram: chon 1 URL Apps Script cho moi update, khong retry de tranh nhan ban tin nhan.
- SePay: always forward ve `PRIMARY_SCRIPT_URL` duy nhat.
- Lead form: forward ve `PRIMARY_SCRIPT_URL`.

## 1) Bien moi truong

Copy `.env.example` va dien:

- `PRIMARY_SCRIPT_URL`: URL web app Apps Script chinh (bat buoc).
- `SCRIPT_BACKEND_URLS` (optional): danh sach backend Apps Script dung chung cho failover (script1 loi/quota -> script2).
- `TELEGRAM_SCRIPT_URLS`: danh sach URL Apps Script xu ly Telegram, cach nhau boi dau phay.
- `LEAD_SCRIPT_URLS` (optional): danh sach URL rieng cho webhook lead.
- `SEPAY_SCRIPT_URLS` (optional): danh sach URL rieng cho webhook SePay.
- `SEPAY_FAILOVER_ENABLED` (optional, mac dinh `0`): bat failover SePay (can than duplicate neu du lieu dedupe khong chia se).
- `UID_CHECKER_ENABLED` (optional, mac dinh `1`): bat endpoint checker tich hop trong LB.
- `UID_CHECKER_API_KEY` (optional): API key bao ve `/check`, `/get-uid`, `/latest-post`, `/cookie-health`. Nen dat cung gia tri voi `EXTERNAL_CHECKER_API_KEY` trong Apps Script. Neu chua set env, service dung legacy fallback de khong lam gian doan bot.
- `UID_CHECKER_TIMEOUT` (optional, mac dinh `10`): timeout goi Facebook public probe.
- `LATEST_POST_TOTAL_TIMEOUT` (optional, mac dinh `25`): timeout tong cho `/latest-post` va `/checkpost`.
- `LATEST_POST_NO_COOKIE_TIMEOUT` (optional, mac dinh `5.8`): timeout nhanh uu tien cho nhanh `no_cookie` khi lay bai moi.
- `LATEST_POST_NO_COOKIE_MAX_ATTEMPTS` (optional, mac dinh `6`): so probe toi da cho `no_cookie` truoc khi fallback cookie.
- `LATEST_POST_WITH_COOKIE_TIMEOUT` (optional, mac dinh `4.5`): timeout cho moi lan probe `with_cookie`.
- `LATEST_POST_WITH_COOKIE_MAX_ATTEMPTS` (optional, mac dinh `5`): so probe toi da cho moi cookie candidate.
- `CHECKER_CACHE_ENABLED` (optional, mac dinh `1`): bat cache ngan han cho checker tich hop.
- `CHECKER_GET_UID_CACHE_TTL_SEC` (optional, mac dinh `21600`): TTL cache resolve UID/profile.
- `CHECKER_CHECK_CACHE_TTL_SEC` (optional, mac dinh `45`): TTL cache live/die.
- `CHECKER_LATEST_POST_CACHE_TTL_SEC` (optional, mac dinh `55`): TTL cache latest post.
- `CHECKER_CACHE_MAX_ITEMS` (optional, mac dinh `2000`): so item cache toi da trong RAM Render.
- `WEBHOOK_SHARED_SECRET`: secret gui header `X-Webhook-Secret` ve Apps Script.
- `TELEGRAM_ASYNC_ENABLED` (optional, mac dinh `1`): tra `200` ngay cho Telegram, forward webhook o background de tranh timeout.
- `TELEGRAM_ASYNC_WORKERS` (optional, mac dinh `8`): so worker async cho Telegram.
- `TELEGRAM_LOADING_ENABLED` (optional, mac dinh `1`): Render gui ngay tin `Dang chay...` truoc khi forward sang Apps Script.
- `TELEGRAM_LOADING_TEXT` (optional): noi dung loading, mac dinh `Dang chay...`.
- `TELEGRAM_BOT_TOKEN` (optional): token bot mac dinh de Render gui loading.
- `TELEGRAM_BOT_TOKEN_MAP` (optional): JSON map token theo bot hint, vi du `{"main":"...","buff":"...","uid":"..."}`.
- `TELEGRAM_HEAVY_QUEUE_ENABLED` (optional, mac dinh `1`): bat queue nhe trong RAM cho lenh nang.
- `TELEGRAM_HEAVY_QUEUE_WORKERS` (optional, mac dinh `2`): so worker xu ly queue lenh nang.
- `TELEGRAM_HEAVY_QUEUE_MAX_SIZE` (optional, mac dinh `200`): so job toi da trong queue.
- `TELEGRAM_HEAVY_QUEUE_NON_COMMANDS` (optional, mac dinh `1`): dua text khong phai command vao queue vi bot dung text de check UID/link.
- `TELEGRAM_HEAVY_COMMANDS` (optional): danh sach command nang cach nhau bang dau phay.
- `TELEGRAM_DURABLE_QUEUE_ENABLED` (optional, mac dinh `1`): uu tien queue ben vung Redis/Upstash neu da cau hinh credentials.
- `UPSTASH_REDIS_REST_URL`: REST URL cua Upstash Redis. Khong co bien nay thi LB tu fallback ve queue RAM.
- `UPSTASH_REDIS_REST_TOKEN`: REST token cua Upstash Redis.
- `TELEGRAM_DURABLE_QUEUE_KEY` (optional): Redis list key cho job cho xu ly, mac dinh `bot:telegram:heavy:queue`.
- `TELEGRAM_DURABLE_PROCESSING_KEY` (optional): Redis list key cho job dang xu ly, mac dinh `bot:telegram:heavy:processing`.
- `TELEGRAM_DURABLE_QUEUE_TIMEOUT_SEC` (optional, mac dinh `8`): timeout goi Redis REST.
- `TELEGRAM_DURABLE_QUEUE_IDLE_SEC` (optional, mac dinh `2`): thoi gian worker cho truoc khi poll lai Redis.
- `TELEGRAM_DURABLE_QUEUE_RECOVER_ON_STARTUP` (optional, mac dinh `1`): khi Render restart, dua job dang nam trong processing ve queue.
- `TELEGRAM_DURABLE_QUEUE_RECOVER_LIMIT` (optional, mac dinh `100`): so job processing toi da recover moi lan worker khoi dong.
- `TELEGRAM_FAILOVER_STRATEGY` (optional, mac dinh `priority`):
  - `priority`: luon chon URL dau tien trong `TELEGRAM_SCRIPT_URLS`.
  - `hash`: phan tan deterministic theo `update_id`, moi update van chi gui 1 backend.
- `CORS_ALLOWED_ORIGINS` (optional): danh sach domain web duoc phep goi lead webhook, cach nhau boi dau phay. Mac dinh `*`.
- `CORS_ALLOW_HEADERS` (optional): header CORS cho phep. Mac dinh da bao gom `Content-Type` va header webhook secret.

## 2) Deploy tren Render

Chon thu muc nay lam root deploy:

`infra/webhook-load-balancer`

Lenh start:

`gunicorn -w 2 -k gthread -b 0.0.0.0:$PORT main:app`

## 3) Dat webhook

### Telegram

Set webhook Telegram tro den:

`https://<your-render-domain>/webhook/telegram`

Neu ban dung nhieu bot Telegram chung 1 Render service, them hint vao URL webhook:

- Bot chinh: `https://<your-render-domain>/webhook/telegram`
- Bot buff: `https://<your-render-domain>/webhook/telegram?bot=buff`
- Bot UID: `https://<your-render-domain>/webhook/telegram?bot=uid`

Neu can set secret token phia Telegram:

`.../setWebhook?url=https://<your-render-domain>/webhook/telegram&secret_token=<token>`

Luu y: secret token cua Telegram khac voi `WEBHOOK_SHARED_SECRET` cua LB->Apps Script.
LB khong retry Telegram sang backend khac neu backend dau tien timeout/loi.
Ly do: lenh Telegram co side effect, Apps Script co the da gui tin nhan truoc khi Render thay timeout.
Mac dinh LB se ack Telegram ngay va forward nen, giam nguy co `Read timeout expired` khi Render cold start hoac Apps Script cham.
Neu can chia tai Telegram, dung `TELEGRAM_FAILOVER_STRATEGY=hash`; khong dung retry/failover theo timeout cho Telegram.

Neu bat `TELEGRAM_LOADING_ENABLED=1` va da set token, LB se gui tin `Dang chay...` ngay lap tuc roi truyen `loading_message_id` ve Apps Script.
Apps Script se khong tao loading trung; lenh thuong se xoa tin loading khi xu ly xong, rieng `/viplike` se tan dung tin loading do lam workflow message.

Queue nhe cho lenh nang:

- Queue nay nam trong RAM cua Render, khong ben vung neu service restart.
- Dung de dieu tiet `/check`, `/checkpost`, `/viplike`, `/lammoiviplike` va text check UID/link, tranh day qua nhieu request vao Apps Script cung luc.
- Callback button khong dua vao queue de nut bam van phan hoi nhanh.
- Neu queue day, LB fallback sang executor cu de khong mat lenh.

Queue ben vung Redis/Upstash cho lenh nang:

- Neu set `UPSTASH_REDIS_REST_URL` va `UPSTASH_REDIS_REST_TOKEN`, LB se day job nang vao Redis list truoc.
- Worker tren Render claim job tu Redis sang processing list roi moi forward sang Apps Script.
- Neu Render restart giua chung, job dang o processing co the duoc recover ve queue khi service khoi dong lai.
- Neu Redis loi hoac chua cau hinh, LB tu fallback ve queue RAM hien co de bot khong dung.
- Endpoint `/` se hien `telegram_heavy_queue.mode=redis` khi queue ben vung dang active, hoac `memory` neu dang fallback.

Neu muc tieu la giam quota cho app chinh (app chinh giu SePay + task he thong):

- Khong dua `PRIMARY_SCRIPT_URL` vao `TELEGRAM_SCRIPT_URLS`.
- Dat `TELEGRAM_SCRIPT_URLS` chi gom app phu theo thu tu uu tien.
- Dat `TELEGRAM_FAILOVER_STRATEGY=priority`.

### SePay

Cau hinh webhook SePay:

`https://<your-render-domain>/webhook/sepay`

### Lead form

Webhook lead:

`https://<your-render-domain>/webhook/lead`

Route nay da ho tro CORS + OPTIONS de browser form submit truc tiep bang `fetch`.
LB da ho tro failover lead backend (neu cau hinh nhieu URL).

### UID checker tich hop

Service nay da tich hop cac endpoint chinh cua `uid-checker-service`, de co the giam 1 Render free service:

- `POST /check`
- `GET /get-uid`
- `POST /get-uid`
- `POST /latest-post`
- `POST /checkpost`
- `GET /cookie-health`
- `POST /cookie-health`
- `GET /checker/health`

Sau khi deploy, Apps Script co the tro checker sang:

`https://<your-render-domain>/check`

Luu y: giu `uid-checker-service` cu trong vai tro rollback cho den khi test `/check`, `/checkpost`, `/viplike` on dinh.

## 4) Tuong thich voi Apps Script hien tai

Code Apps Script da co guard route:

- `source=telegram`
- `source=sepay`
- `source=lead`

LB tu dong append query `source=...` khi forward.

Neu Apps Script set Script Property `WEBHOOK_SHARED_SECRET`, nhat dinh phai set cung gia tri o env `WEBHOOK_SHARED_SECRET` cua LB.
