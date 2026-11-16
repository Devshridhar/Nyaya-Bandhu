---
name: Provider secrets
description: Project rule for storing third-party service credentials.
---

Keep provider credentials in Replit Secrets; do not place their values in source code or regular environment configuration. The user approved this for the Twilio, Resend, and Sarvam credentials.

**Why:** Plain-text project settings can be exposed through collaboration, version control, or accidental logs.

**How to apply:** Use the Replit Secrets flow for provider credentials and only reference them through environment-variable names in application code.
