"""供应商计税约定、发票约定快照与旧凭据迁移。

关键约束：
- 约定按供应商维护为只追加的版本历史；切换产生新版本，旧版本仍可绑定。
- 发票登记在写入事务内读取约定版本，并把约定、净额依据与计算结果一并留档。
- 同一发票重复登记（相同内容指纹）返回首次登记结果，不新增记录。
- 旧凭据逐条在独立事务中迁移，中断后未完成者保持 pending，可再次恢复。
"""
import json
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from zoneinfo import ZoneInfo

from app.config import DEFAULT_TAX_CONVENTION, TIMEZONE
from app.errors import DomainError
from app.store import init_db, read_connection, write_transaction
from app.modules.m1_documents.money import decimal_value
from .calculate import money
from .conventions import calculate_invoice, normalize_convention, normalize_tax_rate, verify_calculation

LOCAL_TZ = ZoneInfo(TIMEZONE)
LEGACY_CONVENTION = "line_by_line"
LEGACY_REVISION = 0
LEGACY_SOURCE = "legacy"


def now_text() -> str:
    return datetime.now(LOCAL_TZ).isoformat(timespec="seconds")


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _row(row) -> dict | None:
    return dict(row) if row is not None else None


# ---------------------------------------------------------------- 供应商约定

def current_policy(supplier_code: str, connection=None) -> dict:
    code = normalize_supplier_code(supplier_code)
    if connection is None:
        init_db()
        with read_connection() as db:
            return current_policy(code, db)
    latest = _row(connection.execute(
        "SELECT supplier_code, revision, convention, updated_at FROM tax_supplier_policy_history "
        "WHERE supplier_code = ? ORDER BY revision DESC LIMIT 1",
        (code,),
    ).fetchone())
    if latest is None:
        return {
            "supplier_code": code,
            "convention": DEFAULT_TAX_CONVENTION,
            "revision": 0,
            "updated_at": None,
            "source": "default",
        }
    return {**latest, "source": "configured"}


def normalize_supplier_code(value: str) -> str:
    token = str(value if value is not None else "").strip()
    if not token:
        raise DomainError("EMPTY_SUPPLIER_CODE", "供应商编码不能为空")
    return token


def policy_history(supplier_code: str) -> list[dict]:
    code = normalize_supplier_code(supplier_code)
    init_db()
    with read_connection() as db:
        rows = db.execute(
            "SELECT supplier_code, revision, convention, updated_at FROM tax_supplier_policy_history "
            "WHERE supplier_code = ? ORDER BY revision",
            (code,),
        ).fetchall()
    return [dict(row) for row in rows]


def set_supplier_convention(supplier_code: str, convention: str, expected_revision: int | None = None) -> dict:
    """切换供应商约定。相同约定为幂等空操作；可选期望版本用于并发切换检测。"""
    code = normalize_supplier_code(supplier_code)
    target = normalize_convention(convention)
    if expected_revision is not None:
        try:
            expected_revision = int(expected_revision)
        except (TypeError, ValueError) as exc:
            raise DomainError("BAD_REVISION", "期望版本必须是整数") from exc

    init_db()
    with write_transaction() as db:
        policy = current_policy(code, db)
        if expected_revision is not None and expected_revision != policy["revision"]:
            raise DomainError(
                "CONVENTION_STALE",
                f"供应商约定已切换：期望版本 {expected_revision}，当前版本 {policy['revision']}",
                status=409,
            )
        if policy["source"] == "configured" and policy["convention"] == target:
            return {**policy, "changed": False}
        revision = policy["revision"] + 1
        if policy["source"] == "default":
            revision = 1
        stamped = now_text()
        db.execute(
            "INSERT INTO tax_supplier_policy_history (supplier_code, revision, convention, updated_at) VALUES (?, ?, ?, ?)",
            (code, revision, target, stamped),
        )
        return {
            "supplier_code": code,
            "convention": target,
            "revision": revision,
            "updated_at": stamped,
            "source": "configured",
            "changed": True,
            "previous": policy["convention"],
        }


def _policy_at(supplier_code: str, revision: int, connection) -> dict:
    if revision == LEGACY_REVISION:
        return {
            "supplier_code": supplier_code,
            "revision": LEGACY_REVISION,
            "convention": LEGACY_CONVENTION,
            "updated_at": None,
            "source": LEGACY_SOURCE,
        }
    row = _row(connection.execute(
        "SELECT supplier_code, revision, convention, updated_at FROM tax_supplier_policy_history "
        "WHERE supplier_code = ? AND revision = ?",
        (supplier_code, revision),
    ).fetchone())
    if row is None:
        raise DomainError(
            "UNKNOWN_POLICY_REVISION",
            f"供应商 {supplier_code} 不存在约定版本 {revision}",
            status=409,
        )
    return {**row, "source": "configured"}


# ---------------------------------------------------------------- 发票登记

def _canonical_number(value) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    return format(decimal_value(value).normalize(), "f")


def _canonical_lines(lines: list[dict]) -> list[dict]:
    canonical = []
    for index, line in enumerate(lines, start=1):
        raw_line_no = line.get("line_no", index)
        try:
            line_no = int(raw_line_no)
        except (TypeError, ValueError) as exc:
            raise ValueError("BAD_LINE_NUMBER") from exc
        rate = normalize_tax_rate(line.get("tax_rate"))
        canonical.append(
            {
                "line_no": line_no,
                "tax_rate": format(rate.normalize(), "f"),
                "net_amount": _canonical_number(line.get("net_amount")),
                "quantity": _canonical_number(line.get("quantity")),
                "unit_price": _canonical_number(line.get("unit_price")),
                "tax_amount": _canonical_number(line.get("tax_amount")),
            }
        )
    return canonical


def invoice_fingerprint(supplier_code: str, doc_no: str, lines: list[dict], invoice_gross: str) -> str:
    payload = {
        "supplier_code": supplier_code,
        "doc_no": doc_no,
        "invoice_gross": str(decimal_value(invoice_gross)),
        "lines": _canonical_lines(lines),
    }
    return sha256(_json(payload).encode("utf-8")).hexdigest()


def _line_taxes(lines: list[dict]) -> dict[int, Decimal]:
    taxes: dict[int, Decimal] = {}
    for index, line in enumerate(lines, start=1):
        line_no = int(line.get("line_no", index))
        raw = line.get("tax_amount")
        if raw is None or str(raw).strip() == "":
            continue
        taxes[line_no] = decimal_value(raw)
    return taxes


def _normalize_doc_no(value: str) -> str:
    token = str(value if value is not None else "").strip()
    if not token:
        raise DomainError("EMPTY_DOC_NO", "发票号不能为空")
    return token


def _serialize_calculation(calculation: dict) -> str:
    return _json(calculation)


def _verify_stored(calculation: dict, invoice_gross: str, line_taxes: dict[int, Decimal] | None) -> dict:
    return verify_calculation(calculation, invoice_gross, line_taxes)


def register_invoice(
    supplier_code: str,
    doc_no: str,
    lines: list[dict],
    invoice_gross: str,
    convention: str | None = None,
    expected_revision: int | None = None,
    source: str = "import",
) -> dict:
    """登记发票并绑定接收时的约定版本。

    - 若发票已以相同指纹登记，直接返回首次登记结果（created=False），不覆盖。
    - 若同号发票内容不同，拒绝登记（INVOICE_CONFLICT）。
    - expected_revision 指向历史版本时，绑定该历史版本，不受后续切换影响。
    """
    code = normalize_supplier_code(supplier_code)
    number = _normalize_doc_no(doc_no)
    fingerprint = invoice_fingerprint(code, number, lines, invoice_gross)
    stated_taxes = _line_taxes(lines)

    init_db()
    with write_transaction() as db:
        existing = _row(db.execute(
            "SELECT * FROM tax_invoices WHERE supplier_code = ? AND doc_no = ?",
            (code, number),
        ).fetchone())
        if existing is not None:
            if existing["fingerprint"] != fingerprint:
                raise DomainError(
                    "INVOICE_CONFLICT",
                    f"发票 {number} 已登记，且内容与本次不一致",
                    status=409,
                )
            return {**_deserialize_invoice(existing), "created": False, "duplicate": True}

        policy = current_policy(code, db)
        if expected_revision is not None:
            try:
                revision = int(expected_revision)
            except (TypeError, ValueError) as exc:
                raise DomainError("BAD_REVISION", "期望版本必须是整数") from exc
            # 在途发票保持其接收时的原版本：只要该版本仍在历史中即可绑定，
            # 即使供应商此后已经切换。
            policy = _policy_at(code, revision, db)
        if convention is not None and normalize_convention(convention) != policy["convention"]:
            raise DomainError(
                "CONVENTION_CONFLICT",
                f"请求约定 {normalize_convention(convention)} 与绑定约定 {policy['convention']} 不一致",
                status=409,
            )

        calculation = calculate_invoice(lines, policy["convention"])
        verification = _verify_stored(calculation, invoice_gross, stated_taxes)
        stamped = now_text()
        db.execute(
            "INSERT INTO tax_invoices (supplier_code, doc_no, policy_revision, convention, net_basis, calculation, "
            "invoice_gross, verification, fingerprint, source, registered_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                code,
                number,
                policy["revision"],
                policy["convention"],
                _json(calculation["net_basis"]),
                _serialize_calculation(calculation),
                str(decimal_value(invoice_gross)),
                _json(verification),
                fingerprint,
                source,
                stamped,
            ),
        )
        stored = _row(db.execute(
            "SELECT * FROM tax_invoices WHERE supplier_code = ? AND doc_no = ?",
            (code, number),
        ).fetchone())
        return {**_deserialize_invoice(stored), "created": True, "duplicate": False}


def _deserialize_invoice(row: dict) -> dict:
    calculation = json.loads(row["calculation"])
    return {
        "supplier_code": row["supplier_code"],
        "doc_no": row["doc_no"],
        "policy_revision": row["policy_revision"],
        "convention": row["convention"],
        "net_basis": json.loads(row["net_basis"]),
        "calculation": calculation,
        "total": calculation["total"],
        "groups": calculation["groups"],
        "lines": calculation["lines"],
        "invoice_gross": row["invoice_gross"],
        "verification": json.loads(row["verification"]),
        "fingerprint": row["fingerprint"],
        "source": row["source"],
        "registered_at": row["registered_at"],
    }


def get_invoice(supplier_code: str, doc_no: str) -> dict | None:
    code = normalize_supplier_code(supplier_code)
    number = _normalize_doc_no(doc_no)
    init_db()
    with read_connection() as db:
        row = _row(db.execute(
            "SELECT * FROM tax_invoices WHERE supplier_code = ? AND doc_no = ?",
            (code, number),
        ).fetchone())
    return _deserialize_invoice(row) if row else None


def list_invoices(supplier_code: str | None = None) -> list[dict]:
    init_db()
    with read_connection() as db:
        if supplier_code:
            rows = db.execute(
                "SELECT * FROM tax_invoices WHERE supplier_code = ? ORDER BY registered_at, doc_no",
                (normalize_supplier_code(supplier_code),),
            ).fetchall()
        else:
            rows = db.execute("SELECT * FROM tax_invoices ORDER BY supplier_code, registered_at, doc_no").fetchall()
    return [_deserialize_invoice(dict(row)) for row in rows]


def review_invoice(supplier_code: str, doc_no: str) -> dict:
    """回看已登记发票在“当前约定”下的假设金额；不写回、不改变原凭据。"""
    invoice = get_invoice(supplier_code, doc_no)
    if invoice is None:
        raise DomainError("INVOICE_NOT_FOUND", f"发票 {doc_no} 未登记", status=404)
    policy = current_policy(supplier_code)
    lines = [
        {
            "line_no": line["line_no"],
            "sku": line.get("sku", ""),
            "tax_rate": line["tax_rate"],
            "net_amount": line["raw_net"],
        }
        for line in invoice["lines"]
    ]
    hypothetical = calculate_invoice(lines, policy["convention"])
    bound_total = invoice["total"]
    deltas = {}
    for key in ("net", "tax", "gross"):
        delta = money(Decimal(hypothetical["total"][key]) - Decimal(bound_total[key]))
        deltas[key] = str(abs(delta) if delta == 0 else delta)  # 不输出 -0.00
    return {
        "supplier_code": invoice["supplier_code"],
        "doc_no": invoice["doc_no"],
        "bound": {
            "convention": invoice["convention"],
            "policy_revision": invoice["policy_revision"],
            "net_basis": invoice["net_basis"],
        },
        "current_policy": {"convention": policy["convention"], "revision": policy["revision"]},
        "convention_changed": invoice["convention"] != policy["convention"],
        "bound_total": bound_total,
        "hypothetical_total": hypothetical["total"],
        "delta": deltas,
        "hypothetical_groups": hypothetical["groups"],
        "hypothetical_verified": verify_calculation(hypothetical, invoice["invoice_gross"]),
        "verification": invoice["verification"],
    }


# ---------------------------------------------------------------- 旧凭据迁移

def queue_legacy(credentials: list[dict]) -> int:
    """把旧凭据放入待迁移队列。

    - 新凭据入队；重复入队幂等。
    - 已拒绝的凭据只有在原始内容被更正后才重新排队，已迁移记录永不覆盖。
    """
    init_db()
    queued = 0
    with write_transaction() as db:
        for credential in credentials:
            credential_id = str(credential.get("credential_id", "")).strip()
            supplier_code = str(credential.get("supplier_code", "")).strip()
            doc_no = str(credential.get("doc_no", "")).strip()
            if not credential_id or not supplier_code or not doc_no:
                raise DomainError("BAD_LEGACY_CREDENTIAL", "旧凭据必须包含 credential_id、supplier_code 与 doc_no")
            document = _json(credential)
            stamped = now_text()
            before = db.total_changes
            db.execute(
                "INSERT INTO legacy_tax_credentials "
                "(credential_id, supplier_code, doc_no, document, state, attempts, reason_code, updated_at) "
                "VALUES (?, ?, ?, ?, 'pending', 0, NULL, ?) "
                "ON CONFLICT(credential_id) DO UPDATE SET "
                "document = excluded.document, state = 'pending', reason_code = NULL, updated_at = excluded.updated_at "
                "WHERE legacy_tax_credentials.state = 'rejected' AND legacy_tax_credentials.document <> excluded.document",
                (credential_id, supplier_code, doc_no, document, stamped),
            )
            queued += db.total_changes > before
    return queued


def _migrate_one(db, row) -> dict:
    document = json.loads(row["document"])
    lines = document.get("lines") or []
    invoice_gross = document.get("invoice_gross")
    credential_id = row["credential_id"]
    if invoice_gross is None or str(invoice_gross).strip() == "":
        return _settle(db, credential_id, "rejected", "EVIDENCE_INSUFFICIENT",
                       "旧凭据缺少发票含税额，拒绝猜算")

    try:
        fingerprint = invoice_fingerprint(row["supplier_code"], row["doc_no"], lines, invoice_gross)
        stated_taxes = _line_taxes(lines)
    except ValueError as exc:
        code = str(exc)
        reason = code if code in _KNOWN_REASONS else "EVIDENCE_INSUFFICIENT"
        return _settle(db, credential_id, "rejected", reason, _REASONS_TEXT.get(reason, "旧凭据无法复核"))

    existing = _row(db.execute(
        "SELECT * FROM tax_invoices WHERE supplier_code = ? AND doc_no = ?",
        (row["supplier_code"], row["doc_no"]),
    ).fetchone())
    if existing is not None:
        if existing["fingerprint"] == fingerprint:
            return _settle(db, credential_id, "migrated", None, "已登记，内容一致", duplicate=True)
        return _settle(db, credential_id, "rejected", "REGISTRATION_CONFLICT",
                       "同号发票已存在且内容不同")

    try:
        calculation = calculate_invoice(lines, LEGACY_CONVENTION)
    except ValueError as exc:
        code = str(exc)
        reason = code if code in _KNOWN_REASONS else "EVIDENCE_INSUFFICIENT"
        return _settle(db, credential_id, "rejected", reason, _REASONS_TEXT.get(reason, "旧凭据无法复核"))

    verification = verify_calculation(calculation, invoice_gross, stated_taxes)
    db.execute(
        "INSERT INTO tax_invoices (supplier_code, doc_no, policy_revision, convention, net_basis, "
        "calculation, invoice_gross, verification, fingerprint, source, registered_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            row["supplier_code"],
            row["doc_no"],
            LEGACY_REVISION,
            LEGACY_CONVENTION,
            _json(calculation["net_basis"]),
            _serialize_calculation(calculation),
            str(decimal_value(invoice_gross)),
            _json(verification),
            fingerprint,
            LEGACY_SOURCE,
            now_text(),
        ),
    )
    return _settle(db, credential_id, "migrated", None, "已按原逐行税额迁移")


_KNOWN_REASONS = {"EVIDENCE_INSUFFICIENT", "MIXED_CONVENTION", "CONVENTION_CONFLICT", "BAD_AMOUNT",
                  "BAD_TAX_RATE", "BAD_LINE_NUMBER", "DUPLICATE_LINE", "EMPTY_INVOICE"}
_REASONS_TEXT = {
    "EVIDENCE_INSUFFICIENT": "旧凭据净额依据不足，拒绝猜算",
    "MIXED_CONVENTION": "旧凭据混用计税约定",
    "CONVENTION_CONFLICT": "旧凭据约定的计税方式与数据冲突",
    "BAD_AMOUNT": "旧凭据金额格式不合法",
    "BAD_TAX_RATE": "旧凭据税率格式不合法",
    "BAD_LINE_NUMBER": "旧凭据行号不合法",
    "DUPLICATE_LINE": "旧凭据行号重复",
    "EMPTY_INVOICE": "旧凭据没有可核验的行",
}


def _settle(db, credential_id: str, state: str, reason_code: str | None, message: str, duplicate: bool = False) -> dict:
    db.execute(
        "UPDATE legacy_tax_credentials SET state = ?, reason_code = ?, attempts = attempts + 1, updated_at = ? "
        "WHERE credential_id = ?",
        (state, reason_code, now_text(), credential_id),
    )
    return {"credential_id": credential_id, "state": state, "reason_code": reason_code, "message": message, "duplicate": duplicate}


def migrate_legacy(batch_size: int | None = None) -> dict:
    """逐条迁移 pending 旧凭据；每条独立事务，异常中断后已提交者保留，其余可恢复。"""
    init_db()
    with read_connection() as db:
        pending_ids = [row["credential_id"] for row in db.execute(
            "SELECT credential_id FROM legacy_tax_credentials WHERE state IN ('pending', 'failed') "
            "ORDER BY credential_id"
        ).fetchall()]
    if batch_size is not None:
        pending_ids = pending_ids[: max(int(batch_size), 0)]

    processed = []
    for credential_id in pending_ids:
        with write_transaction() as db:
            row = _row(db.execute(
                "SELECT * FROM legacy_tax_credentials WHERE credential_id = ? AND state IN ('pending', 'failed')",
                (credential_id,),
            ).fetchone())
            if row is None:
                continue
            processed.append(_migrate_one(db, row))

    with read_connection() as db:
        remaining = db.execute(
            "SELECT COUNT(*) FROM legacy_tax_credentials WHERE state IN ('pending', 'failed')"
        ).fetchone()[0]
        states = {row["state"]: row["count"] for row in db.execute(
            "SELECT state, COUNT(*) AS count FROM legacy_tax_credentials GROUP BY state"
        ).fetchall()}
    return {
        "processed": processed,
        "migrated": [item["credential_id"] for item in processed if item["state"] == "migrated"],
        "rejected": [item for item in processed if item["state"] == "rejected"],
        "pending": remaining,
        "complete": remaining == 0,
        "states": states,
    }


def legacy_status() -> list[dict]:
    init_db()
    with read_connection() as db:
        rows = db.execute(
            "SELECT credential_id, supplier_code, doc_no, state, attempts, reason_code, updated_at "
            "FROM legacy_tax_credentials ORDER BY credential_id"
        ).fetchall()
    return [dict(row) for row in rows]


def tax_summary() -> dict:
    """税额页汇总：当前约定、已登记发票与仍待迁移的旧凭据。"""
    init_db()
    with read_connection() as db:
        supplier_rows = db.execute(
            "SELECT supplier_code, MAX(revision) AS revision FROM tax_supplier_policy_history GROUP BY supplier_code"
        ).fetchall()
        suppliers = []
        for item in supplier_rows:
            policy = current_policy(item["supplier_code"], db)
            suppliers.append(policy)
        invoices = db.execute(
            "SELECT supplier_code, doc_no, convention, policy_revision, source, registered_at, calculation, verification "
            "FROM tax_invoices ORDER BY supplier_code, registered_at, doc_no"
        ).fetchall()
        invoices = [
            {
                "supplier_code": row["supplier_code"],
                "doc_no": row["doc_no"],
                "convention": row["convention"],
                "policy_revision": row["policy_revision"],
                "source": row["source"],
                "registered_at": row["registered_at"],
                "total": json.loads(row["calculation"])["total"],
                "status": json.loads(row["verification"])["status"],
            }
            for row in invoices
        ]
        legacy = db.execute(
            "SELECT state, COUNT(*) AS count FROM legacy_tax_credentials GROUP BY state"
        ).fetchall()
    conventions = {}
    for invoice in invoices:
        conventions.setdefault(invoice["convention"], 0)
        conventions[invoice["convention"]] += 1
    return {
        "suppliers": suppliers,
        "invoices": invoices,
        "legacy": {row["state"]: row["count"] for row in legacy},
        "conventions": conventions,
    }
