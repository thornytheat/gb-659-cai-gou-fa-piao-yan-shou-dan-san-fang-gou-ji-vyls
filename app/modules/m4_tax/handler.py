from fastapi import APIRouter, Body, Request
from fastapi.responses import HTMLResponse

from app.errors import DomainError
from app.web import page
from . import service

router = APIRouter()


def _execute(call, *args, **kwargs):
    try:
        return call(*args, **kwargs)
    except DomainError:
        raise
    except ValueError as exc:
        raise DomainError(str(exc) or "BAD_REQUEST", "请求无法按计税约定处理") from exc


SAMPLE_CSV = (
    "supplier_code,doc_no,line_no,sku,tax_rate,quantity,unit_price,net_amount,tax_amount,invoice_gross,convention\n"
    "000031,IV-101,2,000142,13%,1,1000.00,1000.00,130.00,1130.00,line_by_line\n"
    "000031,IV-101,3,000142,13%,0.5,200.00,100.00,13.00,,line_by_line\n"
)


@router.get("/tax", response_class=HTMLResponse)
def view(request: Request):
    summary = _execute(service.summary)
    rows = [
        {
            "key": invoice["doc_no"],
            "convention": invoice["convention"],
            "status": "通过" if invoice["status"] == "PASS" else "差异",
            "value": invoice["total"]["gross"],
        }
        for invoice in summary["invoices"]
    ] or [{"key": "IV-001", "convention": "line_by_line", "status": "示例", "value": "1130.00"}]
    return page(request, "m4.html", "税额核验", "/api/tax/check", rows, summary=summary, sample_csv=SAMPLE_CSV)


@router.post("/api/tax/check")
def execute():
    return _execute(service.demo)


@router.get("/api/tax/summary")
def summary():
    return _execute(service.summary)


@router.get("/api/tax/supplier/{supplier_code}/convention")
def supplier_convention(supplier_code: str):
    return _execute(service.supplier_convention, supplier_code)


@router.post("/api/tax/supplier/convention")
def switch_convention(payload: dict = Body(default_factory=dict)):
    return _execute(service.switch_convention, payload)


@router.post("/api/tax/invoices/import")
def import_invoice(payload: dict = Body(default_factory=dict)):
    return _execute(service.import_invoice, payload)


@router.post("/api/tax/invoices/import-csv")
def import_invoice_csv(payload: dict = Body(default_factory=dict)):
    return _execute(service.import_invoice_csv, payload)


@router.get("/api/tax/invoices")
def invoices(supplier_code: str | None = None):
    return _execute(service.invoices, supplier_code)


@router.get("/api/tax/invoices/{supplier_code}/{doc_no}")
def invoice_detail(supplier_code: str, doc_no: str):
    return _execute(service.invoice_detail, supplier_code, doc_no)


@router.get("/api/tax/invoices/{supplier_code}/{doc_no}/review")
def review(supplier_code: str, doc_no: str):
    return _execute(service.review, supplier_code, doc_no)


@router.post("/api/tax/legacy/import")
def import_legacy(payload: dict = Body(default_factory=dict)):
    return _execute(service.import_legacy, payload)


@router.post("/api/tax/legacy/migrate")
def migrate_legacy(payload: dict = Body(default_factory=dict)):
    return _execute(service.migrate_legacy, payload)


@router.get("/api/tax/legacy")
def legacy_status():
    return _execute(service.legacy_status)
