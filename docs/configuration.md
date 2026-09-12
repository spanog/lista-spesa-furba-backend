# Configuration

## Environment Variables

```bash
# ── Local dev obbligatorio per backend boot ----------------------------------
SUPABASE_URL=http://127.0.0.1:54321
SUPABASE_SECRET_KEY=<local-secret-key>
FRONTEND_URL=http://localhost:3000
# `127.0.0.1:3000` resta supportato in sviluppo per compatibilita' loopback
CORS_EXTRA_ORIGINS=

# ── Gemini extraction (solo se usi estrazione AI) ---------------------------
LLM_PROVIDER=gemini
GOOGLE_API_KEY=<google-api-key>
GEMINI_MODEL=gemma-4-31b-it

# ── Servizi esterni opzionali in locale -------------------------------------
WEBMASTER_EMAIL=webmaster@example.com
MAIL_FROM=no-reply@girospesa.local
SMTP_HOST=localhost
SMTP_PORT=1025
SMTP_USERNAME=
SMTP_PASSWORD=
SMTP_USE_TLS=false
SMTP_USE_SSL=false

# ── Esempio produzione attuale (Brevo) --------------------------------------
# MAIL_FROM=info@girospesa.it
# WEBMASTER_EMAIL=info@girospesa.it
# SMTP_HOST=smtp-relay.brevo.com
# SMTP_PORT=2525
# SMTP_USERNAME=<brevo-smtp-access>
# SMTP_PASSWORD=<brevo-smtp-key>
# SMTP_USE_TLS=false
# SMTP_USE_SSL=false

# ── Web Push / native push ---------------------------------------------------
VAPID_PRIVATE_KEY=
VAPID_PUBLIC_KEY=
VAPID_MAILTO=mailto:info@girospesa.it
FCM_ENABLED=false
FCM_PROJECT_ID=
FCM_CLIENT_EMAIL=
FCM_PRIVATE_KEY=

# ── Copia da `supabase status -o env` ---------------------------------------
# SUPABASE_SECRET_KEY <- SERVICE_ROLE_KEY
```

Compatibilita' deploy:

- il backend usa come nome canonico `SUPABASE_SECRET_KEY`
- per retrocompatibilita' accetta anche `SUPABASE_SERVICE_ROLE_KEY`, utile se qualche provider o vecchio `.env` usa ancora quel nome
- il backend supporta sia le chiavi legacy JWT (`service_role`) sia le nuove chiavi opache `sb_secret_...`

```bash
# ── Admin seed condiviso -----------------------------------------------------
ADMIN_EMAIL=admin@example.com
ADMIN_PASSWORD=change-me
```

Seed admin da eseguire dopo setup locale o in deploy:

- `.venv/bin/python -m scripts.seed_admin`
- `.venv/bin/python -m scripts.seed_admin --check`

Flow identica in locale, test, prod: cambia solo valore env.

---

## External Services

| Servizio | Scopo | Configurazione | Note |
|----------|-------|----------------|------|
| **Google Gemini** | Estrazione AI volantini | `GOOGLE_API_KEY` + `GEMINI_MODEL` | Unica dipendenza esterna richiesta quando usi AI extraction |
| **SMTP provider** | Email transazionali / contatto pubblico | `MAIL_FROM` + `SMTP_*` | Backend runtime attuale usa SMTP diretto via `smtplib`; in produzione GiroSpesa usa `Brevo` come relay SMTP e `Aruba` solo per ricezione mailbox |
| **Web Push (VAPID)** | Notifiche browser | Coppia VAPID | Standard W3C, nessun servizio proprietario |

### Retry policy Gemini

- I chunk PDF Gemini usano `MAX_RETRIES = 3`.
- I PDF nuovi vengono inviati in chunk da 2 pagine, quindi la dimensione complessiva del PDF non usa il limite di 20 MB previsto per le immagini inviate inline. Ogni richiesta Gemini ha una deadline hard di 8 minuti: gira in un sottoprocesso terminabile, quindi anche una connessione TLS viva ma senza risposta viene chiusa e passa nel retry con backoff invece di lasciare l'estrazione indefinitamente in `processing`. Una ripresa conserva invece la dimensione chunk già checkpointata.
- Errori provider `503/UNAVAILABLE` usano backoff esponenziale lungo con jitter.
- Errori transient server-side `500/502/504` e `INTERNAL` usano backoff esponenziale dedicato con jitter, per evitare tre retry troppo ravvicinati quando il provider e' in stato instabile.
- Se i retry si esauriscono, il flyer passa a `error` con checkpoint di resume sul chunk fallito.

---

## Logging

- In locale e ambienti non-production, il backend logga a livello `INFO`.
- In produzione (`ENVIRONMENT=production`), il root logger sale a `WARNING`, mentre `uvicorn.access` viene ridotto per evitare rumore nei log applicativi.
- Il middleware di timing aggiunge sempre `X-Request-ID` e `Server-Timing: app;dur=...` alla risposta. Logga solo richieste oltre 1000 ms con route parametrica, status, durata, dimensione dichiarata e request-id; non registra query string, JWT, email, coordinate o URL Storage.
- Gli errori dei contact form (`POST /contact-requests`) vengono loggati esplicitamente come `warning` per misconfigurazioni e `exception` per failure SMTP, cosi' i log `Render` mostrano la causa reale del `500`.

---
