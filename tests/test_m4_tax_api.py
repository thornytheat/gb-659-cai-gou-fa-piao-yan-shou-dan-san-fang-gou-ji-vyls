import pytest
from fastapi.testclient import TestClient

import app.store as store
import app.modules.m4_tax.ledger as ledger
from app.main import app

CSV = """supplier_code,doc_no,line_no,sku,tax_rate,quantity,unit_price,net_amount,tax_amount,invoice_gross,convention
000031,IV-301,2,000142,13%,1,1000.004,1000.004,,1243.01,whole_invoice
000031,IV-301,3,000142,13%,0.1,1000.04,100.004,,,whole_invoice
"""


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATABASE_PATH", tmp_path / "api.sqlite")
    monkeypatch.setattr(ledger, "DATABASE_PATH", tmp_path / "api.sqlite", raising=False)
    store.init_db()
    return TestClient(app)


def test_supplier_convention_endpoints(client):
    current = client.get("/api/tax/supplier/000031/convention").json()
    assert current["current"]["convention"] == "line_by_line" and current["current"]["revision"] == 0

    switched = client.post("/api/tax/supplier/convention", json={"supplier_code": "000031", "convention": "whole_invoice"})
    assert switched.status_code == 200
    body = switched.json()
    assert body["changed"] and body["revision"] == 1

    stale = client.post(
        "/api/tax/supplier/convention",
        json={"supplier_code": "000031", "convention": "line_by_line", "expected_revision": 0},
    )
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "CONVENTION_STALE"


def test_tax_page_renders_after_invoice_import(client):
    client.post("/api/tax/supplier/convention", json={"supplier_code": "000031", "convention": "whole_invoice"})
    client.post("/api/tax/invoices/import-csv", json={"csv": CSV})
    page = client.get("/tax")
    assert page.status_code == 200 and "IV-301" in page.text and "whole_invoice" in page.text


def test_csv_import_endpoint_reports_rejections(client):
    client.post("/api/tax/supplier/convention", json={"supplier_code": "000031", "convention": "whole_invoice"})
    text = CSV + "000031,IV-302,2,000143,,1,1.00,1.00,0.13,1.13,whole_invoice\n"
    response = client.post("/api/tax/invoices/import-csv", json={"csv": text})
    body = response.json()
    assert body["imported"] == 1
    assert body["rejected"][0]["reason_code"] == "EMPTY_VALUE"


def test_csv_declaring_other_convention_is_rejected(client):
    response = client.post("/api/tax/invoices/import-csv", json={"csv": CSV})
    body = response.json()
    assert body["imported"] == 0
    assert body["rejected"][0]["reason_code"] == "CONVENTION_CONFLICT"


def test_import_binds_convention_and_review_shows_delta(client):
    client.post("/api/tax/supplier/convention", json={"supplier_code": "000031", "convention": "whole_invoice"})
    imported = client.post("/api/tax/invoices/import-csv", json={"csv": CSV}).json()
    assert imported["invoices"][0]["convention"] == "whole_invoice"
    assert imported["invoices"][0]["total"]["gross"] == "1243.01"

    # 供应商改回逐行；新发票按逐行，旧发票仍按整票复核
    client.post("/api/tax/supplier/convention", json={"supplier_code": "000031", "convention": "line_by_line"})
    newly = """supplier_code,doc_no,line_no,sku,tax_rate,net_amount,tax_amount,invoice_gross,convention
000031,IV-303,2,000142,13%,1000.00,130.00,1130.00,line_by_line
"""
    assert client.post("/api/tax/invoices/import-csv", json={"csv": newly}).json()["imported"] == 1

    review = client.get("/api/tax/invoices/000031/IV-301/review").json()
    assert review["bound"]["convention"] == "whole_invoice"
    assert review["current_policy"]["convention"] == "line_by_line"
    assert review["bound_total"]["gross"] == "1243.01"
    assert review["hypothetical_total"]["gross"] == "1243.00"
    assert review["delta"]["gross"] == "-0.01"

    # 回看不改原凭据
    stored = client.get("/api/tax/invoices/000031/IV-301").json()
    assert stored["total"]["gross"] == "1243.01"


def test_retry_import_is_idempotent(client):
    client.post("/api/tax/supplier/convention", json={"supplier_code": "000031", "convention": "whole_invoice"})
    first = client.post("/api/tax/invoices/import-csv", json={"csv": CSV}).json()["invoices"][0]
    second = client.post("/api/tax/invoices/import-csv", json={"csv": CSV}).json()["invoices"][0]
    assert first["created"] and not second["created"] and second["duplicate"]
    assert len(client.get("/api/tax/invoices").json()["invoices"]) == 1


def test_invoice_import_with_expected_revision(client):
    client.post("/api/tax/supplier/convention", json={"supplier_code": "000031", "convention": "whole_invoice"})
    payload = {
        "supplier_code": "000031",
        "doc_no": "IV-401",
        "invoice_gross": "1130.30",
        "expected_revision": 0,
        "lines": [
            {"line_no": 2, "sku": "000142", "tax_rate": "13%", "net_amount": "1000.00"},
            {"line_no": 3, "sku": "000142", "tax_rate": "13%", "net_amount": "100.00"},
        ],
    }
    response = client.post("/api/tax/invoices/import", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["convention"] == "line_by_line" and body["policy_revision"] == 0


def test_legacy_endpoints_resume_and_reject(client):
    credentials = [
        {"credential_id": "L1", "supplier_code": "000031", "doc_no": "IV-L1", "invoice_gross": "113.00",
         "lines": [{"line_no": 2, "sku": "A", "tax_rate": "13%", "net_amount": "100.00", "tax_amount": "13.00"}]},
        {"credential_id": "L2", "supplier_code": "000031", "doc_no": "IV-L2", "invoice_gross": "113.00",
         "lines": [{"line_no": 2, "sku": "A", "tax_rate": "13%"}]},
    ]
    queued = client.post("/api/tax/legacy/import", json={"credentials": credentials}).json()
    assert queued["queued"] == 2

    outcome = client.post("/api/tax/legacy/migrate", json={}).json()
    assert outcome["complete"]
    assert outcome["migrated"] == ["L1"]
    assert outcome["rejected"][0]["reason_code"] == "EVIDENCE_INSUFFICIENT"

    statuses = {row["credential_id"]: row for row in client.get("/api/tax/legacy").json()["credentials"]}
    assert statuses["L1"]["state"] == "migrated"
    assert statuses["L2"]["state"] == "rejected"

    summary = client.get("/api/tax/summary").json()
    assert summary["legacy"]["migrated"] == 1 and summary["invoices"][0]["source"] == "legacy"


def test_unknown_convention_is_rejected(client):
    response = client.post("/api/tax/supplier/convention", json={"supplier_code": "000031", "convention": "magic"})
    assert response.status_code == 422 and response.json()["detail"]["code"] == "UNKNOWN_CONVENTION"
