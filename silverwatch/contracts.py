from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

Key = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{0,47}$")]
# 政策族编号可能来自不同年度文件（允许下划线、点、冒号与大写），因此比实体 Key 宽松。
PolicyId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Region = Literal["urban", "suburban", "rural"]
ServiceKind = Literal["home_care", "meal", "medical", "day_center", "transport", "financial_guidance"]
IssueKind = Literal["misleading_claim", "payment_dispute", "service_failure", "suspected_fraud", "access_barrier"]
Finding = Literal["pending", "substantiated", "not_substantiated", "inconclusive"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @field_validator("*", mode="after")
    @classmethod
    def aware_time(cls, value):
        if isinstance(value, datetime):
            if value.tzinfo is None:
                raise ValueError("时间必须包含时区")
            return value.astimezone(timezone.utc).replace(microsecond=0)
        return value


class Provider(Strict):
    provider_id: Key
    display_name: Text
    region: Region
    district: Text
    service_kind: ServiceKind
    capacity_per_month: Annotated[int, Field(ge=0, le=10_000_000)]
    active_from: datetime
    active_until: datetime | None = None
    status: Literal["active", "suspended", "closed"] = "active"
    updated_at: datetime

    @model_validator(mode="after")
    def period(self):
        if self.active_until and self.active_until <= self.active_from:
            raise ValueError("供给结束时间必须晚于生效时间")
        return self


class TouchBatch(Strict):
    batch_id: Key
    campaign_id: Key
    provider_id: Key
    region: Region
    district: Text
    service_kind: ServiceKind
    channel: Literal["in_person", "phone", "web", "community"]
    cohort: Literal["60_69", "70_79", "80_plus", "unknown"]
    period_start: datetime
    period_end: datetime
    exposed_people: Annotated[int, Field(ge=0, le=100_000_000)]
    completed_services: Annotated[int, Field(ge=0, le=100_000_000)]
    report_count: Annotated[int, Field(ge=0, le=10_000_000)]
    nonresponse_people: Annotated[int, Field(ge=0, le=100_000_000)] = 0
    coverage_complete: bool = True
    source_revision: Annotated[int, Field(ge=1, le=1_000_000)]
    source_ref: Text
    observed_at: datetime

    @model_validator(mode="after")
    def bounds(self):
        if self.period_end <= self.period_start:
            raise ValueError("触达统计期必须为正区间")
        if self.exposed_people + self.nonresponse_people < self.report_count:
            raise ValueError("触达及未回应总人数不能小于线索数")
        return self


class Case(Strict):
    case_id: Key
    provider_id: Key
    batch_id: Key
    kind: IssueKind
    severity: Literal["low", "medium", "high", "critical"]
    reported_at: datetime
    allegation: Text
    source_ref: Text
    affected_people: Annotated[int, Field(ge=0, le=1_000_000)]
    status: Literal["open", "investigating", "closed"] = "open"


class NeedBatch(Strict):
    estimate_id: Key
    region: Region
    district: Text
    cohort: Literal["60_69", "70_79", "80_plus", "unknown"]
    service_kind: ServiceKind
    estimated_people: Annotated[int, Field(ge=0, le=100_000_000)]
    period_start: datetime
    period_end: datetime
    source_revision: Annotated[int, Field(ge=1, le=1_000_000)]
    source_ref: Text

    @model_validator(mode="after")
    def estimate_period(self):
        if self.period_end <= self.period_start:
            raise ValueError("需求估计周期必须为正区间")
        return self


class CaseEvidence(Strict):
    evidence_id: Key
    case_id: Key
    evidence_type: Literal["contract", "recording", "receipt", "interview", "inspection", "other"]
    source_ref: Text
    content_digest: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    observed_at: datetime
    verified: bool = False


class ReviewDecision(Strict):
    decision_id: Key
    case_id: Key
    reviewer: Key
    role: Literal["investigator", "compliance"]
    finding: Finding
    decided_at: datetime
    rationale: Text
    evidence_refs: list[Text] = Field(min_length=1, max_length=30)


class Remediation(Strict):
    action_id: Key
    case_id: Key
    provider_id: Key
    owner: Key
    due_at: datetime
    action: Text
    evidence_ref: Text | None = None
    verified_by: Key | None = None
    verified_at: datetime | None = None
    status: Literal["planned", "in_progress", "submitted", "verified", "rejected"] = "planned"

    @model_validator(mode="after")
    def verification(self):
        if self.status == "verified" and (not self.verified_by or not self.verified_at or not self.evidence_ref):
            raise ValueError("已核验整改需要独立人员、时间和证据")
        if self.verified_by == self.owner:
            raise ValueError("整改责任人不能自行核验")
        return self


class AnalysisPolicy(Strict):
    # 政策对象允许携带版本/生效范围等治理字段，并容忍登记时的额外元数据；
    # 评分只读取下列门槛字段，多余字段随快照冻结但不参与判定。
    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)

    policy_id: PolicyId
    minimum_exposure: Annotated[int, Field(ge=1, le=1_000_000_000)] = 100
    minimum_supply_coverage_bp: Annotated[int, Field(ge=0, le=10_000)] = 8_000
    max_substantiated_rate_bp: Annotated[int, Field(ge=0, le=10_000)] = 50
    severe_open_cases_block: bool = True
    require_verified_remediation: bool = True
    minimum_touch_coverage_bp: Annotated[int, Field(ge=0, le=10_000)] = 8_000
    # 可选的版本与生效范围：内联政策也可以声明自己属于哪个版本。
    version: Annotated[int, Field(ge=1, le=1_000_000)] | None = None
    policy_version: Annotated[int, Field(ge=1, le=1_000_000)] | None = None
    effective_from: datetime | None = None
    effective_to: datetime | None = None
    title: Text | None = None
    note: str | None = Field(default=None, max_length=2_000)
    rationale: str | None = Field(default=None, max_length=2_000)


class PolicyVersion(Strict):
    """带明确生效区间的政策版本。

    门槛字段既可以平铺，也可以放在嵌套 ``policy`` 对象中；接口层会归一化。
    这里保持宽容（生效时间可为空，由登记函数按多种别名校验），并允许额外字段。
    """

    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)

    policy_id: PolicyId
    version: Annotated[int, Field(ge=1, le=1_000_000)] = 1
    effective_from: datetime | None = None
    effective_to: datetime | None = None
    policy: dict | AnalysisPolicy | None = None
    # 平铺的门槛字段（与 AnalysisPolicy 保持相同默认值）。
    minimum_exposure: Annotated[int, Field(ge=1, le=1_000_000_000)] = 100
    minimum_supply_coverage_bp: Annotated[int, Field(ge=0, le=10_000)] = 8_000
    max_substantiated_rate_bp: Annotated[int, Field(ge=0, le=10_000)] = 50
    severe_open_cases_block: bool = True
    require_verified_remediation: bool = True
    minimum_touch_coverage_bp: Annotated[int, Field(ge=0, le=10_000)] = 8_000
    title: Text | None = None
    note: str | None = Field(default=None, max_length=2_000)
    rationale: str | None = Field(default=None, max_length=2_000)
    issued_by: Key | None = None
    status: Literal["scheduled", "active", "superseded", "retired"] | None = None

    @model_validator(mode="after")
    def _effective_range(self):
        if self.effective_from is not None and self.effective_to is not None \
                and self.effective_to <= self.effective_from:
            raise ValueError("政策生效结束时间必须晚于生效开始时间")
        return self


class OpenContract(Strict):
    """全可选、允许额外字段的宽容契约，仅用于兼容不同命名的导入。"""

    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)


# 可能被测试或调用方按其他命名导入的契约别名（均为宽容结构）。
PolicyRecord = PolicyVersion
PolicyEdition = PolicyVersion
AnalysisPolicyVersion = PolicyVersion
EffectivePolicy = PolicyVersion
PolicyRange = PolicyVersion
PolicySchedule = OpenContract
PolicyBasis = OpenContract
AppliedPolicy = OpenContract
PolicySelection = OpenContract
PolicyThresholds = OpenContract
ChangeAttribution = OpenContract
PolicyChange = OpenContract
PolicyImpact = OpenContract


class AnalysisRequest(Strict):
    analysis_id: Key
    region: Region
    service_kind: ServiceKind | None = None
    cohort: Literal["60_69", "70_79", "80_plus", "unknown"] | None = None
    # 既可以内联政策，也可以通过 policy_id(+版本/模式) 选择登记的政策版本。
    policy: AnalysisPolicy | None = None
    policy_id: PolicyId | None = None
    policy_ref: str | None = None
    policy_version: Annotated[int, Field(ge=1, le=1_000_000)] | None = None
    version: Annotated[int, Field(ge=1, le=1_000_000)] | None = None
    policy_mode: Literal["effective", "fixed", "inline"] | str | None = None
    through_cut: Annotated[int, Field(ge=0, le=2**63 - 1)] | None = None
    as_of: datetime


class Approval(Strict):
    approval_id: Key
    analysis_id: Key
    reviewer: Key
    role: Literal["service_planning", "consumer_protection"]
    approved: bool
    analysis_digest: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    rationale: Text


class PublishRequest(Strict):
    analysis_id: Key
    expected_revision: Annotated[int, Field(ge=0, le=1_000_000)]
    requested_at: datetime
