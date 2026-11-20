# Report format

The report is Markdown. Prose and headings are free; `verify.py` reads only claim lines.

## Claim lines

```
- [<kind>] <claim, with `quoted values`> [E1, E3]
```

- A bullet (`-` or `*`), then the kind in square brackets, then the claim.
- Citations are the last thing on the line: `[E1]` or `[E1, E3]`. A full stop after them is
  fine. Without a trailing citation group the claim cites nothing.
- Lines inside fenced code blocks are ignored.

| Kind | Means | Citations |
|---|---|---|
| `root-cause` | what started it; may be more than one line (change, mechanism) | required |
| `fact` | the cited output shows it directly | required |
| `inference` | follows from the cited outputs with high confidence | required |
| `ruled-out` | a cause you checked and found no sign of | required |
| `hypothesis` | plausible, not shown; say what would confirm it | optional, not checked |
| `undetermined` | what you could not establish, and why | optional |

The report needs at least one `root-cause` or `undetermined` line.

## Quoted values

Everything in backticks on a checked line must be found in at least one output the line cites:

- **Text** must appear verbatim. Runs of whitespace count as one space, and JSON escaping is
  undone, so `"transport: Error"` matches `\"transport: Error\"` in a JSON output.
- **A number** (`0.69`, `9999`, `-3`, `12.5`) matches a number in the output that rounds to it
  at the precision you wrote: `0.69`, `0.7` and `0.690` all match 0.6896795; `0.68` does not.
- **A percentage** (`69%`) also matches the ratio 0.69 at two more digits.
- Numbers are found anywhere in the cited output, not next to a label. `0` matches any zero,
  so a quoted number is weak evidence on its own: quote the label too
  (`` service `payment` has error ratio `0` ``).

Quote values, not whole sentences: `` `i/o timeout` `` and `` `payment:9999` `` rather than a
paraphrase in backticks, which will fail.

## Suggested sections

```
# Investigation: <alert>
**Alert:** ...  **Window:** ...  **Evidence:** .evidence/ (N entries; sources)
## Root cause
## Facts
## Symptoms, not causes
## Ruled out
## Hypotheses
## Gaps            (sources you could not reach, queries that failed)
## Proposed fix    (for a human; not done)
```

A complete example that passes: `../examples/bad-release/report.md`. Reports that fail,
one mistake each: `../examples/bad-release/broken/`.
