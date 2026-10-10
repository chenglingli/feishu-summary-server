# Repository instructions

- Read README.md before changing behavior. Also read docs/IMPLEMENTATION_STATUS.md when present; docs/ is maintained locally and is not distributed through Git.
- Never commit secrets, message bodies, summaries, or real user fixtures.
- Keep response models explicit; update contracts/openapi.json with API changes.
- Add comments for security decisions, idempotency and failure recovery where intent is not obvious.
- Run Ruff, mypy, relevant pytest and Alembic consistency checks. Database tests require a dedicated summary_test database and truncate it.
- Write descriptive Git commit subjects and bodies including validation and remaining limitations. Do not claim mocked integrations were tested against Feishu.
