from app.errors import DomainError
from .calculate import tax_components
from .verify import verify_invoice
from . import ledger
from .importer import parse_invoice_csv, group_payload


def run(payload: dict | None = None) -> dict:
    payload = payload or {"quantity": "10", "unit_price": "100", "tax_rate": "0.13", "gross": "1130.00"}
    try:
        calculated = tax_components(payload["quantity"], payload["unit_price"], payload["tax_rate"])
        return {**calculated, **verify_invoice(calculated["gross"], payload["gross"])}
    except (KeyError, ValueError) as exc:
        raise DomainError("BAD_TAX_INPUT", f"税额输入不合法：{exc}") from exc


def demo():
    return run()


def supplier_convention(supplier_code: str) -> dict:
    return {
        "current": ledger.current_policy(supplier_code),
        "history": ledger.policy_history(supplier_code),
    }


def switch_convention(payload: dict) -> dict:
    supplier_code = payload.get("supplier_code")
    convention = payload.get("convention")
    if not supplier_code or not convention:
        raise DomainError("BAD_REQUEST", "需要 supplier_code 与 convention")
    return ledger.set_supplier_convention(supplier_code, convention, payload.get("expected_revision"))


def import_invoice(payload: dict) -> dict:
    supplier_code = payload.get("supplier_code")
    doc_no = payload.get("doc_no")
    lines = payload.get("lines")
    if not supplier_code or not doc_no or not isinstance(lines, list):
        raise DomainError("BAD_REQUEST", "需要 supplier_code、doc_no 与 lines")
    try:
        return ledger.register_invoice(
            supplier_code,
            doc_no,
            lines,
            payload.get("invoice_gross", payload.get("gross")),
            convention=payload.get("convention"),
            expected_revision=payload.get("expected_revision"),
            source=payload.get("source", "import"),
        )
    except ValueError as exc:
        if isinstance(exc, DomainError):
            raise
        raise DomainError(str(exc) or "BAD_INVOICE", "发票无法按约定计算") from exc


def invoices(supplier_code: str | None = None) -> dict:
    return {"invoices": ledger.list_invoices(supplier_code)}


def invoice_detail(supplier_code: str, doc_no: str) -> dict:
    invoice = ledger.get_invoice(supplier_code, doc_no)
    if invoice is None:
        raise DomainError("INVOICE_NOT_FOUND", f"发票 {doc_no} 未登记", status=404)
    return invoice


def review(supplier_code: str, doc_no: str) -> dict:
    return ledger.review_invoice(supplier_code, doc_no)


def import_legacy(payload: dict) -> dict:
    credentials = payload.get("credentials")
    if not isinstance(credentials, list) or not credentials:
        raise DomainError("BAD_REQUEST", "需要非空 credentials 列表")
    return {"queued": ledger.queue_legacy(credentials), "status": ledger.legacy_status()}


def import_invoice_csv(payload: dict) -> dict:
    text = payload.get("csv")
    if not isinstance(text, str) or not text.strip():
        raise DomainError("BAD_REQUEST", "需要 csv 文本")
    invoices, quarantined = parse_invoice_csv(text)
    registered, rejected = [], list(quarantined)
    for invoice in invoices.values():
        try:
            data = group_payload(invoice)
        except ValueError as exc:
            rejected.append({
                "doc_no": invoice["doc_no"],
                "supplier_code": invoice["supplier_code"],
                "reason_code": str(exc),
                "line_no": invoice["source_lines"][0] if invoice["source_lines"] else None,
            })
            continue
        try:
            result = ledger.register_invoice(
                data["supplier_code"],
                data["doc_no"],
                data["lines"],
                data["invoice_gross"],
                convention=data["convention"],
                expected_revision=payload.get("expected_revision"),
            )
        except DomainError as exc:
            rejected.append({
                "doc_no": invoice["doc_no"],
                "supplier_code": invoice["supplier_code"],
                "reason_code": exc.code,
                "line_no": invoice["source_lines"][0] if invoice["source_lines"] else None,
            })
            continue
        except ValueError as exc:
            rejected.append({
                "doc_no": invoice["doc_no"],
                "supplier_code": invoice["supplier_code"],
                "reason_code": str(exc),
                "line_no": invoice["source_lines"][0] if invoice["source_lines"] else None,
            })
            continue
        registered.append({
            "supplier_code": result["supplier_code"],
            "doc_no": result["doc_no"],
            "convention": result["convention"],
            "policy_revision": result["policy_revision"],
            "total": result["total"],
            "status": result["verification"]["status"],
            "created": result["created"],
            "duplicate": result["duplicate"],
        })
    return {
        "imported": len(registered),
        "rejected": rejected,
        "invoices": registered,
        "quarantined": len(rejected),
    }


def migrate_legacy(payload: dict | None = None) -> dict:
    payload = payload or {}
    return ledger.migrate_legacy(payload.get("batch_size"))


def legacy_status() -> dict:
    return {"credentials": ledger.legacy_status()}


def summary() -> dict:
    return ledger.tax_summary()
