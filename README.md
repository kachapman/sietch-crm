# Sietch CRM

<p align="center">
  <img src="public/assets/sietch-logo-2-nobg2.png" alt="Sietch CRM Logo" width="200">
</p>

<!-- Logo assets: sietch-logo-2-nobg2.png (pure logo, used for dashboard/branding) + sietch-logo-2-nobg1.png (includes name, used in footers) -->

> "The sietch is where the tribe gathers. Water is rationed. Strategy is planned. Nothing is lost."

**Current version:** 3.0.0 — see [CHANGELOG.md](./CHANGELOG.md)

## Overview

Sietch CRM is a standalone project management dashboard built for public adjusters, independent adjusters, building consultants, and engineers who work on property loss claims and construction projects.

Like the Fremen sietch fortresses of Arrakis in Frank Herbert's *Dune*, this is your team's command center — a fortified workspace where all project notes, correspondence, tasks, and communications are centralized, preserved, and strategically accessible.

## Why "Sietch"?

The name draws from Frank Herbert's Dune universe, where sietches were hidden tribal fortresses carved into rock. Each sietch was a self-contained community where:

- **Resources were centralized** — every note, email, and document stored in one protected place
- **Strategy was planned** — timelines, deadlines, and task dependencies need oversight
- **The tribe operated as one** — status updates, presence awareness, and coordination
- **Nothing was lost** — audit trails, change logs, and event tracking protect from disputes

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Docker Compose Stack                      │
│                                                              │
│  ┌──────────────┐  ┌──────────────┐  ┌────────────────────┐ │
│  │  PostgreSQL   │  │  Sietch CRM  │  │ OnlyOffice Docs    │ │
│  │  (database)   │  │  (dashboard) │  │ (Document Server)  │ │
│  │  port 5432    │  │  port 8766   │  │ port 8080          │ │
│  └──────────────┘  └──────────────┘  └────────────────────┘ │
│         │                 │                 │                 │
│         └─────────────────┴─────────────────┘                 │
│                    vanguard-internal network                  │
└─────────────────────────────────────────────────────────────┘
```

- **PostgreSQL 16** — project data, users, history, tasks, documents
- **Python backend** — server.py (API, auth, document storage, Document Server proxy)
- **Vanilla JS frontend** — no frameworks, no build step
- **OnlyOffice Document Server** — collaborative document editing (view/edit Word, Excel, PowerPoint in browser)
- **Redis** — session management for Document Server

## Features

| Category | Capabilities |
|----------|--------------|
| **Project Management** | Kanban boards, stages, tags, custom fields, calendar integration |
| **Document Editing** | OnlyOffice integration — view/edit Word, Excel, PowerPoint in browser |
| **Task Management** | Create, assign, track, close tasks linked to projects |
| **Collaboration** | Team presence, direct messaging, activity feed |
| **Customer Portal** | Telegram bot for automatic project updates |
| **Data Integrity** | PostgreSQL persistence, session-based auth, offline resilience |
| **Administration** | User management, stage/tag/custom field configuration |

## Quick Start

### Prerequisites
- Docker and Docker Compose
- Git

### Run Locally

```bash
git clone https://github.com/kachapman/sietch-crm.git
cd sietch-crm
git checkout new-crm

# Configure
cp config.example.env .env
# Edit .env with your settings (DB credentials, JWT secret, etc.)

# Start
docker compose up -d

# Access
open http://localhost:8766
```

### First-Time Setup

1. Open the dashboard at `http://localhost:8766`
2. Create your admin account
3. Add team members via the admin panel
4. Start creating projects

## Configuration

| Variable | Purpose | Default |
|----------|---------|---------|
| `PORT` | Dashboard HTTP port | `8766` |
| `DB_HOST` | PostgreSQL host | `db` |
| `DB_PORT` | PostgreSQL port | `5432` |
| `DB_NAME` | Database name | `sietch_crm` |
| `DB_USER` | Database user | `sietch` |
| `DB_PASSWORD` | Database password | — |
| `COOKIE_SECRET` | Session cookie secret | — |
| `DOCS_JWT_SECRET` | Document Server JWT secret | — |
| `DOCS_PUBLIC_URL` | Document Server public URL | `https://docs.publicadjustermidwest.com` |
| `SMTP_HOST` | Mail server for password resets | — |
| `SMTP_PORT` | SMTP port | `587` |
| `SMTP_USER` | SMTP username | — |
| `SMTP_PASSWORD` | SMTP password | — |

## Documentation

| Document | Purpose |
|----------|---------|
| [CHANGELOG.md](./CHANGELOG.md) | Version history and release notes |
| [DEPLOY.md](./DEPLOY.md) | Production deployment guide |
| [FUTURE_FEATURES.md](./FUTURE_FEATURES.md) | Roadmap and feature ideas |
| [ISSUES.md](./ISSUES.md) | Tracked bugs and follow-up work |
| [docs/DEPLOYMENT.md](./docs/DEPLOYMENT.md) | Server setup and nginx configuration |

## Development

### Local Testing

```bash
# Normal development
docker compose up -d

# Or with chaos test server (simulates failures)
python test-server.py
```

### API Reference

The dashboard exposes a v2 REST API. See `server.py` for full endpoint documentation.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v2/auth/login` | POST | Authenticate user |
| `/api/v2/projects` | GET/POST | List/create projects |
| `/api/v2/projects/{id}` | GET/PUT/DELETE | Get/update/delete project |
| `/api/v2/tasks` | GET/POST | List/create tasks |
| `/api/v2/documents/{id}/download` | GET | Download document |
| `/api/v2/contacts` | GET/POST | List/create contacts |
| `/api/v2/users` | GET/POST | List/create users |

## License

AGPL v3 — see [LICENSE](./LICENSE)

## Credits

Built for **Sietch CRM**.

Inspired by the sietch fortresses of Arrakis — where strategy is planned, resources are preserved, and nothing is lost.
