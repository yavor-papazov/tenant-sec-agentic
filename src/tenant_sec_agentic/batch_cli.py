"""Resumable, budget-capped multi-provider assessment runner."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from tenant_sec_agentic.artifacts import validate_internal_assessment
from tenant_sec_agentic.config import AssessmentConfig, load_assessment_config
from tenant_sec_agentic.pipeline import prompt_hashes
from tenant_sec_agentic.repo import load_all_controls, resolve_control_scope

ROOT = Path(__file__).resolve().parents[2]


class BudgetTracker:
    """Persist reservations before calls so crashes fail closed."""

    def __init__(self, path: Path, ceiling_usd: float) -> None:
        self.path = path
        self.ceiling_usd = ceiling_usd
        self.lock = asyncio.Lock()
        self.condition = asyncio.Condition(self.lock)
        self.active_run_ids: set[str] = set()
        if path.is_file():
            self.data = json.loads(path.read_text(encoding="utf-8"))
            recorded = float(self.data.get("ceiling_usd", ceiling_usd))
            if recorded != ceiling_usd:
                raise ValueError(
                    f"Existing ledger ceiling is ${recorded:.2f}, not "
                    f"${ceiling_usd:.2f}"
                )
        else:
            self.data = {
                "version": 1,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "ceiling_usd": ceiling_usd,
                "runs": [],
            }
            self._write()

    @property
    def charged_usd(self) -> float:
        return sum(
            float(run.get("charged_cost_usd", 0.0))
            for run in self.data["runs"]
        )

    @property
    def committed_usd(self) -> float:
        """Charges that cannot be released by an active run."""
        return sum(
            float(run.get("charged_cost_usd", 0.0))
            for run in self.data["runs"]
            if str(run.get("id")) not in self.active_run_ids
        )

    async def reserve(
        self,
        *,
        provider: str,
        control: str,
        attempt: int,
        amount_usd: float,
    ) -> str | None:
        async with self.condition:
            while self.charged_usd + amount_usd > self.ceiling_usd:
                if self.committed_usd + amount_usd > self.ceiling_usd:
                    return None
                await self.condition.wait()
            run_id = uuid.uuid4().hex
            self.active_run_ids.add(run_id)
            self.data["runs"].append(
                {
                    "id": run_id,
                    "provider": provider,
                    "control": control,
                    "attempt": attempt,
                    "status": "running",
                    "started_at": datetime.now(timezone.utc).isoformat(),
                    "reserved_cost_usd": amount_usd,
                    "charged_cost_usd": amount_usd,
                }
            )
            self._write()
            return run_id

    async def finalize(
        self,
        run_id: str,
        *,
        status: str,
        actual_model_cost_usd: float | None,
        tavily_max_cost_usd: float | None,
        exit_code: int,
        artifact_current: bool,
        log_path: Path,
    ) -> None:
        async with self.condition:
            run = next(
                value for value in self.data["runs"] if value["id"] == run_id
            )
            if (
                actual_model_cost_usd is not None
                and tavily_max_cost_usd is not None
            ):
                run["charged_cost_usd"] = (
                    actual_model_cost_usd + tavily_max_cost_usd
                )
            run.update(
                {
                    "status": status,
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "actual_model_cost_usd": actual_model_cost_usd,
                    "tavily_max_cost_usd": tavily_max_cost_usd,
                    "exit_code": exit_code,
                    "artifact_current": artifact_current,
                    "log_path": str(log_path),
                }
            )
            self.data["charged_total_usd"] = self.charged_usd
            self._write()
            self.active_run_ids.discard(run_id)
            self.condition.notify_all()

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.data, indent=2, sort_keys=True),
            encoding="utf-8",
        )


def _artifact_path(cfg: AssessmentConfig, control_id: str) -> Path:
    return (
        cfg.paths.assessments_dir
        / cfg.provider.slug
        / f"{control_id}.yaml"
    )


def artifact_is_current(
    cfg: AssessmentConfig,
    control_id: str,
) -> bool:
    path = _artifact_path(cfg, control_id)
    if not path.is_file():
        return False
    try:
        artifact = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return False
    if not isinstance(artifact, dict):
        return False
    if artifact.get("assessment_id") != cfg.provider.assessment_id:
        return False
    if str(artifact.get("methodology_version")) != "2.0":
        return False
    if artifact.get("status") != "needs_human_review":
        return False
    hashes = artifact.get("prompt_hashes") or {}
    if any(hashes.get(key) != value for key, value in prompt_hashes().items()):
        return False
    return not validate_internal_assessment(artifact)


def _fresh_usage(
    cfg: AssessmentConfig,
    control_id: str,
    launched_at: datetime,
) -> dict | None:
    path = (
        cfg.paths.assessments_dir
        / cfg.provider.slug
        / "usage-ledgers"
        / f"{control_id}.json"
    )
    if not path.is_file():
        return None
    try:
        ledger = json.loads(path.read_text(encoding="utf-8"))
        started = datetime.fromisoformat(str(ledger["started_at"]))
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return None
    return ledger if started >= launched_at else None


async def _run_control(
    config_path: Path,
    cfg: AssessmentConfig,
    control_id: str,
    attempt: int,
    tracker: BudgetTracker,
    timeout_seconds: float,
) -> bool | None:
    reservation = (
        cfg.limits.max_projected_cost_usd
        + cfg.limits.max_search_calls_per_run * 0.008
    )
    run_id = await tracker.reserve(
        provider=cfg.provider.slug,
        control=control_id,
        attempt=attempt,
        amount_usd=reservation,
    )
    if run_id is None:
        return None

    log_dir = (
        cfg.paths.reports_dir / cfg.provider.slug / "batch-logs"
    )
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{control_id}-attempt-{attempt}.log"
    session_path = (
        log_dir
        / "sessions"
        / f"{control_id}-attempt-{attempt}.db"
    )
    session_path.parent.mkdir(parents=True, exist_ok=True)
    launched_at = datetime.now(timezone.utc)
    print(
        f"[{cfg.provider.slug}] {control_id} attempt {attempt} "
        f"(charged ${tracker.charged_usd:.2f}/${tracker.ceiling_usd:.2f})",
        flush=True,
    )
    exit_code = 125
    try:
        with log_path.open("wb") as output:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "tenant_sec_agentic.cli",
                "--config",
                str(config_path),
                "--controls",
                control_id,
                "--session-database",
                str(session_path),
                cwd=ROOT,
                stdout=output,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                exit_code = await asyncio.wait_for(
                    process.wait(),
                    timeout=timeout_seconds,
                )
            except asyncio.TimeoutError:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=10)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
                exit_code = 124
    except Exception as exc:
        with log_path.open("a", encoding="utf-8") as output:
            output.write(f"\nBatch launcher error: {type(exc).__name__}: {exc}\n")

    current = artifact_is_current(cfg, control_id)
    usage = _fresh_usage(cfg, control_id, launched_at)
    totals = usage.get("totals", {}) if usage else {}
    model_cost = (
        float(totals.get("actual_cost_usd", 0.0)) if usage else None
    )
    tavily_cost = (
        float(totals.get("tavily_projected_cost_usd", 0.0))
        if usage
        else None
    )
    await tracker.finalize(
        run_id,
        status="completed" if current else "failed",
        actual_model_cost_usd=model_cost,
        tavily_max_cost_usd=tavily_cost,
        exit_code=exit_code,
        artifact_current=current,
        log_path=log_path,
    )
    print(
        f"[{cfg.provider.slug}] {control_id}: "
        f"{'ready for review' if current else 'failed'}; "
        f"cumulative charge ${tracker.charged_usd:.2f}",
        flush=True,
    )
    return current


async def _run_provider(
    config_path: Path,
    tracker: BudgetTracker,
    max_attempts: int,
    provider_semaphore: asyncio.Semaphore,
    control_parallelism: int,
    timeout_seconds: float,
    selected_controls: list[str] | None,
) -> dict[str, list[str]]:
    cfg = load_assessment_config(config_path)
    controls = load_all_controls(cfg.paths.repo_root)
    scope = resolve_control_scope(cfg.controls_scope, controls)
    if selected_controls:
        missing = sorted(set(selected_controls) - set(scope))
        if missing:
            raise ValueError(
                f"{cfg.provider.slug}: controls outside configured scope: "
                + ", ".join(missing)
            )
        scope = [control_id for control_id in scope if control_id in selected_controls]
    completed: list[str] = []
    failed: list[str] = []
    control_semaphore = asyncio.Semaphore(control_parallelism)

    async def assess(control_id: str) -> tuple[str, bool]:
        if artifact_is_current(cfg, control_id):
            return control_id, True
        async with control_semaphore:
            ready: bool | None = False
            for attempt in range(1, max_attempts + 1):
                ready = await _run_control(
                    config_path,
                    cfg,
                    control_id,
                    attempt,
                    tracker,
                    timeout_seconds,
                )
                if ready is True or ready is None:
                    break
            return control_id, ready is True

    async with provider_semaphore:
        outcomes = await asyncio.gather(
            *(assess(control_id) for control_id in scope)
        )
    for control_id, ready in outcomes:
        (completed if ready else failed).append(control_id)
    return {"completed": completed, "failed": failed}


async def _run(args: argparse.Namespace) -> int:
    configs = [path.resolve() for path in args.config]
    tracker = BudgetTracker(args.ledger.resolve(), args.budget_usd)
    semaphore = asyncio.Semaphore(args.provider_parallelism)
    results = await asyncio.gather(
        *[
            _run_provider(
                path,
                tracker,
                args.max_attempts,
                semaphore,
                args.control_parallelism,
                args.control_timeout_minutes * 60,
                args.controls,
            )
            for path in configs
        ]
    )
    failed = [
        control
        for result in results
        for control in result["failed"]
    ]
    summary = {
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "charged_total_usd": tracker.charged_usd,
        "ready_for_review": sum(
            len(result["completed"]) for result in results
        ),
        "failed": failed,
    }
    tracker.data["summary"] = summary
    tracker._write()
    print(json.dumps(summary, indent=2), flush=True)
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Run resumable full-scope provider assessments"
    )
    parser.add_argument(
        "--config",
        action="append",
        type=Path,
        required=True,
        help="Provider config; repeat for parallel providers",
    )
    parser.add_argument("--budget-usd", type=float, required=True)
    parser.add_argument(
        "--ledger",
        type=Path,
        default=Path("assessments/release-usage-ledger.json"),
    )
    parser.add_argument("--max-attempts", type=int, default=2)
    parser.add_argument("--provider-parallelism", type=int, default=4)
    parser.add_argument("--control-parallelism", type=int, default=2)
    parser.add_argument("--control-timeout-minutes", type=float, default=15)
    parser.add_argument(
        "--controls",
        nargs="+",
        default=None,
        help="Run only these exact in-scope control IDs",
    )
    args = parser.parse_args(argv)
    if args.budget_usd <= 0:
        parser.error("--budget-usd must be positive")
    if (
        args.max_attempts <= 0
        or args.provider_parallelism <= 0
        or args.control_parallelism <= 0
        or args.control_timeout_minutes <= 0
    ):
        parser.error("attempts, parallelism, and timeout must be positive")
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
