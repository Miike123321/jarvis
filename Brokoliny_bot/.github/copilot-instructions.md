# Copilot instructions for Brokoliny Bot

Read `docs/PROJECT_STACK.md` before planning changes. This is a Python
aiogram/FastAPI application with SQLite persistence, not a frontend monorepo.
Preserve the existing module boundaries and the privacy constraints described
in `README.md`.

## Graphify

Use the existing graph in `graphify-out/graph.json` to understand symbol and
dependency relationships; the accompanying report and HTML are in
`graphify-out/`. Graph results are navigation aids, not proof: confirm
important contracts and inferred relationships in source code.

The Graphify CLI is separate from application dependencies. With the CLI
available, use:

```sh
graphify query "A specific question about project symbols or dependencies" --budget 3000
graphify update .
graphify cluster-only . --no-label --no-viz
```

Run `graphify update .` after relevant source changes, then recluster when the
report/community view needs to be refreshed. For an intentional code-only
rebuild, use `graphify extract . --code-only`; this does not semantically
extract documents and can replace prior semantic coverage. Do not run that as
a routine update. If the CLI is unavailable, state that explicitly instead
of claiming to have queried or updated the graph. Install it separately, if
needed, with `uv tool install graphifyy`; do not add it to `requirements.txt`.

The installed Graphify CLI does not expose an MCP server command, so no
Graphify MCP server is configured in `.vscode/mcp.json`. Use its CLI through
an agent's `runInTerminal` tool where that tool is available.

## Project conventions

- Run tests with `python -m unittest discover -s tests -v`.
- Keep API contracts in `app/schemas.py`, persistence in `app/database.py`,
  bot flows in `app/bot.py`, administration routes in `app/admin.py`, and AI
  orchestration in `app/ai.py`.
- The web UI is `admin.html` with embedded JavaScript. Styles live in `scss/`;
  compile `app/static/admin.css` with `npm run build:css` after style changes.
  Keep presentation-only changes separate from JavaScript/API behavior.
- Do not inspect, disclose, or include `.env` values or local SQLite data in
  generated artifacts. Respect `.gitignore` and `.graphifyignore`.
- The project uses Sass for CSS builds; it has no configured JavaScript
  formatter, linter, or TypeScript toolchain.
