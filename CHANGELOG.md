# Changelog

All notable changes to the **py_wallet** application are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Deployment and platform changes are tracked separately in
[`amysyutin/py_wallet-infra`](https://github.com/amysyutin/py_wallet-infra).

## [Unreleased]

### Added

- Add user-scoped Binance balances to portfolio summary and all-scope allocation,
  with explicit `exchange` source and health contracts.
- Add persisted portfolio and wallet data-health contracts with freshness,
  coverage, chain-issue, and price-quality details.
- Add snapshot-first wallet asset reads, scoped portfolio allocation, and a
  coverage-aware 24-hour value change.
- Add owner-safe portfolio refresh telemetry and retry for failed snapshot
  chains without exposing wallet or user identifiers in metric labels.
- Add first-snapshot activation progress and API safeguards for snapshot schema
  readiness.
- Add privacy-safe activation and Telegram digest metrics.
- Add Telegram `/start` webhook responses, automatic webhook setup, and daily
  digest health context consistent with the portfolio API.
- Add API build metadata and reliable main-build fallback dispatch.
- Add an incremental mypy gate for `app/core` and `app/services` to pull-request
  and main-branch CI.
- Pin `pytest-cov` and enforce the current 74% statement-coverage baseline in
  pull-request and main-branch CI, with a documented ratchet-up policy.
- Require every pull request to update this `[Unreleased]` section through a
  dedicated CI policy check.

### Changed

- Stop tracking generated security reports and expand ignore rules for local
  Python, coverage, editor, and operating-system artifacts.
- Aggregate each EVM wallet address across all enabled networks and canonicalize
  duplicate legacy rows for portfolio reads.
- Prefer persisted snapshot-service balances for wallet assets and expose the
  same honest health state in wallet detail and Telegram digest responses.
- Harden wallet data ownership, manual-balance validation, refresh scoping, and
  snapshot-service readiness checks.

### Fixed

- Keep validation errors from exposing configuration input values with current
  Pydantic releases.
- Preserve historical portfolio points across wallet address revisions while
  excluding pre-revision data from current value calculations.
- Repair snapshot read-model schema validation and isolated Compose readiness.
- Restore Telegram start responses and make webhook configuration resilient.
- Restore reliable post-merge main builds and immutable release delivery.

## [0.2.0] - 2026-07-22

### Added

- Telegram Mini App authentication and bot integration.
- Wallet groups, manual wallets and balances, and expanded portfolio APIs.
- Snapshot-service integration, scheduled snapshot jobs, and product metrics.
- Component SemVer validation and immutable release-image promotion.

### Security

- Hardened password handling, configuration validation, dependency auditing,
  and the validated GitOps deployment path.

## [0.1.0] - 2026-06-10

First public version of the crypto portfolio service.

### Added

- FastAPI service with async PostgreSQL persistence and Alembic migrations.
- EVM portfolio aggregation across Ethereum Mainnet, Base, BNB Chain, Arbitrum,
  and Linea (native token, USDT, USDC).
- USD valuation via RPC token balances and CoinGecko native token prices.
- JWT authentication: user registration, login, and `/auth/me`.
- Per-user wallets, balance snapshots, portfolio history, and portfolio summary.
- Split health endpoints: `/health`, `/health/live`, `/health/ready`.
- Prometheus `/metrics` endpoint via instrumentator.
- Docker image and Docker Compose stack for local development.
- CI pipeline: ruff lint, black format check, pytest, Docker Compose smoke test.
- Security CI: Gitleaks (blocking), pip-audit and Bandit (advisory), JWT config
  fail-fast check.
- Main-branch pipeline: immutable GHCR image by commit SHA, GitOps image tag bump
  in the infra repo, and Telegram notifications.

### Security

- JWT secret hardening: environment-aware validation, insecure placeholders
  rejected in staging/production, dev-only implicit secret with a startup warning.

### Known limitations

- Refresh tokens are not implemented; rotating `JWT_SECRET` forces re-login.
- Secrets are managed as out-of-band Kubernetes Secrets (SOPS/SealedSecrets planned).
- Snapshot collection supports EVM wallets only.

See [`docs/SECURITY_BACKLOG.md`](docs/SECURITY_BACKLOG.md) for the full hardening backlog.

[Unreleased]: https://github.com/amysyutin/py_wallet-api/compare/20c0f7db92f3ccb9c19cf0a411359ab41ba5f236...HEAD
[0.2.0]: https://github.com/amysyutin/py_wallet-api/compare/v0.1.0...20c0f7db92f3ccb9c19cf0a411359ab41ba5f236
[0.1.0]: https://github.com/amysyutin/py_wallet-api/releases/tag/v0.1.0
