"""SQLite 持久层：医院月报变更流与下游同步轮次。

两张表
------
``report_changes``
    院方每提交一笔（发布/更正整表、撤刊通知）就追加一行，``seq`` 为
    ``INTEGER PRIMARY KEY AUTOINCREMENT``，在库内全局递增、永不复用。

``sync_rounds``
    下游每个游标 ``after_seq`` 首次发起同步时冻结一轮：截止序号
    ``cutoff_seq`` 取该时刻已提交的最大 ``seq``。轮次落库后，同一游标的
    重试（含服务重启）都按同一截止序号续页，期间新提交的变更不可见。
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

# 容器内通过 SYNC_DB_PATH 覆盖为挂载卷 /data/sync.db
DEFAULT_DB_PATH = os.path.join("data", "monthly_reports.db")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS report_changes (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    stat_month   TEXT NOT NULL,
    change_type  TEXT NOT NULL CHECK (change_type IN ('full_table', 'retraction')),
    table_json   TEXT,
    committed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_changes_month_seq
    ON report_changes (stat_month, seq);

CREATE TABLE IF NOT EXISTS sync_rounds (
    after_seq   INTEGER PRIMARY KEY,
    cutoff_seq  INTEGER NOT NULL,
    created_at  TEXT NOT NULL
);
"""


def get_db_path() -> str:
    return os.environ.get("SYNC_DB_PATH", DEFAULT_DB_PATH)


def utc_now_iso() -> str:
    """UTC 时间，ISO 8601 秒精度，Z 后缀。"""
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or get_db_path()
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    # WAL 允许读写并发；busy_timeout 在锁竞争时等待而不是立即报错
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: str | None = None) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def session(db_path: str | None = None) -> Iterator[sqlite3.Connection]:
    """每请求一个短连接，正常退出提交，异常回滚。"""
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 变更写入
# ---------------------------------------------------------------------------

def insert_change(
    conn: sqlite3.Connection,
    stat_month: str,
    change_type: str,
    table: dict | None,
) -> dict:
    """追加一笔月报变更，返回含全局递增序号的记录。"""
    committed_at = utc_now_iso()
    table_json = (
        json.dumps(table, ensure_ascii=False) if table is not None else None
    )
    cur = conn.execute(
        """
        INSERT INTO report_changes
            (stat_month, change_type, table_json, committed_at)
        VALUES (?, ?, ?, ?)
        """,
        (stat_month, change_type, table_json, committed_at),
    )
    return {
        "seq": cur.lastrowid,
        "stat_month": stat_month,
        "change_type": change_type,
        "committed_at": committed_at,
        "table": table,
    }


def max_committed_seq(conn: sqlite3.Connection) -> int:
    """当前已提交的最大序号；库为空时返回 0。"""
    return conn.execute(
        "SELECT COALESCE(MAX(seq), 0) FROM report_changes"
    ).fetchone()[0]


# ---------------------------------------------------------------------------
# 同步轮次
# ---------------------------------------------------------------------------

def get_or_create_round(conn: sqlite3.Connection, after_seq: int) -> int | None:
    """返回该游标的轮次截止序号。

    轮次已存在（含服务重启后）则原样复用；否则以查询开始时的最大序号
    建立新轮次。游标之后没有任何变更时返回 ``None``，不落轮次行，
    便于客户端稍后用同一游标重新发起。
    """
    row = conn.execute(
        "SELECT cutoff_seq FROM sync_rounds WHERE after_seq = ?",
        (after_seq,),
    ).fetchone()
    if row is not None:
        return int(row["cutoff_seq"])

    cutoff = max_committed_seq(conn)
    if cutoff <= after_seq:
        return None

    try:
        conn.execute(
            "INSERT INTO sync_rounds (after_seq, cutoff_seq, created_at) "
            "VALUES (?, ?, ?)",
            (after_seq, cutoff, utc_now_iso()),
        )
    except sqlite3.IntegrityError:
        # 并发请求已为同一游标建轮，复用对方冻结的截止序号
        conn.rollback()
        row = conn.execute(
            "SELECT cutoff_seq FROM sync_rounds WHERE after_seq = ?",
            (after_seq,),
        ).fetchone()
        return int(row["cutoff_seq"])
    return cutoff


# 窗口 (after_seq, cutoff_seq] 内，每个统计月只保留最后一笔变更，
# 再按序号升序排列。相关子查询对每一行求同月最大序号。
_WHERE_WINDOW = "c.seq > ? AND c.seq <= ?"
_MONTH_LAST = (
    "c.seq = ("
    "SELECT MAX(c2.seq) FROM report_changes c2 "
    "WHERE c2.stat_month = c.stat_month AND c2.seq > ? AND c2.seq <= ?"
    ")"
)


def count_effective_changes(
    conn: sqlite3.Connection, after_seq: int, cutoff_seq: int
) -> int:
    return conn.execute(
        f"""
        SELECT COUNT(*) FROM report_changes c
        WHERE {_WHERE_WINDOW} AND {_MONTH_LAST}
        """,
        (after_seq, cutoff_seq, after_seq, cutoff_seq),
    ).fetchone()[0]


def fetch_effective_page(
    conn: sqlite3.Connection,
    after_seq: int,
    cutoff_seq: int,
    limit: int,
    offset: int,
) -> list[dict]:
    rows = conn.execute(
        f"""
        SELECT c.seq, c.stat_month, c.change_type,
               c.table_json, c.committed_at
        FROM report_changes c
        WHERE {_WHERE_WINDOW} AND {_MONTH_LAST}
        ORDER BY c.seq ASC
        LIMIT ? OFFSET ?
        """,
        (after_seq, cutoff_seq, after_seq, cutoff_seq, limit, offset),
    ).fetchall()
    items: list[dict] = []
    for row in rows:
        items.append(
            {
                "seq": row["seq"],
                "stat_month": row["stat_month"],
                "change_type": row["change_type"],
                "committed_at": row["committed_at"],
                "table": (
                    json.loads(row["table_json"])
                    if row["table_json"] is not None
                    else None
                ),
            }
        )
    return items
