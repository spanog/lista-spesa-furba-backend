# Backend agent notes

## Scope

- `README.md` = human-facing entry point only: overview, quick start, and links to detailed docs.
- `docs/*.md` = human-facing documentation by responsibility: architecture, API, local development, testing, configuration, data model, jobs, flows, and deploy runbooks.
- `AGENTS.md` = agent-facing operating rules only: commands, test expectations, deploy guardrails, and repo workflow conventions.
- When a change affects both domains, update both files but keep each change inside its own scope.

## Testing Guardrails

- Every backend change must update the closest test layer first: unit/service for pure logic, integration for real DB/API contracts.
- Stable JSON responses touched by the change should gain or update contract snapshots under `tests/__snapshots__/` or `tests/integration/__snapshots__/`.
- Normalize unstable values before snapshot comparison: UUID, token, timestamp, variable URL host/query.
- Supabase schema or RLS changes must keep `supabase db advisors --local` clean for touched areas; wrap `auth.uid()` / `auth.jwt()` as `select` expressions in policies when possible to avoid advisor performance warnings.
- Snapshot tests support, not replace, explicit assertions on permissions, ordering, lifecycle transitions, and domain invariants.
- `GET /offers` deve risolvere l'area soltanto dal Comune firmato guest o dal profilo e applicare il raggio prima di paginare le offerte.
- `append_list_item` deve incrementare atomically una riga attiva con lo stesso `pinned_offer_id`, senza unire righe già acquistate.
- Cookie località guest: richieste da origin HTTPS Capacitor devono ricevere `SameSite=None; Secure`; mantenere test endpoint per questo contratto cross-site.

## Commands

- Setup local env files: `cp .env.example .env && cp .env.test.example .env.test`
- Start Supabase local stack: `supabase start`
- Run app locally: `.venv/bin/python -m uvicorn main:app --reload --port 8000`
- Seed/check admin: `.venv/bin/python -m scripts.seed_admin` / `.venv/bin/python -m scripts.seed_admin --check`
- Run:
  - `.venv/bin/python -m pytest tests -v --ignore=tests/integration --ignore=tests/performance`
  - `.venv/bin/python -m pytest tests/integration -v`
  - `RUN_PERFORMANCE_TESTS=1 .venv/bin/python -m pytest tests/performance -v -s` for opt-in benchmarks
- Manage integration stack manually: `.venv/bin/python -m scripts.integration_stack up|down|status|env`

## Data Access

- FastAPI is the only application layer allowed to touch database persistence details.
- Frontend-facing features must expose backend endpoints instead of coupling UI code to Supabase tables/RPCs directly.
- Non introdurre provider, endpoint o client di geocoding: utenti e filiali usano esclusivamente codici Comune ISTAT.
- Keep raw SQL, PostgREST, Supabase service-role access, and schema-specific branching inside backend repositories/services, never inside frontend code.
- No endpoint may trust client-supplied `admin`, `manager`, `role`, or similar flags for privileges or data scope. Authorization must derive from validated server-side auth context.
- Public contact flows (`/contact-requests`) are mail-first: do not reintroduce app tables or client-side inserts for bug reports, collaboration requests, or missing-flyer requests.

## Integration Test Isolation

- `tests/integration/` must boot a fresh Docker stack on project `girospesa-itest` and destroy only that stack with `down -v --remove-orphans`.
- Integration env overrides (`SUPABASE_URL`, `DB_DSN`, keys, etc.) must stay scoped to the `pytest` session and be restored afterward. Never mutate process env at module import time.
- Dev stack (`supabase start`, backend on `.env`, local ports `54321+`) must remain untouched by integration test setup/teardown.

## Supabase Conventions

- Match pinned client API exactly. Example: PostgREST `.order()` expects `nullsfirst`, not `nulls_first`.
- Local Supabase API exposure stays limited to `public` schema. `pg_graphql` is disabled; do not build or document `/graphql/v1` flows.
- RLS-only helper functions that need `SECURITY DEFINER` privileges must live in a non-exposed schema such as `private`; do not publish them from `public` or document them as client-callable RPCs.
- Public Storage buckets (`avatars`, `logos`, `product-images`) rely on signed-less `/storage/v1/object/public/...` URLs only. Do not depend on anonymous bucket listing via `storage.objects` policies.
- Le anteprime dei volantini pubblici passano da `GET /flyers/{id}/preview`: restituire binario WebP con cache HTTP, mai URL Storage al browser. Conservare gli URL firmati brevi esclusivamente in endpoint separati per workflow admin privati.

## Deploy / CI conventions

- Production/runtime baseline is Python `3.14.3`: keep `.python-version`, `pyproject.toml`, `render.yaml`, runtime guards, and CI aligned when changing interpreter support.
- Backend CI exposes `performance-test` only through manual `workflow_dispatch`; keep performance benchmarks opt-in unless thresholds become stable enough for every PR.
- Supabase schema source of truth for this repo is `girospesa-backend/supabase/migrations/`; keep one active baseline or forward-only migration chain there, and archive any retired history outside that directory.
- Keep `render.yaml` aligned with runtime expectations and required env vars.
- GitHub Actions under `.github/workflows/` are part of the production contract: update them when commands, Python version, or test entrypoints change.
- Production Supabase migrations are deployed by `.github/workflows/supabase-db-production.yml`; when schema deployment assumptions change, update workflow, deploy/configuration docs, and guard tests together.

## Git / Ignore Rules

- Keep `main` clean for deploy-ready code.
- Never commit local secrets or machine-specific env files.
- Track `.env.example` and `.env.test.example`.
- Ignore `.env`, `.env.local`, `.env.test`, Python caches, coverage artifacts, editor files, macOS files, and Supabase local state.
