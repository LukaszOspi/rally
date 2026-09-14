# Rally Finder

Every Roadsurfer Rally one-way campervan route in Europe on one map and one
list, with exact dates, pickup and drop-off details, vehicle and price —
refreshed hourly, with Telegram alerts for saved searches.

Live at **https://rally.ospi.link** (see [DNS](#dns) below).

## Why it exists

The Roadsurfer Rally booking flow is strictly sequential: pick a departure,
then discover which destinations exist, then discover which dates exist, then
find out whether anything is actually bookable. There is no overview. This
turns that into one screen.

It also removes a trap in the source data: **the timeframes endpoint lists
windows that cannot be booked**. Of ~98 windows on a typical crawl, ~22 are
dead ends. Those are hidden by default, the count is shown so the filtering is
never silent, and **Show dead ends** puts them back in the list with the reason
attached.

### What a "dead end" is

A window Roadsurfer's calendar advertises but the booking step will not sell
you. Only the search call can tell them apart, which is why the manual flow
makes you find them four clicks in. Three kinds, recorded per offer as
`unbookable_reason`:

| Reason | What happened |
|---|---|
| `no_vehicles_offered` | Route and dates valid, but the booking step returns no vehicle at all — nothing is allocated to reposition. The common case. |
| `invalid_booking_end_station` | Booking rejects the departure/destination pair outright with a 400, even though the calendar lists it. The route is advertised but unbookable, period. |
| `all_vehicles_taken` | Campers exist for the window but every one is already booked. |

They are re-checked every 6 hours rather than the usual 24, because stock does
reappear — a dead end flipping back to bookable is a normal event.

### Filters

Deliberately few, because most of the data does not vary:

- **Price is not a filter** — every rally is the same €129 plus local VAT.
- **Trip length is not a filter** — windows are 6–8 days, effectively always 7.
  A window under 7 days gets an `only N days` chip instead; over 7 gets a
  `N-day window` chip.
- **Camper is a multi-select.** `GET /api/models` derives the list from every
  offer ever crawled, with a live count per model, so a camper that is
  temporarily absent can still be picked for an alert. Matching only counts
  campers actually available on that trip.

### Map interaction

The map shows **points only** — departures in orange, destinations in blue,
sized by how many trips touch them. Connections are drawn on demand:

- **Click a point** to see its connections and filter the list to them. Click
  it again, press Escape, click bare map, or hit **Clear** to go back.
  Focus is keyed to the station, not the direction, so a hub city that is both
  a departure and a destination shows both (`3 out · 27 in`).
- **Hover a trip in the list** to trace just that connection on the map. The
  viewport never moves on hover or selection — only on filter changes.

## How the Roadsurfer API works

Undocumented, unauthenticated, but every call needs an `X-Requested-Alias`
header naming the logical endpoint — without it the gateway serves the SPA's
404 page.

| Endpoint | Alias | Returns |
|---|---|---|
| `GET /api/en/rally/stations` | `rally.startStations` | all stations, coordinates, `one_way` flag |
| `GET /api/en/rally/stations/{id}` | `rally.fetchRoutes` | `returns: [ids]` — destinations from that departure |
| `GET /api/en/rally/timeframes/{a}-{b}` | `rally.timeframes` | candidate date windows |
| `GET /api/en/rally/search?stations=[[a,b]]&range=["from","to"]` | `rally.search` | vehicles, seats/beds, photos, real price |

Base URL is `https://booking.roadsurfer.com/api`.

Two quirks worth knowing, both handled in `roadsurfer.py`:

- Responses are padded with placeholder rows (`id: "3-78-empty"`) that carry no
  `availability`. They mean "nothing here", not "a camper".
- The same camper comes back as `SURFER SUITE` or `Surfer Suite` depending on
  whether the category or model name wins, and Roadsurfer ships several model
  ids for one product. Merged on name plus seat/bed spec.

## Architecture

```
Caddy (:443, shared)
  ├── /api/*  →  rally-api        FastAPI + APScheduler + SQLite + Telegram bot
  └── /*      →  rally-frontend   nginx serving static HTML/CSS/JS + Leaflet
```

- **Hourly crawl.** ~100 requests for the graph, plus vehicle/price detail only
  for windows that are new or stale (24h TTL; unbookable ones rechecked after
  6h). Roughly one request every 20 seconds — deliberately unhurried.
- **SQLite**, not Postgres. The whole dataset is ~100 rows; a file in a volume
  beats a seventh Postgres container on this box.
- **Long-polling bot**, not webhooks — no inbound route, no public secret.

## Layout

```
backend/app/
  roadsurfer.py   API client, response compaction, quirk handling
  crawler.py      hourly crawl, diffing, bookability resolution
  db.py           SQLite schema and migrations
  store.py        queries shared by the API and the alert matcher
  matcher.py      saved-search criteria matching
  telegram.py     bot commands, deep-link binding, alert delivery
  main.py         HTTP API and scheduler
backend/tests/    25 unit tests, no network needed
frontend/         index.html, app.js, styles.css, vendored Leaflet
infra/nginx.conf  static file serving
```

## Setup

### DNS

The public hostname needs an **A record → your server's IP** in whatever DNS
you use. Caddy is configured to retry certificate issuance for 30 days, so
HTTPS starts working within a minute of the record propagating — no redeploy
needed.

### Telegram

Alerts are inert until a bot exists. Copy `.env.example` to `.env` on the
server and fill it in — `ADMIN_TOKEN` should be a freshly generated random
string (`openssl rand -hex 32`), and the two Telegram lines come from the
steps below.

1. In Telegram, open [@BotFather](https://t.me/BotFather) and send `/newbot`.
2. Give it a display name (anything, e.g. `Rally Finder`) then a username that
   must end in `bot` (e.g. `ospi_rally_bot`).
3. BotFather replies with a token like `8123456789:AAH...`. Put both values in
   the server's `.env`:
   ```
   TELEGRAM_BOT_TOKEN=8123456789:AAH...
   TELEGRAM_BOT_USERNAME=ospi_rally_bot
   ```
4. `docker compose up -d` in the deployment directory
5. Confirm: `docker logs rally-api 2>&1 | grep telegram` should say
   `telegram bot @<name> connected`, not `telegram disabled`.

The web UI stays fully public; saved searches do not. Creating one mints a
128-bit token and shows a `t.me/<bot>?start=<token>` link — tapping it binds
that search to your chat. Nobody can enumerate or read anyone else's.

Bot commands: `/list`, `/pause <n>`, `/resume <n>`, `/delete <n>`, `/help`.

## Security posture

The map and list are deliberately public and the only write path — creating a
saved search — is unauthenticated by design. Since this box is shared with
several unrelated apps, the limits below exist so that abuse here cannot spill
onto them.

**Application**
- `POST/PATCH/DELETE /api/watches` rate-limited per IP (10/min, 40/hour).
  Client IP is taken from the *last* `X-Forwarded-For` entry — the hop Caddy
  itself appended — so a client cannot spoof its way into a fresh bucket.
- Request bodies capped at 64 KB by Caddy and again in the app.
- Saved-search criteria are bounded before storage (≤50 station ids, ≤25
  country codes/models, ≤4 KB serialised) and a global ceiling of 5 000
  searches applies.
- `chat_id` is never returned by the API; `linked` conveys the same thing.
- `POST /api/crawl` fails closed when `ADMIN_TOKEN` is unset and compares in
  constant time.
- All SQL is parameterised; the frontend escapes every interpolated value.
  Both verified by injecting payloads through the real endpoints and rendering
  the result in a browser.
- FastAPI's `/docs`, `/redoc` and `/openapi.json` are disabled.

**Containers**
- Both run with `read_only` rootfs, `cap_drop: ALL`, `no-new-privileges`, and
  capped memory/PIDs/CPU. The API runs as uid 10001, not root.
- Writable paths are exactly two: the `/data` volume and a small `/tmp` tmpfs.
- No ports published; no Docker socket mounted.

**Edge**
- HSTS, `X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy`,
  `Permissions-Policy`, and a CSP allowing only self-hosted scripts plus
  OpenStreetMap tiles and Roadsurfer's image host.

**Network isolation.** Unlike the other apps on this host, rally does *not*
join the shared `proxy` network. It sits on `rally-proxy`, which only Caddy
also joins. Verified from inside the container: every other app's API and
database is unreachable both by name and by raw IP, while Caddy still routes
to `rally-api` and `rally-frontend` normally.

```
infra-caddy-1 ──┬── proxy        → the host's other apps…
                └── rally-proxy  → rally-api, rally-frontend   (isolated)
```

Adding another app the same way: `docker network create <app>-proxy`, add it
to the `caddy` service in `/opt/infra/docker-compose.yml` and to the app's
compose as `external: true`, then `docker compose up -d` in `/opt/infra`
(recreates Caddy — a few seconds of downtime across all sites).

## Deploying

```bash
./deploy.sh
```

rsyncs to `/opt/rally-finder`, rebuilds on the server (it is aarch64, so images
are never built locally), waits for health, reloads Caddy.

> The Dockerfile installs nothing via `apt`. This host resolves
> `deb.debian.org` to IPv6 only and Docker's bridge has no IPv6 egress, so any
> apt step fails the build. Everything comes from PyPI or the stdlib.

## Local development

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
RALLY_DB=/tmp/rally.db .venv/bin/uvicorn app.main:app --port 8811
.venv/bin/python -m pytest tests/ -q
```

Serve `frontend/` on another port and proxy `/api` to 8811.

## Operations

```bash
docker logs rally-api --tail 50 -f
docker exec rally-api python -c "import urllib.request;print(urllib.request.urlopen('http://localhost:8000/api/health').read())"
curl -H "X-Admin-Token: $ADMIN_TOKEN" -X POST https://rally.ospi.link/api/crawl   # force a crawl
curl https://rally.ospi.link/api/crawls                                          # crawl history
```

`POST /api/crawl` is token-gated (`ADMIN_TOKEN` in `.env`) so a public page
cannot be used to hammer Roadsurfer.

## What the data actually looks like

Worth calibrating expectations, as of the first crawls:

- **~76 bookable trips** at any moment, from ~20 departure cities.
- **Windows are effectively fixed, not flexible.** Nearly every window is
  exactly 7 days; occasionally one is 8. The UI labels anything over 7 days as
  a flexible window and explains that the rental is still capped at 7, but in
  practice you are choosing a fixed departure date, not a week inside a
  fortnight.
- **Price is €129 flat**, plus local VAT, so the real total varies a little by
  pickup country (€129.01 out of the Netherlands, for instance).
- **It is a repositioning scheme**, so the flow is overwhelmingly one-way into
  Germany — France and Spain out, German hubs in.
