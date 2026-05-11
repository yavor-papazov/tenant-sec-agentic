from tenant_sec_agentic.parsers import parse_assessor_output


def test_parse_assessor_mixed():
    text = """
SCORE: mixed
CONFIDENCE: medium
DEEP_RESEARCH_RECOMMENDED: false
EVIDENCE:
Summary here.
SERVICES:
- svc-a: 2
  evidence: Per svc a
- svc-b: 0
  evidence: None
FLAGS:
- thin-docs
SOURCES_USED:
- https://example.com/a
"""
    out = parse_assessor_output(text)
    assert out["score"] == "mixed"
    assert out["confidence"] == "medium"
    assert out["services"]["svc-a"]["score"] == 2
