import unittest
import tempfile
from decimal import Decimal
from pathlib import Path
from trialvault.service import Vault
from trialvault.validation import VaultError, canonical, digest, parse_json
from trialvault.analysis.statistics import (
    holm_adjust,
    ols_residuals,
    permutation_test,
)
from trialvault.analysis.package import verify_package

ASSIGNMENT_SCHEMA = {
    "id": "assignments",
    "version": 1,
    "fields": [
        {"name": "panel", "type": "text"},
        {"name": "arm", "type": "enum", "choices": ["bare", "coated"]},
        {"name": "site", "type": "enum", "choices": ["north", "south"]},
        {"name": "roughness", "type": "decimal", "nullable": True, "required": False},
    ],
    "primary_key": ["panel"],
}

OUTCOME_SCHEMA = {
    "id": "outcomes",
    "version": 1,
    "fields": [
        {"name": "id", "type": "text"},
        {"name": "panel", "type": "text"},
        {"name": "measured_at", "type": "timestamp"},
        {"name": "adhesion", "type": "decimal", "nullable": True, "required": False},
        {"name": "rusted", "type": "boolean", "nullable": True, "required": False},
        {"name": "blistered", "type": "boolean", "nullable": True, "required": False},
        {"name": "chalked", "type": "boolean", "nullable": True, "required": False},
        {
            "name": "applied",
            "type": "enum",
            "choices": ["bare", "coated"],
            "nullable": True,
            "required": False,
        },
    ],
    "primary_key": ["id"],
}

CLEAN_ASSIGNMENT = [
    {"panel": "n1", "arm": "coated", "site": "north", "roughness": "2.5"},
    {"panel": "n2", "arm": "bare", "site": "north", "roughness": "2"},
    {"panel": "n3", "arm": "coated", "site": "north", "roughness": "3"},
    {"panel": "n4", "arm": "bare", "site": "north", "roughness": "2.5"},
    {"panel": "s1", "arm": "coated", "site": "south", "roughness": "3.5"},
    {"panel": "s2", "arm": "bare", "site": "south", "roughness": "3"},
    {"panel": "s3", "arm": "coated", "site": "south", "roughness": "3.2"},
    {"panel": "s4", "arm": "bare", "site": "south", "roughness": "2.9"},
]


def outcome(row_id, panel, at, adhesion, rusted, blistered, chalked, applied):
    return {
        "id": row_id,
        "panel": panel,
        "measured_at": at,
        "adhesion": adhesion,
        "rusted": rusted,
        "blistered": blistered,
        "chalked": chalked,
        "applied": applied,
    }


CLEAN_OUTCOMES = [
    outcome("m1", "n1", "2026-10-01T09:00:00Z", "12", False, True, False, "coated"),
    outcome("m2", "n2", "2026-10-01T10:00:00Z", "8", True, False, False, "bare"),
    outcome("m3", "n3", "2026-10-02T09:00:00Z", "14", False, True, True, "coated"),
    outcome("m4", "n4", "2026-10-02T10:00:00Z", "10", True, False, False, "bare"),
    outcome("m5", "s1", "2026-10-03T09:00:00Z", "20", False, True, False, "coated"),
    outcome("m6", "s2", "2026-10-03T10:00:00Z", "14", True, False, True, "bare"),
    outcome("m7", "s3", "2026-10-04T09:00:00Z", "22", True, True, False, "coated"),
    outcome("m8", "s4", "2026-10-04T10:00:00Z", "16", False, False, True, "bare"),
]


def jsonl(rows):
    return ("\n".join(canonical(row) for row in rows) + ("\n" if rows else "")).encode()


def plan_kwargs(**changes):
    kwargs = {
        "plan_id": "coating",
        "title": "Coating adhesion analysis plan",
        "assignment": {"dataset": "randomization", "version": 1},
        "unit": "panel",
        "arm": {"field": "arm", "control": "bare", "treatment": "coated"},
        "strata": ["site"],
        "primary": {"id": "adhesion", "kind": "continuous", "field": "adhesion"},
        "secondary": [
            {"id": "rust", "kind": "binary", "field": "rusted"},
            {"id": "blister", "kind": "binary", "field": "blistered"},
            {"id": "chalk", "kind": "binary", "field": "chalked"},
        ],
        "cutoff": {"field": "measured_at", "at": "2026-12-31T00:00:00Z"},
        "actual_arm_field": "applied",
        "permutations": 200,
        "seed": "test-seed",
    }
    kwargs.update(changes)
    return kwargs


class VaultCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.vault = Vault(self.root / "vault")
        self.addCleanup(self.vault.close)
        self.vault.registry.add_schema(ASSIGNMENT_SCHEMA)
        self.vault.registry.add_schema(OUTCOME_SCHEMA)
        self.vault.registry.register(
            "randomization", "Synthetic randomization", "assignments", "owner"
        )
        self.vault.registry.register(
            "results", "Synthetic outcomes", "outcomes", "owner"
        )

    def add_assignment(self, rows=None, request="rand", expected=0):
        return self.vault.ingestion.ingest(
            request,
            "randomization",
            1,
            jsonl(CLEAN_ASSIGNMENT if rows is None else rows),
            "jsonl",
            "internal",
            ["research"],
            expected_version=expected,
        )

    def add_outcomes(self, rows=None, request="out", expected=0):
        return self.vault.ingestion.ingest(
            request,
            "results",
            1,
            jsonl(CLEAN_OUTCOMES if rows is None else rows),
            "jsonl",
            "internal",
            ["research"],
            expected_version=expected,
        )

    def define(self, **changes):
        return self.vault.analysis_plans.define(**plan_kwargs(**changes))

    def analyze(self, run_id="run-1", version=1, outcomes_version=1):
        return self.vault.analysis_runs.run(
            run_id, "coating", version, {"dataset": "results", "version": outcomes_version}
        )


def codes(result):
    return {item["code"] for item in result["explanations"]}


class PlanTests(VaultCase):
    def test_plan_locks_fixed_assignment_and_is_immutable(self):
        self.add_assignment()
        plan = self.define()
        record = self.vault.registry.version("randomization", 1)
        self.assertEqual(plan["version"], 1)
        self.assertEqual(plan["assignment"]["digest"], record["digest"])
        self.assertEqual(plan["enrollment"]["units"], 8)
        self.assertEqual(plan["enrollment"]["treatment"], 4)
        self.assertEqual(
            plan["enrollment"]["strata"],
            [
                {"key": {"site": "north"}, "units": 4},
                {"key": {"site": "south"}, "units": 4},
            ],
        )
        again = self.define()
        self.assertEqual(again, plan)
        with self.assertRaises(VaultError) as caught:
            self.define(seed="different-seed")
        self.assertEqual(caught.exception.code, "conflict")
        self.assertEqual(self.vault.analysis_plans.get("coating"), plan)

    def test_plan_revision_creates_linked_new_version(self):
        self.add_assignment()
        first = self.define()
        revised = self.vault.analysis_plans.revise(
            reason="Seed finalized before unblinding", **plan_kwargs(seed="seed-v2")
        )
        self.assertEqual(revised["version"], 2)
        self.assertEqual(
            revised["parent"],
            {"id": "coating", "version": 1, "digest": digest(first)},
        )
        self.assertEqual(revised["change_reason"], "Seed finalized before unblinding")
        self.assertEqual(self.vault.analysis_plans.get("coating", 1), first)
        self.assertEqual(self.vault.analysis_plans.get("coating"), revised)
        versions = self.vault.analysis_plans.versions("coating")
        self.assertEqual([item["version"] for item in versions], [1, 2])
        self.assertIsNone(versions[0]["parent"])
        with self.assertRaises(VaultError) as caught:
            self.vault.analysis_plans.revise(
                reason="No actual change", **plan_kwargs(seed="seed-v2")
            )
        self.assertEqual(caught.exception.code, "conflict")
        with self.assertRaises(VaultError):
            self.vault.analysis_plans.revise(reason="", **plan_kwargs(seed="seed-v3"))

    def test_plan_validation_rejects_bad_design(self):
        self.add_assignment()
        bad = [
            {"unit": "site"},
            {"arm": {"field": "arm", "control": "placebo", "treatment": "coated"}},
            {"arm": {"field": "arm", "control": "bare", "treatment": "bare"}},
            {"arm": {"field": "site", "control": "north", "treatment": "south"}},
            {"strata": ["region"]},
            {"strata": ["roughness"]},
            {"strata": ["arm"]},
            {"strata": ["site", "site"]},
            {"cutoff": {"field": "measured_at", "at": "not-a-time"}},
            {"preprocessing": {"repeated": "average"}},
            {"preprocessing": {"bounds": {"minimum": "10", "maximum": "5"}}},
            {"adjustment": {"covariates": ["unknown"]}},
            {"adjustment": {"covariates": ["arm"]}},
            {"adjustment": {"covariates": ["site"]}},
            {
                "primary": {"id": "rust", "kind": "binary", "field": "rusted"},
                "adjustment": {"covariates": ["roughness"]},
            },
            {"permutations": 5},
            {"seed": ""},
            {"confidence": "0.80"},
            {"assignment": {"dataset": "randomization", "version": 9}},
            {"primary": {"id": "rust", "kind": "binary", "field": "rusted"},
             "secondary": [{"id": "rust", "kind": "binary", "field": "blistered"}]},
        ]
        for change in bad:
            with self.subTest(change=change):
                with self.assertRaises(VaultError):
                    self.define(**change)
        with self.assertRaises(VaultError):
            self.vault.analysis_plans.get("coating")

    def test_plan_requires_enrolled_arms_and_units(self):
        self.add_assignment(rows=[], request="empty")
        with self.assertRaises(VaultError) as caught:
            self.define()
        self.assertEqual(caught.exception.code, "plan")
        one_arm = [
            {"panel": "a", "arm": "coated", "site": "north"},
            {"panel": "b", "arm": "coated", "site": "south"},
        ]
        self.add_assignment(rows=one_arm, request="one-arm", expected=1)
        with self.assertRaises(VaultError) as caught:
            self.define(assignment={"dataset": "randomization", "version": 2})
        self.assertEqual(caught.exception.code, "plan")

    def test_plan_allows_outcomes_to_arrive_late(self):
        self.add_assignment()
        plan = self.define()
        self.assertEqual(plan["version"], 1)
        self.add_outcomes()
        result = self.analyze()
        self.assertEqual(result["primary"]["estimate"], "5")

    def test_withdrawn_assignment_blocks_plan_and_run(self):
        self.add_assignment()
        self.add_outcomes()
        self.define()
        self.vault.registry.withdraw("randomization", 1, "Randomization list error")
        with self.assertRaises(VaultError) as caught:
            self.analyze()
        self.assertEqual(caught.exception.code, "state")
        with self.assertRaises(VaultError) as caught:
            self.define(plan_id="other")
        self.assertEqual(caught.exception.code, "state")


class RunTests(VaultCase):
    def setUp(self):
        super().setUp()
        self.add_assignment()
        self.add_outcomes()
        self.define()

    def test_exact_stratified_estimate_and_interval(self):
        result = self.analyze()
        primary = result["primary"]
        self.assertEqual(primary["estimate"], "5")
        self.assertEqual(primary["standard_error"], "1")
        self.assertEqual(primary["interval"]["low"], "3.040036015459946")
        self.assertEqual(primary["interval"]["high"], "6.959963984540054")
        self.assertEqual(primary["interval"]["level"], "0.95")
        self.assertEqual(primary["interval"]["method"], "normal_approximation")
        self.assertIn("assumption", primary["interval"])
        self.assertEqual(primary["test"]["method"], "stratified_randomization_permutation")
        self.assertEqual(primary["test"]["permutations"], 200)
        p_value = Decimal(primary["test"]["p_value"])
        self.assertTrue(Decimal(0) < p_value <= Decimal(1))
        north, south = primary["strata"]
        self.assertEqual(north["stratum"], {"site": "north"})
        self.assertEqual(
            (north["enrolled"], north["treated"], north["control"]), (4, 2, 2)
        )
        self.assertEqual(north["weight"], "0.5")
        self.assertEqual(north["effect"], "4")
        self.assertEqual(south["effect"], "6")
        self.assertEqual(result["unit"], "panel")
        self.assertEqual(result["disposition"]["assigned"], 8)
        self.assertEqual(result["disposition"]["analyzed_primary"], 8)
        self.assertEqual(result["disposition"]["unmeasured"], [])
        self.assertEqual(
            codes(result), {"allocation", "constant_stratum", "degenerate_interval"}
        )

    def test_secondary_family_holm_adjustment(self):
        result = self.analyze()
        secondary = result["secondary"]
        self.assertEqual(len(secondary), 3)
        rust = secondary[0]
        self.assertEqual(rust["estimate"], "-0.5")
        self.assertEqual(rust["standard_error"], "0.25")
        self.assertEqual(rust["interval"]["low"], "-0.9899909961350135")
        self.assertEqual(rust["interval"]["high"], "-0.0100090038649865")
        raw = [Decimal(item["test"]["p_value"]) for item in secondary]
        expected = holm_adjust(raw)
        for item, adjusted in zip(secondary, expected):
            self.assertEqual(item["holm"]["family_size"], 3)
            self.assertEqual(Decimal(item["holm"]["p_adjusted"]), adjusted)
            self.assertTrue(
                Decimal(item["holm"]["p_adjusted"]) >= Decimal(item["test"]["p_value"])
            )

    def test_run_is_idempotent_and_deterministic(self):
        first = self.analyze("run-1")
        again = self.analyze("run-1")
        self.assertEqual(first, again)
        other = self.analyze("run-2")
        self.assertEqual({**other, "id": "run-1"}, first)
        self.vault.analysis_plans.revise(
            reason="More permutations", **plan_kwargs(permutations=500)
        )
        with self.assertRaises(VaultError) as caught:
            self.analyze("run-1", version=2)
        self.assertEqual(caught.exception.code, "conflict")

    def test_itt_crossover_analyzed_as_assigned(self):
        rows = [
            row if row["id"] != "m7" else {**row, "applied": "bare"}
            for row in CLEAN_OUTCOMES
        ]
        self.add_outcomes(rows=rows, request="out-x", expected=1)
        result = self.analyze(outcomes_version=2)
        self.assertEqual(
            result["disposition"]["crossovers"],
            [{"unit": "s3", "assigned": "coated", "actual": "bare"}],
        )
        self.assertEqual(result["primary"]["estimate"], "5")
        self.assertIn("crossover", codes(result))

    def test_repeated_measures_follow_planned_rule(self):
        rows = CLEAN_OUTCOMES + [
            outcome("m1b", "n1", "2026-11-01T09:00:00Z", "20", False, True, False, "coated")
        ]
        self.add_outcomes(rows=rows, request="out-r", expected=1)
        result = self.analyze(outcomes_version=2)
        self.assertEqual(
            result["disposition"]["repeated"],
            [
                {
                    "unit": "n1",
                    "kept_at": "2026-11-01T09:00:00.000000Z",
                    "dropped_at": "2026-10-01T09:00:00.000000Z",
                }
            ],
        )
        self.assertEqual(result["primary"]["estimate"], "7")
        self.vault.analysis_plans.revise(
            reason="Switch to earliest measure",
            **plan_kwargs(preprocessing={"repeated": "earliest_before_cutoff"}),
        )
        early = self.analyze("run-early", version=2, outcomes_version=2)
        self.assertEqual(early["primary"]["estimate"], "5")
        self.assertEqual(
            early["disposition"]["repeated"][0]["kept_at"],
            "2026-10-01T09:00:00.000000Z",
        )

    def test_after_cutoff_rows_excluded_and_explained(self):
        rows = [
            row
            for row in CLEAN_OUTCOMES
            if row["id"] != "m2"
        ] + [
            outcome("m2b", "n2", "2027-02-01T09:00:00Z", "9", True, False, False, "bare")
        ]
        self.add_outcomes(rows=rows, request="out-c", expected=1)
        result = self.analyze(outcomes_version=2)
        self.assertEqual(result["disposition"]["unmeasured"], ["n2"])
        excluded = result["disposition"]["excluded_rows"]
        self.assertEqual(
            excluded,
            [
                {
                    "unit": "n2",
                    "reason": "after_cutoff",
                    "observed_at": "2027-02-01T09:00:00.000000Z",
                }
            ],
        )
        self.assertEqual(result["primary"]["estimate"], "4.5")
        self.assertIn("after_cutoff", codes(result))
        self.assertIn("singleton_arm", codes(result))

    def test_all_late_results_make_primary_inestimable(self):
        rows = [
            {**row, "measured_at": "2027-02-01T09:00:00Z"} for row in CLEAN_OUTCOMES
        ]
        self.add_outcomes(rows=rows, request="out-late", expected=1)
        result = self.analyze(outcomes_version=2)
        self.assertFalse(result["primary"]["estimable"])
        self.assertIsNone(result["primary"]["estimate"])
        self.assertIsNone(result["primary"]["test"])
        self.assertEqual(result["disposition"]["analyzed_primary"], 0)
        self.assertIn("no_contributing_stratum", codes(result))
        for item in result["secondary"]:
            self.assertIsNone(item["holm"])

    def test_unmeasured_units_reported(self):
        rows = [row for row in CLEAN_OUTCOMES if row["id"] != "m6"]
        self.add_outcomes(rows=rows, request="out-u", expected=1)
        result = self.analyze(outcomes_version=2)
        self.assertEqual(result["disposition"]["unmeasured"], ["s2"])
        self.assertEqual(result["primary"]["estimate"], "4.5")
        self.assertIn("unmeasured", codes(result))

    def test_unassigned_outcome_units_reported(self):
        rows = CLEAN_OUTCOMES + [
            outcome("m9", "ghost", "2026-10-05T09:00:00Z", "30", False, False, False, "coated")
        ]
        self.add_outcomes(rows=rows, request="out-g", expected=1)
        result = self.analyze(outcomes_version=2)
        self.assertEqual(
            result["disposition"]["excluded_rows"],
            [{"unit": "ghost", "reason": "unassigned_unit"}],
        )
        self.assertEqual(result["primary"]["estimate"], "5")
        self.assertIn("unassigned_unit", codes(result))

    def test_empty_stratum_explained_and_weights_renormalized(self):
        assignment = [
            {"panel": "n1", "arm": "coated", "site": "north"},
            {"panel": "n2", "arm": "bare", "site": "north"},
            {"panel": "s1", "arm": "bare", "site": "south"},
            {"panel": "s2", "arm": "bare", "site": "south"},
        ]
        self.add_assignment(rows=assignment, request="rand-e", expected=1)
        rows = [
            outcome("m1", "n1", "2026-10-01T09:00:00Z", "12", False, True, False, "coated"),
            outcome("m2", "n2", "2026-10-01T10:00:00Z", "8", True, False, False, "bare"),
            outcome("m3", "s1", "2026-10-02T09:00:00Z", "5", True, False, False, "bare"),
            outcome("m4", "s2", "2026-10-02T10:00:00Z", "6", False, False, True, "bare"),
        ]
        self.add_outcomes(rows=rows, request="out-e", expected=1)
        self.vault.analysis_plans.revise(
            reason="Point to corrected randomization",
            **plan_kwargs(assignment={"dataset": "randomization", "version": 2}),
        )
        result = self.analyze(version=2, outcomes_version=2)
        self.assertEqual(result["primary"]["estimate"], "4")
        self.assertIn("empty_stratum", codes(result))
        self.assertIn("weights_renormalized", codes(result))
        south = result["primary"]["strata"][1]
        self.assertFalse(south["contributing"])
        self.assertEqual(south["treated"], 0)

    def test_constant_outcome_explained(self):
        rows = [
            {**row, "adhesion": "7", "rusted": False, "blistered": False, "chalked": False}
            for row in CLEAN_OUTCOMES
        ]
        self.add_outcomes(rows=rows, request="out-k", expected=1)
        result = self.analyze(outcomes_version=2)
        primary = result["primary"]
        self.assertEqual(primary["estimate"], "0")
        self.assertEqual(primary["standard_error"], "0")
        self.assertEqual(primary["interval"]["low"], "0")
        self.assertEqual(primary["interval"]["high"], "0")
        self.assertEqual(primary["test"]["p_value"], "1")
        self.assertIn("constant_stratum", codes(result))
        self.assertIn("degenerate_interval", codes(result))

    def test_planned_bounds_preprocessing_is_visible(self):
        self.vault.analysis_plans.revise(
            reason="Pre-declared plausible adhesion range",
            **plan_kwargs(
                preprocessing={"bounds": {"minimum": "0", "maximum": "15"}}
            ),
        )
        result = self.analyze(version=2)
        self.assertEqual(result["disposition"]["excluded_bounds"], ["s1", "s3", "s4"])
        self.assertEqual(result["primary"]["estimate"], "4")
        self.assertIn("bounds", codes(result))
        self.assertIn("empty_stratum", codes(result))

    def test_adjustment_records_fitted_sample(self):
        self.vault.analysis_plans.revise(
            reason="Adjust for baseline roughness",
            **plan_kwargs(adjustment={"covariates": ["roughness"]}),
        )
        result = self.analyze(version=2)
        adjustment = result["primary"]["adjustment"]
        self.assertEqual(adjustment["covariates"], ["roughness"])
        self.assertEqual(adjustment["fitted_count"], 8)
        self.assertEqual(
            adjustment["fitted_units"],
            ["n1", "n2", "n3", "n4", "s1", "s2", "s3", "s4"],
        )
        self.assertEqual(adjustment["excluded_incomplete_covariates"], 0)
        self.assertEqual(
            result["primary"]["unadjusted"], {"estimate": "5", "standard_error": "1"}
        )
        self.assertIsNotNone(result["primary"]["estimate"])
        self.assertIn("adjustment_fit", codes(result))
        repeat = self.analyze("run-adj", version=2)
        self.assertEqual(repeat["primary"], result["primary"])

    def test_adjustment_excludes_incomplete_covariates(self):
        assignment = [
            {**row, "roughness": None} if row["panel"] == "n4" else row
            for row in CLEAN_ASSIGNMENT
        ]
        self.add_assignment(rows=assignment, request="rand-a", expected=1)
        self.vault.analysis_plans.revise(
            reason="Adjust with partial covariates",
            **plan_kwargs(
                assignment={"dataset": "randomization", "version": 2},
                adjustment={"covariates": ["roughness"]},
            ),
        )
        result = self.analyze(version=2)
        adjustment = result["primary"]["adjustment"]
        self.assertEqual(adjustment["fitted_count"], 7)
        self.assertEqual(adjustment["excluded_incomplete_covariates"], 1)
        self.assertNotIn("n4", adjustment["fitted_units"])
        self.assertIn("adjustment_complete_cases", codes(result))

    def test_adjustment_singular_covariate_rejected(self):
        assignment = [
            {**row, "roughness": "2.5"} for row in CLEAN_ASSIGNMENT
        ]
        self.add_assignment(rows=assignment, request="rand-s", expected=1)
        self.vault.analysis_plans.revise(
            reason="Adjust with constant covariate",
            **plan_kwargs(
                assignment={"dataset": "randomization", "version": 2},
                adjustment={"covariates": ["roughness"]},
            ),
        )
        with self.assertRaises(VaultError) as caught:
            self.analyze(version=2)
        self.assertEqual(caught.exception.code, "adjustment")

    def test_late_data_creates_new_run_and_keeps_history(self):
        assignment = CLEAN_ASSIGNMENT + [
            {"panel": "n5", "arm": "coated", "site": "north", "roughness": "2.8"},
            {"panel": "s5", "arm": "bare", "site": "south", "roughness": "3.1"},
        ]
        self.add_assignment(rows=assignment, request="rand-l", expected=1)
        self.vault.analysis_plans.revise(
            reason="Point to ten panel randomization",
            **plan_kwargs(assignment={"dataset": "randomization", "version": 2}),
        )
        first = self.analyze("run-v1", version=2, outcomes_version=1)
        self.assertEqual(first["disposition"]["analyzed_primary"], 8)
        self.assertEqual(first["disposition"]["unmeasured"], ["n5", "s5"])
        late = CLEAN_OUTCOMES + [
            outcome("m9", "n5", "2026-11-01T09:00:00Z", "11", False, True, False, "coated"),
            outcome("m10", "s5", "2026-11-02T09:00:00Z", "15", True, False, False, "bare"),
        ]
        self.add_outcomes(rows=late, request="out-l2", expected=1)
        second = self.analyze("run-v2", version=2, outcomes_version=2)
        self.assertEqual(second["disposition"]["analyzed_primary"], 10)
        self.assertEqual(second["disposition"]["unmeasured"], [])
        self.assertTrue(second["primary"]["estimate"].startswith("4.666"))
        self.assertEqual(self.vault.analysis_runs.get("run-v1"), first)
        self.assertEqual(self.vault.analysis_plans.get("coating")["version"], 2)
        self.assertEqual(
            first["inputs"]["outcomes"]["version"],
            1,
        )
        self.assertEqual(second["inputs"]["outcomes"]["version"], 2)

    def test_withdrawn_outcomes_block_run(self):
        self.vault.registry.withdraw("results", 1, "Measurement device fault")
        with self.assertRaises(VaultError) as caught:
            self.analyze()
        self.assertEqual(caught.exception.code, "state")

    def test_outcome_schema_mismatch_rejected(self):
        partial_schema = {
            "id": "partial",
            "version": 1,
            "fields": [
                {"name": "id", "type": "text"},
                {"name": "panel", "type": "text"},
                {"name": "measured_at", "type": "timestamp"},
                {"name": "applied", "type": "enum", "choices": ["bare", "coated"]},
            ],
            "primary_key": ["id"],
        }
        self.vault.registry.add_schema(partial_schema)
        self.vault.registry.register("partial", "Partial outcomes", "partial", "owner")
        self.vault.ingestion.ingest(
            "out-p",
            "partial",
            1,
            jsonl(
                [
                    {
                        "id": "x1",
                        "panel": "n1",
                        "measured_at": "2026-10-01T09:00:00Z",
                        "applied": "coated",
                    }
                ]
            ),
            "jsonl",
            "internal",
            ["research"],
        )
        with self.assertRaises(VaultError) as caught:
            self.vault.analysis_runs.run(
                "run-p", "coating", 1, {"dataset": "partial", "version": 1}
            )
        self.assertEqual(caught.exception.code, "analysis")


class PackageTests(VaultCase):
    def setUp(self):
        super().setUp()
        self.add_assignment()
        self.add_outcomes()
        self.define()
        self.analyze("run-1")

    def test_export_verify_and_reuse(self):
        target = self.root / "package"
        exported = self.vault.analysis_packages.export("pkg-1", "run-1", target)
        self.assertEqual(exported["state"], "complete")
        self.assertEqual(exported["artifacts"], 2)
        verified = verify_package(target)
        self.assertEqual(verified["run"], "run-1")
        self.assertEqual(verified["digest"], exported["digest"])
        again = self.vault.analysis_packages.export("pkg-1", "run-1", target)
        self.assertTrue(again["reused"])
        with self.assertRaises(VaultError):
            self.vault.analysis_packages.export("pkg-1", "run-1", self.root / "other")
        self.assertEqual(self.vault.analysis_packages.pending(), [])

    def test_package_carries_inputs_disposition_and_seed(self):
        target = self.root / "package"
        self.vault.analysis_packages.export("pkg-1", "run-1", target)
        envelope = parse_json((target / "manifest.json").read_bytes())
        package = envelope["package"]
        self.assertEqual(package["seed"], "test-seed")
        self.assertEqual(package["permutations"], 200)
        roles = {item["role"] for item in package["inputs"]}
        self.assertEqual(roles, {"assignment", "outcomes"})
        document = parse_json((target / "analysis.json").read_bytes())
        self.assertEqual(document["result"]["disposition"]["assigned"], 8)
        self.assertEqual(document["plan"]["id"], "coating")

    def test_package_content_is_deterministic(self):
        self.vault.analysis_packages.export("pkg-1", "run-1", self.root / "one")
        self.vault.analysis_packages.export("pkg-2", "run-1", self.root / "two")
        self.assertEqual(
            (self.root / "one" / "analysis.json").read_bytes(),
            (self.root / "two" / "analysis.json").read_bytes(),
        )

    def test_package_tampering_detected(self):
        target = self.root / "package"
        self.vault.analysis_packages.export("pkg-1", "run-1", target)
        analysis = target / "analysis.json"
        original = analysis.read_bytes()
        analysis.write_bytes(b"{}")
        with self.assertRaises(VaultError):
            verify_package(target)
        analysis.write_bytes(original)
        self.assertEqual(verify_package(target)["run"], "run-1")
        (target / "extra").write_text("unexpected")
        with self.assertRaises(VaultError):
            verify_package(target)


class StatisticsTests(unittest.TestCase):
    def test_holm_step_down(self):
        adjusted = holm_adjust([Decimal("0.01"), Decimal("0.04"), Decimal("0.03")])
        self.assertEqual(
            adjusted, [Decimal("0.03"), Decimal("0.06"), Decimal("0.06")]
        )
        self.assertEqual(holm_adjust([Decimal("0.5")]), [Decimal("0.5")])

    def test_ols_residuals_exact_fit(self):
        residuals = ols_residuals(
            [[Decimal(1)], [Decimal(2)], [Decimal(3)], [Decimal(4)]],
            [Decimal(5), Decimal(8), Decimal(11), Decimal(14)],
        )
        self.assertEqual(residuals, [Decimal(0)] * 4)
        with self.assertRaises(VaultError):
            ols_residuals(
                [[Decimal(2)], [Decimal(2)], [Decimal(2)], [Decimal(2)]],
                [Decimal(1), Decimal(2), Decimal(3), Decimal(4)],
            )

    def test_permutation_is_deterministic(self):
        strata = [([1.0, 2.0, 10.0, 20.0], 2, 1.0)]
        observed, exceedances, p_value = permutation_test(strata, 200, 12345)
        self.assertEqual(observed, -13.5)
        self.assertEqual(
            (observed, exceedances, p_value), permutation_test(strata, 200, 12345)
        )
        self.assertEqual(p_value, Decimal(exceedances + 1) / Decimal(201))
        self.assertTrue(0 <= exceedances <= 200)


if __name__ == "__main__":
    unittest.main()
