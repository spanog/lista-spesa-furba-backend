# Render Local Deploy Without GitHub Actions

Use this runbook when GitHub Actions minutes are limited and backend deploys must be controlled from the local machine.

## What Still Uses Git

The current Render service is Git-backed. Triggering a deploy locally does not consume GitHub Actions, but Render deploys a pushed commit from the connected repository.

Current service contract:

- repository: `spanog/girospesa-backend`;
- service: `girospesa-backend`;
- runtime: Python;
- build command: `pip install -r requirements.txt`;
- start command: `uvicorn main:app --host 0.0.0.0 --port $PORT --no-access-log`;
- health check: `/health`;
- auto-deploy trigger: `commit` / `On Commit`;
- production API: `https://api.girospesa.it`.

## Local Prerequisites

The local-only production env inventory lives in `.env.production`. It is ignored by Git and contains placeholders for secrets plus a source comment for each variable.

```bash
brew install render
render --version
render login
render whoami -o json
render workspaces
```

Backend quality gate:

```bash
cd /Users/giacomo/progetti/girospesa/girospesa-backend
python3.14 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pytest tests -v --ignore=tests/integration --ignore=tests/performance
.venv/bin/python -m pytest tests/integration -v
```

Optional performance suite:

```bash
cd /Users/giacomo/progetti/girospesa/girospesa-backend
RUN_PERFORMANCE_TESTS=1 .venv/bin/python -m pytest tests/performance -v -s --tb=short
```

Validate Render blueprint:

```bash
cd /Users/giacomo/progetti/girospesa/girospesa-backend
render blueprints validate render.yaml
```

## Deploy By Git Push

This path does not run backend CI GitHub Actions. Render auto-deploy is the deployment automation for `main`; `.github/workflows/ci.yml` is manual-only.

```bash
cd /Users/giacomo/progetti/girospesa/girospesa-backend
git status --short
git branch --show-current
git push origin main
```

Then monitor Render:

```bash
render services
render deploys list <service-id>
render logs --resources <service-id> --tail
```

## Trigger Deploy From Render CLI

Use this when the target commit is already pushed and you want to redeploy from the local machine.

```bash
render services
render deploys create <service-id> --wait
```

Use `--clear-cache` when dependency cache may be stale:

```bash
render deploys create <service-id> --clear-cache --wait
```

## Trigger Deploy Hook

In Render Dashboard:

1. open `girospesa-backend`;
2. open `Settings`;
3. copy `Deploy Hook URL`;
4. store it in a password manager or shell-local env only.

Trigger:

```bash
BACKEND_RENDER_DEPLOY_HOOK_URL="https://api.render.com/deploy/..."
curl -fsS -X POST "$BACKEND_RENDER_DEPLOY_HOOK_URL"
```

## Environment Change Redeploy

1. Update env vars in Render Dashboard.
2. Trigger `Manual Deploy -> Deploy latest commit`, or run:

```bash
render deploys create <service-id> --wait
```

3. Check health and affected endpoint.

Minimum smoke:

```bash
curl -fsS https://api.girospesa.it/health
curl -fsS "https://api.girospesa.it/municipalities?query=Polistena" >/dev/null
```

## Post Deploy Record

After production deploys, update the workspace status file:

```text
/Users/giacomo/progetti/girospesa/docs/deploy-production-status.md
```

Record date, commit, deploy method, smoke result, and any env change. Never write secret values.
