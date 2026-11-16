import threading
from decimal import Decimal

import pytest

import app.store as store
from app.errors import DomainError
from app.modules.m4_tax import ledger
from app.modules.m4_tax.conventions import calculate_invoice, normalize_convention
from app.modules.m4_tax.importer import parse_invoice_csv


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATABASE_PATH", tmp_path / "tax.sqlite")
    monkeypatch.setattr(ledger, "DATABASE_PATH", tmp_path / "tax.sqlite", raising=False)
    store.init_db()
    return tmp_path


def lines(*entries):
    result = []
    for index, (rate, net) in enumerate(entries, start=2):
        result.append({"line_no": index, "sku": "SKU-A", "tax_rate": rate, "net_amount": net})
    return result


# ---------------------------------------------------------------- 约定计算

def test_whole_invoice_rounds_net_before_tax():
    rows = lines(("13%", "0.125"), ("13%", "0.125"))
    whole = calculate_invoice(rows, "whole_invoice")
    per_line = calculate_invoice(rows, "line_by_line")
    assert whole["total"] == {"net": "0.25", "tax": "0.03", "gross": "0.28"}
    assert per_line["total"] == {"net": "0.26", "tax": "0.04", "gross": "0.30"}


def test_whole_invoice_groups_by_tax_rate_without_offset():
    rows = lines(("13%", "100.125"), ("9%", "100.125"))
    result = calculate_invoice(rows, "whole_invoice")
    groups = {group["tax_rate"]: group for group in result["groups"]}
    assert groups["0.13"]["net"] == "100.13" and groups["0.13"]["tax"] == "13.02"
    assert groups["0.09"]["net"] == "100.13" and groups["0.09"]["tax"] == "9.01"
    assert Decimal(result["total"]["net"]) == Decimal("200.26")


def test_basis_from_quantity_and_unit_price():
    rows = [{"line_no": 2, "sku": "A", "tax_rate": "0.13", "quantity": "3", "unit_price": "33.335"}]
    result = calculate_invoice(rows, "line_by_line")
    assert result["lines"][0]["basis_source"] == "quantity_unit_price"
    assert result["total"]["net"] == "100.01"


def test_missing_evidence_is_rejected_not_guessed():
    rows = [{"line_no": 2, "sku": "A", "tax_rate": "0.13"}]
    with pytest.raises(ValueError, match="EVIDENCE_INSUFFICIENT"):
        calculate_invoice(rows, "line_by_line")


def test_mixed_conventions_on_one_invoice_rejected():
    rows = lines(("13%", "1.00"), ("13%", "1.00"))
    rows[0]["convention"] = "whole_invoice"
    rows[1]["convention"] = "line_by_line"
    with pytest.raises(ValueError, match="MIXED_CONVENTION"):
        calculate_invoice(rows, "whole_invoice")
    # 整票内即使声明同一约定，也必须等于绑定约定
    uniform = lines(("13%", "1.00"), ("13%", "1.00"))
    for row in uniform:
        row["convention"] = "whole_invoice"
    with pytest.raises(ValueError, match="CONVENTION_CONFLICT"):
        calculate_invoice(uniform, "line_by_line")


def test_unknown_convention_rejected():
    with pytest.raises(ValueError, match="UNKNOWN_CONVENTION"):
        normalize_convention("by_mood")


def test_line_by_line_review_checks_each_line_tax():
    result = calculate_invoice(lines(("13%", "100.00"), ("9%", "200.00")), "line_by_line")
    stated = {2: Decimal("13.00"), 3: Decimal("17.00")}
    check = ledger.verify_calculation(result, result["total"]["gross"], stated)
    assert check["difference"] == "0.00"
    assert check["status"] == "TAX_DIFFERENCE"
    assert check["line_differences"][0]["line_no"] == 3
    # 整票约定只核对含税额，不逐行复核
    whole = calculate_invoice(lines(("13%", "100.00"), ("9%", "200.00")), "whole_invoice")
    assert ledger.verify_calculation(whole, whole["total"]["gross"], stated)["status"] == "PASS"


# ---------------------------------------------------------------- 供应商切换

def test_switch_only_affects_new_invoices(db):
    base = ledger.register_invoice("000031", "IV-1", lines(("13%", "0.125"), ("13%", "0.125")), "0.30")
    assert base["convention"] == "line_by_line" and base["total"]["gross"] == "0.30"

    switched = ledger.set_supplier_convention("000031", "whole_invoice")
    assert switched["changed"] and switched["revision"] == 1

    fresh = ledger.register_invoice("000031", "IV-2", lines(("13%", "0.125"), ("13%", "0.125")), "0.28")
    assert fresh["convention"] == "whole_invoice" and fresh["total"]["gross"] == "0.28"

    stored = ledger.get_invoice("000031", "IV-1")
    assert stored["convention"] == "line_by_line"
    assert stored["policy_revision"] == 0
    assert stored["total"]["gross"] == "0.30"


def test_invoice_binds_received_revision_during_concurrent_switch(db):
    first = ledger.set_supplier_convention("000031", "whole_invoice")
    # 在途发票在切换前接收，携带当时的版本号
    in_flight = ledger.register_invoice(
        "000031", "IV-9", lines(("13%", "0.125"), ("13%", "0.125")), "0.30",
        expected_revision=first["revision"] - 1,
    )
    assert in_flight["convention"] == "line_by_line"
    assert in_flight["policy_revision"] == 0


def test_stale_expected_revision_is_reported(db):
    ledger.set_supplier_convention("000031", "whole_invoice")
    with pytest.raises(DomainError) as error:
        ledger.set_supplier_convention("000031", "line_by_line", expected_revision=0)
    assert error.value.code == "CONVENTION_STALE"


def test_same_convention_is_idempotent(db):
    first = ledger.set_supplier_convention("000031", "whole_invoice")
    again = ledger.set_supplier_convention("000031", "whole_invoice")
    assert not again["changed"] and again["revision"] == first["revision"]
    assert len(ledger.policy_history("000031")) == 1


def test_duplicate_registration_does_not_double_record(db):
    rows = lines(("13%", "100.00"))
    first = ledger.register_invoice("000031", "IV-1", rows, "113.00")
    second = ledger.register_invoice("000031", "IV-1", rows, "113.00")
    assert first["created"] and not second["created"] and second["duplicate"]
    assert len(ledger.list_invoices("000031")) == 1


def test_same_doc_no_different_content_is_conflict(db):
    ledger.register_invoice("000031", "IV-1", lines(("13%", "100.00")), "113.00")
    with pytest.raises(DomainError) as error:
        ledger.register_invoice("000031", "IV-1", lines(("13%", "200.00")), "226.00")
    assert error.value.code == "INVOICE_CONFLICT"


def test_concurrent_switches_leave_consistent_history(db):
    errors = []

    def worker(convention):
        try:
            for _ in range(5):
                ledger.set_supplier_convention("000031", convention)
        except Exception as exc:  # pragma: no cover - failure detail below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(name,)) for name in ("whole_invoice", "line_by_line")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    history = ledger.policy_history("000031")
    revisions = [row["revision"] for row in history]
    assert revisions == list(range(1, len(history) + 1))
    # 相邻版本不得相同，切换历史单调且可回放
    assert all(a["convention"] != b["convention"] for a, b in zip(history, history[1:]))


def test_in_flight_registration_keeps_original_version_under_switching(db):
    rows = lines(("13%", "0.125"), ("13%", "0.125"))
    received_revision = ledger.current_policy("000031")["revision"]
    stop = {"value": False}
    failures = []

    def switch_loop():
        try:
            while not stop["value"]:
                ledger.set_supplier_convention("000031", "whole_invoice")
                ledger.set_supplier_convention("000031", "line_by_line")
        except Exception as exc:  # pragma: no cover
            failures.append(exc)

    thread = threading.Thread(target=switch_loop)
    thread.start()
    try:
        for index in range(20):
            result = ledger.register_invoice(
                "000031", f"IV-FLIGHT-{index}", rows, "0.30", expected_revision=received_revision,
            )
            assert result["convention"] == "line_by_line"
            assert result["policy_revision"] == received_revision
            assert result["total"]["gross"] == "0.30"
    finally:
        stop["value"] = True
        thread.join()
    assert not failures


def test_unknown_historical_revision_is_rejected(db):
    with pytest.raises(DomainError) as error:
        ledger.register_invoice("000031", "IV-X", lines(("13%", "1.00")), "1.13", expected_revision=9)
    assert error.value.code == "UNKNOWN_POLICY_REVISION"
    assert ledger.get_invoice("000031", "IV-X") is None


# ---------------------------------------------------------------- 差异回看

def test_review_shows_delta_without_mutating_credential(db):
    rows = lines(("13%", "0.125"), ("13%", "0.125"))
    ledger.register_invoice("000031", "IV-1", rows, "0.30")
    ledger.set_supplier_convention("000031", "whole_invoice")
    review = ledger.review_invoice("000031", "IV-1")
    assert review["convention_changed"]
    assert review["bound"]["convention"] == "line_by_line"
    assert review["bound_total"]["gross"] == "0.30"
    assert review["hypothetical_total"]["gross"] == "0.28"
    assert review["delta"]["gross"] == "-0.02"
    assert ledger.get_invoice("000031", "IV-1")["total"]["gross"] == "0.30"


# ---------------------------------------------------------------- 旧凭据迁移

def legacy(credential_id, doc_no, lines_value, gross="113.00", **extra):
    document = {"supplier_code": "000031", "doc_no": doc_no, "invoice_gross": gross, "lines": lines_value, **extra}
    return {"credential_id": credential_id, "supplier_code": "000031", "doc_no": doc_no, **document}


def test_legacy_keeps_line_by_line_after_supplier_switched(db):
    ledger.set_supplier_convention("000031", "whole_invoice")
    ledger.queue_legacy([legacy("C1", "IV-OLD", lines(("13%", "0.125"), ("13%", "0.125")), gross="0.30")])
    outcome = ledger.migrate_legacy()
    assert outcome["complete"] and outcome["migrated"] == ["C1"]
    invoice = ledger.get_invoice("000031", "IV-OLD")
    assert invoice["convention"] == "line_by_line" and invoice["source"] == "legacy"
    assert invoice["policy_revision"] == 0
    assert invoice["calculation"]["total"]["gross"] == "0.30"


def test_legacy_without_evidence_is_rejected_not_guessed(db):
    ledger.queue_legacy([legacy("C2", "IV-BAD", [{"line_no": 2, "sku": "A", "tax_rate": "13%"}] , gross="113.00")])
    outcome = ledger.migrate_legacy()
    rejected = outcome["rejected"][0]
    assert rejected["state"] == "rejected" and rejected["reason_code"] == "EVIDENCE_INSUFFICIENT"
    assert ledger.get_invoice("000031", "IV-BAD") is None


def test_legacy_migration_resumes_after_interruption(db, monkeypatch):
    credentials = [
        legacy("C1", "IV-1", lines(("13%", "100.00"))),
        legacy("C2", "IV-2", lines(("13%", "200.00"))),
        legacy("C3", "IV-3", lines(("13%", "300.00"))),
    ]
    ledger.queue_legacy(credentials)

    original = ledger._migrate_one
    calls = {"count": 0}

    def flaky(connection, row):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("simulated crash")
        return original(connection, row)

    monkeypatch.setattr(ledger, "_migrate_one", flaky)
    with pytest.raises(RuntimeError, match="simulated crash"):
        ledger.migrate_legacy()

    states = {row["credential_id"]: row["state"] for row in ledger.legacy_status()}
    assert states["C1"] == "migrated"
    assert states["C2"] == "pending" and states["C3"] == "pending"

    monkeypatch.setattr(ledger, "_migrate_one", original)
    outcome = ledger.migrate_legacy()
    assert outcome["complete"] and set(outcome["migrated"]) == {"C2", "C3"}
    assert sorted(invoice["doc_no"] for invoice in ledger.list_invoices("000031")) == ["IV-1", "IV-2", "IV-3"]


def test_legacy_replay_is_idempotent(db):
    ledger.queue_legacy([legacy("C1", "IV-1", lines(("13%", "100.00")))])
    ledger.migrate_legacy()
    ledger.queue_legacy([legacy("C1", "IV-1", lines(("13%", "100.00")))])
    outcome = ledger.migrate_legacy()
    assert outcome["complete"] and outcome["migrated"] == []
    assert len(ledger.list_invoices("000031")) == 1
    assert ledger.legacy_status()[0]["state"] == "migrated"
    assert len(ledger.list_invoices("000031")) == 1


def test_corrected_legacy_credential_can_be_retried(db):
    ledger.queue_legacy([legacy("C1", "IV-1", [{"line_no": 2, "sku": "A", "tax_rate": "13%"}], gross="113.00")])
    first = ledger.migrate_legacy()
    assert first["rejected"][0]["reason_code"] == "EVIDENCE_INSUFFICIENT"
    ledger.queue_legacy([legacy("C1", "IV-1", lines(("13%", "100.00")), gross="113.00")])
    second = ledger.migrate_legacy()
    assert second["migrated"] == ["C1"]
    assert ledger.get_invoice("000031", "IV-1")["total"]["gross"] == "113.00"


# ---------------------------------------------------------------- CSV 导入

CSV = """supplier_code,doc_no,line_no,sku,tax_rate,quantity,unit_price,net_amount,tax_amount,invoice_gross,convention
000031,IV-201,2,000142,13%,1,1000.00,1000.00,130.00,1130.20,whole_invoice
000031,IV-201,3,000142,13%,0.5,200.00,100.00,13.00,,whole_invoice
000031,IV-202,2,000143,9%,1,500.00,500.00,45.00,545.00,line_by_line
000031,IV-202,3,000143,13%,1,100.00,100.00,13.00,,line_by_line
"""


def test_csv_import_registers_grouped_invoices(db):
    invoices, quarantined = parse_invoice_csv(CSV)
    assert not quarantined and len(invoices) == 2
    assert len(invoices[("000031", "IV-201")]["lines"]) == 2


def test_mixed_convention_invoice_rejected_on_import(db):
    text = CSV.replace(
        "000031,IV-202,3,000143,13%,1,100.00,100.00,13.00,,line_by_line",
        "000031,IV-202,3,000143,13%,1,100.00,100.00,13.00,,whole_invoice",
    )
    invoices, _ = parse_invoice_csv(text)
    from app.modules.m4_tax.importer import group_payload
    with pytest.raises(ValueError, match="MIXED_CONVENTION"):
        group_payload(invoices[("000031", "IV-202")])


def test_import_quarantines_missing_values(db):
    text = "supplier_code,doc_no,line_no,sku,tax_rate,net_amount,invoice_gross\n000031,IV-1,2,A,,1.00,1.00\n"
    invoices, quarantined = parse_invoice_csv(text)
    assert quarantined and quarantined[0]["reason_code"] == "EMPTY_VALUE" and not invoices
