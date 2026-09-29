"""CLI entry point for the localhost assessment review UI."""

from __future__ import annotations

import argparse
from pathlib import Path

from tenant_sec_agentic.review_ui_server import run_server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Localhost-only web UI for reviewing assessment YAML artifacts"
        )
    )
    parser.add_argument(
        "--assessments-dir",
        type=Path,
        required=True,
        help="Root folder containing provider assessment subdirectories",
    )
    parser.add_argument(
        "--provider",
        required=True,
        help="Provider slug (subfolder under assessments-dir)",
    )
    parser.add_argument(
        "--reviewer",
        required=True,
        help="Reviewer identity recorded in artifact review blocks",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address (localhost only; default 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="TCP port (default 8765)",
    )
    args = parser.parse_args(argv)
    run_server(
        host=args.host,
        port=args.port,
        assessments_dir=args.assessments_dir,
        provider=args.provider,
        reviewer=args.reviewer,
    )


if __name__ == "__main__":
    main()
