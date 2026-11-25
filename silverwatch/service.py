from __future__ import annotations

import json
from datetime import datetime, timezone
from sqlite3 import Connection

from pydantic import ValidationError

from silverwatch import analysis, publication
from silverwatch import policies as policy_registry
from silverwatch import attribution as change_attribution
from silverwatch.contracts import AnalysisPolicy, Approval
from silverwatch.errors import Problem, invalid, missing
from silverwatch.ledger import current_cut


def analyze(db: Connection, request: dict) -> dict:
    required = {"analysis_id", "region", "as_of"}
    if required - request.keys():
        raise invalid("analysis_request", "分析请求缺少必要字段",
                      {"missing": sorted(required - request.keys())})
    if not request.get("policy") and not request.get("policy_id") and not request.get("policy_ref"):
        raise invalid("analysis_policy_missing",
                      "分析请求必须给出内联 policy 或可选择的 policy_id")
    as_of = request["as_of"]
    if as_of is None:
        as_of = policy_registry.current_instant()
    if isinstance(as_of, str):
        as_of = policy_registry.parse_instant(as_of, field="as_of")
    selection = policy_registry.select_policy(db, data=request, at=as_of,
                                              default_mode=request.get("default_policy_mode", "effective"))
    policy = selection["policy"]
    cut = request.get("through_cut")
    if cut is None:
        cut = current_cut(db)
    document = analysis.run(db, cut=cut, as_of=as_of, region=request["region"],
                            service_kind=request.get("service_kind"),
                            cohort=request.get("cohort"), policy=policy,
                            selection=selection)
    stored = publication.store_analysis(db, request["analysis_id"], cut, as_of, policy, document)
    return {**stored,
            "as_of": as_of.isoformat() if hasattr(as_of, "isoformat") else as_of,
            "policy_id": selection.get("policy_id"),
            "policy_version": selection.get("version"),
            "policy_mode": selection["basis"].get("mode"),
            "policy_basis": selection.get("basis"),
            "applied_policy": document.get("applied_policy"),
            "policy": policy_registry.dump_policy(policy),
            "document": document,
            "areas": document["areas"]}


def recompute(db: Connection, analysis_id: str, revision: int | None = None,
              *, policy_id: str | None = None, policy_version: int | None = None,
              version: int | None = None, policy_mode: str | None = "fixed",
              mode: str | None = None, at=None, store_as_new_revision: bool = False,
              request: dict | None = None) -> dict:
    """用指定（默认最新）政策对一份已存分析的相同数据截点重新计算。

    不指定政策时沿用原分析冻结的内联政策；这使得“同一政策重算历史”与
    “按新政策试算”都可完成。默认只返回重算文档，不改写已存/已签结果；
    只有 ``store_as_new_revision=True`` 才写入一个新的分析修订版本。
    """
    row = publication.selected_analysis(db, analysis_id, revision)
    original = json.loads(row["document"])
    as_of = row["as_of"] if at is None else at
    req = dict(request or {})
    if policy_id:
        req["policy_id"] = policy_id
    if policy_version or version:
        req["policy_version"] = policy_version or version
    req["policy_mode"] = policy_mode or mode or "fixed"
    if policy_id or policy_version or version or (request or {}).get("policy_id"):
        selection = policy_registry.select_policy(db, data=req, at=as_of, default_mode="fixed")
        chosen_policy, basis, selection_obj = selection["policy"], selection["basis"], selection
    else:
        chosen_policy = AnalysisPolicy.model_validate(json.loads(row["policy"]))
        selection_obj = None
        basis = original.get("policy_basis") or policy_registry.build_basis(
            mode="inline", record={"policy_id": chosen_policy.policy_id,
                                   "version": chosen_policy.version,
                                   "policy": policy_registry.dump_policy(chosen_policy)})
    document = analysis.run(
        db, cut=row["cut"], as_of=policy_registry.parse_instant(as_of, field="as_of"),
        region=original["region"], service_kind=original.get("service_kind"),
        cohort=original.get("cohort"), policy=chosen_policy,
        policy_basis=basis, selection=selection_obj)
    result = {"analysis_id": analysis_id, "source_revision": row["revision"],
              "stored": False, "document": document,
              "digest": document["digest"], "policy_basis": basis}
    if store_as_new_revision:
        stored = publication.store_analysis(
            db, analysis_id, row["cut"], policy_registry.parse_instant(row["as_of"]),
            chosen_policy, document)
        result.update({"stored": True, **stored})
    return result


def get_analysis(db: Connection, analysis_id: str, revision: int | None = None,
                 *, include_document: bool = True) -> dict:
    row = publication.selected_analysis(db, analysis_id, revision)
    import json

    document = json.loads(row["document"])
    result = {"analysis_id": row["analysis_id"], "revision": row["revision"],
              "cut": row["cut"], "as_of": row["as_of"], "state": row["state"],
              "digest": row["digest"], "created_at": row["created_at"],
              "policy": json.loads(row["policy"]),
              "policy_basis": document.get("policy_basis"),
              "applied_policy": document.get("applied_policy")}
    if include_document:
        result["document"] = document
    return result


def list_analysis_revisions(db: Connection, analysis_id: str) -> dict:
    rows = db.execute("SELECT revision,cut,as_of,state,digest,created_at FROM analysis "
                      "WHERE analysis_id=? ORDER BY revision", (analysis_id,)).fetchall()
    if not rows:
        raise missing("analysis", analysis_id)
    return {"analysis_id": analysis_id,
            "revisions": [dict(row) for row in rows], "count": len(rows)}


def review_status(db: Connection, analysis_id: str, revision: int | None = None) -> dict:
    row = publication.selected_analysis(db, analysis_id, revision)
    status = publication.approval_set(db, analysis_id, row["revision"])
    import json

    document = json.loads(row["document"])
    return {**status, "source_cut": row["cut"], "created_at": row["created_at"],
            "policy": json.loads(row["policy"]),
            "policy_basis": document.get("policy_basis"),
            "applied_policy": document.get("applied_policy")}


def approve(db: Connection, payload: dict) -> dict:
    try:
        model = Approval.model_validate(payload)
    except ValidationError as exc:
        raise invalid("approval_invalid", "审批内容不符合业务契约",
                      exc.errors(include_url=False)) from exc
    return publication.approve(db, model)


def publish(db: Connection, analysis_id: str, expected_edition: int) -> dict:
    row = publication.selected_analysis(db, analysis_id)
    review = review_status(db, analysis_id, row["revision"])
    if not review["ready"]:
        raise Problem(409, "publication_blocked", "复核未通过，不能生成公告",
                      {"failures": review["failures"], "analysis_revision": row["revision"]})
    result = publication.publish(db, analysis_id, expected_edition)
    result["published_at"] = result.get("published_at") or datetime.now(timezone.utc).isoformat()
    return result


def compare_publications(db: Connection, analysis_id: str, left: int, right: int) -> dict:
    first = publication.bulletin(db, analysis_id, left)
    second = publication.bulletin(db, analysis_id, right)
    comparison = publication.compare(db, analysis_id, left, right)
    changed_areas = _changed_rows(first["document"]["areas"],
                                  second["document"]["areas"])
    result = {**comparison,
              "changed_areas": changed_areas,
              "left_cut": first["document"]["cut"],
              "right_cut": second["document"]["cut"],
              "left_as_of": first["document"]["as_of"],
              "right_as_of": second["document"]["as_of"]}
    # 在可重算账本上做反事实归因，区分数据更正与政策切换。
    try:
        attribution = change_attribution.attribute_bulletins(db, analysis_id, left, right)
        result["attribution"] = attribution
        source_by_area = {(item["district"], item["service_kind"], item["cohort"]): item
                          for item in attribution["areas"]}
        for changed in result["changed_areas"]:
            info = source_by_area.get((changed["district"], changed["service_kind"],
                                       changed["cohort"]))
            if info is not None:
                changed["change_source"] = info["change_source"]
                changed["source"] = info["source"]
                changed["data_change"] = info["data_change"]
                changed["policy_change"] = info["policy_change"]
                changed["attribution_detail"] = info
        result["change_sources"] = attribution["summary"]
    except Problem:
        # 归因是增强信息；若历史快照无法重算（例如旧库缺少表），不影响原有比较。
        pass
    return result


def attribute_changes(db: Connection, payload: dict | None = None, **kwargs) -> dict:
    if payload is not None:
        return change_attribution.attribute_changes(db, request=payload, **kwargs)
    return change_attribution.attribute_changes(db, **kwargs)


def attribute_analysis_revisions(db: Connection, analysis_id: str,
                                 left_revision: int | None = None,
                                 right_revision: int | None = None, **kwargs) -> dict:
    return change_attribution.attribute_revisions(
        db, analysis_id, left_revision=left_revision,
        right_revision=right_revision, **kwargs)


def compare_analysis_revisions(db: Connection, analysis_id: str,
                               left_revision: int | None = None,
                               right_revision: int | None = None, **kwargs) -> dict:
    """比较同一分析的两个修订，并附带数据/规则归因与逐区域差异。"""
    attribution = attribute_analysis_revisions(
        db, analysis_id, left_revision=left_revision,
        right_revision=right_revision, **kwargs)
    left_doc = json.loads(publication.selected_analysis(
        db, analysis_id, attribution["left_revision"])["document"])
    right_doc = json.loads(publication.selected_analysis(
        db, analysis_id, attribution["right_revision"])["document"])
    return {**attribution,
            "changed_areas": _changed_rows(left_doc["areas"], right_doc["areas"])}


def _changed_rows(left: list[dict], right: list[dict]) -> list[dict]:
    identity = lambda row: (row["district"], row["service_kind"], row["cohort"])
    before = {identity(row): row for row in left}
    after = {identity(row): row for row in right}
    result = []
    for key in sorted(set(before) | set(after)):
        old, new = before.get(key), after.get(key)
        if old == new:
            continue
        result.append({"district": key[0], "service_kind": key[1], "cohort": key[2],
                       "before": old, "after": new})
    return result
