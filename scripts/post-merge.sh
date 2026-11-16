#!/bin/bash
set -e
pnpm install --frozen-lockfile
# Python dependencies for the Nyaya Bandhu FastAPI service (artifacts/api-server).
# Database tables are created by the service itself on startup in development,
# so there is no schema push step here.
uv sync --locked
