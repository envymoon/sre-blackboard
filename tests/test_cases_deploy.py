"""Case + deploy tests. Maps to spec Sec.19/Sec.28."""
import json, pathlib
from app.limits import LIMITS, check_overload


def test_cases_20_coverage():
    cases = json.loads(pathlib.Path("eval/cases_20.json").read_text(encoding="utf-8"))
    assert len(cases) == 20
    pris = {c["priority"] for c in cases}
    assert pris == {"P0", "P1", "P2", "P3"}


def test_limits_and_deploy_files():
    assert LIMITS["api_workers"] == 2 and LIMITS["max_active_incidents"] == 5
    assert check_overload(6, 0, 0)["actions"] == ["pause_P2_P3_dispatch"]
    assert check_overload(1, 0, 0)["actions"] == ["ok"]
    dc = pathlib.Path("docker-compose.yml").read_text()
    assert "--workers 2" in dc and "1.25" in dc and "python -m app.worker" in dc
