# Nyaya Bandhu

Nyaya Bandhu turns consumer payment complaints into evidence-grounded drafts, private PDF bundles, and National Consumer Helpline filing guidance.

## Run & Operate

- `python artifacts/api-server/serve.py` — start the FastAPI service (port 8000 in the artifact workflow)
- `uv sync --locked` — install the Python environment from `pyproject.toml`
- `python -m pytest` — run the backend tests
- `DATABASE_URL` is provided by Replit PostgreSQL; SQLite is used only when it is absent.

## Stack and layout

- Python 3.13, FastAPI, SQLAlchemy, PostgreSQL, Replit App Storage, OpenAI AI Integrations
- `artifacts/api-server/app/core/` — validated settings, database, encryption, logging, and rate limiting
- `artifacts/api-server/app/models/domain.py` — persisted case, message, merchant, audit, and idempotency records
- `artifacts/api-server/app/api/v1/` — REST endpoints and Twilio WhatsApp webhook
- `artifacts/api-server/app/services/` — evidence, AI, legal drafting, PDF, email, storage, and case lifecycle
- `artifacts/api-server/static/index.html` — the operator and user test console

## Security and behavior

- All case PII and generated complaint content are encrypted with a per-case AES-GCM key; a wrapped key enables cryptographic erasure.
- Contact and UTR lookups use keyed HMAC indexes. Private evidence and PDF objects are encrypted before App Storage upload.
- Development mode does not send emails to merchants. A sandbox recipient can be configured for testing; production sending requires deliberate enablement.
- Unknown or unverified merchant addresses are never used for email dispatch.
- Set `ADMIN_API_KEY` before production use. Production also needs persistent encryption configuration (`MASTER_AES_KEY` or `SESSION_SECRET`), and Twilio signature verification is enabled by default.
- Optional integrations: `BHASHINI_USER_ID`, `BHASHINI_ULCA_API_KEY`, `BHASHINI_INFERENCE_KEY`, and `BHASHINI_SERVICE_ID` enable Bhashini fallback; `SARVAM_API_KEY` is the secondary fallback. Twilio account credentials and WhatsApp number, and Resend or SMTP, enable messaging and email. Replit AI Integrations provides the default OpenAI access.
- Purge scheduling runs in each live app instance. An autoscaling instance that is not running cannot execute the hourly retention job; production retention should be monitored accordingly.

## Database

The service creates tables in development for local use. Production startup intentionally does not run DDL; ensure production schema setup is handled before publishing and review the publishing database diff.
