"""真实发票 CSV 导入：按发票分组、保留物理行号、混用约定拒收。"""
import csv
import io

from app.errors import DomainError
from app.modules.m1_documents.money import decimal_value
from .conventions import normalize_convention

FIELDS = (
    "supplier_code", "doc_no", "line_no", "sku", "tax_rate",
    "quantity", "unit_price", "net_amount", "tax_amount", "invoice_gross", "convention",
)
REQUIRED = ("supplier_code", "doc_no", "tax_rate")


def _clean(value) -> str | None:
    if value is None:
        return None
    token = str(value).strip()
    return token if token else None


def _amount(value):
    token = _clean(value)
    if token is None:
        return None
    try:
        return str(decimal_value(token))
    except ValueError as exc:
        raise ValueError("BAD_AMOUNT") from exc


def parse_invoice_csv(text: str) -> tuple[dict, list[dict]]:
    """解析 CSV 文本，返回 (按发票分组的数据, 隔离行)。"""
    buffer = io.StringIO(text.lstrip("﻿"))
    reader = csv.DictReader(buffer)
    missing_headers = [name for name in REQUIRED if name not in (reader.fieldnames or [])]
    if missing_headers:
        raise DomainError("BAD_CSV_HEADER", f"缺少列：{', '.join(missing_headers)}")

    invoices: dict[tuple[str, str], dict] = {}
    quarantined: list[dict] = []
    for physical_line, row in enumerate(reader, start=2):
        if not any(_clean(value) for value in row.values()):
            continue
        supplier_code = _clean(row.get("supplier_code"))
        doc_no = _clean(row.get("doc_no"))
        tax_rate = _clean(row.get("tax_rate"))
        invoice_gross = _clean(row.get("invoice_gross"))
        if not supplier_code or not doc_no or not tax_rate:
            reason = "EMPTY_VALUE"
            quarantined.append({"line_no": physical_line, "reason_code": reason, "raw": row})
            continue

        raw_line_no = _clean(row.get("line_no"))
        try:
            line_no = int(raw_line_no) if raw_line_no else physical_line
        except ValueError:
            quarantined.append({"line_no": physical_line, "reason_code": "BAD_LINE_NUMBER", "raw": row})
            continue

        try:
            line = {
                "line_no": line_no,
                "sku": _clean(row.get("sku")) or "",
                "tax_rate": tax_rate,
                "quantity": _clean(row.get("quantity")),
                "unit_price": _clean(row.get("unit_price")),
                "net_amount": _amount(row.get("net_amount")),
                "tax_amount": _amount(row.get("tax_amount")),
            }
            if line["quantity"] is not None:
                decimal_value(line["quantity"])
            if line["unit_price"] is not None:
                decimal_value(line["unit_price"])
        except ValueError as exc:
            quarantined.append({"line_no": physical_line, "reason_code": str(exc), "raw": row})
            continue

        convention = _clean(row.get("convention"))
        key = (supplier_code, doc_no)
        invoice = invoices.get(key)
        if invoice is None:
            invoice = {
                "supplier_code": supplier_code,
                "doc_no": doc_no,
                "invoice_gross": None,
                "lines": [],
                "declared_conventions": set(),
                "source_lines": [],
                "conflicts": [],
            }
            invoices[key] = invoice
        if invoice_gross:
            try:
                gross = str(decimal_value(invoice_gross))
            except ValueError:
                quarantined.append({"line_no": physical_line, "reason_code": "BAD_AMOUNT", "raw": row})
                continue
            if invoice["invoice_gross"] not in (None, gross):
                invoice["conflicts"].append(physical_line)
            invoice["invoice_gross"] = invoice["invoice_gross"] or gross
        if convention:
            try:
                invoice["declared_conventions"].add(normalize_convention(convention))
            except ValueError:
                quarantined.append({"line_no": physical_line, "reason_code": "UNKNOWN_CONVENTION", "raw": row})
                continue
        invoice["lines"].append(line)
        invoice["source_lines"].append(physical_line)

    return invoices, quarantined


def group_payload(invoice: dict) -> dict:
    """把分组结果整理成登记入参；混用约定在这里直接拒收。"""
    if invoice["conflicts"]:
        raise ValueError("VALUE_CONFLICT")
    if len(invoice["declared_conventions"]) > 1:
        raise ValueError("MIXED_CONVENTION")
    if invoice["invoice_gross"] is None:
        raise ValueError("EVIDENCE_INSUFFICIENT")
    convention = next(iter(invoice["declared_conventions"]), None)
    return {
        "supplier_code": invoice["supplier_code"],
        "doc_no": invoice["doc_no"],
        "invoice_gross": invoice["invoice_gross"],
        "convention": convention,
        "lines": invoice["lines"],
    }
