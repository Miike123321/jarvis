# Project Architecture, Stack, and Graphify

## Project overview

Brokoliny Bot is a single Python application, not a monorepo. It combines an
aiogram Telegram bot, a local FastAPI administration API and HTML page, SQLite
persistence, scheduled plan delivery, and AI-provider integrations. The
application is organized as a modular `app/` package; the admin stylesheet has
a separate Sass build, but there is no frontend application or state manager.

## Languages, frameworks, and dependencies

- **Language/runtime:** Python 3.11 or newer (documented in `README.md` and
  enforced by `start.command`).
- **Telegram bot:** aiogram 3; conversation flows and user interaction are in
  `app/bot.py`.
- **Administration API:** FastAPI, served locally by Uvicorn from
  `app.admin:app`; API routes and app creation are in `app/admin.py`.
- **Administration UI:** the root-level `admin.html`, served by the
  administration API, with vanilla JavaScript that calls the API. Sass source
  is at `scss/admin.scss`, with runtime CSS at `app/static/admin.css`.
- **State and validation:** SQLite persistence in `app/database.py`,
  aiogram FSM state in `app/bot.py`, and Pydantic request/AI response models
  in `app/schemas.py`. There is no separate frontend state-management
  framework.
- **AI:** LiteLLM provider routing and orchestration in `app/ai.py`; structured
  data is validated with Pydantic models and JSON Schema.
- **Scheduling:** APScheduler, with bot scheduling in `app/scheduler.py` and
  admin retention scheduling in `app/admin.py`.
- **Generated files:** ReportLab/Pillow are used by `app/printables.py` and
  `app/image_cache.py` for PDFs and images. Cryptography/Fernet is used for
  protected application data.
- **Dependency manifests:** `requirements.txt` for Python runtime;
  `package.json` and `package-lock.json` for the pinned development Sass build.

## Important directories and files

| Path | Responsibility |
| --- | --- |
| `app/bot.py` | Telegram handlers, onboarding, FSM flows, user-facing bot interactions, and bot startup |
| `app/admin.py` | FastAPI app, authenticated admin API routes, and serving `admin.html` and public `/static/admin.css` |
| `app/database.py` | SQLite schema, persistence operations, and prompt/configuration storage |
| `app/schemas.py` | Pydantic request and structured AI response models |
| `app/ai.py` | Provider routing, AI requests, response validation, request logging, and budget handling |
| `app/content_library.py` | Reusable content selection and per-user content history |
| `app/scheduler.py` | Scheduled plan delivery |
| `app/plans.py` | Plan formatting and delivery helpers |
| `app/age.py` | Birth-date parsing and age calculations |
| `app/security.py` | Secret handling and admin-token validation |
| `app/config.py` | Environment-based runtime settings |
| `app/printables.py`, `app/image_cache.py` | Printable and image generation/caching |
| `app/localization.py` | Localized messages |
| `admin.html` | Admin UI markup and browser-side API interactions |
| `scss/`, `app/static/admin.css` | Sass source and compiled runtime stylesheet |
| `tests/test_core.py` | Unit and API integration tests |
| `data/` | Local SQLite database; database files are excluded from Git and must not be inspected or included in generated documentation |
| `graphify-out/` | Graphify graph, report, and HTML visualization |

## UI and styling

The only browser UI is `admin.html` at the project root. Behavior is vanilla
JavaScript in its `<script>` element. The Sass build compiles
`scss/admin.scss` to `app/static/admin.css`, served at `/static/admin.css`.
There is no Tailwind, CSS Modules, React, Vue, or TypeScript setup.

The Telegram user interface is implemented through aiogram handlers and
keyboards in `app/bot.py`, rather than a separate visual frontend. UI-only
changes to the web admin page should be limited to presentation markup and
SCSS. The embedded JavaScript and API behavior are functional code and
must not be changed by a presentation-only task.

The admin prompt workspace edits four active prompt versions:
`diagnostics`, `daily_plan_task1`, `daily_plan_task2`, and
`daily_plan_advice`. Test generation uses the selected child's current profile
without changing the active prompt; **Save and replace** creates the next
active version and keeps the previous text in prompt history. Prompt templates
can interpolate the validated `{{child.*}}`, `{{plan.*}}`, and
`{{diagnostics.*}}` fields exposed by the prompt API. The context preview
includes the profile's latest skill observations and their non-independent
subset. Encrypted diagnosis notes are deliberately excluded from this admin
test context. Prompt test traces show the actual encrypted AI request logs
only after authorized retrieval from the admin API; task-one tests label
whether the associated image came from cache or a new AI request.

## Tests and development commands

The project uses the Python standard-library `unittest` framework, with
FastAPI `TestClient` used for API tests. `pytest` is not the documented runner
and is not listed as a project dependency.

Run the full test suite from the project root:

```sh
python -m unittest discover -s tests -v
```

The launch script creates/uses `.venv`, installs `requirements.txt` when
needed, and starts the admin API and Telegram bot. The underlying service
entry points are `python -m app.bot` and Uvicorn's `app.admin:app`.
Node.js/npm are needed only to build styles: run `npm ci`, then
`npm run build:css` or `npm run watch:css`; `npm run test:ui` runs focused
JavaScript behavior tests. Runtime serves the compiled CSS without running
Sass. See `README.md` for the asset contract.

## Graphify status and integration

Graphify is a separate CLI tool (`graphifyy`), not an application dependency.
The inspected environment has Graphify CLI version `0.9.109`; its executable
is available as `graphify`. The project already contains `graphify-out/` with
`graph.json`, `GRAPH_REPORT.md`, and `graph.html`. After `graphify update .`
on 2026-11-16 the CLI reported 413 nodes, 1,453 edges and 15 communities.
The later code update reported 447 nodes, 1,509 edges and 18 communities;
it skipped SCSS files and did not semantically extract changed documentation.
The working tree can change further, so refresh the graph before relying on it.

The installed CLI supports querying and maintaining the project graph:

```sh
# Ask a focused question about symbols, imports, or dependencies.
graphify query "How does app.admin create and authenticate its API routes?" --budget 3000

# Incrementally update the graph after source changes.
graphify update .

# Refresh clustering/report without LLM community labels or HTML generation.
graphify cluster-only . --no-label --no-viz

# Rebuild a code-only graph (does not include semantic extraction of documents).
graphify extract . --code-only
```

If Graphify is not installed, install it separately from application
dependencies with `uv tool install graphifyy`. Do not install it into
`requirements.txt`. Prefer `graphify update .` for normal source changes; a
full code-only extraction can replace semantic/document coverage in the
existing graph, so use it only when that is intended. Treat inferred graph
edges as hypotheses and verify important contracts against source files.

The installed `graphify --help` exposes `query`, `update`, and `extract`, but
does not expose a Graphify MCP server command. Therefore this project does not
declare a Graphify server in `.vscode/mcp.json`; a non-working MCP entry would
mislead contributors. VS Code Copilot agents with `runInTerminal` use the
Graphify CLI directly. Repository-wide Copilot guidance is in
`.github/copilot-instructions.md`.

The current Graphify scan exclusions include local secrets, virtual
environments, bytecode, `data/`, `node_modules/`, and Graphify's AST cache. Keep
secrets and local user data out of graph outputs, prompts, and reports.
