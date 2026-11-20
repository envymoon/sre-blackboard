# Evidence-cited investigation

An agent skill for investigating production incidents with read-only queries, where every
claim in the final report has to survive a mechanical check.

Most incident-investigation prompts ask the model to "cite evidence". This one makes the
citations checkable:

- **`scripts/ev.py`** runs a query only if a read-only allowlist names it (kubectl
  get/describe/logs/events, curl GET, logcli, promtool query, helm history, git log), with no
  shell, and records the full output under an id: E1, E2, ... in a hash-chained ledger.
- **`scripts/verify.py`** checks the report without a model: every claim cites ids that
  exist, every `quoted` value (a message, an address, a revision, a number to the precision
  given) is in an output the claim cites, the ledger was not edited after recording, and every
  recorded command was read-only.

The skill's instructions (`SKILL.md`) tell the agent how to investigate (all services first,
changes against onset, deepest failing span, rule things out) and to fix a failing check by
correcting the claim, never the evidence.

## See it work, offline

The example is real data: the tool results recorded in a run of the
[sre-blackboard](../../README.md) fault lab while a bad checkout release was live in the
OpenTelemetry Demo on Kubernetes.

```
cd examples/bad-release
python3 ../../scripts/ev.py list                      # 5 entries: metrics, rollout, events, traces, logs
python3 ../../scripts/verify.py report.md             # passed: 9 claims, 23 quotes checked
for f in broken/*.md; do python3 ../../scripts/verify.py "$f"; done   # each fails, naming the line
```

The reports in `broken/` make one mistake each, the kind a model makes:

| File | Mistake | What the verifier says |
|---|---|---|
| `invented-id.md` | cites E7, which does not exist | `E7 is not in the ledger` |
| `wrong-number.md` | error ratio `0.96` instead of 0.69 | `` `0.96` is not in E1 `` |
| `misquote.md` | `connection refused`; the traces say `i/o timeout` | `` `connection refused` is not in E4 `` |
| `wrong-source.md` | true value, cited to the wrong output | `` `payment:9999` is not in E1 `` |
| `uncited.md` | a fact with no citation | `cites no evidence` |

Editing any file in `.evidence/` makes every report fail with the entry that changed.

The example's evidence was imported from the lab recording rather than run through `ev.py`,
so it is marked `added` and the verifier warns that the root cause rests on added evidence.
That warning is correct.

## Install

Copy the directory into your agent's skills folder, for Claude Code:

```
cp -r skills/evidence-cited-investigation ~/.claude/skills/
```

It needs Python 3.9+ and whichever of `kubectl`, `curl`, `logcli`, `promtool`, `helm` and `git`
your sources need, with read access already configured. Then ask the agent to investigate an
alert.

## Limits

- **It checks citations, not reasoning.** A wrong root cause can quote true values.
- **Numbers are matched anywhere in the cited output**, not next to their label: `0` is in
  most outputs. Quoting the label with the number helps a reader, not the check.
- **`added` evidence** (MCP tool results, pastes, exports) is protected against later edits,
  not checked against its claimed source.
- **The allowlist only covers `ev.py`.** It cannot stop an agent from running other tools, and
  an agent with file access could rebuild the whole ledger. `ev.py` prints the ledger head hash
  after every write, so a transcript pins it; compare it with the head `verify.py` prints.
- **Not yet run with a live model on a live cluster as a skill.** The checks are tested on
  the recorded data (`tests/test_skill_evidence.py` in this repository). The investigation
  method comes from the sre-blackboard agents, which were measured in the lab: see
  [results](../../docs/results.md).
