"""请求/响应 Pydantic 模型与 Swagger 示例。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
# 示例数据（行列序号均从 0 开始，与请求中的矩阵下标一致）---------------

# 示例 1：3x3，4 个隐藏格构成自由块，取值为区间，不可还原。
EX_RANGE: dict = {
    "matrix": [
        [5, None, None],
        [None, 3, None],
        [None, None, 2],
    ],
    "row_totals": [12, 10, 8],
    "col_totals": [9, 9, 12],
}

# 示例 2：3x3，隐藏格被合计完全锁定，全部可还原。
EX_RECOVERABLE: dict = {
    "matrix": [
        [2, None, 3],
        [None, 4, None],
        [5, 1, None],
    ],
    "row_totals": [10, 10, 9],
    "col_totals": [9, 10, 10],
}

# 示例 3：行合计之和与列合计之和不相等，直接冲突。
EX_TOTAL_CONFLICT: dict = {
    "matrix": [
        [1, None],
        [None, 2],
    ],
    "row_totals": [5, 5],
    "col_totals": [6, 6],
}

# 示例 4：总量相等但运输结构无解（第 0 行只能把量送进第 0 列，容量不够）。
EX_HALL_CONFLICT: dict = {
    "matrix": [
        [None, 4],
        [3, None],
    ],
    "row_totals": [10, 7],
    "col_totals": [5, 12],
}


class CountTable(BaseModel):
    """计数整表：遮盖矩阵 + 行/列合计。

    月报整表与核查请求共用同一遮盖矩阵和行列合计格式，因此形状、
    类型、非负等校验集中在此基类。
    """

    matrix: list[list[StrictInt | None]] = Field(
        ...,
        description=(
            "最多 12x12 的非负整数计数矩阵；null 表示被遮盖的格子，"
            "其余数值视为已公开的固定格"
        ),
    )
    row_totals: list[StrictInt] = Field(
        ..., description="每一行的公开合计，长度须等于矩阵行数"
    )
    col_totals: list[StrictInt] = Field(
        ..., description="每一列的公开合计，长度须等于矩阵列数"
    )

    @field_validator("matrix")
    @classmethod
    def _validate_matrix_cells(cls, value):
        # StrictInt 在联合类型里仍可能放行 bool（bool 是 int 子类），显式拒绝；
        # 同时统一拦截负数
        for i, row in enumerate(value):
            for j, cell in enumerate(row):
                if isinstance(cell, bool):
                    raise ValueError(
                        f"matrix[{i}][{j}] 不能是布尔值，须为整数或 null"
                    )
                if cell is not None and cell < 0:
                    raise ValueError(f"matrix[{i}][{j}] 不能为负数：{cell}")
        return value

    @field_validator("row_totals", "col_totals")
    @classmethod
    def _validate_totals(cls, value):
        for i, cell in enumerate(value):
            if isinstance(cell, bool):
                raise ValueError(f"合计值不能是布尔值：位置 {i}")
            if cell < 0:
                raise ValueError(f"合计值不能为负数：位置 {i} = {cell}")
        return value

    @model_validator(mode="after")
    def _check_shape(self) -> "CountTable":
        if not self.matrix:
            raise ValueError("matrix 至少要包含一行")
        if len(self.matrix) > 12:
            raise ValueError("matrix 行数不能超过 12")
        width = len(self.matrix[0])
        if width == 0 or width > 12:
            raise ValueError("matrix 列数须在 1..12 之间")
        for i, row in enumerate(self.matrix):
            if len(row) != width:
                raise ValueError(f"第 {i} 行长度与首行不一致（应为 {width}）")
        if len(self.row_totals) != len(self.matrix):
            raise ValueError("row_totals 长度须等于矩阵行数")
        if len(self.col_totals) != width:
            raise ValueError("col_totals 长度须等于矩阵列数")
        if any(v < 0 for v in self.row_totals):
            raise ValueError("row_totals 不能包含负数")
        if any(v < 0 for v in self.col_totals):
            raise ValueError("col_totals 不能包含负数")
        return self


class SuppressionRequest(CountTable):
    """核查请求：计数矩阵（null 表示遮盖格）与公开行/列合计。"""

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"summary": "可行：隐藏格存在取值区间（不可还原）", "value": EX_RANGE},
                {"summary": "可行：隐藏格被合计唯一锁定（可还原）", "value": EX_RECOVERABLE},
                {"summary": "冲突：行列总合计不相等", "value": EX_TOTAL_CONFLICT},
                {"summary": "冲突：总量相等但结构无解（Hall 条件）", "value": EX_HALL_CONFLICT},
            ]
        }
    }


class Dimensions(BaseModel):
    rows: int
    cols: int


class CellRange(BaseModel):
    row: int = Field(..., description="隐藏格行下标（从 0 开始）")
    col: int = Field(..., description="隐藏格列下标（从 0 开始）")
    min: int = Field(..., description="在全部合计同时成立前提下该格的最小取值")
    max: int = Field(..., description="在全部合计同时成立前提下该格的最大取值")
    recoverable: bool = Field(
        ..., description="min == max 时为 true，表示可被公开合计精确反推"
    )


class FeasibleResponse(BaseModel):
    status: Literal["feasible"]
    dimensions: Dimensions
    grand_total: int = Field(..., description="行合计之和（等于列合计之和）")
    hidden_count: int
    recoverable_count: int = Field(..., description="可还原隐藏格数量")
    cells: list[CellRange] = Field(
        ..., description="每个隐藏格的取值区间与可还原标记，按行优先顺序"
    )
    minimizer_matrix: list[list[int]] = Field(
        ..., description="达到各隐藏格下界的完整可行矩阵（对首个隐藏格取 min）"
    )
    maximizer_matrix: list[list[int]] = Field(
        ..., description="达到各隐藏格上界的完整可行矩阵（对首个隐藏格取 max）"
    )


class ConflictWitness(BaseModel):
    max_flow: int
    required_flow: int
    row_subset: list[int] = Field(
        ..., description="最小割源侧的行下标集合 R（0 起）"
    )
    neighbor_cols: list[int] = Field(
        ..., description="与 R 中隐藏格相邻的列下标集合 N(R)"
    )
    subset_demand: int = Field(..., description="行子集 R 的隐藏格总需求")
    neighbor_supply: int = Field(
        ..., description="邻接列 N(R) 能承接的总供给，小于需求即无解"
    )


class ConflictResponse(BaseModel):
    status: Literal["conflict"]
    reasons: list[str] = Field(
        ..., description="冲突的文字说明；无解时不返回任何取值区间"
    )
    witness: ConflictWitness | None = Field(
        default=None, description="最大流最小割给出的 Hall 型冲突见证（若适用）"
    )


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str


# ---------------------------------------------------------------------------
# 月报同步
# ---------------------------------------------------------------------------

# 院方首次发布：2026-09 统计月整表（遮盖矩阵 + 行列合计，沿用既有格式）
EX_MONTHLY_PUBLISH: dict = {
    "stat_month": "2026-09",
    "change_type": "full_table",
    "table": {
        "matrix": [
            [5, None, None],
            [None, 3, None],
            [None, None, 2],
        ],
        "row_totals": [12, 10, 8],
        "col_totals": [9, 9, 12],
    },
}

# 同一统计月更正：整表替换，遮盖位置与数值均可能变化
EX_MONTHLY_CORRECTION: dict = {
    "stat_month": "2026-09",
    "change_type": "full_table",
    "table": {
        "matrix": [
            [6, None, None],
            [None, 3, None],
            [None, None, 2],
        ],
        "row_totals": [13, 10, 8],
        "col_totals": [9, 10, 12],
    },
}

# 撤刊：删除本地副本，不携带整表
EX_MONTHLY_RETRACTION: dict = {
    "stat_month": "2026-09",
    "change_type": "retraction",
}


class ReportSubmission(BaseModel):
    """院方提交的一笔月报变更：已发布整表或撤刊通知。"""

    stat_month: str = Field(
        ...,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
        description="统计月，格式 YYYY-MM",
    )
    change_type: Literal["full_table", "retraction"] = Field(
        ..., description="full_table=发布/更正整表；retraction=撤刊该月"
    )
    table: CountTable | None = Field(
        default=None,
        description=(
            "整表内容，沿用遮盖矩阵与行/列合计格式；"
            "full_table 必填，retraction 必须为空"
        ),
    )

    @model_validator(mode="after")
    def _check_type_table_consistency(self) -> "ReportSubmission":
        if self.change_type == "full_table" and self.table is None:
            raise ValueError("change_type=full_table 时必须提交 table 整表")
        if self.change_type == "retraction" and self.table is not None:
            raise ValueError("change_type=retraction 时不得携带 table 整表")
        return self

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"summary": "发布/更正：提交整表（遮盖矩阵+行列合计）", "value": EX_MONTHLY_PUBLISH},
                {"summary": "撤刊：删除该统计月本地副本", "value": EX_MONTHLY_RETRACTION},
            ]
        }
    }


class ReportChange(BaseModel):
    """同步页中的一笔有效变更（每月已折叠为最后一笔）。"""

    seq: int = Field(..., description="全局递增序号")
    stat_month: str = Field(..., description="统计月 YYYY-MM")
    change_type: Literal["full_table", "retraction"]
    table: CountTable | None = Field(
        default=None,
        description="full_table 时为整表；retraction 时为 null，下游应删除本地副本",
    )
    committed_at: str = Field(..., description="入库时间（UTC ISO 8601）")


class SubmissionResponse(BaseModel):
    """提交成功响应（HTTP 201）。"""

    seq: int = Field(..., description="本笔变更分配到的全局递增序号")
    stat_month: str
    change_type: Literal["full_table", "retraction"]
    committed_at: str


class SyncPageResponse(BaseModel):
    """下游拉取的一页变更。"""

    after_seq: int = Field(..., description="本轮起点序号（即请求游标）")
    cutoff_seq: int = Field(
        ...,
        description=(
            "本轮截止序号：取该游标首次查询时已提交的最大序号；"
            "下一轮以它作为 after_seq"
        ),
    )
    page_size: int
    offset: int = Field(..., description="本页在轮次有效变更序列中的偏移")
    has_more: bool = Field(..., description="本轮是否还有后续页")
    next_page_token: str | None = Field(
        default=None, description="续页令牌；has_more=false 时为 null"
    )
    changes: list[ReportChange] = Field(
        ..., description="本页变更，按 seq 升序，每月仅保留轮次内最后一笔"
    )
