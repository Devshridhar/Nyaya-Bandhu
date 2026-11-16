from __future__ import annotations

import os
import sys
from pathlib import Path

# Tests use an isolated SQLite file and local object storage; the running app uses
# Replit PostgreSQL and App Storage. Set before importing the global app/engine.
os.environ["APP_ENV"] = "development"
test_db = Path("/tmp/nyaya-bandhu-api-flow.sqlite3")
for suffix in ("", "-shm", "-wal"):
    Path(f"{test_db}{suffix}").unlink(missing_ok=True)
os.environ["DATABASE_URL"] = f"sqlite:///{test_db}"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "artifacts" / "api-server"))

from fastapi.testclient import TestClient

from app.api.v1 import cases as cases_api
from app.main import app


def test_complete_case_flow_is_encrypted_pdf_ready_and_purgeable(monkeypatch, tmp_path):
    async def fake_extract(text: str, language: str | None = None):
        if "UTR" in text:
            return {
                "transaction_date": "02/11/2026",
                "utr_or_order_id": "123456789012",
                "detected_language": "en",
            }
        return {
            "merchant_name": "Swiggy",
            "claim_amount_inr": 500,
            "grievance_description": "The order was not delivered and I want a refund.",
            "detected_language": "en",
        }

    monkeypatch.setattr(cases_api, "extract_text_facts", fake_extract)
    monkeypatch.setattr(cases_api.storage, "_client", None)
    monkeypatch.setattr(cases_api.storage, "_root", tmp_path / "objects")
    with TestClient(app) as client:
        intake = client.post(
            "/api/v1/cases/intake",
            data={
                "user_contact": "+919000000001",
                "preferred_language": "en",
                "text": "I paid Swiggy ₹500 but the order was not delivered. I want a refund.",
            },
            headers={"Idempotency-Key": "api-flow-intake"},
        )
        assert intake.status_code == 200, intake.text
        first = intake.json()
        reference = first["case_reference"]
        assert first["status"] == "AWAITING_MISSING_INFO"
        assert "transaction_date" in first["missing_fields"]
        replay = client.post(
            "/api/v1/cases/intake",
            data={
                "user_contact": "+919000000001",
                "preferred_language": "en",
                "text": "I paid Swiggy ₹500 but the order was not delivered. I want a refund.",
            },
            headers={"Idempotency-Key": "api-flow-intake"},
        )
        assert replay.status_code == 200
        assert replay.json()["case_reference"] == reference

        follow_up = client.post(
            f"/api/v1/cases/{reference}/respond",
            json={"text": "The date was 02/11/2026 and the UTR is 123456789012."},
            headers={"Idempotency-Key": "api-flow-follow-up"},
        )
        assert follow_up.status_code == 200, follow_up.text
        ready = follow_up.json()
        assert ready["status"] == "READY_FOR_USER_REVIEW"
        assert ready["extracted_facts"]["utr_or_order_id"] == "123456789012"
        assert "Reply YES / हाँ" in ready["draft_summary_native"]
        duplicate = client.post(
            "/api/v1/cases/intake",
            data={
                "user_contact": "+919000000002",
                "preferred_language": "en",
                "text": "A duplicate transaction has UTR 123456789012.",
            },
        )
        assert duplicate.status_code == 409

        approved = client.post(
            f"/api/v1/cases/{reference}/approve",
            json={"approved": True},
            headers={"Idempotency-Key": "api-flow-approve"},
        )
        assert approved.status_code == 200, approved.text
        result = approved.json()
        assert result["pdf_size_bytes"] < 2_000_000
        assert result["delivery_status"] == "MOCK_NOT_SENT"
        assert result["status"] == "APPROVED_AND_FILING"
        pdf = client.get(result["pdf_url"])
        assert pdf.status_code == 200
        assert pdf.content.startswith(b"%PDF")

        docket = client.patch(
            f"/api/v1/cases/{reference}/docket",
            json={"nch_docket_number": "NCH/2026/0001"},
        )
        assert docket.status_code == 200, docket.text

        purged = client.delete(f"/api/v1/cases/{reference}/purge")
        assert purged.status_code == 200, purged.text
        assert purged.json()["status"] == "PURGED"
        after_purge = client.get(f"/api/v1/cases/{reference}")
        assert after_purge.status_code == 200
        assert after_purge.json()["extracted_facts"] == {}
        assert after_purge.json()["audit_log"][-1]["new_status"] == "PURGED"
