import sqlite3
from contextlib import closing, contextmanager

from app.config import DATABASE_PATH

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, input_sha256 TEXT NOT NULL, accepted INTEGER NOT NULL, quarantined INTEGER NOT NULL)",
    """CREATE TABLE IF NOT EXISTS tax_supplier_policy_history (
        supplier_code TEXT NOT NULL,
        revision INTEGER NOT NULL,
        convention TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (supplier_code, revision)
    )""",
    """CREATE TABLE IF NOT EXISTS tax_invoices (
        supplier_code TEXT NOT NULL,
        doc_no TEXT NOT NULL,
        policy_revision INTEGER NOT NULL,
        convention TEXT NOT NULL,
        net_basis TEXT NOT NULL,
        calculation TEXT NOT NULL,
        invoice_gross TEXT NOT NULL,
        verification TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        source TEXT NOT NULL,
        registered_at TEXT NOT NULL,
        PRIMARY KEY (supplier_code, doc_no)
    )""",
    """CREATE TABLE IF NOT EXISTS legacy_tax_credentials (
        credential_id TEXT PRIMARY KEY,
        supplier_code TEXT NOT NULL,
        doc_no TEXT NOT NULL,
        document TEXT NOT NULL,
        state TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        reason_code TEXT,
        updated_at TEXT NOT NULL
    )""",
)


def connect():
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def init_db():
    with closing(connect()) as db:
        for statement in SCHEMA:
            db.execute(statement)


class Transaction:
    """显式写事务：并发切换与登记在同一把写锁内读取彼此的最新状态。"""

    def __init__(self):
        self.connection = None

    def __enter__(self):
        init_db()
        self.connection = connect()
        self.connection.execute("BEGIN IMMEDIATE")
        return self.connection

    def __exit__(self, exc_type, exc, traceback):
        try:
            if exc_type is None:
                self.connection.execute("COMMIT")
            else:
                self.connection.execute("ROLLBACK")
        finally:
            self.connection.close()
        return False


def write_transaction() -> Transaction:
    return Transaction()


@contextmanager
def read_connection():
    init_db()
    connection = connect()
    try:
        yield connection
    finally:
        connection.close()


def record_run(manifest: dict) -> bool:
    init_db()
    with write_transaction() as db:
        before = db.total_changes
        db.execute("INSERT OR IGNORE INTO runs VALUES (?, ?, ?, ?)", (manifest["run_id"], manifest["input_sha256"], manifest["accepted"], manifest["quarantined"]))
        return db.total_changes > before


def run_count() -> int:
    init_db()
    with read_connection() as db:
        return db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
