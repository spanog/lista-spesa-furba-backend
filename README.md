# GiroSpesa — Backend

> Scope: questo file e' documentazione per persone. Regole operative per agenti e convenzioni di esecuzione vivono in `AGENTS.md`.

FastAPI backend per GiroSpesa. Contiene la logica di business dell'applicazione: offerte estratte dai volantini, gestione liste condivise, archivio ISTAT dei Comuni, notifiche di pubblicazione e analytics B2B. Utenti e filiali selezionano soltanto un Comune ISTAT: non usa provider di geocoding, indirizzi o coordinate di filiale.

> **Estrazione AI volantini:** questo repo contiene runtime, review flow e CLI di valutazione. Il vecchio workspace di estrazione e' stato assorbito in questo backend. Quando il modello localizza un packshot nel PDF, la bozza conserva automaticamente il relativo ritaglio per la review. I PDF nuovi vengono inviati a Gemini in chunk da due pagine, generati uno alla volta: la dimensione complessiva del PDF può quindi superare il limite inline di 20 MB applicato alle immagini singole. I crop sono renderizzati a risoluzione 2× e ogni pagina viene renderizzata una sola volta per tutti i suoi prodotti. Se una pagina del chunk restituisce al massimo un prodotto mentre la pagina vicina ne contiene almeno quattro, il backend la riestrae singolarmente con un controllo di copertura e conserva il risultato solo se più completo. La localizzazione del crop è persistita internamente, così dopo un riavvio vengono completate solo le immagini mancanti. I checkpoint conservano la dimensione chunk originale per una ripresa coerente.

## System Overview

Il frontend web usa Supabase Auth con `@supabase/ssr`: browser, proxy Next.js e Server Components condividono la sessione tramite cookie SSR di Supabase. Le chiamate autenticate a questo backend inoltrano sempre il token accesso Supabase in `Authorization: Bearer <token>`.

Il backend valida i bearer token utente tramite signing keys/JWKS di Supabase e usa `SUPABASE_SECRET_KEY` per operazioni server-side privilegiate su Auth, Database e Storage. Non esiste un cookie sessione backend per auth applicativa o stream SSE; i guest ricevono soltanto un cookie tecnico firmato e `HttpOnly` per mantenere il filtro geografico delle discovery pubbliche. Per client HTTPS esterni, incluso Capacitor, il cookie usa `SameSite=None; Secure`.

Le notifiche di pubblicazione volantino sono accodate in `notification_jobs` e drenate fuori dalle richieste utente da APScheduler o da `POST /ops/cron/notifications`. Per volantini con data di inizio futura, il job diventa eseguibile alle 10:00 `Europe/Rome` del giorno di validità; senza data, è subito eseguibile. Un job padre individua tutti gli admin, il manager della sede pubblicata e i customer la cui area Comune+raggio include la filiale; job figli indipendenti persistono l'inbox e inviano il push solo quando l'account lo ha abilitato. Il messaggio usa il titolo `Nuovo volantino` e indica numero offerte, sede e Comune della filiale.

L'accettazione di un invito a una lista condivisa crea subito una notifica inbox/push per chi ha inviato l'invito, con il nome dell'utente che entra nella lista.
L'abbandono di una lista condivisa crea subito una notifica inbox/push per il proprietario con il messaggio `<nome utente> ha lasciato la lista`.

`NOTIFICATION_DELIVERY_WORKERS` è opzionale (default `8`, minimo `1`, massimo `16`) e limita i job figlio consegnati in parallelo.
`GET /offers`, `GET /supermarkets` e `GET /flyers/public` applicano sempre la stessa area Comune+raggio: tutte le filiali del Comune selezionato, anche oltre la soglia, più le filiali di altri Comuni entro il raggio tra i rispettivi centri geografici ISTAT. Il profilo conserva solo il codice ISTAT del Comune e un raggio da 1 a 20 km; per gli ospiti il cookie firmato conserva solo il codice e il raggio è sempre 10 km. Utenti e filiali non memorizzano né inviano indirizzi o coordinate. Vale anche per admin e gestori nelle pagine di discovery. Le aperture iniziali usano `GET /offers/discovery` e `GET /flyers/discovery`, che restituiscono rispettivamente offerte+sedi e volantini+sedi usando una singola risoluzione dell'area; la paginazione delle offerte continua su `GET /offers`. Le sedi del Comune selezionato precedono le sedi esterne, poi seguono la distanza crescente. Le sedi con offerte attive sono calcolate nel database tramite un controllo `EXISTS` indicizzato: il risultato non dipende dal limite di righe di PostgREST. La gestione volantini usa `GET /flyers/targets`: restituisce tutte le sedi attive agli admin e solo quelle assegnate ai gestori. Quando una stessa offerta di un volantino è pubblicata per più sedi, `GET /offers` restituisce una sola copia, associata alla sede più vicina. Il filtro ripetibile `supermarket_ids` limita prima le sedi candidate. L'endpoint non espone un parametro di ordinamento: mantiene una sequenza stabile per la paginazione. Per ciascun supermercato, `GET /flyers/public` ordina i volantini per scadenza più vicina. Il bucket `flyers` è privato: `GET /flyers/{id}/file-url` verifica accesso e validità e restituisce un URL Storage firmato per 15 minuti con `Cache-Control: no-store`. Il PDF viene quindi scaricato direttamente dal CDN, senza attraversare Render. `GET /flyers/{id}/file` è mantenuto solo come redirect compatibile verso lo stesso URL firmato. Le preview pubbliche restano cacheabili tramite backend, mentre i flussi amministrativi privati usano URL firmati brevi.

Le nuove immagini estratte dalle offerte sono WebP con lato lungo massimo di 640 px, qualità 82 e oggetti immutabili con cache di un anno. Per convertire le PNG esistenti, eseguire prima il dry-run e poi l'applicazione: `python -m scripts.migrate_offer_images_to_webp` e `python -m scripts.migrate_offer_images_to_webp --apply`. L'opzione `--delete-originals` è esplicita e disponibile solo insieme a `--apply`, dopo la verifica dei nuovi file.

I nuovi loghi supermercato usano path basati sul contenuto e cache CDN di un anno; durante una sostituzione il file precedente resta disponibile per le sessioni che lo hanno già richiesto. Per aggiornare la policy cache degli asset esistenti, eseguire prima `python -m scripts.refresh_supermarket_logo_cache`, poi `python -m scripts.refresh_supermarket_logo_cache --apply`.

La registrazione richiede la selezione di un Comune dall'archivio ISTAT. Salva il solo codice ISTAT, prima della conferma email: il flusso non dipende da una sessione autenticata nel browser.

In locale, impostare anche `SUPABASE_JWT_SECRET` uguale al `JWT_SECRET` di Docker: consente al backend di verificare i token HS256 emessi dall'istanza Supabase locale. In produzione il backend continua a verificare i token ES256/RS256 tramite JWKS.
L'aggiunta ripetuta della stessa offerta attiva a una lista incrementa la quantità della riga esistente in modo atomico.

Dettagli architetturali: [docs/architecture.md](docs/architecture.md).

## Quick Start

```bash
cp .env.example .env
cp .env.test.example .env.test
supabase start
python3.14 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m scripts.seed_admin
python -m uvicorn main:app --reload --port 8000
```

## Baseline catalogo prodotti

Il tag annotato `v.0.1-product-catalog` conserva l'architettura precedente basata su prodotti canonici, preferiti e immagini di catalogo. Il modello corrente è basato esclusivamente sulle offerte e sulle immagini estratte dal volantino. Il tag è il punto di ripartenza per una futura reintroduzione del catalogo, delle notifiche sui preferiti e di immagini curate ad alta qualità.

- API locale: `http://localhost:8000`
- Swagger: `http://localhost:8000/docs`
- Health check: `http://localhost:8000/health`
- Frontend separato: `../girospesa-webapp/`, porta `3000`

Guida completa: [docs/local-development.md](docs/local-development.md).

## Template email Auth

I template di conferma account e recupero password sono in `supabase/templates/`. Usano il logo raster pubblico ufficiale per compatibilità con i client email. In Supabase hosted vanno pubblicati manualmente da **Authentication → Emails → Templates** dopo ogni modifica; `config.toml` applica questi file soltanto allo stack locale.

## Documentation

| Tema | File |
|------|------|
| Architettura e struttura repo | [docs/architecture.md](docs/architecture.md) |
| API, autenticazione e contratti endpoint | [docs/api.md](docs/api.md) |
| Flussi principali | [docs/flows.md](docs/flows.md) |
| Sviluppo locale | [docs/local-development.md](docs/local-development.md) |
| Test e snapshot | [docs/testing.md](docs/testing.md) |
| Variabili env, servizi esterni e logging | [docs/configuration.md](docs/configuration.md) |
| Schema, RLS, Storage e Analytics B2B | [docs/data-model.md](docs/data-model.md) |
| Scheduled jobs e cleanup | [docs/jobs.md](docs/jobs.md) |
| Deploy produzione backend | [docs/deploy-production.md](docs/deploy-production.md) |
| Deploy Render locale senza GitHub Actions | [docs/deploy-render-local.md](docs/deploy-render-local.md) |
| Assessment sicurezza e follow-up staging | [docs/security-assessment-2026-07-29.md](docs/security-assessment-2026-07-29.md) |

Use the workspace guide [../docs/deploy-production-guide.md](../docs/deploy-production-guide.md) only when coordinating multiple systems together: backend, frontend, DNS, Supabase, mobile apps, stores, and UAT.

## Common Commands

```bash
.venv/bin/python -m pytest tests -v --ignore=tests/integration --ignore=tests/performance
.venv/bin/python -m pytest tests/integration -v
RUN_PERFORMANCE_TESTS=1 .venv/bin/python -m pytest tests/performance -v -s
.venv/bin/python -m scripts.seed_admin --check
```

## Reset iniziale modello solo offerte

La migrazione `20260724000000_offer_only_reset.sql` è intenzionalmente distruttiva: elimina volantini, offerte, catalogo e preferiti, mantenendo account, supermercati, avatar, loghi e liste normalizzate come voci manuali. Prima applicare la migrazione, eseguire una sola volta il comando seguente con credenziali service-role: svuota esclusivamente i bucket `flyers` e `product-images`, verifica che siano vuoti e non tocca `avatars` o `logos`.

```bash
.venv/bin/python -m scripts.reset_offer_only_storage --confirm-offer-only-reset
```

## Project Map

```text
girospesa-backend/
├── main.py
├── api/routers/
├── core/
├── services/
├── scripts/
├── supabase/migrations/
├── tests/
└── docs/
```
