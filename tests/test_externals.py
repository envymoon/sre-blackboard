"""External-service tests: local stubs hold by default; compose ships all four services. Maps to spec Sec.28.3."""
import pathlib
from app import settings
from app.ext import backend_status


def test_default_local_fallback():
    assert settings.RERANKER_MODE == "base_plus_rules"
    st = backend_status()
    assert st == {"mysql": "local-stub", "redis": "local-stub", "es": "local-stub", "reranker": "base_plus_rules"}


def test_compose_has_externals():
    dc = pathlib.Path("docker-compose.yml").read_text()
    for svc in ["mysql:", "redis:", "elasticsearch:", "harness-worker:"]:
        assert svc in dc
    assert "REDIS_URL" in dc and "ES_URL" in dc and "RERANKER_MODE" in dc
