"""FastAPI 应用：遮盖值反推核查 + 医院月报同步。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Body, FastAPI, Query
from fastapi.responses import JSONResponse

from . import db, solver, sync
from .schemas import (
    ConflictResponse,
    EX_HALL_CONFLICT,
    EX_MONTHLY_PUBLISH,
    EX_MONTHLY_RETRACTION,
    EX_RANGE,
    EX_RECOVERABLE,
    EX_TOTAL_CONFLICT,
    FeasibleResponse,
    HealthResponse,
    ReportSubmission,
    SubmissionResponse,
    SuppressionRequest,
    SyncPageResponse,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动即建表，保证重启后变更流与同步轮次仍在同一 SQLite 文件中
    db.init_db()
    yield


app = FastAPI(
    title="医院计数矩阵遮盖核查与月报同步 API",
    description=(
        "发布分年龄组、乡镇的病例计数前，检查 null 遮盖格能否被公开的"
        "行/列合计精确反推；并接收院方按统计月提交的已发布整表或撤刊通知，"
        "向下游数仓提供带全局序号与冻结轮次的增量同步。"
    ),
    version="1.1.0",
    lifespan=lifespan,
)


@app.get(
    "/health",
    response_model=HealthResponse,
    tags=["运维"],
    summary="健康检查",
)
def health() -> dict:
    return {"status": "ok", "service": "suppression-check"}


@app.post(
    "/api/v1/suppression-check",
    response_model=FeasibleResponse,
    responses={409: {"model": ConflictResponse, "description": "约束无解"}},
    tags=["核查"],
    summary="提交计数矩阵与公开合计，核查隐藏格反推风险",
)
def suppression_check(
    payload: Annotated[
        SuppressionRequest,
        Body(
            openapi_examples={
                "feasible_range": {
                    "summary": "可行：隐藏格存在取值区间（不可还原）",
                    "description": "3x3 矩阵，6 个遮盖格只能确定到区间，工程师需调整遮盖位置",
                    "value": EX_RANGE,
                },
                "feasible_recoverable": {
                    "summary": "可行：隐藏格被合计唯一锁定（可还原）",
                    "description": "4 个遮盖格的 min == max，存在精确反推风险",
                    "value": EX_RECOVERABLE,
                },
                "conflict_totals": {
                    "summary": "冲突：行列总合计不相等",
                    "value": EX_TOTAL_CONFLICT,
                },
                "conflict_hall": {
                    "summary": "冲突：总量相等但结构无解（Hall 条件）",
                    "description": "最大流小于所需流量，返回最小割冲突见证",
                    "value": EX_HALL_CONFLICT,
                },
            }
        ),
    ]
):
    try:
        result = solver.analyze(
            matrix=payload.matrix,
            row_totals=payload.row_totals,
            col_totals=payload.col_totals,
        )
    except solver.SuppressionConflict as exc:
        return JSONResponse(
            status_code=409,
            content={
                "status": "conflict",
                "reasons": exc.reasons,
                "witness": exc.witness,
            },
        )
    return result


@app.get(
    "/method",
    tags=["核查"],
    summary="算法说明",
)
def method() -> dict:
    return solver.fraction_explanation()


# ---------------------------------------------------------------------------
# 月报同步
# ---------------------------------------------------------------------------


@app.post(
    "/api/v1/monthly-reports",
    response_model=SubmissionResponse,
    status_code=201,
    tags=["月报同步"],
    summary="院方提交已发布整表（更正换整表）或撤刊通知",
)
def submit_monthly_report(
    payload: Annotated[
        ReportSubmission,
        Body(
            openapi_examples={
                "publish_full_table": {
                    "summary": "发布/更正：提交整表",
                    "description": (
                        "按统计月提交已发布的整表；同一统计月再次提交即更正，"
                        "整表沿用遮盖矩阵与行/列合计格式"
                    ),
                    "value": EX_MONTHLY_PUBLISH,
                },
                "retract_month": {
                    "summary": "撤刊：删除该统计月本地副本",
                    "description": "撤刊通知不携带整表，下游同步到后删除本地副本",
                    "value": EX_MONTHLY_RETRACTION,
                },
            }
        ),
    ]
) -> dict:
    with db.session() as conn:
        record = db.insert_change(
            conn,
            stat_month=payload.stat_month,
            change_type=payload.change_type,
            table=(
                payload.table.model_dump()
                if payload.change_type == "full_table"
                else None
            ),
        )
    return {
        "seq": record["seq"],
        "stat_month": record["stat_month"],
        "change_type": record["change_type"],
        "committed_at": record["committed_at"],
    }


@app.get(
    "/api/v1/monthly-reports/sync",
    response_model=SyncPageResponse,
    tags=["月报同步"],
    summary="下游增量拉取：带游标与页大小取首页，凭令牌续页",
)
def sync_monthly_reports(
    after_seq: Annotated[
        int,
        Query(
            ge=0,
            description="上次收完的序号，首次同步填 0；只取该序号之后的变更",
        ),
    ] = 0,
    page_size: Annotated[
        int,
        Query(ge=1, le=100, description="每页最多返回的变更条数"),
    ] = 50,
    page_token: Annotated[
        str | None,
        Query(description="上一页返回的 next_page_token；取首页时留空"),
    ] = None,
) -> dict:
    with db.session() as conn:
        try:
            return sync.pull_page(after_seq, page_size, page_token, conn)
        except sync.SyncTokenError as exc:
            return JSONResponse(status_code=400, content={"detail": str(exc)})
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"detail": str(exc)})
