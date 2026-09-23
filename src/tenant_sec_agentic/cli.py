"""CLI entry: run assessment pipeline with ADK Runner."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from google.genai import types

from tenant_sec_agentic.config import load_assessment_config
from tenant_sec_agentic.pipeline import AssessmentPipeline, initial_session_state

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tenant_sec_agentic.cli")


def main(argv: list[str] | None = None) -> None:
    load_dotenv()
    p = argparse.ArgumentParser(description="tenant-sec agentic assessment pipeline")
    p.add_argument(
        "--config",
        type=Path,
        default=Path("assessment-config.yaml"),
        help="Path to assessment-config.yaml",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Process at most N controls (0 = all)",
    )
    p.add_argument(
        "--controls",
        nargs="+",
        default=None,
        help="Run only these exact control IDs",
    )
    p.add_argument(
        "--session-database",
        type=Path,
        default=None,
        help="Override the configured session database path",
    )
    args = p.parse_args(argv)

    cfg = load_assessment_config(args.config.resolve())
    if args.session_database is not None:
        cfg.session.database_path = args.session_database.resolve()
    state = initial_session_state(cfg)
    if args.controls:
        available = {
            str(item["control"]): item
            for item in state["assessment_queue"]
        }
        missing = sorted(set(args.controls) - set(available))
        if missing:
            p.error("Unknown or out-of-scope controls: " + ", ".join(missing))
        state["assessment_queue"] = [
            available[control_id] for control_id in args.controls
        ]
    if args.limit > 0:
        q = state["assessment_queue"]
        state["assessment_queue"] = q[: args.limit]

    db_path = str(cfg.session.database_path)
    session_service = DatabaseSessionService(db_url=f"sqlite+aiosqlite:///{db_path}")

    async def _run() -> None:
        user_id = "pipeline"
        if cfg.session.resume_existing:
            session_id = f"{cfg.provider.slug}-assessment"
        else:
            session_id = f"{cfg.provider.slug}-{uuid.uuid4().hex[:8]}"

        async with session_service:
            existing = await session_service.get_session(
                app_name="tenant_sec_agentic",
                user_id=user_id,
                session_id=session_id,
            )
            if existing is None:
                existing = await session_service.create_session(
                    app_name="tenant_sec_agentic",
                    user_id=user_id,
                    session_id=session_id,
                    state=state,
                )
            elif not cfg.session.resume_existing:
                await session_service.create_session(
                    app_name="tenant_sec_agentic",
                    user_id=user_id,
                    session_id=session_id,
                    state=state,
                )

            runner = Runner(
                app_name="tenant_sec_agentic",
                agent=AssessmentPipeline(),
                session_service=session_service,
            )
            msg = types.Content(
                role="user",
                parts=[types.Part(text="Run assessment queue for configured provider.")],
            )
            async for _ev in runner.run_async(
                user_id=user_id,
                session_id=session_id,
                new_message=msg,
            ):
                pass
            completed = await session_service.get_session(
                app_name="tenant_sec_agentic",
                user_id=user_id,
                session_id=session_id,
            )
            failed = (
                completed.state.get("pipeline_failed_controls", [])
                if completed is not None
                else ["unknown"]
            )
            if failed:
                raise RuntimeError(
                    "Assessment pipeline failed controls: "
                    + ", ".join(failed)
                )

    asyncio.run(_run())
    print(
        f"Done. Assessments: {cfg.paths.assessments_dir / cfg.provider.slug}\n"
        f"Reports: {cfg.paths.reports_dir / cfg.provider.slug}\n"
        f"Usage: {cfg.paths.assessments_dir / cfg.provider.slug / 'usage-ledger.json'}"
    )


if __name__ == "__main__":
    main(sys.argv[1:])
