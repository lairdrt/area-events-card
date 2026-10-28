# Area Events — Codex Operating Instructions

## Architecture
Preserve the approved modular architecture:

Independent producers -> standard candidate contract ->
normalization and validation -> separate deduplication ->
one authoritative SQLite database -> stable API ->
Home Assistant presentation.

All interfaces must remain replaceable, versioned,
and contract-tested.

No source-specific processing bypasses or hidden
dependencies between architectural layers.

## Development Rules
- Preserve known-good functionality.
- Prefer existing implementations and interfaces.
- Fix root causes rather than symptoms.
- Keep changes narrowly scoped.
- Work on dedicated branches for new development.
- Run relevant tests before committing.
- Do not merge to main or deploy without explicit
  authorization in the current task.
- Do not alter production database records unless
  explicitly authorized.
- Preserve tested milestones in GitHub.

## Efficiency
- Inspect only files relevant to the task.
- Avoid unrelated repository exploration.
- Avoid unnecessary refactoring.
- Prefer focused tests and bounded experiments.
- Keep investigation and reporting concise.

## Completion Report
Provide:
- What changed and why
- Tests performed and results
- Any remaining issues
- Branch, commit SHA, and Git status

Stop after the report for independent review.