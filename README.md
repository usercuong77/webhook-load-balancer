# Apps Script Webhook Load Balancer

Service nay dung cho luong webhook cua bot:

- Telegram: fanout theo `update_id` (deterministic) qua nhieu URL Apps Script.
- SePay: always forward ve `PRIMARY_SCRIPT_URL` duy nhat.
- Lead form: forward ve `PRIMARY_SCRIPT_URL`.

## 1) Bien moi truong

Copy `.env.example` va dien:

- `PRIMARY_SCRIPT_URL`: URL web app Apps Script chinh (bat buoc).
- `TELEGRAM_SCRIPT_URLS`: danh sach URL Apps Script xu ly Telegram, cach nhau boi dau phay.
- `WEBHOOK_SHARED_SECRET`: secret gui header `X-Webhook-Secret` ve Apps Script.

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

### SePay

Cau hinh webhook SePay:

`https://<your-render-domain>/webhook/sepay`

### Lead form

Webhook lead:

`https://<your-render-domain>/webhook/lead`

## 4) Tuong thich voi Apps Script hien tai

Code Apps Script da co guard route:

- `source=telegram`
- `source=sepay`
- `source=lead`

LB tu dong append query `source=...` khi forward.

Neu Apps Script set Script Property `WEBHOOK_SHARED_SECRET`, nhat dinh phai set cung gia tri o env `WEBHOOK_SHARED_SECRET` cua LB.
