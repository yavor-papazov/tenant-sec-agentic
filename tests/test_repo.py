import pytest
import yaml

from tenant_sec_agentic.repo import (
    load_provider_profiles_scores,
    resolve_control_scope,
)


def test_resolve_scope_all_keys_exist():
    controls = {"a.b": {}, "c.d": {}}
    assert set(resolve_control_scope("all", controls)) == {"a.b", "c.d"}


def test_resolve_scope_all_excludes_vignette_controls():
    controls = {
        "a.b": {"surface": "tenant"},
        "c.d": {"surface": "vignette"},
    }
    assert resolve_control_scope("all", controls) == ["a.b"]


def test_resolve_scope_unknown():
    controls = {"a.b": {}}
    with pytest.raises(ValueError):
        resolve_control_scope(["x.y"], controls)


def test_peer_profiles_ignore_empty_provider_stubs(tmp_path):
    (tmp_path / "empty.yaml").write_text(
        yaml.safe_dump({"provider": "empty", "controls": {}}),
        encoding="utf-8",
    )
    (tmp_path / "peer.yaml").write_text(
        yaml.safe_dump(
            {
                "provider": "peer",
                "controls": {"a.b": {"score": 2}},
            }
        ),
        encoding="utf-8",
    )

    assert load_provider_profiles_scores(tmp_path, "current") == {
        "peer": {
            "controls": {
                "a.b": {"score": 2, "services": None},
            }
        }
    }
