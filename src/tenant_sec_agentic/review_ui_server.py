"""Localhost-only HTTP server for human assessment review."""

from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from tenant_sec_agentic.review_decisions import (
    artifact_path_for_control,
    load_artifact,
    resolve_assessments_root,
)
from tenant_sec_agentic.review_ui_store import (
    FilterKind,
    control_detail,
    list_assessment_files,
    load_provider_artifacts,
    pending_navigation,
    progress_counts,
    save_review_decision,
    summarize_controls,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
CSRF_HEADER = "X-Review-Token"


@dataclass(frozen=True)
class ReviewUiConfig:
    assessments_root: Path
    provider: str
    reviewer: str
    csrf_token: str


class ReviewUiServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        config: ReviewUiConfig,
    ) -> None:
        self.review_config = config
        super().__init__(server_address, ReviewUiHandler)


class ReviewUiHandler(BaseHTTPRequestHandler):
    server: ReviewUiServer  # type: ignore[assignment]

    def log_message(self, format: str, *args: Any) -> None:
        return

    @property
    def config(self) -> ReviewUiConfig:
        return self.server.review_config

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status: int, content: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw.strip():
            return {}
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("request body must be a JSON object")
        return data

    def _artifacts(self) -> list[tuple[Path, dict[str, Any]]]:
        return load_provider_artifacts(
            self.config.assessments_root,
            self.config.provider,
        )

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path in {"/", "/index.html"}:
                page = (STATIC_DIR / "review_ui.html").read_bytes()
                self._send_html(HTTPStatus.OK, page)
                return
            if path == "/api/session":
                artifacts = self._artifacts()
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "provider": self.config.provider,
                        "reviewer": self.config.reviewer,
                        "assessments_root": str(
                            resolve_assessments_root(
                                self.config.assessments_root
                            )
                        ),
                        "csrf_token": self.config.csrf_token,
                        "progress": progress_counts(artifacts),
                    },
                )
                return
            if path == "/api/controls":
                query = parse_qs(parsed.query)
                filter_raw = (query.get("filter") or ["all"])[0]
                filter_kind: FilterKind = (
                    "pending" if filter_raw == "pending" else "all"
                )
                artifacts = self._artifacts()
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "controls": summarize_controls(
                            artifacts,
                            filter_kind=filter_kind,
                        ),
                        "progress": progress_counts(artifacts),
                    },
                )
                return
            match = re.fullmatch(r"/api/controls/([a-z][a-z0-9.-]*)", path)
            if match:
                control_id = match.group(1)
                root = resolve_assessments_root(self.config.assessments_root)
                artifact_path = artifact_path_for_control(
                    root,
                    self.config.provider,
                    control_id,
                )
                if not artifact_path.is_file():
                    self._send_json(
                        HTTPStatus.NOT_FOUND,
                        {"error": "control not found"},
                    )
                    return
                data = load_artifact(artifact_path)
                artifacts = self._artifacts()
                payload = control_detail(data)
                payload["navigation"] = pending_navigation(artifacts, control_id)
                self._send_json(HTTPStatus.OK, payload)
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": str(exc)},
            )

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            match = re.fullmatch(r"/api/controls/([a-z][a-z0-9.-]*)/review", path)
            if not match:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            if self.headers.get(CSRF_HEADER) != self.config.csrf_token:
                self._send_json(
                    HTTPStatus.FORBIDDEN,
                    {"error": "missing or invalid review token"},
                )
                return
            body = self._read_json_body()
            if body.get("csrf_token") != self.config.csrf_token:
                self._send_json(
                    HTTPStatus.FORBIDDEN,
                    {"error": "missing or invalid review token"},
                )
                return
            control_id = match.group(1)
            review = save_review_decision(
                self.config.assessments_root,
                self.config.provider,
                control_id,
                self.config.reviewer,
                decision=str(body.get("decision") or ""),
                notes=body.get("notes"),
                adjusted_score=body.get("adjusted_score"),
                adjusted_services=body.get("adjusted_services"),
                reasoning=body.get("reasoning"),
            )
            artifacts = self._artifacts()
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "review": review,
                    "progress": progress_counts(artifacts),
                    "navigation": pending_navigation(artifacts, control_id),
                },
            )
        except FileNotFoundError as exc:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except json.JSONDecodeError:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid JSON body"},
            )
        except Exception as exc:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": str(exc)},
            )


def run_server(
    *,
    host: str,
    port: int,
    assessments_dir: Path,
    provider: str,
    reviewer: str,
) -> None:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("review UI may only bind to localhost")
    assessments_root = resolve_assessments_root(assessments_dir)
    provider_dir = assessments_root / provider
    if not provider_dir.is_dir():
        raise ValueError(f"missing assessments folder: {provider_dir}")
    if not list_assessment_files(provider_dir):
        raise ValueError(f"no assessment YAML files in {provider_dir}")
    config = ReviewUiConfig(
        assessments_root=assessments_root,
        provider=provider,
        reviewer=reviewer,
        csrf_token=secrets.token_hex(16),
    )
    server = ReviewUiServer((host, port), config)
    print(
        f"TenantSec review UI at http://{host}:{port}/ "
        f"(provider={provider}, reviewer={reviewer})"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
