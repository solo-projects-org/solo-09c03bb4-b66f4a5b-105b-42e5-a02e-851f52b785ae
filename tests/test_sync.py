"""月报同步 API 测试：序号、每月折叠、冻结轮次、分页与重启续页。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.schemas import (
    EX_MONTHLY_PUBLISH,
    EX_MONTHLY_RETRACTION,
)
from app.sync import decode_page_token, encode_page_token


def _full_table(month: str, corner: int = 5) -> dict:
    return {
        "stat_month": month,
        "change_type": "full_table",
        "table": {
            "matrix": [[corner, None, None], [None, 3, None], [None, None, 2]],
            "row_totals": [corner + 7, 10, 8],
            "col_totals": [corner + 4, 9, 12],
        },
    }


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    # 每个测试使用独立 SQLite 文件，避免与默认 data/ 目录互相污染
    db_file = tmp_path / "sync_test.db"
    monkeypatch.setenv("SYNC_DB_PATH", str(db_file))
    with TestClient(app) as c:  # lifespan 触发建表
        yield c


def _submit(client: TestClient, payload: dict) -> dict:
    resp = client.post("/api/v1/monthly-reports", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# 提交与全局序号
# ---------------------------------------------------------------------------

def test_submit_assigns_globally_increasing_seq(client):
    first = _submit(client, EX_MONTHLY_PUBLISH)
    second = _submit(client, _full_table("2026-10"))
    third = _submit(client, EX_MONTHLY_RETRACTION)
    assert [first["seq"], second["seq"], third["seq"]] == [1, 2, 3]
    assert first["stat_month"] == "2026-09"
    assert first["change_type"] == "full_table"
    assert third["change_type"] == "retraction"
    assert first["committed_at"].endswith("Z")


def test_submit_validation_errors(client):
    # 统计月格式错误
    resp = client.post(
        "/api/v1/monthly-reports",
        json={"stat_month": "2026/09", "change_type": "retraction"},
    )
    assert resp.status_code == 422

    # full_table 缺少整表
    resp = client.post(
        "/api/v1/monthly-reports",
        json={"stat_month": "2026-09", "change_type": "full_table"},
    )
    assert resp.status_code == 422

    # retraction 携带整表
    resp = client.post(
        "/api/v1/monthly-reports",
        json={**EX_MONTHLY_RETRACTION, "table": EX_MONTHLY_PUBLISH["table"]},
    )
    assert resp.status_code == 422

    # 整表沿用遮盖矩阵格式：负数/形状不符仍被拒绝
    bad_table = {
        "stat_month": "2026-09",
        "change_type": "full_table",
        "table": {
            "matrix": [[-1, None]],
            "row_totals": [0],
            "col_totals": [-1, 0],
        },
    }
    assert client.post("/api/v1/monthly-reports", json=bad_table).status_code == 422


# ---------------------------------------------------------------------------
# 首页、每月折叠与分页
# ---------------------------------------------------------------------------

def test_first_sync_returns_effective_changes_sorted(client):
    _submit(client, _full_table("2026-09"))          # seq 1
    _submit(client, _full_table("2026-09", corner=6))  # seq 2 更正
    _submit(client, _full_table("2026-10"))          # seq 3
    _submit(client, EX_MONTHLY_RETRACTION)           # seq 4 撤刊 09 月

    resp = client.get("/api/v1/monthly-reports/sync?after_seq=0&page_size=10")
    assert resp.status_code == 200
    body = resp.json()
    # 09 月只保留最后一笔（seq 4 撤刊），共两个月两条，按 seq 升序
    assert [c["seq"] for c in body["changes"]] == [3, 4]
    assert body["changes"][0]["stat_month"] == "2026-10"
    assert body["changes"][0]["change_type"] == "full_table"
    assert body["changes"][0]["table"]["matrix"][0][0] == 5
    assert body["changes"][1]["change_type"] == "retraction"
    assert body["changes"][1]["table"] is None
    assert body["cutoff_seq"] == 4
    assert body["has_more"] is False
    assert body["next_page_token"] is None


def test_pagination_walks_all_pages(client):
    # 5 个不同月份，每页 2 条
    for month in ("2026-01", "2026-02", "2026-03", "2026-04", "2026-05"):
        _submit(client, _full_table(month))

    seen: list[int] = []
    token = None
    cutoff = None
    for page_no in range(3):
        url = "/api/v1/monthly-reports/sync?after_seq=0&page_size=2"
        if token:
            url += f"&page_token={token}"
        body = client.get(url).json()
        cutoff = body["cutoff_seq"]
        seen.extend(c["seq"] for c in body["changes"])
        if page_no < 2:
            assert body["has_more"] is True
            token = body["next_page_token"]
            assert token
        else:
            assert body["has_more"] is False
            assert body["next_page_token"] is None

    assert seen == [1, 2, 3, 4, 5]
    assert cutoff == 5


def test_retry_same_token_returns_same_page(client):
    for month in ("2026-01", "2026-02", "2026-03", "2026-04"):
        _submit(client, _full_table(month))

    first = client.get(
        "/api/v1/monthly-reports/sync?after_seq=0&page_size=2"
    ).json()
    token = first["next_page_token"]

    # 轮次冻结后新提交两笔，续页与重试都看不到新变更
    _submit(client, _full_table("2026-07"))
    _submit(client, _full_table("2026-08"))

    page_two_a = client.get(
        f"/api/v1/monthly-reports/sync?after_seq=0&page_size=2&page_token={token}"
    ).json()
    page_two_b = client.get(
        f"/api/v1/monthly-reports/sync?after_seq=0&page_size=2&page_token={token}"
    ).json()
    assert page_two_a == page_two_b
    assert [c["seq"] for c in page_two_a["changes"]] == [3, 4]
    assert page_two_a["cutoff_seq"] == 4
    assert page_two_a["has_more"] is False

    # 用末页截止序号开新一轮，新变更可见
    nxt = client.get(
        "/api/v1/monthly-reports/sync?after_seq=4&page_size=10"
    ).json()
    assert [c["seq"] for c in nxt["changes"]] == [5, 6]
    assert nxt["cutoff_seq"] == 6


def test_round_survives_restart(client, tmp_path):
    _submit(client, _full_table("2026-01"))
    _submit(client, _full_table("2026-02"))
    _submit(client, _full_table("2026-03"))

    body = client.get(
        "/api/v1/monthly-reports/sync?after_seq=0&page_size=2"
    ).json()
    assert body["cutoff_seq"] == 3
    token = body["next_page_token"]

    # 模拟服务重启：丢弃全部连接，重新建应用实例，SQLite 文件保留
    del client
    with TestClient(app) as restarted:
        # 重启后用原令牌续页，仍按原轮次
        page_two = restarted.get(
            f"/api/v1/monthly-reports/sync?after_seq=0&page_size=2"
            f"&page_token={token}"
        ).json()
        assert [c["seq"] for c in page_two["changes"]] == [3]
        assert page_two["cutoff_seq"] == 3
        assert page_two["has_more"] is False

        # 同一游标重新取首页仍命中冻结轮次
        again = restarted.get(
            "/api/v1/monthly-reports/sync?after_seq=0&page_size=10"
        ).json()
        assert again["cutoff_seq"] == 3
        assert len(again["changes"]) == 3


def test_correction_after_round_opened_is_invisible_until_next_round(client):
    _submit(client, _full_table("2026-09"))  # seq 1
    body = client.get(
        "/api/v1/monthly-reports/sync?after_seq=0&page_size=10"
    ).json()
    assert body["cutoff_seq"] == 1
    assert body["changes"][0]["table"]["matrix"][0][0] == 5

    # 轮次冻结后更正同月并撤刊另一月
    _submit(client, _full_table("2026-09", corner=6))  # seq 2
    _submit(client, _full_table("2026-10"))            # seq 3

    retry = client.get(
        "/api/v1/monthly-reports/sync?after_seq=0&page_size=10"
    ).json()
    assert retry["cutoff_seq"] == 1
    assert len(retry["changes"]) == 1
    assert retry["changes"][0]["seq"] == 1

    nxt = client.get(
        "/api/v1/monthly-reports/sync?after_seq=1&page_size=10"
    ).json()
    # 窗口 (1,3]：09 月折叠到 seq 2，10 月 seq 3
    assert [c["seq"] for c in nxt["changes"]] == [2, 3]
    assert nxt["changes"][0]["table"]["matrix"][0][0] == 6
    assert nxt["cutoff_seq"] == 3


def test_empty_when_no_new_changes(client):
    body = client.get(
        "/api/v1/monthly-reports/sync?after_seq=0&page_size=10"
    ).json()
    assert body["changes"] == []
    assert body["cutoff_seq"] == 0
    assert body["has_more"] is False
    assert body["next_page_token"] is None

    # 来一笔后同游标可正常开轮
    _submit(client, _full_table("2026-09"))
    body = client.get(
        "/api/v1/monthly-reports/sync?after_seq=0&page_size=10"
    ).json()
    assert len(body["changes"]) == 1
    assert body["cutoff_seq"] == 1


def test_after_seq_in_window_excludes_earlier_changes(client):
    for month in ("2026-01", "2026-02", "2026-03"):
        _submit(client, _full_table(month))
    body = client.get(
        "/api/v1/monthly-reports/sync?after_seq=1&page_size=10"
    ).json()
    assert [c["seq"] for c in body["changes"]] == [2, 3]
    assert body["after_seq"] == 1
    assert body["cutoff_seq"] == 3


def test_token_cursor_mismatch_returns_400(client):
    for month in ("2026-01", "2026-02", "2026-03"):
        _submit(client, _full_table(month))
    token = client.get(
        "/api/v1/monthly-reports/sync?after_seq=0&page_size=2"
    ).json()["next_page_token"]

    resp = client.get(
        f"/api/v1/monthly-reports/sync?after_seq=1&page_size=2&page_token={token}"
    )
    assert resp.status_code == 400
    assert "不匹配" in resp.json()["detail"]

    # 损坏令牌
    resp = client.get(
        "/api/v1/monthly-reports/sync?after_seq=0&page_size=2&page_token=not-base64!!"
    )
    assert resp.status_code == 400


def test_page_size_bounds(client):
    assert client.get(
        "/api/v1/monthly-reports/sync?after_seq=0&page_size=0"
    ).status_code == 422
    assert client.get(
        "/api/v1/monthly-reports/sync?after_seq=0&page_size=101"
    ).status_code == 422
    assert client.get(
        "/api/v1/monthly-reports/sync?after_seq=-1&page_size=10"
    ).status_code == 422


def test_token_helpers_roundtrip():
    token = encode_page_token(after_seq=7, cutoff_seq=12, offset=3)
    assert decode_page_token(token) == {
        "after_seq": 7,
        "cutoff_seq": 12,
        "offset": 3,
    }
