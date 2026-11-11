import tempfile
from pathlib import Path
from .service import Vault
from .validation import canonical
from .releases.exporter import verify_directory
from .analysis.package import verify_package


def demonstrate():
    with tempfile.TemporaryDirectory() as directory:
        with Vault(Path(directory) / "vault") as vault:
            schema = {
                "id": "measurements",
                "version": 1,
                "fields": [
                    {"name": "id", "type": "text"},
                    {"name": "pressure", "type": "decimal", "minimum": "0"},
                ],
                "primary_key": ["id"],
            }
            vault.registry.add_schema(schema)
            vault.registry.register(
                "trial", "Synthetic trial measurements", "measurements", "operator"
            )
            content = (
                canonical({"id": "sample-A", "pressure": "12.5"})
                + "\n"
                + canonical({"id": "sample-B", "pressure": "13"})
                + "\n"
            ).encode()
            ingested = vault.ingestion.ingest(
                "request", "trial", 1, content, "jsonl", "internal", ["research"]
            )
            vault.releases.plan(
                "release", [{"dataset": "trial", "version": 1}], "research", "internal"
            )
            vault.releases.approve(
                "release", 1, "independent-reviewer", "Synthetic dataset verified"
            )
            exported = vault.exporter.export(
                "export", "release", Path(directory) / "release"
            )
            verified = verify_directory(Path(directory) / "release")
            assert ingested["rows"] == 2
            assert verified["rows"] == 2
            assert exported["state"] == "complete"

            analysis = _demonstrate_analysis(vault, Path(directory))
            return {
                "ingestion": ingested,
                "export": exported,
                "verification": verified,
                "analysis": analysis,
                "integrity": vault.checks.inspect(),
            }


def _demonstrate_analysis(vault, directory):
    assignment_schema = {
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
    outcome_schema = {
        "id": "outcomes",
        "version": 1,
        "fields": [
            {"name": "id", "type": "text"},
            {"name": "panel", "type": "text"},
            {"name": "measured_at", "type": "timestamp"},
            {"name": "adhesion", "type": "decimal", "nullable": True, "required": False},
            {"name": "rusted", "type": "boolean", "nullable": True, "required": False},
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
    vault.registry.add_schema(assignment_schema)
    vault.registry.add_schema(outcome_schema)
    vault.registry.register(
        "randomization", "Synthetic coating randomization", "assignments", "operator"
    )
    vault.registry.register(
        "adhesion-results", "Synthetic coating outcomes", "outcomes", "operator"
    )
    assignment_rows = [
        {"panel": "n1", "arm": "coated", "site": "north", "roughness": "2.5"},
        {"panel": "n2", "arm": "bare", "site": "north", "roughness": "2"},
        {"panel": "n3", "arm": "coated", "site": "north", "roughness": "3"},
        {"panel": "n4", "arm": "bare", "site": "north", "roughness": "2.5"},
        {"panel": "n5", "arm": "coated", "site": "north", "roughness": "2.8"},
        {"panel": "n6", "arm": "bare", "site": "north", "roughness": "2.2"},
        {"panel": "s1", "arm": "coated", "site": "south", "roughness": "3.5"},
        {"panel": "s2", "arm": "bare", "site": "south", "roughness": "3"},
        {"panel": "s3", "arm": "coated", "site": "south", "roughness": "3.2"},
        {"panel": "s4", "arm": "bare", "site": "south", "roughness": "2.9"},
        {"panel": "s5", "arm": "coated", "site": "south", "roughness": "3.6"},
        {"panel": "s6", "arm": "bare", "site": "south", "roughness": "3.1"},
    ]
    vault.ingestion.ingest(
        "rand-1",
        "randomization",
        1,
        _jsonl(assignment_rows),
        "jsonl",
        "internal",
        ["research"],
    )
    outcome_rows = [
        {"id": "m01", "panel": "n1", "measured_at": "2026-10-01T09:00:00Z", "adhesion": "12", "rusted": False, "applied": "coated"},
        {"id": "m02", "panel": "n2", "measured_at": "2026-10-01T10:00:00Z", "adhesion": "8", "rusted": True, "applied": "bare"},
        {"id": "m03", "panel": "n3", "measured_at": "2026-10-02T09:00:00Z", "adhesion": "14", "rusted": False, "applied": "coated"},
        {"id": "m04", "panel": "n4", "measured_at": "2026-10-02T10:00:00Z", "adhesion": "10", "rusted": True, "applied": "bare"},
        {"id": "m05", "panel": "n5", "measured_at": "2026-10-03T09:00:00Z", "adhesion": "11", "rusted": False, "applied": "coated"},
        {"id": "m05b", "panel": "n5", "measured_at": "2027-01-05T09:00:00Z", "adhesion": "13", "rusted": False, "applied": "coated"},
        {"id": "m06", "panel": "s1", "measured_at": "2026-10-03T10:00:00Z", "adhesion": "20", "rusted": False, "applied": "coated"},
        {"id": "m07", "panel": "s2", "measured_at": "2026-10-04T09:00:00Z", "adhesion": "14", "rusted": True, "applied": "bare"},
        {"id": "m08", "panel": "s3", "measured_at": "2026-10-04T10:00:00Z", "adhesion": "22", "rusted": False, "applied": "bare"},
        {"id": "m09", "panel": "s4", "measured_at": "2026-10-05T09:00:00Z", "adhesion": "16", "rusted": True, "applied": "bare"},
        {"id": "m10", "panel": "s5", "measured_at": "2026-10-05T10:00:00Z", "adhesion": "21", "rusted": False, "applied": "coated"},
        {"id": "m10b", "panel": "s5", "measured_at": "2026-10-06T09:00:00Z", "adhesion": "23", "rusted": False, "applied": "coated"},
        {"id": "m11", "panel": "s6", "measured_at": "2026-10-06T10:00:00Z", "adhesion": "15", "rusted": True, "applied": "bare"},
    ]
    vault.ingestion.ingest(
        "out-1",
        "adhesion-results",
        1,
        _jsonl(outcome_rows),
        "jsonl",
        "internal",
        ["research"],
    )
    plan = vault.analysis_plans.define(
        plan_id="coating-plan",
        title="Synthetic coating adhesion analysis",
        assignment={"dataset": "randomization", "version": 1},
        unit="panel",
        arm={"field": "arm", "control": "bare", "treatment": "coated"},
        strata=["site"],
        primary={"id": "adhesion", "kind": "continuous", "field": "adhesion"},
        secondary=[{"id": "rust", "kind": "binary", "field": "rusted"}],
        cutoff={"field": "measured_at", "at": "2026-12-31T00:00:00Z"},
        actual_arm_field="applied",
        permutations=500,
        seed="demo-seed",
    )
    run = vault.analysis_runs.run(
        "analysis-1", "coating-plan", 1, {"dataset": "adhesion-results", "version": 1}
    )
    package = vault.analysis_packages.export(
        "package-1", "analysis-1", directory / "analysis-package"
    )
    verified = verify_package(directory / "analysis-package")
    assert run["primary"]["estimate"] == "5"
    assert run["disposition"]["unmeasured"] == ["n6"]
    assert len(run["disposition"]["crossovers"]) == 1
    assert len(run["disposition"]["repeated"]) == 1
    assert package["state"] == "complete"
    return {
        "plan_version": plan["version"],
        "primary_estimate": run["primary"]["estimate"],
        "primary_p_value": run["primary"]["test"]["p_value"],
        "secondary": [
            {"id": item["id"], "holm": item["holm"]} for item in run["secondary"]
        ],
        "package": {"state": package["state"], "digest": package["digest"]},
        "package_verified": verified,
    }


def _jsonl(rows):
    return ("\n".join(canonical(row) for row in rows) + "\n").encode()
