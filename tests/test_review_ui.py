"""Tests for localhost assessment review UI helpers and HTTP surface."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

from tenant_sec_agentic.review_decisions import (
    artifact_path_for_control,
    atomic_write_artifact,
    resolve_assessments_root,
)
from tenant_sec_agentic.review_ui_server import ReviewUiConfig, ReviewUiHandler
from tenant_sec_agentic.review_ui_store import is_pending, save_review_decision


def _write_artifact(path: Path, control: str, review: dict | None = None) -> None:
    payload = {
        "control": control,
        "provider": "example",
        "result_status": "assessed",
        "recommended_score": 2,
        "overall_confidence": "high",
        "status": "needs_human_review",
        "assessor": {
            "evidence": "Documented.",
            "claims": [],
            "criteria_results": [],
            "evidence_items": [],
            "sources_used": ["https://example.com/docs"],
        },
        "skeptic": {"reasoning": "Looks fine.", "skepticism_flags": []},
        "consistency": {"overall_consistency": "consistent", "consistency_flags": []},
        "review": review
        or {
            "status": "pending",
            "reviewer": None,
            "reviewed_at": None,
            "adjusted_score": None,
            "adjusted_services": None,
            "adjustment_reasoning": None,
            "notes": None,
        },
    }
    atomic_write_artifact(path, payload)


def test_artifact_path_rejects_traversal(tmp_path: Path) -> None:
    root = resolve_assessments_root(tmp_path / "assessments")
    (root / "aws").mkdir(parents=True)
    with pytest.raises(ValueError, match="invalid control"):
        artifact_path_for_control(root, "aws", "../secrets")
    with pytest.raises(ValueError, match="invalid provider"):
        artifact_path_for_control(root, "../etc", "enc.cmk")


def test_save_review_preserves_unrelated_fields(tmp_path: Path) -> None:
    root = resolve_assessments_root(tmp_path / "assessments")
    provider_dir = root / "aws"
    provider_dir.mkdir(parents=True)
    path = provider_dir / "enc.cmk.yaml"
    _write_artifact(path, "enc.cmk")
    save_review_decision(
        root,
        "aws",
        "enc.cmk",
        "human:reviewer",
        decision="approved",
        notes="Checked sources.",
    )
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["assessor"]["evidence"] == "Documented."
    assert data["review"]["status"] == "approved"
    assert data["review"]["reviewer"] == "human:reviewer"
    assert data["review"]["notes"] == "Checked sources."
    assert data["review"]["adjusted_score"] is None


def test_adjusted_mixed_requires_services(tmp_path: Path) -> None:
    root = resolve_assessments_root(tmp_path / "assessments")
    provider_dir = root / "aws"
    provider_dir.mkdir(parents=True)
    path = provider_dir / "enc.cmk.yaml"
    _write_artifact(path, "enc.cmk")
    with pytest.raises(ValueError, match="adjusted_services"):
        save_review_decision(
            root,
            "aws",
            "enc.cmk",
            "human:reviewer",
            decision="adjusted",
            adjusted_score="mixed",
            reasoning="Per-service variance.",
        )


class _OneShotServer(ThreadingHTTPServer):
    def __init__(self, config: ReviewUiConfig) -> None:
        self.review_config = config
        super().__init__(("127.0.0.1", 0), ReviewUiHandler)


def test_http_get_controls_and_post_review(tmp_path: Path) -> None:
    root = resolve_assessments_root(tmp_path / "assessments")
    provider_dir = root / "aws"
    provider_dir.mkdir(parents=True)
    _write_artifact(provider_dir / "enc.cmk.yaml", "enc.cmk")
    _write_artifact(
        provider_dir / "log.audit.yaml",
        "log.audit",
        review={"status": "approved", "reviewer": "human:reviewer"},
    )
    config = ReviewUiConfig(
        assessments_root=root,
        provider="aws",
        reviewer="human:reviewer",
        csrf_token="test-token",
    )
    server = _OneShotServer(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    base = f"http://127.0.0.1:{port}"

    try:
        with urllib.request.urlopen(f"{base}/api/controls?filter=pending") as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        assert payload["progress"] == {"total": 2, "pending": 1, "reviewed": 1}
        assert [row["control"] for row in payload["controls"]] == ["enc.cmk"]

        outside = json.dumps(
            {
                "decision": "approved",
                "csrf_token": "test-token",
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{base}/api/controls/enc.cmk/review",
            data=outside,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req)
        assert exc.value.code == 403

        body = json.dumps(
            {"decision": "approved", "csrf_token": "test-token", "notes": "ok"}
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{base}/api/controls/enc.cmk/review",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Review-Token": "test-token",
            },
        )
        with urllib.request.urlopen(req) as resp:
            saved = json.loads(resp.read().decode("utf-8"))
        assert saved["ok"] is True
        assert saved["review"]["status"] == "approved"
        assert not is_pending(yaml.safe_load((provider_dir / "enc.cmk.yaml").read_text()))
    finally:
        server.shutdown()
        thread.join(timeout=2)
