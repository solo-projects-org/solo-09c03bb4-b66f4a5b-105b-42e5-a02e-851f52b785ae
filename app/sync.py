"""月报同步的业务逻辑：轮次冻结、每月折叠与游标分页。

分页协议
--------
- 下游携带 ``after_seq``（上次收完的序号，首次为 0）与 ``page_size`` 请求首页；
- 服务端以查询开始时已提交的最大序号冻结本轮 ``cutoff_seq`` 并落库，
  之后同一游标的任何重试（含重启、期间有新提交）都按同一轮次续页；
- 轮次窗口 ``(after_seq, cutoff_seq]`` 内每个统计月只取最后一笔变更，
  按序号升序分页；
- 续页令牌为不透明 base64，内含游标、轮次截止与偏移；
- 末页 ``has_more=false``，并在 ``cutoff_seq`` 给出下一轮的新游标。
"""

from __future__ import annotations

import base64
import json
import sqlite3

from . import db

_MAX_PAGE_SIZE = 100


def encode_page_token(after_seq: int, cutoff_seq: int, offset: int) -> str:
    raw = json.dumps(
        {"after_seq": after_seq, "cutoff_seq": cutoff_seq, "offset": offset},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_page_token(token: str) -> dict:
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii"))
        data = json.loads(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError("page_token 无法解析") from exc
    if not isinstance(data, dict) or set(data) != {
        "after_seq",
        "cutoff_seq",
        "offset",
    }:
        raise ValueError("page_token 内容不完整")
    try:
        after_seq = int(data["after_seq"])
        cutoff_seq = int(data["cutoff_seq"])
        offset = int(data["offset"])
    except (TypeError, ValueError) as exc:
        raise ValueError("page_token 字段类型错误") from exc
    if after_seq < 0 or cutoff_seq < after_seq or offset < 0:
        raise ValueError("page_token 字段取值非法")
    return {
        "after_seq": after_seq,
        "cutoff_seq": cutoff_seq,
        "offset": offset,
    }


class SyncTokenError(ValueError):
    """分页令牌与请求参数不匹配，返回 HTTP 400。"""


def _resolve_round(
    conn: sqlite3.Connection, after_seq: int
) -> int | None:
    """复用已冻结轮次或建立新轮次，返回截止序号；无可用变更时为 None。"""
    return db.get_or_create_round(conn, after_seq)


def pull_page(
    after_seq: int,
    page_size: int,
    page_token: str | None,
    conn: sqlite3.Connection,
) -> dict:
    """执行一次同步查询，返回可直接序列化为 ``SyncPageResponse`` 的字典。"""
    if page_token:
        token = decode_page_token(page_token)
        if token["after_seq"] != after_seq:
            raise SyncTokenError(
                "page_token 属于游标 "
                f"after_seq={token['after_seq']} 的轮次，与当前 after_seq="
                f"{after_seq} 不匹配；请用该游标重新取首页"
            )
        cutoff_seq = token["cutoff_seq"]
        offset = token["offset"]
        # 重启后续页：轮次行仍在 SQLite 中；校验令牌中的截止序号与冻结轮一致
        row = conn.execute(
            "SELECT cutoff_seq FROM sync_rounds WHERE after_seq = ?",
            (after_seq,),
        ).fetchone()
        if row is not None and int(row["cutoff_seq"]) != cutoff_seq:
            raise SyncTokenError(
                "page_token 的截止序号与已冻结轮次不一致，请重新取首页"
            )
    else:
        cutoff = _resolve_round(conn, after_seq)
        if cutoff is None:
            # 游标之后暂无变更：返回空页，不冻结轮次，客户端稍后用同游标重试
            return {
                "after_seq": after_seq,
                "cutoff_seq": after_seq,
                "page_size": page_size,
                "offset": 0,
                "has_more": False,
                "next_page_token": None,
                "changes": [],
            }
        cutoff_seq = cutoff
        offset = 0

    total = db.count_effective_changes(conn, after_seq, cutoff_seq)
    if offset > total:
        # 损坏/越界令牌
        raise SyncTokenError("page_token 的偏移超出本轮变更总数")

    changes = db.fetch_effective_page(
        conn, after_seq, cutoff_seq, page_size, offset
    )
    next_offset = offset + len(changes)
    has_more = next_offset < total
    next_token = (
        encode_page_token(after_seq, cutoff_seq, next_offset) if has_more else None
    )
    return {
        "after_seq": after_seq,
        "cutoff_seq": cutoff_seq,
        "page_size": page_size,
        "offset": offset,
        "has_more": has_more,
        "next_page_token": next_token,
        "changes": changes,
    }
