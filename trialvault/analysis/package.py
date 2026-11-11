import hashlib
import os
from pathlib import Path
from ..validation import name, canonical, digest, parse_json, reject


class AnalysisPackages:
    """Exports a frozen analysis run as a self-contained package.

    The package carries the locked plan, the full run result (including
    sample disposition), the input dataset versions and the randomization
    seed, so the analysis can be audited offline without the vault.
    """

    def __init__(self, database, runs, plans, artifacts):
        self.db = database
        self.runs = runs
        self.plans = plans
        self.artifacts = artifacts
        with database.transaction() as con:
            con.execute(
                "CREATE TABLE IF NOT EXISTS analysis_exports("
                "id TEXT PRIMARY KEY,run TEXT NOT NULL,target TEXT NOT NULL,"
                "state TEXT NOT NULL,payload TEXT NOT NULL)"
            )

    def export(self, export_id, run_id, target):
        name(export_id)
        target = Path(target).absolute()
        if (
            not target.parent.is_dir()
            or target.is_symlink()
            or target.parent.is_symlink()
        ):
            reject("path", "package target requires safe existing parent")
        stage = target.parent / (".vault-analysis-" + export_id)
        result = self.runs.get(run_id)
        plan = self.plans.get(result["plan"]["id"], result["plan"]["version"])
        if digest(plan) != result["plan"]["digest"]:
            reject("integrity", "stored plan differs from analysis run")
        analysis = (canonical({"plan": plan, "result": result}) + "\n").encode()
        inputs = [
            {"role": "assignment", **result["inputs"]["assignment"]},
            {"role": "outcomes", **result["inputs"]["outcomes"]},
        ]
        package = {
            "schema": 1,
            "id": export_id,
            "run": run_id,
            "plan": {
                "id": plan["id"],
                "version": plan["version"],
                "digest": result["plan"]["digest"],
            },
            "seed": result["seed"],
            "permutations": result["permutations"],
            "disposition_digest": digest(result["disposition"]),
            "inputs": inputs,
            "analysis": hashlib.sha256(analysis).hexdigest(),
        }
        envelope = {"digest": digest(package), "package": package}
        with self.db.transaction() as con:
            old = self.db.one(
                "SELECT * FROM analysis_exports WHERE id=?", (export_id,)
            )
            if old and (old["run"] != run_id or old["target"] != str(target)):
                reject("conflict", "analysis package identity differs")
            if old and old["state"] == "complete":
                verified = verify_package(target)
                if verified["digest"] != envelope["digest"]:
                    reject("integrity", "completed package differs from analysis run")
                return {"id": export_id, "state": "complete", "reused": True, **verified}
            if target.exists():
                if old:
                    verified = verify_package(target)
                    if verified["digest"] == envelope["digest"]:
                        con.execute(
                            "UPDATE analysis_exports SET state='complete' WHERE id=?",
                            (export_id,),
                        )
                        return {
                            "id": export_id,
                            "state": "complete",
                            "reused": True,
                            **verified,
                        }
                reject("conflict", "package target already exists")
            if stage.is_symlink():
                reject("path", "package staging path cannot be a link")
            if not old:
                if stage.exists():
                    reject("conflict", "unowned staging path exists")
                stage.mkdir()
                con.execute(
                    "INSERT INTO analysis_exports VALUES(?,?,?,'writing',?)",
                    (
                        export_id,
                        run_id,
                        str(target),
                        canonical({"stage": str(stage), "digest": envelope["digest"]}),
                    ),
                )
                self.db.audit(
                    "analysis_package_started",
                    export_id,
                    {"run": run_id, "digest": envelope["digest"]},
                )
            elif not stage.is_dir():
                reject("recovery", "package staging directory missing")
        _write(stage / "analysis.json", analysis)
        for checksum in sorted({item["artifact"] for item in inputs}):
            _write(stage / (checksum + ".jsonl"), self.artifacts.read(checksum))
        _write(stage / "manifest.json", (canonical(envelope) + "\n").encode())
        verified = verify_package(stage)
        with self.db.transaction() as con:
            if target.exists():
                reject("conflict", "package destination appeared concurrently")
            os.rename(stage, target)
            parent_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
            con.execute(
                "UPDATE analysis_exports SET state='complete' WHERE id=?", (export_id,)
            )
            self.db.audit(
                "analysis_package_completed",
                export_id,
                {"run": run_id, "digest": envelope["digest"]},
            )
        return {"id": export_id, "state": "complete", "reused": False, **verified}

    def pending(self):
        return self.db.rows(
            "SELECT id,run,target,state FROM analysis_exports WHERE state!='complete' ORDER BY id"
        )


def _write(path, content):
    if path.is_symlink():
        reject("path", "staging member cannot be a link")
    if path.exists():
        if path.read_bytes() != content:
            reject("integrity", "staged member differs from frozen content")
        return
    with path.open("xb") as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())


def verify_package(path):
    root = Path(path)
    if root.is_symlink() or not root.is_dir():
        reject("path", "analysis package must be a regular directory")
    manifest_path = root / "manifest.json"
    if (
        manifest_path.is_symlink()
        or not manifest_path.is_file()
        or manifest_path.stat().st_size > 16000000
    ):
        reject("integrity", "package manifest absent or invalid")
    envelope = parse_json(manifest_path.read_bytes())
    if not isinstance(envelope, dict) or set(envelope) != {"digest", "package"}:
        reject("input", "invalid package envelope")
    package = envelope["package"]
    if digest(package) != envelope["digest"] or package.get("schema") != 1:
        reject("integrity", "package manifest fingerprint or schema invalid")
    artifacts = sorted({item["artifact"] for item in package["inputs"]})
    required = {"manifest.json", "analysis.json"} | {
        checksum + ".jsonl" for checksum in artifacts
    }
    actual = {item.name for item in root.iterdir()}
    if actual != required:
        reject(
            "integrity",
            "package member set differs",
            missing=sorted(required - actual),
            extra=sorted(actual - required),
        )
    analysis_content = (root / "analysis.json").read_bytes()
    if hashlib.sha256(analysis_content).hexdigest() != package["analysis"]:
        reject("integrity", "analysis document hash differs")
    document = parse_json(analysis_content)
    if not isinstance(document, dict) or set(document) != {"plan", "result"}:
        reject("integrity", "analysis document structure invalid")
    plan, result = document["plan"], document["result"]
    if result["id"] != package["run"]:
        reject("integrity", "packaged run identity differs")
    if (
        plan["id"] != package["plan"]["id"]
        or plan["version"] != package["plan"]["version"]
        or digest(plan) != package["plan"]["digest"]
        or result["plan"]["digest"] != package["plan"]["digest"]
    ):
        reject("integrity", "packaged plan digest differs")
    if digest(result["disposition"]) != package["disposition_digest"]:
        reject("integrity", "packaged sample disposition differs")
    if (
        result["seed"] != package["seed"]
        or result["permutations"] != package["permutations"]
    ):
        reject("integrity", "packaged randomization parameters differ")
    declared = {item["role"]: item for item in package["inputs"]}
    for role in ("assignment", "outcomes"):
        recorded = result["inputs"][role]
        expected = declared[role]
        for key in ("dataset", "version", "digest", "artifact"):
            if recorded[key] != expected[key]:
                reject("integrity", "packaged input version differs", role=role)
    for checksum in artifacts:
        member = root / (checksum + ".jsonl")
        if (
            member.is_symlink()
            or not member.is_file()
            or member.stat().st_size > 64000000
        ):
            reject("path", "invalid package artifact")
        if hashlib.sha256(member.read_bytes()).hexdigest() != checksum:
            reject("integrity", "package artifact hash differs")
    return {
        "package": package["id"],
        "run": package["run"],
        "digest": envelope["digest"],
        "artifacts": len(artifacts),
        "analysis": package["analysis"],
    }
