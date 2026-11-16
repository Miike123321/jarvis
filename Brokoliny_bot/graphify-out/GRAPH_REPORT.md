# Graph Report - Brokoliny_bot  (2026-11-16)

## Corpus Check
- cluster-only mode — file stats not available

## Summary
- 538 nodes · 1701 edges · 11 communities (10 shown, 1 thin omitted)
- Extraction: 77% EXTRACTED · 23% INFERRED · 0% AMBIGUOUS · INFERRED: 385 edges (avg confidence: 0.88)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `e8000c5a`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- Database
- test_core.py
- bot.py
- create_app
- cloudflare_access.py
- ContentLibrary
- ImageCache
- package.json
- admin-ui.test.js
- AdminUiAcceptanceTests
- launcher.py

## God Nodes (most connected - your core abstractions)
1. `Database` - 133 edges
2. `create_app()` - 56 edges
3. `ImageCache` - 44 edges
4. `SecretVault` - 43 edges
5. `tr()` - 34 edges
6. `AIOrchestrator` - 33 edges
7. `Settings` - 33 edges
8. `ContentLibrary` - 29 edges
9. `deliver_plan()` - 27 edges
10. `_start_assessment()` - 22 edges

## Surprising Connections (you probably didn't know these)
- `AdminPlanResendTests` --uses--> `Database`  [INFERRED]
  tests/test_core.py → app/database.py
- `AdminPromptTestTests` --uses--> `Database`  [INFERRED]
  tests/test_core.py → app/database.py
- `ContentLibraryTests` --uses--> `Database`  [INFERRED]
  tests/test_core.py → app/database.py
- `ImageCacheTests` --uses--> `Database`  [INFERRED]
  tests/test_core.py → app/database.py
- `PlanDeliveryTests` --uses--> `Database`  [INFERRED]
  tests/test_core.py → app/database.py

## Import Cycles
- None detected.

## Communities (11 total, 1 thin omitted)

### Community 0 - "Database"
Cohesion: 0.06
Nodes (8): AIBudgetExceeded, AIOrchestrator, Database, SecretVault, AdminServingIntegrationTests, AIRequestLogTests, AssessmentOnboardingTests, PersistenceTests

### Community 1 - "test_core.py"
Cohesion: 0.05
Nodes (30): authorize(), feedback_keyboard(), plan_message(), _draw_difference_rows(), _draw_shape(), _font_path(), make_stimulus(), make_worksheet() (+22 more)

### Community 2 - "bot.py"
Cohesion: 0.11
Nodes (36): _ai_sharing_keyboard(), assess_result(), _assessment_keyboard(), _begin_birth_date_flow(), change_delivery_time(), choose_language(), diagnosis_ai_consent(), _diagnosis_keyboard() (+28 more)

### Community 4 - "create_app"
Cohesion: 0.05
Nodes (23): create_app(), attach_admin_identity(), budget(), cached_image_file(), generate_prompt_test(), plan_worksheet(), prompt_test_context(), prompt_test_image() (+15 more)

### Community 5 - "cloudflare_access.py"
Cohesion: 0.09
Nodes (11): AccessVerificationError, CloudflareAccessMiddleware, CloudflareAccessVerifier, _decode_segment(), _fetch_jwks(), _json_without_duplicate_names(), reject_duplicates(), _normalize_email() (+3 more)

### Community 6 - "ContentLibrary"
Cohesion: 0.12
Nodes (11): _age_cohort(), _canonical_json(), _component_identity(), ContentExhausted, ContentLibrary, _descriptor(), _hash(), _similar_to_history() (+3 more)

### Community 11 - "ImageCache"
Cohesion: 0.07
Nodes (18): regenerate_daily_plan(), resend_daily_plan(), ImageCache, deliver_plan(), AdminPlanResendTests, generate_image(), FakeAI, ImageCacheTests (+10 more)

### Community 13 - "package.json"
Cohesion: 0.22
Nodes (9): devDependencies, sass@1.83.4, name, private, scripts, build:css, test:ui, watch:css (+1 more)

### Community 14 - "admin-ui.test.js"
Cohesion: 0.14
Nodes (8): assert, fs, html, path, setup(), storage(), test, vm

### Community 17 - "launcher.py"
Cohesion: 0.06
Nodes (12): _health_ok(), remote_unavailable(), _reserve_origin(), start_remote_services(), start_services(), _stop_processes(), _wait_for_admin(), FakeChild (+4 more)

## Knowledge Gaps
- **1 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `Database` connect `Database` to `test_core.py`, `bot.py`, `create_app`, `ContentLibrary`, `ImageCache`, `launcher.py`?**
  _High betweenness centrality (0.279) - this node is a cross-community bridge._
- **Are the 29 inferred relationships involving `Database` (e.g. with `AIOrchestrator` and `change_delivery_time()`) actually correct?**
  _`Database` has 29 INFERRED edges - model-reasoned connections that need verification._
- **Should `Database` be split into smaller, more focused modules?**
  _Cohesion score 0.06096491228070176 - nodes in this community are weakly interconnected._
- **Why does `create_app()` connect `create_app` to `Database`, `test_core.py`, `bot.py`, `cloudflare_access.py`, `ContentLibrary`, `ImageCache`?**
  _High betweenness centrality (0.144) - this node is a cross-community bridge._
- **Are the 4 inferred relationships involving `create_app()` (e.g. with `authorize()` and `lifespan()`) actually correct?**
  _`create_app()` has 4 INFERRED edges - model-reasoned connections that need verification._
- **Should `test_core.py` be split into smaller, more focused modules?**
  _Cohesion score 0.05396825396825397 - nodes in this community are weakly interconnected._
- **Why does `CloudflareAccessVerifier` connect `cloudflare_access.py` to `test_core.py`, `create_app`?**
  _High betweenness centrality (0.050) - this node is a cross-community bridge._