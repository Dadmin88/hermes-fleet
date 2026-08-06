# Contributing to Hermes Fleet

Hermes Fleet is an early-stage project. Contributions are welcome, but changes must preserve the responsibility boundary between Fleet, Keryx, and Hermes Agent.

## Before opening a change

- Search existing issues and pull requests.
- Keep changes focused on one capability or defect.
- Do not commit private topology, real peer IDs, tokens, credentials, machine names, local paths, operational evidence, or internal planning files.
- Use generic examples such as `controller-1`, `worker-1`, and `<peer-id>` in public documentation and tests.
- Do not introduce a duplicate transport, task ledger, result poller, or message lifecycle database inside Fleet.

## Development setup

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
```

Run the standard checks:

```bash
pytest
ruff check .
ruff format --check .
python -m build
```

## Pull requests

A good pull request should include:

- a clear problem statement;
- the narrow ownership boundary affected;
- focused tests that fail before the fix and pass afterward;
- security and compatibility considerations;
- documentation updates when public behavior changes;
- confirmation that no secrets or machine-specific evidence were added.

Avoid mixing broad refactors with functional changes.

## Security-sensitive changes

Changes affecting authentication, authorization, routing receipts, deadlines, task reclaim, execution binding, cancellation, or compatibility require focused adversarial tests and review.

Never treat caller-provided identity fields as authenticated identity. Never describe a transport receipt as proof of completed remote execution.

## Documentation style

Public documentation should read like product documentation:

- explain reusable concepts and supported workflows;
- use placeholders and generic node names;
- keep deployment-specific evidence outside Git;
- avoid dated implementation plans and private acceptance records;
- distinguish implemented behavior from planned work;
- state limitations plainly.

## Commit hygiene

- Keep commits reviewable and purpose-specific.
- Do not commit generated caches, virtual environments, local `.hermes` state, evidence bundles, or environment files.
- Preserve unrelated work.

By contributing, you agree that your contribution is licensed under the repository's MIT License.
