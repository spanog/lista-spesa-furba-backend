# Local Development

## Workflow

Workflow ufficiale:

- Docker solo per servizi Supabase locali
- FastAPI come processo host separato
- Next.js come processo host separato nel repo `girospesa-webapp/`
- Volumi Docker nominati su PostgreSQL e Storage: restart normali preservano dati locali

### 1. Prerequisiti

- Docker + Docker Compose
- Python 3.14+
- Node.js 20+ (per il frontend separato)

### 2. Variabili d'ambiente

```bash
cp .env.example .env
cp .env.test.example .env.test
```

Valori locali canonici:

- `SUPABASE_URL=http://127.0.0.1:54321`
- `FRONTEND_URL=http://localhost:3000` come valore canonico; `http://127.0.0.1:3000` resta supportato in CORS per compatibilita' loopback
- `GOOGLE_API_KEY` richiesto solo se si vuole usare estrazione AI Gemini
- `WEBMASTER_EMAIL`, `MAIL_FROM`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_USE_TLS` e `SMTP_USE_SSL` servono per i form pubblici `/contact-requests`
- in produzione attuale GiroSpesa usa `Brevo` come relay SMTP applicativo; `Aruba` resta il provider delle mailbox umane (`info@`, ecc.)
- `SUPABASE_SECRET_KEY` si copia da `supabase status -o env` oppure dall'output equivalente del bootstrap locale
- `ADMIN_EMAIL` e `ADMIN_PASSWORD` servono per seedare utente admin via API service-role
- `.env` backend deve contenere solo variabili lette da FastAPI; credenziali Docker/Supabase CLI come `POSTGRES_PASSWORD`, `ANON_KEY`, `JWT_SECRET` e `SERVICE_ROLE_KEY` non vanno copiate qui
- `.env.test` e' locale-only ed e' ignorato da Git; i test integration iniettano questi valori solo dentro processo `pytest`, puntando allo stack Docker isolato su porte `55421`/`55422`, poi ripristinano l'env della sessione a fine run
- `POST /auth/signup` logga sempre causa reale lato backend con stack trace, ma verso frontend restituisce solo messaggi sanitizzati: duplicato account -> `400 {"detail":"Registrazione non riuscita. Verifica i dati inseriti oppure accedi se hai già un account."}`; password/email non valide hanno copy dedicato; errori imprevisti restano generici

### 3. Avviare lo stack Supabase locale

```bash
supabase start
```

Per copiare le variabili locali aggiornate:

```bash
supabase status -o env
```

Servizi disponibili dopo l'avvio:

| Servizio | URL | Scopo |
|----------|-----|-------|
| Supabase Studio | `http://localhost:54323` | Admin UI (DB, Auth, Storage) |
| Supabase API | `http://127.0.0.1:54321` | `SUPABASE_URL` locale per backend e frontend |
| PostgreSQL | `postgresql://postgres:postgres@127.0.0.1:54322/postgres` | DB diretto |
| Mailpit | `http://127.0.0.1:54324` | Inbox email locale |

Bootstrap admin condiviso per locale/test/prod:

- Schema canonico: `supabase/migrations/*.sql`
- Baseline attiva iniziale: `supabase/migrations/20260617000000_initial_schema.sql`
- SQL seed locale: `supabase/seed.sql` non crea piu' admin auth
- Script Python canonico: `.venv/bin/python -m scripts.seed_admin`
- Alias task locale: `.venv/bin/task dev-setup`
- Input richiesti:
  - `ADMIN_EMAIL`
  - `ADMIN_PASSWORD`
- Output garantiti:
  - utente auth esiste
  - `app_metadata.role = "admin"`
  - `public.user_profiles.role = 'admin'`
  - profilo admin con Comune ISTAT Polistena (`080061`), senza indirizzo o coordinate utente
  - almeno una `shopping_lists` attiva e vuota, con membership `owner`
- Script e' idempotente:
  - crea admin se manca
  - se esiste gia', non duplica utente
  - non crea liste duplicate se l'utente ha gia' una lista attiva
  - riallinea ruolo JWT/profile se necessario
- Credenziali locali gia' configurate in `.env`:
  - `ADMIN_EMAIL=dev-admin@local.test`
  - `ADMIN_PASSWORD=Admin123!`
- L'admin non viene creato automaticamente all'avvio app: dopo primo `supabase start`
  o dopo `supabase db reset`, esegui `.venv/bin/python -m scripts.seed_admin`.

Login consigliato per primo accesso:

- Frontend: `http://localhost:3000/login`
- Supabase Studio/Auth inspect: `http://localhost:54323`

Comandi utili:

```bash
supabase status
supabase status -o env
.venv/bin/task dev-setup                      # alias locale per seed admin
.venv/bin/python -m scripts.seed_admin         # seed idempotente admin
.venv/bin/python -m scripts.seed_admin --check # verifica auth user, ruolo DB, login password grant
supabase stop                  # preserva dati locali
supabase db reset              # reset totale locale: utenti, prodotti, offerte, storage
```

> `supabase stop` preserva volumi e dati. Dopo `supabase db reset` o su nuovo ambiente, riesegui `.venv/bin/python -m scripts.seed_admin`.
>
> `supabase status -o env` espone chiavi locali correnti: `ANON_KEY`, `PUBLISHABLE_KEY`, `SERVICE_ROLE_KEY`, `JWT_SECRET`.
>
> `supabase db reset` resta comando di riallineamento totale quando vuoi ripartire da zero.

### 4. Avviare FastAPI

```bash
python3.14 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
.venv/bin/task dev-setup
python -m uvicorn main:app --reload --port 8000
```

- API locale: `http://localhost:8000`
- Swagger: `http://localhost:8000/docs`
- Health check: `http://localhost:8000/health`
- Se usi Python < 3.14, boot fallisce subito con errore esplicito prima di caricare router/app.
- Se shell mostra `zsh: command not found: uvicorn`, virtualenv non e' attivo oppure dipendenze non sono ancora installate.
- `requirements.txt` tiene `httpx==0.27.2` per compatibilita' con `supabase==2.10.0` (`supabase` richiede `httpx<0.28`).
- Avvio equivalente senza activation:

```bash
.venv/bin/python -m uvicorn main:app --reload --port 8000
```

### 4.1 Primo avvio locale completo

```bash
# Terminale 1
cd girospesa-backend
supabase start

# Terminale 2
cd girospesa-backend
python3.14 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m scripts.seed_admin
python -m uvicorn main:app --reload --port 8000

# Terminale 3
cd ../girospesa-webapp
npm install
npm run dev
```

Credenziali admin locale configurate:

- email: `dev-admin@local.test`
- password: `Admin123!`

Queste credenziali diventano valide solo dopo il seed manuale:

```bash
cd girospesa-backend
.venv/bin/task dev-setup
```

Check rapido seed:

```bash
cd girospesa-backend
.venv/bin/python -m scripts.seed_admin --check
```

Comando rapido alternativo:

```bash
npx taskipy dev
```

### 5. Avviare il frontend separato

Dal repo fratello `../girospesa-webapp/`:

```bash
npm install
npm run dev
```

- Frontend locale: `http://localhost:3000`
- Tutte le chiamate business passano a FastAPI; Next.js non espone API routes

### 6. CLI e valutazione estrazione

I CLI di valutazione e QA vivono in `scripts/extraction/`. Il runtime ufficiale resta `ExtractionService`, invocato dagli endpoint `POST /flyers/{flyer_id}/extract`.

### Local Dependency Matrix

| Tipo | Elemento | Richiesto per boot locale | Note |
|------|----------|---------------------------|------|
| Supabase CLI | Postgres/Auth/Storage/Studio | Sì | Avvio via `supabase start` |
| Processo host | FastAPI backend | Sì | Porta `8000` |
| Processo host | Next.js frontend | Sì | Repo separato, porta `3000` |
| API esterna | Google Gemini | Solo per estrazione volantini | Unica dipendenza esterna richiesta per AI extraction |
| Servizio esterno | SMTP server | No | Necessario solo per invio mail da `/contact-requests` |

---
