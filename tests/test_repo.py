import pytest

from tenant_sec_agentic.repo import resolve_control_scope


def test_resolve_scope_all_keys_exist():
    controls = {"a.b": {}, "c.d": {}}
    assert set(resolve_control_scope("all", controls)) == {"a.b", "c.d"}


def test_resolve_scope_unknown():
    controls = {"a.b": {}}
    with pytest.raises(ValueError):
        resolve_control_scope(["x.y"], controls)
