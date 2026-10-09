# JanNetra — Social Sentiment Watchtower

FastAPI + React/TypeScript + Bootstrap CSS + Poppins (Google Fonts) + Font Awesome + Recharts, MongoDB/Motor + Beanie, a local Hugging Face multilingual transformer, and APScheduler. The dashboard monitors aggregate conversation about Jan Suraaj Party and Prashant Kishore. It does not infer voting intentions or profile people. The product does not assert an elected office or title for any tracked entity.

## Start JanNetra

Install Docker Desktop / Docker Engine with Compose **2.24+**. Copy `.env.example` to `.env`, set the required database, security, administrator, YouTube and classifier credentials, then run:

```sh
docker compose up --build
```

Open http://localhost:8080 and sign in with the administrator email and password you configured. The login form never exposes or prefills credentials. Compose defaults to live mode (`SEED_MOCK_DATA=false`).

The API and MongoDB have no published host ports. The frontend publishes `PUBLIC_PORT` (8080 by default) on all host interfaces and securely proxies `/api/*` to FastAPI over the private Docker network. MongoDB uses a persistent volume and an internal network. Its bundled development instance has no authentication; use an authenticated Atlas cluster or secured MongoDB deployment for production.

## Local development without Docker

Python 3.11+ and Node 22+ are required. Use two terminals. From `backend`:

```sh
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

From `frontend`:

```sh
npm ci
npm run dev
```

Open http://localhost:5173. Vite proxies `/api` to port 8000.

## Configuration

A local `.env` is provided beside `docker-compose.yml`. It is ignored by Git and excluded from the downloadable source ZIP. A fresh checkout can copy `.env.example` to `.env`. Never overwrite an existing encryption key. The API reads the root `.env` and then optional `backend/.env`; shell variables take precedence.

1. Set `MONGODB_URI` to an authenticated connection, preferably Atlas with TLS and a restricted network allowlist.
2. Generate two independent secrets:

   ```sh
   python -c "import secrets; print(secrets.token_urlsafe(48))"
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

   Set the first as `JWT_SECRET`, the second as `ENCRYPTION_KEY`. Keep the encryption key stable across restarts. Rotating it requires decrypting and re-encrypting stored secrets under maintenance; merely changing it makes existing credentials unreadable.
3. For the first startup only, set `BOOTSTRAP_EMAIL` and a unique `BOOTSTRAP_PASSWORD` of at least 14 characters (bcrypt maximum 72 UTF-8 bytes). After the administrator exists in MongoDB, remove both variables. Changing them does not reset an existing password.
4. Configure platform keys and restart, or enter them through **Settings → API connections**. A saved key is **pending** until a successful sync. No secret is returned by the settings API.
5. Run **Refresh**, inspect source health, and compare a sample of classifications against source material. A source failure preserves its last successful checkpoint and displays a partial-data warning.

### Environment variables

| Variable | Purpose |
|---|---|
| `MONGODB_URI`, `MONGODB_DATABASE` | Database connection and database name |
| `JWT_SECRET`, `ENCRYPTION_KEY` | JWT signing and Fernet credential encryption; required in live mode |
| `BOOTSTRAP_EMAIL`, `BOOTSTRAP_PASSWORD` | Optional one-time initial administrator; required only when the users collection is empty |
| `HF_MODEL`, `HF_REVISION` | Model repository and pinned commit; default Cardiff NLP XLM-R sentiment |
| `HF_TOKEN` | Optional Hub download token; not required for the public default model |
| `HF_CACHE_DIR` | Persistent downloaded model cache; Compose supplies a named volume |
| `HF_DEVICE` | `cpu` (default), `cuda`, or `mps`; GPU runtime must be installed separately |
| `HF_BATCH_SIZE`, `HF_MAX_LENGTH` | Default 16 posts, 256 tokens per post; long posts are flagged as truncated |
| `HF_CPU_THREADS` | Default 2 CPU threads; inference runs in a background thread |
| `HF_LOCAL_FILES_ONLY` | Offline cache-only loading after downloading weights; default false |
| `GEMINI_API_KEY`, `GEMINI_MODEL` | Target-aware verifier key and model; default `gemini-3.5-flash-lite` |
| `HF_CONFIDENCE_THRESHOLD`, `GEMINI_CONCURRENCY` | Gemini gate defaults to 0.80; 2 concurrent calls |
| `ENABLED_PLATFORMS` | Comma-separated sources: `facebook,instagram,x,youtube,news,reddit`; set `youtube` for YouTube-only operation |
| `YOUTUBE_API_KEY`, `YOUTUBE_BACKUP_API_KEY` | Primary YouTube Data API v3 key and optional quota-fallback key |
| `YOUTUBE_INITIAL_LOOKBACK_DAYS` | First-run YouTube backfill window (default 7 days) |
| `APIFY_API_TOKEN` | Shared by the Facebook, Instagram, X and Reddit connectors; News does not use Apify |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Enable one Telegram message, including the source link, for each newly classified negative post |
| `ALLOWED_ORIGINS` | Exact CORS origins for the UI |
| `REPORTING_TIMEZONE` | `Asia/Kolkata` by default |
| `SCHEDULER_ENABLED` | Enables server-side automation; no admin session or open browser is required |
| `YOUTUBE_SYNC_INTERVAL_MINUTES` | Fixed YouTube/Google News 15-minute schedule; social Apify sources run every 4 hours |

### Hugging Face multilingual classifier

The primary classifier is `cardiffnlp/twitter-xlm-roberta-base-sentiment`, run locally with Transformers/PyTorch. Its [official model card](https://huggingface.co/cardiffnlp/twitter-xlm-roberta-base-sentiment) lists Hindi and English among eight sentiment fine-tuning languages. **Hinglish, sarcasm and Bihar political discourse have not been validated here.** It predicts overall text tone, not entity-targeted stance. A negative news report can concern an issue rather than the tracked party. Validate on human-labeled examples before relying on political alerts; model probability is not calibrated accuracy.

Hugging Face runs locally. YouTube classification uses the video title only. Each title passes through language detection, tracked-entity detection and Cardiff multilingual sentiment. Results below `HF_CONFIDENCE_THRESHOLD` (default 0.80) go to Gemini, as do apparently confident results whose negative language may target an opponent, quoted speaker or unrelated event. Gemini measures stance specifically toward Jan Suraaj and Prashant Kishore and handles Hindi, English, Hinglish and sarcasm. Mixed requires meaningful positive and negative stance toward a tracked target. `GEMINI_API_KEY` is required for verification. Missing keys/provider failures leave these items pending and excluded from final totals. Strict JSON schema output, result ID/count validation, per-item retry on malformed batches, exponential backoff and bounded concurrency are implemented. First local inference downloads about 1.1 GB of model weights. The model is cached once per API process, and serialized inference runs off the async event loop. Start deployment planning around 3 GB RAM for the API and benchmark on the actual host; CPU works and GPU is optional.

Only unclassified live posts are processed. Model upgrades do not silently requeue historical records; any deliberate reclassification must be run as a separate maintenance operation. Failed inference leaves posts pending and exposes `unavailable`; the next sync retries. Before first inference the status is `pending`. Model output count, named labels and probabilities are validated. Gemini results below the threshold and truncated HF inputs are flagged for review. Confidence is not calibrated accuracy; stored records include raw HF confidence, language, targets and final model provenance.

To change models, set `HF_MODEL` with its matching `HF_REVISION`, or set `GEMINI_MODEL`, then restart. Only sequence classifiers with exactly negative/neutral/positive named labels are accepted; unknown `LABEL_0` mappings fail closed. Remote repository code is disabled. The pinned official checkpoint uses restricted `weights_only=True` loading. Enable `HF_LOCAL_FILES_ONLY=true` after caching for offline HF operation; Gemini verification still requires network access. Keep one API worker with the built-in scheduler.

To download the model and run an explicit smoke check, from `backend` run:

```sh
python -m app.check_classifier
```

This uses built-in sample text only. It does not write posts, connect to social platforms or send alerts. It is a smoke test, not an accuracy benchmark.

## Connector coverage and limitations

| Platform | Implemented connector | Required access / coverage |
|---|---|---|
| X | `apidojo/tweet-scraper` primary with a charge-bounded `apidojo/twitter-scraper-lite` fallback, normalized output, overlap deduplication and retry/backoff | Apify token and Actor access. The primary avoids the Lite Actor's high per-query FREE-tier price. |
| Reddit | `fatihtahta/reddit-scraper-search-fast`, post-only keyword search, newest-first date filtering | Apify token. Titles and post bodies are stored; comments and NSFW posts are excluded. |
| YouTube | Official Data API v3 video search + `videos.list` snippet/statistics | Every 15 minutes inside the admin-configured active window, one combined search covers all tracked terms and includes regular videos and Shorts. `videos.list` supplies duration and engagement metadata. If the primary key reports quota exhaustion, the connector switches to `YOUTUBE_BACKUP_API_KEY` for the rest of that run. The initial strategy upgrade backfills 7 days. Only the video title is matched, stored, and classified. Descriptions and comments are excluded. |
| Facebook | Configured Apify Actor | Actor access and a compatible output schema. |
| Instagram | Configured Apify Actor | Actor access and a compatible output schema. |
| News | Google News RSS search feeds | Credential-free RSS ingestion runs every 15 minutes inside the admin-configured active window. Approved Bihar coverage includes News18, Zee Bihar, ABP Bihar, News State, Sahara Samay, Bihar Tak, First Bihar, Live Cities, News4Nation, Hindustani Media, Dainik Jagran Bihar, TV9 Bihar/Jharkhand, Dainik Bhaskar Bihar, Prabhat Khabar, Live Hindustan Bihar, ETV Bharat Bihar and Navbharat Times Bihar. The headline is classified and the Google News article link is retained. |

There is no manual upload/CSV/JSON import endpoint or UI; CSV is export only. Missing Actor credentials show **Not connected**, never fabricated zeros. An unavailable source remains identifiable and its last observed figures are marked partial/stale. Zero engagement metrics may represent unavailable fields; ?interactions? is likes + comments + shares, not views.

YouTube Search API is relevance-ranked and does not promise an exhaustive list of every matching upload. JanNetra searches public videos across channels; it cannot use a person's YouTube watch history. One combined search per run keeps the normal 06:00-22:00 schedule near 65 search calls per day; manual refreshes add calls. YouTube quota is allocated per Google Cloud project, so use a backup key from another properly configured project if independent fallback capacity is required. Short keywords such as PK are noisy; deactivate them in Settings if appropriate.

### Apify Actor contract

`APIFY_API_TOKEN` is the only Apify environment value. JanNetra fixes the social Actors to `apify/facebook-posts-scraper`, `apify/instagram-scraper`, `apidojo/tweet-scraper` (with a bounded `apidojo/twitter-scraper-lite` fallback), and `fatihtahta/reddit-scraper-search-fast`. Because Facebook's Posts Actor requires page URLs, JanNetra first discovers relevant public pages through `apify/facebook-search-scraper`, then fetches their latest posts. Reddit collection stores posts only and does not request comments. News is fetched separately from Google News RSS and requires no API token. Social connectors use bearer authentication, bounded synchronous runs, keyword filtering and retry/backoff. Restart the API container after rotating `APIFY_API_TOKEN`; startup replaces all encrypted social credentials with the new environment token and clears stale provider errors.

Actor IDs and JSON templates remain optional advanced overrides because Store Actors and their schemas can change independently of JanNetra. Output normalization accepts common IDs, post text/caption/title, publication timestamps, URLs, authors and engagement counts. Items without a usable timestamp or tracked term are excluded.

## Data flow, reporting and alerts

```text
APScheduler → connected API adapters → deduplicated raw posts
                                      ↓ only unclassified posts
                                   Hugging Face local transformer → validated sentiment
                                      ↓
MongoDB $match → $group with $dateTrunc(timezone) → daily aggregates
                                      ↓
Unique daily alert → dashboard; per-post negative → Telegram
```

Daily counts include classified posts from configured platforms only. `negativity_index = negative / classified × 100`; pending classifications are displayed separately and not counted as neutral. A reporting day is midnight-to-midnight in Asia/Kolkata, including the correct UTC boundaries. Aggregation recomputes the most recent 35 days to incorporate late-arriving posts. The dashboard shows 30 days. Weekly/monthly exports mean rolling 7/30 reporting days, including today; they do not mean calendar weeks/months.

Alert creation is idempotent on a unique `date` index and fires strictly above the internal 500-negative-post daily threshold. Evidence contains the five negative posts with highest likes + replies/comments + shares, and per-platform negative counts. Repeated syncs refresh counts/evidence without reopening resolved alerts. Resolving an alert acknowledges that reporting day; it will not create a second alert for the day. Past days can trigger if late data crosses the threshold.

Per-post Telegram alerts are enabled when both `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set. Each newly classified negative post is queued once and sent with its platform, author, confidence, excerpt and original link. Successful posts are marked sent; failed deliveries retry on a later sync. Existing historical negatives are not backfilled automatically, preventing a notification flood when Telegram is first enabled.

The scheduler runs inside the API process without a logged-in admin. YouTube and Google News RSS run every 15 minutes; Facebook, Instagram and X run every four hours. The initial active window is 06:00-22:00 IST and an admin can change it in Settings. Saving immediately replaces the scheduler jobs and also changes the Telegram-delivery window. Classification follows ingestion, and each newly classified negative post is handed to Telegram after its batch. Only posts published today in Asia/Kolkata are alerted; older queued posts are marked skipped.

## Security and deployment

- OAuth2 password form login issues a 60-minute JWT, validates audience/issuer/algorithm and loads the current user's role on every request. Tokens remain in browser memory, not local storage. Reloading signs out.
- Passwords use bcrypt. Login attempt buckets are stored in MongoDB with TTL; configure trusted proxy/rate limiting at the public edge. The bundled API ignores untrusted forwarded headers.
- Admins can change configuration, credentials, users, and alert resolution. Viewers can read and export. Create viewer accounts with authenticated `POST /api/users` (OpenAPI at `/docs` when accessing the API directly).
- Clicking the navbar account opens the authenticated profile page. Name and email changes are stored in MongoDB; password changes require the current password and are re-hashed with bcrypt.
- Platform credentials are encrypted with Fernet. API responses never disclose ciphertext or raw secrets. Audit logs record logins, views, exports, profile changes and administrative changes; TTL is 90 days.
- Custom outbound endpoints require an environment-controlled exact hostname allowlist and public DNS resolution, checked at configuration and delivery. Redirects are disabled. Also enforce network egress rules in production to prevent DNS-rebinding and cloud-metadata access.
- Beanie models use `schema_version=1`; startup initializes all specified indexes. Native Motor updates implement idempotent upserts and aggregation. Future breaking schemas require a reviewed, versioned data migration before startup; no SQL or Alembic is used.
- Run **one API worker / one scheduler instance** with this APScheduler setup. For horizontal scaling, move ingestion/classification to dedicated workers with durable queue leases (arq/Celery) and disable the API scheduler. In-process scheduling is not a distributed queue.
- Deploy the API Dockerfile and frontend Dockerfile on a container host; provision Atlas, set private networking/service discovery, and point the frontend proxy to the API service. Terminate TLS at the edge, update CORS, keep database ports private, configure secrets, backups and retention/deletion policy. No hosting account has been provisioned by this project.
- Motor/Beanie 1.x are deliberately pinned to honor the requested stack. Motor is a legacy driver; assess its maintenance status and plan a Beanie/PyMongo Async migration before long-term production operation.

## Verification

```sh
cd backend
python -m pytest -q
cd ../frontend
npm ci
npm run build
```

Tests cover IST boundaries, strict threshold semantics, daily alert deduplication, Google News RSS parsing, encrypted secrets, profile/password updates, batch-length fallback, invalid confidence, Hindi/PK matching, X token failover without quota evasion, authentication, viewer restrictions, filtering/sorting and exports. See `VERIFICATION.md` for checks actually performed in the build environment and checks still requiring real infrastructure/credentials.

Official references: [Hugging Face model card](https://huggingface.co/cardiffnlp/twitter-xlm-roberta-base-sentiment), [YouTube API](https://developers.google.com/youtube/v3/docs), [YouTube full video snippets](https://developers.google.com/youtube/v3/docs/videos/list), [Beanie documentation](https://beanie-odm.dev/).

### UI assets

Bootstrap CSS and Font Awesome SVG icons are bundled locally. Poppins loads from Google Fonts, with a system-font fallback when Google Fonts is unavailable. Production CSP allows only the required Google Fonts stylesheet/font hosts. No Tailwind runtime or build plugin is used.

## Local verification (2026-10-01)

See `MODEL-VALIDATION.md` for real model and YouTube results, including a high-confidence target-stance error. The current local preview uses port 5174 with `API_PROXY_TARGET=http://127.0.0.1:8001`; the API uses port 8001 because an existing Windows process occupies 8000. Default Docker ports are unchanged. Run only one scheduler instance against a database.
