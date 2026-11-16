"""计税约定：逐行计税与整票计税。

供应商在登记发票时绑定当时的约定；此后切换供应商约定不会重算已登记发票。
同票不同税率的行永远按税率分别核算，绝不互相抵消。
"""
from decimal import Decimal, InvalidOperation

from app.config import DEFAULT_TAX_CONVENTION, TAX_CONVENTIONS
from app.modules.m1_documents.money import decimal_value
from .calculate import money

ROUNDING = "ROUND_HALF_UP"
CENT = "0.01"

_CONVENTION_ALIASES = {
    "line_by_line": "line_by_line",
    "line-by-line": "line_by_line",
    "linebyline": "line_by_line",
    "line": "line_by_line",
    "per_line": "line_by_line",
    "whole_invoice": "whole_invoice",
    "whole-invoice": "whole_invoice",
    "wholeinvoice": "whole_invoice",
    "whole": "whole_invoice",
    "invoice": "whole_invoice",
}


def normalize_convention(value: str) -> str:
    token = str(value if value is not None else "").strip().lower().replace(" ", "_")
    if not token:
        raise ValueError("UNKNOWN_CONVENTION")
    convention = _CONVENTION_ALIASES.get(token)
    if convention not in TAX_CONVENTIONS:
        raise ValueError("UNKNOWN_CONVENTION")
    return convention


def normalize_tax_rate(value: str) -> Decimal:
    token = str(value if value is not None else "").strip().replace("%", "").replace("％", "")
    if not token:
        raise ValueError("BAD_TAX_RATE")
    try:
        rate = Decimal(token.replace(",", "").replace("，", ""))
    except InvalidOperation as exc:
        raise ValueError("BAD_TAX_RATE") from exc
    if rate > 1:
        rate = rate / 100
    if rate < 0 or rate > 1:
        raise ValueError("BAD_TAX_RATE")
    return rate


def _present(value) -> bool:
    return value is not None and str(value).strip() != ""


def line_evidence(line: dict) -> dict:
    """返回一行可复核的净额依据；证据不足时拒绝计算而不是补零。"""
    if _present(line.get("net_amount")):
        try:
            raw = decimal_value(line["net_amount"])
        except ValueError as exc:
            raise ValueError("BAD_AMOUNT") from exc
        source = "net_amount"
    elif _present(line.get("quantity")) and _present(line.get("unit_price")):
        try:
            raw = decimal_value(line["quantity"]) * decimal_value(line["unit_price"])
        except ValueError as exc:
            raise ValueError("BAD_AMOUNT") from exc
        source = "quantity_unit_price"
    else:
        raise ValueError("EVIDENCE_INSUFFICIENT")
    if raw < 0:
        raise ValueError("NEGATIVE_COMPONENT")
    return {"raw_net": raw, "basis_source": source}


def _declared_conventions(lines: list[dict]) -> set[str]:
    declared = set()
    for line in lines:
        if _present(line.get("convention")):
            declared.add(normalize_convention(line["convention"]))
    return declared


def _rate_text(rate: Decimal) -> str:
    return format(rate.normalize(), "f")


def _prepare(lines: list[dict]) -> list[dict]:
    if not lines:
        raise ValueError("EMPTY_INVOICE")
    prepared = []
    seen = set()
    for index, line in enumerate(lines, start=1):
        line_no = line.get("line_no", index)
        try:
            line_no = int(line_no)
        except (TypeError, ValueError) as exc:
            raise ValueError("BAD_LINE_NUMBER") from exc
        if line_no in seen:
            raise ValueError("DUPLICATE_LINE")
        seen.add(line_no)
        evidence = line_evidence(line)
        prepared.append(
            {
                "line_no": line_no,
                "sku": str(line.get("sku", "")),
                "tax_rate": _rate_text(normalize_tax_rate(line.get("tax_rate"))),
                "raw_net": evidence["raw_net"],
                "basis_source": evidence["basis_source"],
            }
        )
    return prepared


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _group(prepared: list[dict], convention: str) -> list[dict]:
    """按税率分组汇总；不同税率的行绝不合并。"""
    buckets: dict[str, dict] = {}
    order: list[str] = []
    for line in prepared:
        rate = line["tax_rate"]
        if rate not in buckets:
            buckets[rate] = {"tax_rate": rate, "raw_net": Decimal("0"), "net": Decimal("0"), "tax": Decimal("0"), "line_numbers": []}
            order.append(rate)
        bucket = buckets[rate]
        bucket["raw_net"] += line["raw_net"]
        bucket["line_numbers"].append(line["line_no"])
        if convention == "line_by_line":
            bucket["net"] += line["net"]
            bucket["tax"] += line["tax"]
        else:
            bucket["net"] = money(bucket["raw_net"])
            bucket["tax"] = money(bucket["net"] * Decimal(rate))

    groups = []
    for rate in order:
        bucket = buckets[rate]
        gross = bucket["net"] + bucket["tax"]
        groups.append(
            {
                "tax_rate": rate,
                "net": str(bucket["net"]),
                "tax": str(bucket["tax"]),
                "gross": str(gross),
                "raw_net": _decimal_text(bucket["raw_net"]),
                "line_numbers": bucket["line_numbers"],
            }
        )
    return groups


def calculate_invoice(lines: list[dict], convention: str = DEFAULT_TAX_CONVENTION) -> dict:
    """按约定计算发票。line_by_line 逐行舍入；whole_invoice 按税率分组后整组舍入。"""
    convention = normalize_convention(convention)
    declared = _declared_conventions(lines)
    if len(declared) > 1:
        raise ValueError("MIXED_CONVENTION")
    if declared and convention not in declared:
        raise ValueError("CONVENTION_CONFLICT")

    prepared = _prepare(lines)
    for line in prepared:
        line["convention"] = convention
        if convention == "line_by_line":
            line["net"] = money(line["raw_net"])
            line["tax"] = money(line["net"] * Decimal(line["tax_rate"]))
            line["gross"] = line["net"] + line["tax"]
        else:
            # 整票净额依据由税率分组统一舍入，行级不再单独舍入。
            line["net"] = None
            line["tax"] = None
            line["gross"] = None

    groups = _group(prepared, convention)
    total_net = sum((Decimal(group["net"]) for group in groups), start=Decimal("0"))
    total_tax = sum((Decimal(group["tax"]) for group in groups), start=Decimal("0"))
    total_gross = sum((Decimal(group["gross"]) for group in groups), start=Decimal("0"))

    serialized_lines = []
    for line in prepared:
        serialized_lines.append(
            {
                "line_no": line["line_no"],
                "sku": line["sku"],
                "tax_rate": line["tax_rate"],
                "basis_source": line["basis_source"],
                "raw_net": _decimal_text(line["raw_net"]),
                "net": None if line["net"] is None else str(line["net"]),
                "tax": None if line["tax"] is None else str(line["tax"]),
                "gross": None if line["gross"] is None else str(line["gross"]),
                "convention": convention,
            }
        )

    net_basis = {
        "convention": convention,
        "rounding": ROUNDING,
        "cent": CENT,
        "groups": groups,
        "total": {"net": str(total_net), "tax": str(total_tax), "gross": str(total_gross)},
    }
    return {
        "convention": convention,
        "lines": serialized_lines,
        "groups": groups,
        "total": {"net": str(total_net), "tax": str(total_tax), "gross": str(total_gross)},
        "net_basis": net_basis,
    }


def verify_calculation(calculation: dict, invoice_gross: str, line_taxes: dict[int, Decimal] | None = None, tolerance: str = CENT) -> dict:
    """按绑定的计算结果复核发票；逐行约定同时核对行税额。"""
    limit = Decimal(tolerance)
    stated = decimal_value(invoice_gross)
    expected = Decimal(calculation["total"]["gross"])
    difference = money(stated - expected)
    line_differences = []

    if calculation["convention"] == "line_by_line" and line_taxes:
        for line in calculation["lines"]:
            line_no = line["line_no"]
            if line_no not in line_taxes or line_taxes[line_no] is None:
                continue
            stated_tax = line_taxes[line_no]
            computed_tax = Decimal(line["tax"])
            delta = money(stated_tax - computed_tax)
            if abs(delta) > limit:
                line_differences.append({"line_no": line_no, "stated_tax": str(stated_tax), "computed_tax": str(computed_tax), "difference": str(delta)})

    status = "TAX_DIFFERENCE" if abs(difference) > limit or line_differences else "PASS"
    return {
        "expected_gross": str(expected),
        "invoice_gross": str(stated),
        "difference": str(difference),
        "line_differences": line_differences,
        "status": status,
        "tolerance": str(limit),
    }
