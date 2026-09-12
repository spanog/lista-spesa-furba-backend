# Testing

## Test Suites

### Test unitari (nessuna infrastruttura)

```bash
pytest tests/unit -v
pytest tests/ -v --ignore=tests/integration   # tutto tranne integration
```

Questa suite include anche snapshot contract mirati per router/unit test. Gli snapshot JSON vivono in `tests/__snapshots__/` e devono restare leggibili: normalizzare UUID, token, timestamp e URL variabili prima del confronto, mantenendo assertion esplicite per regole di business critiche.

### Test di integrazione (stack Docker isolato)

```bash
# 1. Prepara il file locale dei test
cp .env.test.example .env.test

# 2. Esegui i test: pytest avvia e distrugge solo i container integration
.venv/bin/python -m pytest tests/integration -v
```

Lo stack integration usa `docker-compose.integration.yml` con progetto Docker `girospesa-itest`, volumi dedicati e porte `55421` (API/Kong) + `55422` (PostgreSQL). Non usa `supabase start`, non legge `supabase status`, non cancella dati dello stack locale e non deve lasciare variabili integration esportate fuori dalla sessione `pytest`.

Comandi manuali utili:

```bash
.venv/bin/python -m scripts.integration_stack up
.venv/bin/python -m scripts.integration_stack status
.venv/bin/python -m scripts.integration_stack env
.venv/bin/python -m scripts.integration_stack down
```

FastAPI non deve essere avviato separatamente: i test HTTP usano l'app in-process via HTTPX/ASGI.

I test di integrazione coprono: area Comune+raggio, upload/revisione/conferma volantino, inviti lista, scadenza offerte ed eliminazione account (GDPR).

I contract snapshot di integrazione vivono in `tests/integration/__snapshots__/`. Servono a bloccare regressioni di shape JSON su `/offers`, `/invite`, `/lists/active` e route affini senza sostituire le assertion semantiche.

### Test di performance (opt-in)

```bash
cp .env.test.example .env.test
RUN_PERFORMANCE_TESTS=1 .venv/bin/python -m pytest tests/performance -v -s
```

Performance benchmarks use the same isolated integration stack as integration tests. Normal `pytest tests` runs skip them to avoid machine-dependent timing failures.
GitHub Actions exposes backend CI only as a manual `workflow_dispatch` workflow. The same workflow includes the performance suite as a manual job.
