# py_wallet

FastAPI service for aggregating and monitoring a crypto portfolio.

The app collects balances from EVM networks, calculates USD values, and stores
wallet snapshots in PostgreSQL. Supported EVM networks are Ethereum Mainnet,
Base, BNB Chain, Arbitrum, and Linea.

The application repository contains the service code, database migrations,
tests, Docker image, and CI pipeline. Kubernetes manifests are maintained in the
separate `amysyutin/py_wallet-infra` repository.

## Features

- EVM portfolio aggregation across multiple chains.
- Native token, USDT, and USDC balance tracking.
- USD valuation through RPC token balances and CoinGecko native token prices.
- PostgreSQL persistence with Alembic migrations.
- User registration and JWT authentication.
- Telegram Mini App authentication, email account linking, and opt-in daily balance messages.
- Per-user wallets, balance snapshots, portfolio history, and portfolio summary.
- Local development through Docker Compose.
- GitHub Actions checks, Docker image publishing to GHCR, and GitOps image tag updates.

## Project Structure

```text
py_wallet/
├── app/
│   ├── main.py                       # FastAPI entry point
│   ├── config.py                     # EVM and token configuration
│   ├── core/
│   │   ├── config.py                 # Database and JWT settings
│   │   └── security.py               # Password hashing and JWT helpers
│   ├── db/
│   │   ├── models/                   # SQLAlchemy models
│   │   └── session.py                # Async database sessions
│   ├── routers/
│   │   ├── auth.py                   # Registration, login, current user
│   │   ├── wallet_groups.py          # Wallet group CRUD
│   │   ├── wallets.py                # User wallet management
│   │   ├── snapshots.py              # Balance snapshot creation
│   │   └── portfolio.py              # Portfolio history and summary
│   ├── connectors/                   # RPC, ERC-20, and CoinGecko clients
│   ├── services/                     # Portfolio, snapshot, and admin promote logic
│   ├── demo/                         # Public demo payloads (no external API calls)
│   ├── cli/                          # Operational CLI (promote-admin)
│   └── routes.py                     # Health and legacy aggregation endpoints
├── alembic/                          # Database migrations
├── tests/
├── .github/workflows/
│   ├── ci.yml                        # Pull request checks
│   └── main-build.yml                # Main branch build, publish, GitOps update
├── Dockerfile
├── docker-compose.yml                # Local app and PostgreSQL stack
├── docker-compose.ci.yml             # CI smoke-test stack
├── requirements.txt
├── requirements-dev.txt
├── .env.example
└── pytest.ini
```

## API

Public endpoints:

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/` | Basic availability check. Returns `{"status": "ok"}`. |
| `GET` | `/health` | Database health check. Includes non-sensitive application version and build SHA. |
| `POST` | `/auth/register` | Create a user account. |
| `POST` | `/auth/login` | Return a JWT bearer token. |
| `POST` | `/auth/telegram` | Validate Telegram Mini App `initData` and return a JWT. |
| `POST` | `/telegram/webhook` | Telegram-authenticated webhook; `/start` returns an app description and Mini App button. |
| `GET` | `/assets?address=0x...` | EVM portfolio summary for the provided address. |

Authenticated endpoints require `Authorization: Bearer <token>`:

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/auth/me` | Return the authenticated user (includes `role`). |
| `POST` | `/auth/telegram/link-email` | Link a new Telegram-only identity to an existing email account. |
| `GET` | `/telegram/settings` | Read Telegram daily balance settings. |
| `PATCH` | `/telegram/settings` | Explicitly opt in/out and set IANA timezone, local send time, and `ru`/`en`. |
| `POST` | `/wallet-groups` | Create a wallet group (`name`, optional `description`, `sort_order`). |
| `GET` | `/wallet-groups` | List the authenticated user's wallet groups. |
| `GET` | `/wallet-groups/{id}` | Get a wallet group by ID. |
| `PATCH` | `/wallet-groups/{id}` | Update a wallet group. |
| `DELETE` | `/wallet-groups/{id}` | Delete a wallet group (wallets keep `group_id = NULL`). |
| `POST` | `/wallets` | Add a wallet (`wallet_type`: `evm` or `manual`; EVM requires only `address` and defaults to `chain_type=all`; manual requires `chain_type=manual`, no address). |
| `GET` | `/wallets` | List active wallets (`?active_only=false` for all). |
| `GET` | `/wallets/{id}` | Get a wallet by ID. |
| `GET` | `/wallets/{id}/summary` | Return the saved wallet value/assets plus scoped freshness, source, price quality, active-refresh state, and safe affected-network details. |
| `GET` | `/wallets/{id}/assets` | Latest persisted EVM portfolio, with a guarded live fallback when no current snapshot exists. |
| `GET` | `/wallets/{id}/snapshots` | List recent persisted snapshots for one wallet. |
| `POST` | `/wallets/{id}/snapshots` | Request an explicit snapshot refresh for one active wallet. |
| `PATCH` | `/wallets/{id}` | Update wallet fields (`label`, `group_id`, `is_active`, `notes`; EVM wallets also `chain_type` and `address`). |
| `DELETE` | `/wallets/{id}` | Soft-delete a wallet (`is_active=false`). |
| `GET` | `/wallets/{id}/balances` | List manual balances for a wallet (`total_usd`, per-asset `value_usd`). |
| `PUT` | `/wallets/{id}/balances` | Upsert manual balances (manual wallets only). |
| `DELETE` | `/wallets/{id}/balances/{asset_id}` | Delete one manual balance row. |
| `POST` | `/snapshots` | Request an owner-scoped `all`, `group`, or `wallet` refresh. Returns the active job with `reused=true` instead of duplicating work for the same scope. |
| `GET` | `/snapshot-jobs/{id}` | Read owner-scoped refresh progress and terminal status. |
| `POST` | `/snapshot-jobs/{id}/retry-failed` | Retry only failed chains from an owner-scoped terminal job; reuses an active child retry. |
| `POST` | `/snapshot` | Create a snapshot for one active wallet or all active EVM wallets; EVM snapshots aggregate the wallet address across all supported EVM chains. |
| `GET` | `/portfolio?wallet_id=<id>&days=30` | Return snapshot history for a wallet (`total_usd` from multi-chain EVM snapshots). |
| `GET` | `/portfolio/history?group_id=<id>&days=30` | Return history with on-chain, CEX, and manual source totals; CEX is included only in the unfiltered all-wallet scope. |
| `GET` | `/portfolio/allocation` | Return current allocation; `mode=all` includes exchange assets, while group selections remain wallet-only. |
| `GET` | `/portfolio/summary` | Return total USD value, top assets, per-source totals, and wallet/exchange health from the latest snapshots. |

New EVM wallets use `chain_type=all`: every snapshot checks the address across
all EVM networks enabled in the snapshot service. Legacy per-network values
(`mainnet`, `base`, `bnb`, `arbitrum`, and `linea`) remain accepted for API
compatibility but do not limit snapshot collection. Manual wallets use
`chain_type=manual`.

Manual balance rows with `price_usd=null` are priced by ticker during the next
snapshot: supported crypto symbols use CoinGecko, while three-letter ISO 4217
fiat tickers use the latest Frankfurter `<TICKER>/USD` rate. A supplied
`price_usd` remains an explicit manual override. Portfolio price quality reports
Frankfurter separately from manual and CoinGecko prices.

Release metadata can be supplied with `APP_VERSION` (defaults to `0.2.0`) and
`BUILD_SHA` (defaults to `unknown`). These non-sensitive values are returned by
`/health`, `/health/live`, and `/health/ready`; `APP_VERSION` is also used for
the OpenAPI `info.version`. Do not put credentials or tokens in these variables.

## Releases

`VERSION` is the source of truth for the component version. Immutable `vX.Y.Z`
tags trigger the release workflow, which verifies the tag, promotes the already
tested `:<commit-sha>` image to `:vX.Y.Z`, and creates a GitHub Release. Product
releases and their exact component SHAs are recorded in `py_wallet-infra`.

## Product metrics

In addition to automatic HTTP RED metrics, `/metrics` exposes:

- `py_wallet_build_info`
- `py_wallet_snapshot_service_client_requests_total`
- `py_wallet_snapshot_service_client_request_duration_seconds`
- `py_wallet_snapshot_job_create_total`
- `py_wallet_registration_completed_total{channel}`
- `py_wallet_first_wallet_added_total{channel,wallet_type}`
- `py_wallet_manual_refresh_total{channel,scope,outcome}`
- `py_wallet_failed_chain_retry_total{channel,outcome}`
- `py_wallet_snapshot_scheduler_last_tick_timestamp_seconds`
- `py_wallet_snapshot_scheduler_ticks_total`
- `py_wallet_snapshot_scheduler_jobs_total`
- `py_wallet_snapshot_scheduler_active_users`
- `py_wallet_wallet_balance_source_observations_total`
- `py_wallet_wallet_snapshot_freshness_seconds`

Labels are restricted to bounded operation, scope, outcome, trigger, source,
channel (`web` or `telegram`), wallet type, version, SHA, and environment
values. Registration and first-wallet attribution are derived server-side.
Manual refresh accepts only the bounded `X-Client-Channel` values `web` and
`telegram`; missing or unexpected values collapse to `web`.
User IDs, wallet IDs, addresses, job IDs, RPC URLs, and error messages are never
exported as Prometheus labels.

For existing EVM wallets, `PATCH /wallets/{id}` accepts `chain_type` and
`address` (together or separately). The UI always saves EVM wallets with
`chain_type=all`. `wallet_type` cannot be changed after creation; manual wallets
cannot switch to an on-chain network.

`GET /wallets/{id}/assets` returns the latest readable snapshot for the current
wallet state. If no snapshot exists after the wallet's most recent update, it
falls back to the same cached and capacity-limited live lookup used by public
`GET /assets`. The response keeps the same USD total and per-chain shape.
Use `POST /wallets/{id}/snapshots` to request an explicit refresh.

`GET /wallets/{id}/summary` is the wallet-detail source of truth for the saved
value. Its `data_health` uses the same configurable freshness thresholds as the
portfolio, distinguishes `latest_snapshot|manual|none`, reports an active
wallet/group/portfolio refresh, and exposes only bounded chain status/error
categories. Provider messages and endpoints are never returned. A frontend
live check is diagnostic and must not replace this persisted total or history.

When `EXCHANGE_INTERNAL_API_TOKEN` is configured, `/portfolio/summary` reads the
current user's latest successful Binance snapshot from `py_wallet-exchange-service`.
Supported symbols are valued through cached CoinGecko prices and appear under
`source=exchange`; unsupported or temporarily unpriced symbols remain visible
and make exchange health `partial`. Timeouts, missing snapshots, and invalid
service responses do not fail the portfolio request: wallet totals remain
available and exchange health becomes `unavailable` with a bounded error type.
The all-scope allocation includes exchange assets with keys such as
`exchange:binance:BTC`. Unfiltered portfolio history merges fully valued CEX
snapshots into the wallet timeline and returns `sources.onchain_usd`,
`sources.cex_usd`, and `sources.manual_usd` for every point. Historical CEX
balances use the valuation stored when the exchange snapshot was collected;
legacy or partially unpriced CEX snapshots are omitted rather than repriced with
today's quote. Wallet and group history remain wallet-only. The separate 24-hour
change contract stays unavailable while current exchange assets are present.

`POST /snapshot` uses the same multi-chain EVM scope for stored history, so
`GET /portfolio?wallet_id=<id>&days=30` can power a chart of the wallet's
total USD value across all EVM networks over time. Group and all-wallet history
carry forward each wallet's latest value and emit the aggregate after every
snapshot event.

If live RPC balances are available but a native-token price cannot be resolved,
the chain is returned as `partial_success` with
`error_type=native_price_unavailable`; stablecoin and priced-token values remain
in `total_usd` instead of presenting the chain as fully valued.

```bash
# Latest total across all EVM chains for a wallet
curl http://localhost:8000/wallets/1/assets \
  -H "Authorization: Bearer $TOKEN"

# Change the address on an EVM wallet (all enabled networks are scanned)
curl -X PATCH http://localhost:8000/wallets/1 \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"chain_type":"all","address":"0x..."}'
```

### Manual wallet example

```bash
# Create a manual wallet (no on-chain address)
curl -X POST http://localhost:8000/wallets \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"label":"Manual BTC","wallet_type":"manual","chain_type":"manual"}'

# Add or update balances (blank price asks the snapshot worker for a live ticker price)
curl -X PUT http://localhost:8000/wallets/1/balances \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"balances":[{"symbol":"BTC","amount":"0.125","price_usd":"68000"}]}'

# Fiat ticker with a live EUR/USD rate on the next snapshot
curl -X PUT http://localhost:8000/wallets/1/balances \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"balances":[{"symbol":"EUR","amount":"250","price_usd":null}]}'

# List balances and total_usd
curl http://localhost:8000/wallets/1/balances \
  -H "Authorization: Bearer $TOKEN"

# Delete one balance by asset_id
curl -X DELETE http://localhost:8000/wallets/1/balances/1 \
  -H "Authorization: Bearer $TOKEN"
```

If `/assets` is called without the `address` query parameter, the app uses
`EVM1_ADDRESS` from the environment. The public endpoint validates EVM addresses,
caches each result briefly, and limits concurrent live lookups. When lookup
capacity is exhausted it returns `429` with a `Retry-After` header.

### Admin access

New registrations always receive `role=user`. To grant admin after a user
registers:

```bash
python -m app.cli promote-admin user@example.com
```

Exit codes: `0` promoted, `1` user not found, `2` already admin.

Manual SQL fallback:

```sql
UPDATE users SET role = 'admin' WHERE email = 'user@example.com';
```

Interactive API documentation:

- Swagger UI: `/docs`
- ReDoc: `/redoc`

## Environment Variables

Copy the example file before starting the Docker Compose stack:

```bash
cp .env.example .env
```

| Variable | Required | Description |
| --- | --- | --- |
| `APP_ENV` | No | Application environment: `development`, `test`, `staging`, or `production`. Defaults to `development`. `ci` is treated as `test`. |
| `DATABASE_URL` | No | Async PostgreSQL URL. Defaults to `postgresql+asyncpg://wallet:wallet@localhost:5432/wallet`; Docker Compose sets the container URL. |
| `JWT_SECRET` | Staging/Production | Secret used to sign access tokens. Required in staging/production (minimum 32 characters). |
| `JWT_ALG` | No | JWT algorithm. Only `HS256` is allowed. Defaults to `HS256`. |
| `ACCESS_TOKEN_TTL_MIN` | No | Access token lifetime in minutes. Defaults to `60`. |
| `SNAPSHOT_SCHEMA_REQUIRED` | No | Require snapshot-service tables in `/health/ready`; defaults to `true`. It may be `false` only in development/test isolation and is rejected in staging/production. |
| `EXCHANGE_SERVICE_URL` | No | Internal exchange-service URL; defaults to `http://localhost:8002`. |
| `EXCHANGE_INTERNAL_API_TOKEN` | To enable exchange aggregation | Shared internal API token. When empty, exchange aggregation is disabled and does not degrade portfolio health. |
| `EXCHANGE_SERVICE_TIMEOUT_SECONDS` | No | Timeout for the exchange-service read; defaults to `5`. |
| `PORTFOLIO_FRESH_SECONDS` | No | Maximum age in seconds classified as fresh in the portfolio data-health contract; defaults to `900`. |
| `PORTFOLIO_STALE_SECONDS` | No | Age in seconds after which portfolio snapshot data is stale; defaults to `1800` and must be greater than `PORTFOLIO_FRESH_SECONDS`. |
| `EVM1_ADDRESS` | For default `/assets` address | Default EVM wallet address. |
| `RPC_URL_MAINNET` | For Mainnet aggregation | Comma-separated Ethereum Mainnet RPC URLs. |
| `RPC_URL_BASE` | For Base aggregation | Comma-separated Base RPC URLs. |
| `RPC_URL_BNB` | For BNB aggregation | Comma-separated BNB Chain RPC URLs. |
| `RPC_URL_ARB` | For Arbitrum aggregation | Comma-separated Arbitrum RPC URLs. |
| `RPC_URL_LINEA` | For Linea aggregation | Comma-separated Linea RPC URLs. |
| `TELEGRAM_BOT_TOKEN` | Telegram features | Bot token, supplied only through a local or deployment secret. Never commit it. |
| `TELEGRAM_BOT_USERNAME` | No | Bot username without `@`; defaults to `py_WalletBot`. |
| `TELEGRAM_MINI_APP_URL` | No | HTTPS Mini App URL; defaults to `https://pywallet.dev/telegram`. |
| `TELEGRAM_WEBHOOK_URL` | No | Public HTTPS Bot API webhook URL; defaults to `https://pywallet.dev/api/telegram/webhook`. |
| `TELEGRAM_WEBHOOK_SECRET` | No | Optional explicit secret verified against Telegram's webhook header. When omitted, the API derives a stable webhook-only secret from `TELEGRAM_BOT_TOKEN`. |
| `TELEGRAM_AUTH_MAX_AGE_SECONDS` | No | Maximum accepted age of signed Mini App `initData`; defaults to `300`. |
| `TELEGRAM_DAILY_BALANCE_ENABLED` | No | Global kill switch for the scheduled sender; defaults to `false`. User opt-in is always additionally required. |
| `TELEGRAM_API_BASE_URL` | No | Telegram Bot API base URL; override only for tests/proxies. |
| `TELEGRAM_REQUEST_TIMEOUT_SECONDS` | No | Bot API timeout; defaults to `10`. |

Do not commit `.env`. It is ignored by git.

## Telegram Mini App

Configure BotFather's Main Mini App and menu button to open
`https://pywallet.dev/telegram`. The frontend sends Telegram's original `initData`
to `POST /auth/telegram`; the backend verifies its HMAC signature and `auth_date`
before creating or reusing the Telegram identity. The bot token is never returned,
logged, or included in frontend code.

The `/telegram/webhook` endpoint handles `/start` in Russian or English and
returns a short product description with a button that opens the Mini App. After
deploying the endpoint and setting `TELEGRAM_WEBHOOK_SECRET`, the API registers
the webhook automatically at startup. To register it manually without printing
the bot token or secret:

```bash
python scripts/configure_telegram_webhook.py
```

Telegram-only users have nullable email/password fields. They can safely merge into
an existing account with `POST /auth/telegram/link-email`; usernames are metadata,
while the immutable numeric Telegram user ID is the identity key.

Daily messages are disabled globally and per-user by default. A user explicitly
enables them via `PATCH /telegram/settings` after the Mini App requests Telegram
write access. Run the idempotent sender from a scheduler (for example, every five
minutes):

```bash
python -m app.jobs.telegram_daily_balance
```

The job reads only persisted portfolio data, sends at most one digest per
Telegram account and local calendar date, and links back to the Mini App. The
message includes the conservative oldest `as of` timestamp, wallet coverage,
and the same `fresh|updating|partial|stale` health state as the portfolio API;
it never performs live RPC in the delivery path. Delivery attempts export
`py_wallet_telegram_digest_total{language,outcome,health_state}` without user,
chat, wallet, job, or address labels. Telegram `400`/`403` send failures disable
that user's notifications until they opt in again.

## JWT Auth Security

`JWT_SECRET` is the symmetric signing key for access tokens (HS256).

**Why the default is dangerous:** this project is open source. A known signing key
lets anyone forge valid tokens if the app runs with that value in production.

**Generate a strong secret:**

```bash
openssl rand -hex 32
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

**Environment behavior:**

| `APP_ENV` | `JWT_SECRET` | Behavior |
| --- | --- | --- |
| `development` | not set | Uses an implicit dev-only secret and logs a warning. |
| `development` | set explicitly | Must be at least 16 characters and not a known placeholder. |
| `test` | not set | Uses `ci-test-secret` for CI and local test runs. |
| `staging` / `production` | required | Minimum 32 characters; insecure placeholders are rejected at startup. |

**Do not commit `.env`.** Use `.env.example` with placeholders only.

**Verify production config locally:**

```bash
python scripts/check_config_security.py
APP_ENV=production JWT_SECRET="$(openssl rand -hex 32)" \
  python -c "from app.core.config import Settings; Settings(_env_file=None)"
```

Production runtime secrets are managed in the separate `amysyutin/py_wallet-infra`
repository (Kubernetes Secret, not committed to Git).

## Password Hashing

New passwords are hashed with Argon2id. Existing bcrypt hashes remain valid and
are automatically upgraded to Argon2id after the user's next successful login;
no offline password migration is required. Password hashing and verification run
outside the async event loop because they are intentionally CPU-intensive.

**Secret rotation:** changing `JWT_SECRET` invalidates all existing access tokens.
Users must log in again. Refresh tokens are not implemented yet; for this
project a hard cutover (change secret, everyone re-authenticates) is sufficient.
For production rotation steps (future), see `amysyutin/py_wallet-infra` and
[`docs/SECURITY_BACKLOG.md`](docs/SECURITY_BACKLOG.md).

## Security CI

Pull requests and main branch builds run additional security checks in a
parallel `security` job:

| Tool | Purpose | Blocks merge |
| --- | --- | --- |
| Gitleaks | Detect secrets in the current repository tree | Yes |
| pip-audit | Dependency vulnerability scan (JSON report) | No (advisory) |
| Bandit | Python SAST on `app/` (JSON report) | No (advisory) |
| `scripts/check_config_security.py` | JWT config fail-fast rules | Yes (in `test` job) |

CI uploads `security-reports` artifacts (`pip-audit-report.json`,
`bandit-report.json`, 14-day retention) even when advisory steps report findings.
These reports are generated artifacts and are intentionally ignored by Git; use
the CI artifact or regenerate them locally instead of committing report output.

**Run locally:**

```bash
# JWT config rules (blocking in CI)
python scripts/check_config_security.py

# Current tree secret scan (same as CI; does not scan full Git history)
docker run --rm -v "$PWD:/repo" zricethezav/gitleaks:v8.21.2 detect \
  --source /repo --no-git --redact --config /repo/.gitleaks.toml

# Full Git history audit (manual, one-time; may report historical findings)
docker run --rm -v "$PWD:/repo" zricethezav/gitleaks:v8.21.2 detect \
  --source /repo --redact --config /repo/.gitleaks.toml

# Dependency + SAST (pinned in requirements-dev.txt)
pip install -r requirements-dev.txt
pip-audit -r requirements.txt -f json -o pip-audit-report.json
bandit -r app/ -ll -f json -o bandit-report.json
```

Do not widen `.gitleaks.toml` to ignore whole directories (`tests/`, workflows).
Only known fake JWT constants are allowlisted. Triage full-history findings
separately.

Future hardening backlog: [`docs/SECURITY_BACKLOG.md`](docs/SECURITY_BACKLOG.md).

## Running Locally

### Docker Compose

```bash
docker compose up --build -d
docker compose exec app alembic upgrade head
docker compose logs -f app
```

The service will be available at `http://127.0.0.1:8000`.

Stop the stack:

```bash
docker compose down
```

### Python

Start PostgreSQL first. The Docker Compose database can be reused:

```bash
docker compose up -d postgres

python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
alembic upgrade head

uvicorn app.main:app --reload --port 8000
```

## Testing

Tests require a dedicated PostgreSQL database whose name contains a standalone
`test` segment (for example, `wallet_test`).
The test suite refuses to use the application database and rebuilds the
dedicated API schema through Alembic. It then creates the snapshot-service
read-model tables from the API's read-only mappings for isolated tests;
production ownership of those tables remains with snapshot-service migrations:

```bash
docker compose up -d postgres
docker compose exec postgres createdb -U wallet wallet_test
TEST_DATABASE_URL=postgresql+asyncpg://wallet:wallet@localhost:5432/wallet_test \
  pytest -v
```

Run the fast checks used by CI:

```bash
ruff check .
black --check .
mypy
TEST_DATABASE_URL=postgresql+asyncpg://wallet:wallet@localhost:5432/wallet_test \
  pytest -m "not slow and not e2e" -v --tb=short \
  --cov --cov-report=term-missing
```

The test suite is designed to run without external network calls or real
secrets. External APIs are mocked in tests.

The CI test jobs enforce the statement-coverage baseline in `.coveragerc`.
The threshold is a ratchet: never lower it, and raise it when the measured CI
total reaches the next whole percentage point. Targeted local test runs can
omit `--cov`; use the command above before opening a pull request.

Static typing is configured in `pyproject.toml`. The initial mypy gate covers
`app/core` and `app/services`; expand `files` as neighboring packages are made
type-clean instead of weakening the checks already enabled for this scope.

Every pull request must add a user- or developer-facing bullet to the
`[Unreleased]` section of `CHANGELOG.md`. PR CI compares the branch with its base
commit and blocks the merge when the changelog is missing. To run the same check
locally:

```bash
python scripts/check_changelog.py --base-ref origin/main
```

The API and snapshot-service intentionally share one PostgreSQL database but
use separate Alembic version tables. Before the API becomes ready,
snapshot-service must apply its own migrations so that `snapshot_runs`,
`wallet_snapshots`, `chain_snapshots`, and `snapshot_balance_snapshots` exist.
The production manifests enforce this with the snapshot-service PreSync
migration job; `/health/ready` verifies the resulting read-model schema.

### Test Markers

- `slow` marks tests that may call real APIs or take longer to run.
- `e2e` marks end-to-end tests that require network access and secrets.

## CI/CD

GitHub Actions contains two workflows:

| Workflow | Trigger | Purpose |
| --- | --- | --- |
| `.github/workflows/ci.yml` | Pull request to `main` | Changelog, lint, format and mypy checks, coverage-gated tests, security scans (Gitleaks, pip-audit, Bandit), JWT config checks, Docker Compose smoke test, and Telegram notification on failure. |
| `.github/workflows/main-build.yml` | Push to `main` | Mypy and coverage-gated tests, security scans, immutable Docker image build, Compose smoke test, GHCR publish, GitOps image tag update, and Telegram notification. |

The main branch pipeline follows this flow:

```text
test + security -> docker-build -> compose-smoke -> build-and-tag -> bump-gitops -> notify
```

The Docker image is built once, smoke-tested, and published to
`ghcr.io/<repo>:<sha>`. The `bump-gitops` job then updates
`manifests/app/kustomization.yaml` in `amysyutin/py_wallet-infra` and pushes the
new image tag. Kubernetes deployment configuration and cluster reconciliation
are handled outside this application repository.

## GitHub Secrets

| Secret | Used for |
| --- | --- |
| `INFRA_REPO_TOKEN` | Push access to `amysyutin/py_wallet-infra` for GitOps image tag updates. |
| `TELEGRAM_BOT_TOKEN` | Telegram notification bot token. |
| `TELEGRAM_CHAT_ID` | Telegram notification chat ID. |
| `GITHUB_TOKEN` | Automatically provided by GitHub Actions and used to push images to GHCR. |

Runtime application secrets are managed outside this repository and are never
committed to Git.
