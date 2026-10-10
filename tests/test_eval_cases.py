"""Eval case-file coverage test."""

import json
import pathlib


def test_cases_20_coverage():
    cases = json.loads(pathlib.Path("eval/cases_20.json").read_text(encoding="utf-8"))
    assert len(cases) == 20
    pris = {c["priority"] for c in cases}
    assert pris == {"P0", "P1", "P2", "P3"}
