# Apps Script Webhook Load Balancer

Service nay dung cho luong webhook cua bot:

- Telegram: failover qua nhieu URL Apps Script.
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
- `WEBHOOK_SHARED_SECRET`: secret gui header `X-Webhook-Secret` ve Apps Script.
- `TELEGRAM_ASYNC_ENABLED` (optional, mac dinh `1`): tra `200` ngay cho Telegram, forward webhook o background de tranh timeout.
- `TELEGRAM_ASYNC_WORKERS` (optional, mac dinh `8`): so worker async cho Telegram.
- `TELEGRAM_FAILOVER_STRATEGY` (optional, mac dinh `priority`):
  - `priority`: uu tien URL dau tien trong `TELEGRAM_SCRIPT_URLS`, loi/quota/timeout thi chuyen URL tiep theo.
  - `hash`: phan tan deterministic theo `update_id`.
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
LB da ho tro failover khi backend Telegram bi loi/het quota/timeout.
Mac dinh LB se ack Telegram ngay va forward nen, giam nguy co `Read timeout expired` khi Render cold start hoac Apps Script cham.

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

## 4) Tuong thich voi Apps Script hien tai

Code Apps Script da co guard route:

- `source=telegram`
- `source=sepay`
- `source=lead`

LB tu dong append query `source=...` khi forward.

Neu Apps Script set Script Property `WEBHOOK_SHARED_SECRET`, nhat dinh phai set cung gia tri o env `WEBHOOK_SHARED_SECRET` cua LB.
