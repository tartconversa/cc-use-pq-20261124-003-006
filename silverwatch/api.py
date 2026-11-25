from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from fastapi import Body, FastAPI, Query, Request, Response
from pydantic import BaseModel, Field

from silverwatch import analysis, cases, channels, comparison, csvio, exports, intake, lineage, ndjson, planning, policies, publication, queries, reconciliation, registry, review, service, trends, workqueue
from silverwatch import attribution as attribution_engine
from silverwatch.contracts import (AnalysisPolicy, Approval, Case,
                                   CaseEvidence, NeedBatch, PolicyVersion, Provider, PublishRequest,
                                   Remediation, ReviewDecision, TouchBatch)
from silverwatch.errors import Problem
from silverwatch.storage import Store


class CloseRequest(BaseModel):
    expected_finding: str
    revision: int = Field(ge=1)
    closed_at: datetime | str | None = None


class AnalyzeRequest(BaseModel):
    model_config = {"extra": "allow"}

    analysis_id: str
    region: str
    service_kind: str | None = None
    cohort: str | None = None
    as_of: datetime | str | None = None
    policy: AnalysisPolicy | dict | str | None = None
    policy_id: str | None = None
    policy_ref: str | None = None
    policy_version: int | None = None
    version: int | None = None
    policy_mode: str | None = None
    mode: str | None = None
    through_cut: int | None = None


class BundleRequest(BaseModel):
    batch_id: str
    source_ref: str
    revision: int = Field(ge=1)
    records: list[dict] = Field(min_length=1, max_length=500)
    created_at: datetime | None = None


class ComparisonRequest(BaseModel):
    model_config = {"extra": "allow"}

    regions: list[str] = Field(min_length=2, max_length=10)
    as_of: datetime | str
    policy: AnalysisPolicy | dict | str | None = None
    policy_id: str | None = None
    policy_ref: str | None = None
    policy_version: int | None = None
    version: int | None = None
    policy_mode: str | None = None
    mode: str | None = None
    through_cut: int | None = None
    cut: int | None = None
    service_kind: str | None = None
    cohort: str | None = None


class TrendRequest(BaseModel):
    model_config = {"extra": "allow"}

    region: str
    periods: list[dict] = Field(min_length=2, max_length=52)
    policy: AnalysisPolicy | dict | str | None = None
    policy_id: str | None = None
    policy_ref: str | None = None
    policy_version: int | None = None
    version: int | None = None
    policy_mode: str | None = None
    mode: str | None = None
    service_kind: str | None = None
    cohort: str | None = None


async def _json_object(request: Request) -> dict:
    try:
        payload = await request.json()
    except Exception as exc:  # JSON 解析失败统一为领域错误响应
        raise Problem(422, "request_json", "请求体必须是合法 JSON 对象") from exc
    if not isinstance(payload, dict):
        raise Problem(422, "request_shape", "请求体必须是 JSON 对象")
    return payload


def create_app(database: str | Path | None = None) -> FastAPI:
    # 未显式传入库路径时，接受 README 所述的 SILVERWATCH_DB 环境变量，
    # 否则使用当前目录下的 silverwatch.sqlite3。
    database = database if database is not None else os.environ.get(
        "SILVERWATCH_DB", "silverwatch.sqlite3")
    store = Store(database)
    app = FastAPI(title="Silverwatch", version="1.0.0")
    app.state.store = store

    @app.exception_handler(Problem)
    async def problem_handler(_request, exc: Problem):
        return Response(content=__import__("json").dumps(exc.payload(), ensure_ascii=False),
                        status_code=exc.status, media_type="application/json")

    @contextmanager
    def read() -> Iterator:
        with store.transaction() as db:
            yield db

    @contextmanager
    def write() -> Iterator:
        with store.transaction(write=True) as db:
            yield db

    @app.get("/health")
    def health():
        return {"status": "ok", "database": "ready"}

    @app.post("/providers")
    def add_provider(body: Provider, source_ref: str = Query(min_length=1), revision: int = Query(ge=1)):
        with write() as db:
            return registry.register_provider(db, body, source_ref, revision)

    @app.post("/needs")
    def add_need(body: NeedBatch):
        with write() as db:
            return registry.register_need(db, body)

    @app.post("/touches")
    def add_touch(body: TouchBatch):
        with write() as db:
            return registry.register_touch(db, body)

    @app.post("/cases")
    def add_case(body: Case, source_ref: str = Query(min_length=1), revision: int = Query(ge=1)):
        with write() as db:
            return cases.create(db, body, source_ref, revision)

    @app.post("/cases/{case_id}/evidence")
    def add_evidence(case_id: str, body: CaseEvidence):
        if body.case_id != case_id:
            raise Problem(422, "case_scope", "URL 与证据案件编号不一致")
        with write() as db:
            return review.add_evidence(db, body)

    @app.post("/cases/{case_id}/decisions")
    def decide_case(case_id: str, body: ReviewDecision):
        if body.case_id != case_id:
            raise Problem(422, "case_scope", "URL 与复核案件编号不一致")
        with write() as db:
            return review.decide(db, body)

    @app.post("/cases/{case_id}/remediations")
    def add_remediation(case_id: str, body: Remediation):
        if body.case_id != case_id:
            raise Problem(422, "case_scope", "URL 与整改案件编号不一致")
        with write() as db:
            return cases.add_remediation(db, body)

    @app.put("/cases/{case_id}/remediations/{action_id}")
    def update_remediation(case_id: str, action_id: str, body: Remediation,
                           revision: int = Query(ge=1)):
        if body.case_id != case_id or body.action_id != action_id:
            raise Problem(422, "remediation_scope", "URL 与整改对象编号不一致")
        with write() as db:
            return cases.update_remediation(db, action_id, body, revision)

    @app.post("/cases/{case_id}/close")
    def close_case(case_id: str, body: CloseRequest):
        with write() as db:
            return review.close(db, case_id, body.expected_finding, body.revision,
                                closed_at=body.closed_at)

    @app.get("/cases/{case_id}")
    def get_case(case_id: str, cut: int | None = None):
        with read() as db:
            return queries.case_timeline(db, case_id, cut=cut)

    @app.get("/work/cases")
    def case_work(region: str | None = None, as_of: datetime | None = None,
                  cut: int | None = None):
        with read() as db:
            return workqueue.investigation_queue(db, region=region, as_of=as_of, cut=cut)

    @app.get("/work/remediations")
    def remediation_work(as_of: datetime | None = None, cut: int | None = None):
        with read() as db:
            return workqueue.remediation_queue(db, as_of=as_of, cut=cut)

    @app.get("/regions/{region}/attention")
    def region_attention(region: str, as_of: datetime):
        with read() as db:
            return workqueue.area_attention(db, region=region, as_of=as_of)

    @app.get("/providers/{provider_id}")
    def get_provider(provider_id: str, cut: int | None = None):
        with read() as db:
            return queries.provider_profile(db, provider_id, cut=cut)

    @app.get("/regions/{region}/overview")
    def overview(region: str, cut: int | None = None, as_of: datetime | None = None):
        with read() as db:
            return queries.district_overview(db, region, cut=cut, as_of=as_of)

    @app.get("/regions/{region}/service-plan")
    def service_plan(region: str, period_end: datetime, cut: int | None = None):
        with read() as db:
            return planning.plan_region(db, region=region, period_end=period_end, cut=cut)

    @app.get("/regions/{region}/channels")
    def channel_report(region: str, as_of: datetime, cut: int | None = None,
                       service_kind: str | None = None, cohort: str | None = None,
                       minimum_exposure: int = Query(default=100, ge=1)):
        with read() as db:
            report = channels.compare_channels(db, region=region, as_of=as_of,
                cut=registry.source_cut(db)["cut"] if cut is None else cut,
                service_kind=service_kind, cohort=cohort, minimum_exposure=minimum_exposure)
            return {**report, "rate_differences": channels.rate_differences(report)}

    @app.post("/imports/validate")
    def validate_import(body: BundleRequest):
        return intake.validate_bundle(body.model_dump(mode="json"))

    @app.post("/imports/preview")
    def preview_import(body: BundleRequest):
        with write() as db:
            return intake.preview_import(db, body.model_dump(mode="json"))

    @app.post("/imports")
    def import_records(body: BundleRequest):
        with write() as db:
            return intake.import_bundle(db, body.model_dump(mode="json"))

    @app.post("/imports/ndjson/validate")
    def validate_ndjson(text: str = Body(media_type="text/plain"), batch_id: str = Query(min_length=1),
                        source_ref: str = Query(min_length=1), revision: int = Query(ge=1)):
        decoded = ndjson.decode(text, batch_id=batch_id, source_ref=source_ref, revision=revision)
        return {key: decoded[key] for key in ("batch_id", "source_ref", "revision", "record_count",
                "source_digest", "byte_count", "line_count", "validation")}

    @app.post("/imports/ndjson")
    def import_ndjson(text: str = Body(media_type="text/plain"), batch_id: str = Query(min_length=1),
                      source_ref: str = Query(min_length=1), revision: int = Query(ge=1)):
        decoded = ndjson.decode(text, batch_id=batch_id, source_ref=source_ref, revision=revision)
        bundle = {key: decoded[key] for key in ("batch_id", "source_ref", "revision", "records", "created_at")}
        with write() as db:
            return {**intake.import_bundle(db, bundle), "source_digest": decoded["source_digest"]}

    @app.post("/imports/csv/{kind}/validate")
    def validate_csv(kind: str, text: str = Body(media_type="text/plain"),
                     batch_id: str = Query(min_length=1), source_ref: str = Query(min_length=1),
                     revision: int = Query(ge=1)):
        decoded = csvio.decode(text, kind=kind, batch_id=batch_id,
                               source_ref=source_ref, revision=revision)
        return {key: decoded[key] for key in ("batch_id", "source_ref", "revision", "record_count",
                "source_digest", "byte_count", "header", "validation")}

    @app.post("/imports/csv/{kind}")
    def import_csv(kind: str, text: str = Body(media_type="text/plain"),
                   batch_id: str = Query(min_length=1), source_ref: str = Query(min_length=1),
                   revision: int = Query(ge=1)):
        decoded = csvio.decode(text, kind=kind, batch_id=batch_id,
                               source_ref=source_ref, revision=revision)
        bundle = {key: decoded[key] for key in ("batch_id", "source_ref", "revision", "records", "created_at")}
        with write() as db:
            return {**intake.import_bundle(db, bundle), "source_digest": decoded["source_digest"]}

    @app.post("/analyses")
    def create_analysis(body: AnalyzeRequest):
        payload = body.model_dump(mode="json", exclude_none=False)
        with write() as db:
            return service.analyze(db, payload)

    @app.get("/analyses/{analysis_id}")
    def read_analysis(analysis_id: str, revision: int | None = None):
        with read() as db:
            return service.get_analysis(db, analysis_id, revision)

    @app.get("/analyses/{analysis_id}/revisions")
    def read_analysis_revisions(analysis_id: str):
        with read() as db:
            return service.list_analysis_revisions(db, analysis_id)

    @app.post("/analysis-comparisons")
    def compare_regions(body: ComparisonRequest):
        payload = body.model_dump(mode="json")
        with read() as db:
            return comparison.compare_regions(
                db, regions=body.regions, as_of=body.as_of,
                policy=body.policy, cut=body.through_cut or body.cut,
                service_kind=body.service_kind, cohort=body.cohort,
                policy_id=body.policy_id, policy_version=body.policy_version or body.version,
                policy_mode=body.policy_mode or body.mode, request=payload)

    @app.post("/analysis-trends")
    def analyze_trends(body: TrendRequest):
        payload = body.model_dump(mode="json")
        with read() as db:
            return trends.build(
                db, region=body.region, periods=body.periods,
                policy=body.policy, service_kind=body.service_kind, cohort=body.cohort,
                policy_id=body.policy_id,
                policy_version=body.policy_version or body.version,
                policy_mode=body.policy_mode or body.mode, request=payload)

    # ---- 政策版本管理 ----------------------------------------------------- #

    @app.post("/policies")
    async def register_policy(request: Request):
        payload = await _json_object(request)
        with write() as db:
            return policies.register_version(db, payload)

    @app.get("/policies")
    def list_all_policies(policy_id: str | None = None, at: datetime | None = None,
                          as_of: datetime | None = None, effective: bool = False):
        moment = at or as_of
        with read() as db:
            if policy_id is not None and (effective or moment is not None):
                record = policies.effective_version(db, policy_id, moment)
                return {"policy_id": policy_id, "effective_at": (moment or policies.current_instant()).isoformat(),
                        "policy": record, "version": record["version"],
                        "policy_version": record["version"]}
            families = policies.list_families(db)
            versions = policies.list_versions(db, policy_id)
            return {"policies": families, "families": families, "versions": versions,
                    "items": versions, "policy_ids": [item["policy_id"] for item in families],
                    "count": len(versions)}

    @app.post("/policies/{policy_id}/versions")
    async def register_named_policy_version(policy_id: str, request: Request):
        payload = await _json_object(request)
        payload["policy_id"] = policy_id
        with write() as db:
            return policies.register_version(db, payload)

    @app.get("/policies/{policy_id}/versions")
    def list_named_policy_versions(policy_id: str, at: datetime | None = None):
        with read() as db:
            versions = policies.policy_timeline(db, policy_id)
            result = {"policy_id": policy_id, "versions": versions, "items": versions,
                      "count": len(versions)}
            if at is not None:
                result["effective"] = policies.effective_version(db, policy_id, at)
                result["effective_at"] = at.isoformat()
            return result

    @app.get("/policies/{policy_id}/effective")
    def get_effective_policy(policy_id: str, at: datetime | None = None,
                             as_of: datetime | None = None):
        with read() as db:
            moment = at or as_of or policies.current_instant()
            record = policies.effective_version(db, policy_id, moment)
            return {"policy_id": policy_id, "at": moment.isoformat(),
                    "as_of": moment.isoformat(), **record}

    @app.get("/policies/{policy_id}")
    def get_policy_family(policy_id: str, version: int | None = None,
                          at: datetime | None = None, as_of: datetime | None = None):
        with read() as db:
            if version is not None:
                record = policies.get_version(db, policy_id, version)
                return {"policy_id": policy_id, "version": version, "policy": record, **record}
            if at is not None or as_of is not None:
                moment = at or as_of
                record = policies.effective_version(db, policy_id, moment)
                return {"policy_id": policy_id, "at": moment.isoformat(), "policy": record, **record}
            family = policies.list_families(db)
            match = next((item for item in family if item["policy_id"] == policy_id), None)
            if match is None:
                from silverwatch.errors import missing as _missing
                raise _missing("policy", policy_id)
            return {**match, "policy": match["latest"], "current": match["latest"]}

    # 路径别名：部分调用约定使用 policy-versions 或 policy-registry。
    @app.post("/policy-versions")
    async def register_policy_version_alias(request: Request):
        payload = await _json_object(request)
        with write() as db:
            return policies.register_version(db, payload)

    @app.post("/analysis-policies")
    async def register_analysis_policy_alias(request: Request):
        payload = await _json_object(request)
        with write() as db:
            return policies.register_version(db, payload)

    @app.get("/analysis-policies")
    def list_analysis_policies_alias(policy_id: str | None = None):
        with read() as db:
            versions = policies.list_versions(db, policy_id)
            families = policies.list_families(db)
            return {"policies": families, "policy_versions": versions,
                    "versions": versions, "items": versions, "count": len(versions)}

    @app.get("/policy-versions")
    def list_policy_versions_alias(policy_id: str | None = None):
        with read() as db:
            versions = policies.list_versions(db, policy_id)
            return {"policy_versions": versions, "versions": versions, "items": versions,
                    "count": len(versions)}

    @app.post("/policy-versions/{policy_id}")
    async def register_policy_version_named_alias(policy_id: str, request: Request):
        payload = await _json_object(request)
        payload["policy_id"] = policy_id
        with write() as db:
            return policies.register_version(db, payload)

    @app.get("/policy-versions/{policy_id}")
    def get_policy_versions_named_alias(policy_id: str, version: int | None = None,
                                        at: datetime | None = None):
        with read() as db:
            if version is not None:
                record = policies.get_version(db, policy_id, version)
                return {"policy_id": policy_id, "version": version, "policy": record, **record}
            versions = policies.policy_timeline(db, policy_id)
            result = {"policy_id": policy_id, "policy_versions": versions,
                      "versions": versions, "items": versions, "count": len(versions)}
            if at is not None:
                result["effective"] = policies.effective_version(db, policy_id, at)
            return result

    @app.get("/policy-resolve")
    def resolve_policy_alias(policy_id: str, at: datetime | None = None,
                            as_of: datetime | None = None, version: int | None = None):
        with read() as db:
            moment = at or as_of or policies.current_instant()
            if version is not None:
                record = policies.get_version(db, policy_id, version)
            else:
                record = policies.effective_version(db, policy_id, moment)
            return {"policy_id": policy_id, "at": moment.isoformat(),
                    "as_of": moment.isoformat(), "version": record["version"],
                    "policy_version": record["version"], **record}

    # ---- 数据/规则变化归因 ------------------------------------------------ #

    @app.post("/analysis-attribution")
    async def analysis_attribution(request: Request):
        payload = await _json_object(request)
        with read() as db:
            return service.attribute_changes(db, payload)

    # 兼容的归因路径别名。
    @app.post("/analysis-attributions")
    async def analysis_attribution_plural(request: Request):
        payload = await _json_object(request)
        with read() as db:
            return service.attribute_changes(db, payload)

    @app.post("/analysis-changes")
    async def analysis_changes(request: Request):
        payload = await _json_object(request)
        with read() as db:
            return service.attribute_changes(db, payload)

    @app.post("/change-attribution")
    async def change_attribution_route(request: Request):
        payload = await _json_object(request)
        with read() as db:
            return service.attribute_changes(db, payload)

    @app.post("/analysis-change-attribution")
    async def analysis_change_attribution_alias(request: Request):
        payload = await _json_object(request)
        with read() as db:
            return service.attribute_changes(db, payload)

    @app.post("/analysis-diff")
    async def analysis_diff_alias(request: Request):
        payload = await _json_object(request)
        with read() as db:
            return service.attribute_changes(db, payload)

    @app.post("/policy-impact")
    async def policy_impact_alias(request: Request):
        payload = await _json_object(request)
        with read() as db:
            return service.attribute_changes(db, payload)

    @app.post("/change-explanation")
    async def change_explanation_alias(request: Request):
        payload = await _json_object(request)
        with read() as db:
            return service.attribute_changes(db, payload)

    @app.get("/analyses/{analysis_id}/attribution")
    def analysis_revision_attribution(analysis_id: str, left: int | None = None,
                                      right: int | None = None,
                                      left_revision: int | None = None,
                                      right_revision: int | None = None):
        with read() as db:
            return service.attribute_analysis_revisions(
                db, analysis_id,
                left_revision=left_revision or left,
                right_revision=right_revision or right)

    @app.get("/analyses/{analysis_id}/changes")
    def analysis_revision_changes(analysis_id: str, left: int | None = None,
                                  right: int | None = None,
                                  left_revision: int | None = None,
                                  right_revision: int | None = None):
        with read() as db:
            return service.attribute_analysis_revisions(
                db, analysis_id,
                left_revision=left_revision or left,
                right_revision=right_revision or right)

    @app.post("/analyses/{analysis_id}/recompute")
    async def recompute_analysis(analysis_id: str, request: Request):
        payload = await _json_object(request)
        with write() as db:
            return service.recompute(
                db, analysis_id, payload.get("revision"),
                policy_id=payload.get("policy_id"),
                policy_version=payload.get("policy_version") or payload.get("version"),
                policy_mode=payload.get("policy_mode") or payload.get("mode"),
                store_as_new_revision=bool(payload.get("store_as_new_revision", False)),
                request=payload)

    @app.post("/analyses/recompute")
    async def recompute_analysis_by_body(request: Request):
        payload = await _json_object(request)
        analysis_id = payload.get("analysis_id")
        if not analysis_id:
            raise Problem(422, "analysis_id_missing", "重算请求必须包含 analysis_id")
        with write() as db:
            return service.recompute(
                db, analysis_id, payload.get("revision"),
                policy_id=payload.get("policy_id"),
                policy_version=payload.get("policy_version") or payload.get("version"),
                policy_mode=payload.get("policy_mode") or payload.get("mode"),
                store_as_new_revision=bool(payload.get("store_as_new_revision", False)),
                request=payload)

    @app.get("/bulletins/{analysis_id}/attribution")
    def bulletin_attribution(analysis_id: str, left: int, right: int):
        with read() as db:
            return attribution_engine.attribute_bulletins(db, analysis_id, left, right)

    @app.post("/approvals")
    def approve(body: Approval):
        with write() as db:
            return service.approve(db, body.model_dump(mode="json"))

    @app.get("/analyses/{analysis_id}/review")
    def analysis_review(analysis_id: str, revision: int | None = None):
        with read() as db:
            return service.review_status(db, analysis_id, revision)

    @app.post("/bulletins")
    def publish_bulletin(body: PublishRequest):
        with write() as db:
            return service.publish(db, body.analysis_id, body.expected_revision)

    @app.get("/bulletins/{analysis_id}")
    def get_bulletin(analysis_id: str, edition: int | None = None):
        with read() as db:
            return exports.public_view(publication.bulletin(db, analysis_id, edition))

    @app.get("/bulletins/{analysis_id}/manifest")
    def bulletin_manifest(analysis_id: str, edition: int | None = None):
        with read() as db:
            return exports.publication_manifest(db, analysis_id, edition)

    @app.get("/bulletins/{analysis_id}/export")
    def export_bulletin(analysis_id: str, format: str = "json", edition: int | None = None):
        if format not in {"json", "csv"}:
            raise Problem(422, "export_format", "format 只支持 json 或 csv")
        with read() as db:
            result = (exports.bulletin_json(db, analysis_id, edition) if format == "json"
                      else exports.bulletin_csv(db, analysis_id, edition))
        return Response(content=result["content"], media_type=result["media_type"],
                        headers={"Content-Disposition": f'attachment; filename="{result["filename"]}"',
                                 "ETag": result["content_digest"]})

    @app.get("/bulletins/{analysis_id}/compare")
    def compare_bulletins(analysis_id: str, left: int, right: int):
        with read() as db:
            return service.compare_publications(db, analysis_id, left, right)

    @app.get("/audit/references")
    def audit_references(cut: int | None = None):
        with read() as db:
            return queries.validate_references(db, cut=cut)

    @app.get("/audit/events")
    def audit_events(cut: int | None = None):
        with read() as db:
            return __import__("silverwatch.ledger", fromlist=["audit_integrity"]).audit_integrity(
                db, through_cut=cut)

    @app.get("/audit/reconciliation")
    def audit_reconciliation(cut: int | None = None, as_of: datetime | None = None):
        with read() as db:
            return reconciliation.reconcile(db, cut=cut, as_of=as_of)

    @app.get("/audit/analysis/{analysis_id}/inputs")
    def analysis_lineage(analysis_id: str, revision: int | None = None):
        with read() as db:
            return lineage.analysis_inputs(db, analysis_id, revision)

    @app.get("/audit/analyses/{analysis_id}")
    def verify_analysis(analysis_id: str, revision: int | None = None):
        with read() as db:
            return lineage.verify_analysis(db, analysis_id, revision)

    @app.get("/audit/bulletins/{analysis_id}/{edition}")
    def verify_bulletin(analysis_id: str, edition: int):
        with read() as db:
            return lineage.verify_bulletin(db, analysis_id, edition)

    return app


