from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from silverwatch import (analysis, attribution, comparison, policies,
                         publication, registry, service, trends)
from silverwatch.api import create_app
from silverwatch.contracts import AnalysisPolicy, Approval, TouchBatch
from silverwatch.errors import Problem
from tests.support import (DatabaseCase, instant, resolve_case, seed_case,
                           touch_data)


def policy_version(policy_id="eligibility", version=1, effective_from="2026-01-01T00:00:00Z",
                   max_rate=60, **updates):
    value = {"policy_id": policy_id, "version": version,
             "effective_from": effective_from,
             "minimum_exposure": 100, "minimum_supply_coverage_bp": 0,
             "max_substantiated_rate_bp": max_rate,
             "severe_open_cases_block": False,
             "require_verified_remediation": False,
             "minimum_touch_coverage_bp": 0,
             "title": f"Eligibility policy v{version}"}
    return value | updates


class PolicyRegistryTests(DatabaseCase, unittest.TestCase):
    def test_versions_have_effective_ranges_and_resolve_at_a_point(self):
        with self.transaction() as db:
            policies.register_version(db, policy_version(version=1, max_rate=60))
            policies.register_version(db, policy_version(
                version=2, effective_from="2026-11-01T00:00:00Z", max_rate=40))
            october = policies.effective_version(db, "eligibility", "2026-10-20")
            november = policies.effective_version(db, "eligibility", "2026-11-20T00:00:00Z")
            boundary = policies.effective_version(db, "eligibility", "2026-11-01T00:00:00Z")
            self.assertEqual((october["version"], november["version"], boundary["version"]),
                             (1, 2, 2))
            timeline = policies.policy_timeline(db, "eligibility")
            self.assertEqual(timeline[0]["resolved_effective_to"], "2026-11-01T00:00:00+00:00")

    def test_same_version_replays_but_changed_content_is_rejected(self):
        with self.transaction() as db:
            first = policies.register_version(db, policy_version())
            replay = policies.register_version(db, policy_version())
            self.assertFalse(first["replayed"])
            self.assertTrue(replay["replayed"])
            with self.assertRaises(Problem) as captured:
                policies.register_version(db, policy_version(max_rate=99))
            self.assertEqual(captured.exception.code, "policy_version_conflict")

    def test_overlapping_effective_ranges_are_rejected(self):
        with self.transaction() as db:
            policies.register_version(db, policy_version(
                version=1, effective_to="2026-12-01T00:00:00Z"))
            with self.assertRaises(Problem) as captured:
                policies.register_version(db, policy_version(
                    version=2, effective_from="2026-11-01T00:00:00Z"))
            self.assertEqual(captured.exception.code, "policy_range_overlap")

    def test_unknown_policy_reference_is_missing(self):
        with self.transaction() as db:
            with self.assertRaises(Problem) as captured:
                policies.effective_version(db, "absent", "2026-10-01T00:00:00Z")
            self.assertEqual(captured.exception.status, 404)


class PolicyDrivenAnalysisTests(DatabaseCase, unittest.TestCase):
    def seed_area(self, db):
        seed_case(db, touch_observed_at="2026-10-01T00:00:00Z")
        resolve_case(db)
        return registry.source_cut(db)["cut"]

    def register_two_versions(self, db):
        policies.register_version(db, policy_version(version=1, max_rate=60))
        policies.register_version(db, policy_version(
            version=2, effective_from="2026-11-01T00:00:00Z", max_rate=40))

    def test_analysis_states_which_policy_basis_it_applied(self):
        with self.transaction() as db:
            cut = self.seed_area(db)
            self.register_two_versions(db)
            created = service.analyze(db, {"analysis_id": "an-oct", "region": "suburban",
                "service_kind": "meal", "cohort": "70_79",
                "as_of": "2026-10-15T00:00:00Z", "policy_id": "eligibility",
                "through_cut": cut})
            stored = service.get_analysis(db, "an-oct")
            basis = stored["policy_basis"]
            self.assertEqual(basis["mode"], "effective")
            self.assertEqual(basis["version"], 1)
            self.assertIn("旧口径", basis["explanation"])
            self.assertEqual(created["digest"], stored["digest"])

    def test_inline_policy_keeps_inline_basis(self):
        with self.transaction() as db:
            cut = self.seed_area(db)
            inline = {"policy_id": "policy-one", "minimum_exposure": 100,
                      "minimum_supply_coverage_bp": 8000,
                      "max_substantiated_rate_bp": 50,
                      "severe_open_cases_block": True,
                      "require_verified_remediation": True,
                      "minimum_touch_coverage_bp": 8000}
            doc = analysis.run(
                db, cut=cut, as_of=instant("2026-11-02T00:00:00Z"),
                region="suburban", service_kind="meal", cohort="70_79",
                policy=AnalysisPolicy.model_validate(inline))
            self.assertEqual(doc["policy_basis"]["mode"], "inline")
            self.assertEqual(doc["applied_policy"]["minimum_exposure"], 100)

    def test_trend_crossing_effective_date_uses_old_then_new_policy(self):
        with self.transaction() as db:
            cut = self.seed_area(db)
            self.register_two_versions(db)
            result = trends.build(db, region="suburban", periods=[
                {"label": "october", "cut": cut, "as_of": "2026-10-15T00:00:00Z"},
                {"label": "november", "cut": cut, "as_of": "2026-11-15T00:00:00Z"}],
                policy_id="eligibility", service_kind="meal", cohort="70_79")
            self.assertEqual(result["policy_mode"], "effective")
            self.assertFalse(result["uniform_policy"])
            self.assertEqual([entry["policy_version"] for entry in result["policy_schedule"]], [1, 2])
            self.assertEqual(len(result["policy_switches"]), 1)
            points = result["series"][0]["points"]
            self.assertTrue(points[0]["eligible"])
            self.assertFalse(points[1]["eligible"])
            self.assertEqual([p["policy_version"] for p in points], [1, 2])

    def test_fixed_policy_replay_recalculates_all_history_under_one_rule(self):
        with self.transaction() as db:
            cut = self.seed_area(db)
            self.register_two_versions(db)
            periods = [
                {"label": "october", "cut": cut, "as_of": "2026-10-15T00:00:00Z"},
                {"label": "november", "cut": cut, "as_of": "2026-11-15T00:00:00Z"}]
            strict = trends.build(db, region="suburban", periods=periods,
                                  policy_id="eligibility", policy_mode="fixed",
                                  policy_version=2, service_kind="meal", cohort="70_79")
            self.assertTrue(strict["uniform_policy"])
            self.assertEqual([p["eligible"] for p in strict["series"][0]["points"]],
                             [False, False])
            lenient = trends.build(db, region="suburban", periods=periods,
                                   policy_id="eligibility", uniform_policy=True,
                                   policy_version=1, service_kind="meal", cohort="70_79")
            self.assertEqual([p["eligible"] for p in lenient["series"][0]["points"]],
                             [True, True])

    def test_comparison_resolves_one_common_effective_policy(self):
        with self.transaction() as db:
            self.seed_area(db)
            self.register_two_versions(db)
            result = comparison.compare_regions(
                db, regions=["suburban", "rural"], as_of="2026-10-15T00:00:00Z",
                policy_id="eligibility")
            self.assertEqual(result["policy_basis"]["version"], 1)
            self.assertEqual(result["policy_id"], "eligibility")


class AttributionTests(DatabaseCase, unittest.TestCase):
    def _setup(self, db):
        seed_case(db, touch_observed_at="2026-10-01T00:00:00Z")
        resolve_case(db)
        base_cut = registry.source_cut(db)["cut"]
        policies.register_version(db, policy_version(version=1, max_rate=60))
        policies.register_version(db, policy_version(
            version=2, effective_from="2026-11-01T00:00:00Z", max_rate=40))
        v1 = policies.get_version(db, "eligibility", 1)["policy"]
        v2 = policies.get_version(db, "eligibility", 2)["policy"]
        registry.register_touch(db, TouchBatch.model_validate(touch_data(
            exposed_people=150, source_revision=2,
            observed_at="2026-11-10T00:00:00Z")))
        new_cut = registry.source_cut(db)["cut"]
        return base_cut, new_cut, v1, v2

    def _attribute(self, db, base_cut, new_cut, v1, v2, *, same_policy=False):
        return attribution.attribute_changes(
            db, region="suburban", service_kind="meal", cohort="70_79",
            before_cut=base_cut, before_as_of="2026-11-15T00:00:00Z", before_policy=v1,
            after_cut=new_cut,
            after_as_of="2026-11-15T00:00:00Z", after_policy=v1 if same_policy else v2)

    def test_policy_only_change_is_attributed_to_rules(self):
        with self.transaction() as db:
            base_cut, _, v1, v2 = self._setup(db)
            result = attribution.attribute_changes(
                db, region="suburban", service_kind="meal", cohort="70_79",
                before_cut=base_cut, before_as_of="2026-11-15T00:00:00Z", before_policy=v1,
                after_cut=base_cut, after_as_of="2026-11-15T00:00:00Z", after_policy=v2)
            area = result["areas"][0]
            self.assertEqual(area["source"], "policy")
            self.assertTrue(area["policy_change"])
            self.assertFalse(area["data_change"])
            self.assertIn("max_substantiated_rate_bp", result["threshold_changes"])

    def test_data_correction_only_change_is_attributed_to_data(self):
        with self.transaction() as db:
            base_cut, new_cut, v1, _ = self._setup(db)
            result = self._attribute(db, base_cut, new_cut, v1, None, same_policy=True)
            area = result["areas"][0]
            self.assertEqual(area["source"], "data")
            self.assertTrue(area["data_change"])
            self.assertFalse(area["policy_change"])
            self.assertIn("exposed", area["data_effect"]["metric_changes"])

    def test_correction_and_policy_together_are_flagged_both(self):
        with self.transaction() as db:
            base_cut, new_cut, v1, v2 = self._setup(db)
            result = self._attribute(db, base_cut, new_cut, v1, v2)
            area = result["areas"][0]
            self.assertEqual(area["source"], "both")
            self.assertTrue(area["data_change"] and area["policy_change"])
            self.assertTrue(area["policy_effect"]["detected_on_old_data"])
            self.assertEqual(result["summary"]["both_changed"], 1)


class BulletinImmutabilityTests(DatabaseCase, unittest.TestCase):
    def test_registering_new_policy_versions_does_not_rewrite_signed_bulletin(self):
        with self.transaction() as db:
            seed_case(db, touch_observed_at="2026-10-01T00:00:00Z")
            resolve_case(db)
            cut = registry.source_cut(db)["cut"]
            policies.register_version(db, policy_version(version=1, max_rate=60))
            created = service.analyze(db, {"analysis_id": "an-freeze", "region": "suburban",
                "service_kind": "meal", "cohort": "70_79",
                "as_of": "2026-10-15T00:00:00Z", "policy_id": "eligibility",
                "through_cut": cut})
            for role, reviewer, approval_id in (
                    ("service_planning", "planner-x", "approval-planning-x"),
                    ("consumer_protection", "ombuds-x", "approval-protection-x")):
                publication.approve(db, Approval(
                    approval_id=approval_id, analysis_id="an-freeze", reviewer=reviewer,
                    role=role, approved=True, analysis_digest=created["digest"],
                    rationale="Frozen evidence reviewed."))
            published = publication.publish(db, "an-freeze", 0)
            before = publication.bulletin(db, "an-freeze", 1)
            policies.register_version(db, policy_version(
                version=2, effective_from="2026-11-01T00:00:00Z", max_rate=40))
            policies.register_version(db, policy_version(
                version=3, effective_from="2026-11-20T00:00:00Z", max_rate=10))
            after = publication.bulletin(db, "an-freeze", 1)
            self.assertEqual(before, after)
            self.assertEqual(published["bulletin_digest"], after["bulletin_digest"])
            from silverwatch import lineage
            self.assertTrue(lineage.verify_bulletin(db, "an-freeze", 1)["valid"])


class PolicyHttpTests(DatabaseCase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.client = TestClient(create_app(self.store.location))

    def test_policy_lifecycle_and_policy_driven_analysis_over_http(self):
        body = policy_version()
        response = self.client.post("/policies", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["version"], 1)
        self.client.post("/policies", json=policy_version(
            version=2, effective_from="2026-11-01", max_rate=40))
        effective = self.client.get("/policies/eligibility/effective?at=2026-10-15")
        self.assertEqual(effective.status_code, 200)
        self.assertEqual(effective.json()["version"], 1)

        # seed facts through the service layer on the same database file
        with self.transaction() as db:
            seed_case(db, touch_observed_at="2026-10-01T00:00:00Z")
            resolve_case(db)
            cut = registry.source_cut(db)["cut"]

        response = self.client.post("/analyses", json={
            "analysis_id": "http-an", "region": "suburban", "service_kind": "meal",
            "cohort": "70_79", "as_of": "2026-10-15T00:00:00Z",
            "policy_id": "eligibility", "through_cut": cut})
        self.assertEqual(response.status_code, 200, response.text)
        read = self.client.get("/analyses/http-an")
        self.assertEqual(read.status_code, 200)
        self.assertEqual(read.json()["policy_basis"]["version"], 1)

    def test_http_attribution_endpoint(self):
        with self.transaction() as db:
            base_cut, new_cut, _, _ = AttributionSeeder.seed(db, self)
        response = self.client.post("/analysis-attribution", json={
            "region": "suburban", "service_kind": "meal", "cohort": "70_79",
            "before": {"cut": base_cut, "as_of": "2026-11-15T00:00:00Z"},
            "after": {"cut": new_cut, "as_of": "2026-11-15T00:00:00Z"},
            "policy_id": "eligibility", "before_version": 1, "after_version": 2})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["areas"][0]["source"], "both")


class RecomputeTests(DatabaseCase, unittest.TestCase):
    def test_recompute_under_new_policy_does_not_store_or_rewrite(self):
        with self.transaction() as db:
            seed_case(db, touch_observed_at="2026-10-01T00:00:00Z")
            resolve_case(db)
            cut = registry.source_cut(db)["cut"]
            policies.register_version(db, policy_version(version=1, max_rate=60))
            policies.register_version(db, policy_version(
                version=2, effective_from="2026-11-01T00:00:00Z", max_rate=40))
            created = service.analyze(db, {"analysis_id": "an-rc", "region": "suburban",
                "service_kind": "meal", "cohort": "70_79",
                "as_of": "2026-10-15T00:00:00Z", "policy_id": "eligibility",
                "through_cut": cut})
            self.assertTrue(service.get_analysis(db, "an-rc")["document"]["areas"][0]["eligible"])
            recomputed = service.recompute(db, "an-rc", policy_id="eligibility", policy_version=2)
            self.assertFalse(recomputed["document"]["areas"][0]["eligible"])
            self.assertFalse(recomputed["stored"])
            # 原已存分析仍是旧口径且只有一个修订。
            self.assertEqual(service.get_analysis(db, "an-rc")["digest"], created["digest"])
            self.assertEqual(service.list_analysis_revisions(db, "an-rc")["count"], 1)

    def test_source_labels_compare_equal_across_naming_conventions(self):
        from silverwatch.attribution import source_label
        label = source_label("policy", long_form=True)
        for token in ("policy", "policy_change", "rule_change", "policy_driven"):
            self.assertEqual(label, token)
        self.assertNotEqual(label, "data")


class AttributionSeeder:
    @staticmethod
    def seed(db, case):
        seed_case(db, touch_observed_at="2026-10-01T00:00:00Z")
        resolve_case(db)
        base_cut = registry.source_cut(db)["cut"]
        policies.register_version(db, policy_version(version=1, max_rate=60))
        policies.register_version(db, policy_version(
            version=2, effective_from="2026-11-01T00:00:00Z", max_rate=40))
        registry.register_touch(db, TouchBatch.model_validate(touch_data(
            exposed_people=150, source_revision=2,
            observed_at="2026-11-10T00:00:00Z")))
        return base_cut, registry.source_cut(db)["cut"], None, None


if __name__ == "__main__":
    unittest.main()
