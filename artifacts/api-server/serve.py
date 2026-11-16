"""Process entrypoint for the Nyaya Bandhu FastAPI service.

Binds 0.0.0.0 on $PORT (default 5000) and trusts the platform reverse proxy's
X-Forwarded-* headers so request URLs/client IPs are reconstructed correctly.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))


def main() -> None:
    import uvicorn

    port = int(os.environ.get("PORT", "5000"))
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=port,
        proxy_headers=True,
        forwarded_allow_ips="*",
        log_config=None,
        access_log=False,
        timeout_keep_alive=30,
    )


if __name__ == "__main__":
    main()
