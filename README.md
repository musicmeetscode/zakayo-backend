# Django REST Framework API Starter

A starter project for building REST APIs with Django and Django REST Framework.

## Features

* **JWT authentication** (`djangorestframework-simplejwt`): login, refresh with rotation, and logout via token blacklist.
* **Custom user model** that uses email as the identifier, with soft delete. Logging in again restores a deleted account.
* **Account flows**: registration, email verification (optional or required), and password reset.
* **Social login** with Google and Facebook.
* **Consistent responses**: every endpoint returns `{success, status_code, message, data, errors}`.
* **Secure defaults**: endpoints require authentication unless marked public, auth endpoints are rate limited, and production hardening applies when `DEBUG=0`.
* **API docs** with Swagger and ReDoc (`drf-yasg`).
* **Celery + Redis** for background tasks. Shop emails are sent by the worker, with retries (see below).
* **SQLite, Postgres or MySQL**, chosen with one environment variable.
* **Docker**: a production image, plus a `docker-compose.yml` with web, Postgres, Redis, a Celery worker and beat.
* **Health checks** at `/health/` (liveness) and `/ready/` (checks the database).

## Getting started

### Prerequisites

* Python 3.12+
* Redis, only if you run Celery locally

### Local setup

```bash
git clone https://github.com/musicmeetscode/django-rest-framework-starter.git
cd django-rest-framework-starter

python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
python generate_key.py            # paste the output into SECRET_KEY in .env

python manage.py migrate
python manage.py create_super_user   # uses ADMIN_EMAIL / ADMIN_PASSWORD from .env
python manage.py runserver
```

The API runs at `http://127.0.0.1:8000/`. To also start Celery, use `./run_dev.sh`.

### With Docker

```bash
cp .env.example .env              # set SECRET_KEY (and ADMIN_EMAIL / ADMIN_PASSWORD)
docker compose up --build
```

This starts the API on port 8000 with Postgres, Redis, a Celery worker and beat. Migrations, `collectstatic`
and the admin user are handled on startup by `entrypoint.sh`. For a server deployment behind Nginx, see [DEPLOY.md](DEPLOY.md).

### Running tests

```bash
python manage.py test
```

## Configuration

All settings come from environment variables. The full list is in [.env.example](.env.example). The main ones:

| Variable | Default | Notes |
|---|---|---|
| `SECRET_KEY` | (required) | `python generate_key.py` |
| `DEBUG` | `0` | `1` for local development |
| `ALLOWED_HOSTS`, `CORS_ALLOWED_ORIGINS`, `CSRF_TRUSTED_ORIGINS` | empty | comma separated |
| `DB_CHOICE` | `sqlite` | `postgres` requires `DATABASE_URL`; `mysql` requires `DB_NAME`, `DB_USER`, `DB_PASSWORD` (plus `DB_HOST`, `DB_PORT`, default `127.0.0.1:3306`) |
| `REQUIRE_EMAIL_VERIFICATION` | `0` | block login until the email is verified |
| `EMAIL_BACKEND` | console | set to the SMTP backend to send real email |
| `GOOGLE_CLIENT_ID`, `FACEBOOK_APP_ID`, `FACEBOOK_APP_SECRET` | empty | required for social login |

## API endpoints

* Swagger: `/swagger/` · ReDoc: `/redoc/` · Admin: `/admin/`

### Authentication (`/user/`)

| Method | Path | Auth | Body |
|---|---|---|---|
| POST | `/user/register` | public | `{"email", "password", "full_name"?}` |
| GET | `/user/verify-email?token=...` | public | link from the verification email |
| POST | `/user/resend-verification` | public | `{"email"}` |
| POST | `/user/login` | public | `{"email", "password"}` → `{user, tokens: {access, refresh}}` |
| POST | `/user/token/refresh` | public | `{"refresh"}` → new `access` and `refresh` |
| POST | `/user/logout` | user | `{"refresh"}` (blacklists the token) |
| POST | `/user/request-reset-password` | public | `{"email"}` |
| GET | `/user/password-reset/<uidb64>/<token>` | public | checks a reset link |
| PATCH | `/user/password-reset-complete` | public | `{"password", "uidb64", "token"}` |
| GET/PATCH | `/user/me` | user | the current user's profile |
| GET | `/user/users` | admin | paginated list of users |
| GET/PATCH/DELETE | `/user/<id>` | owner or admin | `DELETE` soft deletes |
| POST | `/social-auth/google/` | public | `{"auth_token": "<Google ID token>"}` |
| POST | `/social-auth/facebook/` | public | `{"auth_token": "<Facebook access token>"}` |

Send the access token on protected endpoints:

```
Authorization: Bearer <access_token>
```

### Response format

```json
{"success": true, "status_code": 200, "message": "", "data": {"...": "..."}}
{"success": false, "status_code": 400, "message": "email: Enter a valid email address.", "data": null, "errors": ["email: Enter a valid email address."]}
```

## Adding your own endpoints

Put your models and views in the `api` app (or a new app) and register routes in `api/urls.py`; they're served under `/api/`.
For soft delete, UUIDs and timestamps, models can extend `accounts.models.BaseModel`. Views require
authentication by default; set `permission_classes = (AllowAny,)` to make one public.

## Zakayo Industries shop (`shop` app)

Backend for the Zakayo Industries website (`../zakayo`): catalogue API, checkout, quote requests,
contact form, newsletter, transactional emails and the staff admin.

### First run

```bash
python manage.py migrate
python manage.py create_super_user     # or: python manage.py createsuperuser
python manage.py setup_admin_roles     # "Sales" and "Catalogue managers" groups
python manage.py seed_catalogue        # SAMPLE products — replace with the real register
```

Admin: `http://127.0.0.1:8000/admin/` (path set by `ADMIN_URL`; it is not linked from the website).
To give a colleague access: Users → add user → tick **Staff status** → add them to a group.

### Public API (no auth, rate-limited by `PUBLIC_FORM_THROTTLE_RATE`)

| Method | Path | |
| --- | --- | --- |
| GET | `/api/products/?page_size=500` | Published products |
| GET | `/api/products/{slug}/` | One product |
| GET | `/api/categories/` | Categories |
| POST | `/api/orders/` | Checkout. Prices are re-read from the database; emails customer + `SALES_NOTIFICATION_EMAILS`. Returns `tracking_token` |
| GET | `/api/orders/{reference}/?token=…` | Customer tracking view (token required; wrong token = 404). Throttled by `TRACKING_THROTTLE_RATE` |
| POST | `/api/orders/{reference}/confirm-delivery/` | Body `{token, received_by, feedback}`. Only for dispatched orders; sets Delivered and emails customer + sales |
| POST | `/api/customers/lookup/` | Body `{phone}`. Returning customer at checkout: emails a 6-digit code to the address on file; returns only `{found, masked_email}`. Throttled by `CUSTOMER_LOOKUP_THROTTLE_RATE` |
| POST | `/api/customers/verify/` | Body `{phone, code}`. Code is single-use, 10-minute expiry, 5 attempts. Returns the saved details for pre-fill |
| POST | `/api/quote-requests/` | Quote request; emails customer + sales |
| POST | `/api/contact/` | Contact form; emails customer + sales |
| POST | `/api/newsletter/` | Subscribe (idempotent) |
| POST | `/api/products/{slug}/notify-me/` | Body `{ email }`. "Back in stock" alert for an out-of-stock product; everyone waiting is emailed once when it is marked in stock (form, bulk action or import). Listed under **Stock alerts** in the admin |

Validation errors include `field_errors: {"customer.email": [...]}` alongside the flat `errors` list.

### What staff can do in the admin

- **Products & categories** — add/edit, upload images, publish/hide, stock flags, CSV/XLSX import & export (matched on SKU).
- **Orders** — review items, set delivery fee and payment instructions, then use the status buttons in the order
  page's side panel (Confirm → Processing → Dispatched, or Cancel). Each button saves the page and emails the customer,
  with an optional message. Bulk actions do the same from the list. Export to CSV/XLSX.
- **Delivery notes** — "Print delivery note" (order page or the 🖨 link in the list) gives an A4 note with a QR code.
  The customer scans it on arrival and taps "I've received my order" on the website: that is the only way an order
  becomes **Delivered** (staff can't set it), and it emails the customer and sales.
- **Customers** — created automatically from orders/quotes (matched by email), with order history and value;
  "Send email to selected customers" composes a branded email.
- **Quote requests** — fill in the quoted price, then "Send quote to customer".
- **Contact messages** — write a reply, then "Send reply".
- **Newsletter** — export subscribers or email them.
- **Email log** — every email sent, with failures.

Emails live in `shop/templates/emails/`. Locally they print to the console; set the SMTP variables in `.env` to send.

## Emails

Shop emails (order confirmations, status updates, quotes, replies, sign-in codes) are rendered during the request,
saved to **Email log** in the admin, and sent by the Celery worker once the database transaction commits. The
request never waits for the mail server.

* Run a worker: `celery -A main worker -l info` (`./run_dev.sh` and `docker-compose.yml` already do).
* Failed sends retry after 1, 2, 4 and 8 minutes. Each log shows its status (queued / sent / failed), attempts and
  last error. Select logs and use **Resend selected emails** to try again.
* If Redis is unreachable, the email is sent during the request instead, so nothing is lost.
* `EMAIL_IN_BACKGROUND=0` always sends during the request, for setups without a worker.

