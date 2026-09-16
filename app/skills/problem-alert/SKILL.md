# problem-alert SKILL (spec Sec.13.1): incident ontology and root-cause candidates. Read-only; Remediation MCP forbidden.
# Script chain: build_window -> collect_problem_evidence -> rank_hypotheses
# P0 window: look back 30m, look ahead 15m; P0/P1 root_cause_candidate needs >= 2 independent evidence classes, else HYPOTHESIS.
