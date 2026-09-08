# EpiAlert

**Disease outbreak early warning at street level.**

EpiAlert reads weekly community health reports, spots unusual rises in disease
counts on individual streets, and notifies only the affected street — not the
whole village.

---

## Quick start

Three commands. Takes about 5 minutes.

```bash
git clone https://github.com/VARSHITH707/EpiAlert.git
cd EpiAlert
python -m venv .venv
```

**Windows**

```bash
.venv\Scripts\activate
pip install -r requirements.txt
python setup.py
```

**macOS / Linux**

```bash
source .venv/bin/activate
pip install -r requirements.txt
python setup.py
```

Then start the website:

```bash
python -m uvicorn src.web.main:app --reload
```

Open **<http://127.0.0.1:8000>**

### Login

| | |
|---|---|
| **Username** | `admin` |
| **Password** | `epialert2026` |

> Change this before putting the app anywhere other people can reach:
> `python -m src.cli createuser --username yourname`

`setup.py` generates the data, builds the database, runs detection, creates the
alerts, and sends the mock SMS. Re-running it is safe — completed steps are
skipped.

---

## What you will see

| Page | What it shows |
|---|---|
| **Dashboard** | Live counts: people, reports, detections, alerts, SMS |
| **Upload** | Submit a new week of reports and watch it get processed |
| **Results** | Every street-week, filterable by disease, village, street, week |
| **Alerts** | Confirmed alerts, and the exact message sent to residents |
| **Statistics** | How the four detectors compare |
| **Evaluation** | Accuracy measured against known outbreaks |
| **SMS** | Full delivery audit trail |

**The two pages worth looking at first:**

**SMS** — 3,000 people share 3 phone numbers, so an alert covering a whole
street sends **3 messages, not 180**. The page shows the deduplication working.

**Alerts** → click any alert — the warning text sent to residents, next to the
statistics that triggered it.

---

## Try the live path

The interesting demo is uploading a new week and watching an outbreak get
caught.

1. Go to **Upload**
2. Week number: `200`
3. Upload a text file, one report per line:

```
On 11 January 2027, P0001 from Street 4 in Village A was reported as having dengue.
On 11 January 2027, P0002 from Street 4 in Village A was reported as having dengue.
On 11 January 2027, P0003 from Street 1 in Village B was reported as having no infection.
```

The system extracts each report, validates it, stores it, runs detection, and
tells you what it found. Put a dozen dengue cases on one street and it raises a
`HIGH_ALERT` for that street alone.

---

## What it is built with

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.11 | |
| Web | FastAPI + Jinja2 | Server-rendered HTML — no JavaScript build step |
| Database | SQLite | No server to install; runs anywhere immediately |
| Statistics | NumPy | CUSUM, EWMA, baselines |
| Validation | Pydantic | Schema-checked extraction |
| Passwords | bcrypt | Deliberately slow, so a stolen table cannot be cracked fast |
| Sessions | itsdangerous | Signed cookies that cannot be forged |
| AI (optional) | Ollama + phi3 | Phrases alert messages. Works fine without it |
| Tests | pytest | 250+ tests |
| Deployment | Docker | Optional |

**Ollama is optional.** Without it, alert messages use a fixed template and
everything else is unchanged. To use it: install [Ollama](https://ollama.com),
run `ollama pull phi3`, then `ollama serve`.

The project brief specified MySQL. This build uses SQLite so it runs with
nothing to install. All database access sits in `src/database/db.py`, which is
where a MySQL backend would go.

---

## How it works

```
Weekly reports  ->  Extract  ->  Validate  ->  Database  ->  Detect
                                                               |
                             SMS  <-  Alert  <-  Confirm  <----+
```

Full explanation in **[ARCHITECTURE.md](ARCHITECTURE.md)** — written in plain
English, no statistics background needed.

### The short version

Counts are tracked per **(disease, village, street, week)**. The pairing
matters: 9 of the 10 street names appear in more than one village, so keying on
street alone would merge unrelated streets and let one village's outbreak
corrupt another's baseline.

Four detectors run on every street independently:

| Detector | Catches |
|---|---|
| **Baseline** | This week is much higher than normal here |
| **CUSUM** | Small rises adding up over several weeks |
| **EWMA** | The smoothed recent average drifting upward |
| **Combined** | All of the above fused into one decision |

The combined detector can fuse in two ways:

- **union** — any 2 of 4 signals agree
- **confirmation** — CUSUM **and** EWMA must both fire ← *default*

Confirmation is the default because union fires on 16.6% of street-weeks when
real outbreaks occur in only 1.3% — 12× too often to be usable.

---

## Measured results

Against 6 known outbreaks over 84 weeks and 26 streets:

| Fusion mode | False alarms | Fires on | Precision | Sensitivity |
|---|---|---|---|---|
| Union | 1,737 | 16.6% of street-weeks | 0.042 | 0.531 |
| **Confirmation** | **280** | **2.9%** | **0.119** | 0.262 |

**The confirmation rule cuts false alarms by 84% and still catches 5 of 6
outbreaks in their first week.**

**Honest limitation:** even at its best, 88% of alerts are false. A street sees
0–4 cases in a normal week, so the gap between "quiet" and "outbreak starting"
is one or two cases — well inside ordinary random variation. A parameter sweep
found no settings reaching usable precision. This is a real property of
small-count data, and it matches published work where sophisticated methods
failed to beat simple control charts.

---

## Command line

```bash
python -m src.cli <command>
```

| Command | Does |
|---|---|
| `validate` | Check the dataset is readable and consistent |
| `ingest-all` | Load all weeks into the database |
| `evaluate --start-week 21 --end-week 104` | Run detection |
| `alerts` | Create alerts (`--no-ollama` for the fast path) |
| `sms` | Dispatch SMS (mock by default) |
| `createuser --username NAME` | Create a web login |
| `reports` | Write report files |

Other scripts:

```bash
python generate_dataset.py    # rebuild the synthetic dataset
python run_all.py             # run the whole pipeline, print a summary
python -m pytest tests/ -q    # run the tests
```

---

## SMS

Alerts go only to people on the affected street, deduplicated by number.

**Mock mode is the default and sends nothing real** — it simulates a provider
response and still writes the full audit trail, so the whole flow can be
demonstrated safely.

```
SMS_MODE=mock   # default, sends nothing
SMS_MODE=live   # sends, only when provider credentials are configured
```

The idempotency check runs *before* the provider is contacted. A repeated
dispatch sends nothing further, rather than relying on the database to discard
a duplicate after the message has already gone out.

Providers sit behind one interface in `src/sms/`, so Twilio or MSG91 can be
added without touching the alert logic.

Phone numbers come from `ALERT_PHONE_NUMBERS` in `.env`. The defaults are
unreachable placeholders — a real number in source code is a real person anyone
who clones the repo could call.

---

## Docker

```bash
python -c "import secrets;print('EPIALERT_SECRET_KEY='+secrets.token_urlsafe(32))" > .env
docker compose up
```

Open <http://127.0.0.1:8000>. The database and dataset are mounted as volumes,
so rebuilding the image never touches your data. The container runs as a
non-root user with a health check.

---

## Privacy

- Alerts name **an area and a disease only** — never a person, household or
  address. At street level with small counts, naming a case could identify a
  household even with no identifier stored.
- **No real patient data.** The dataset is synthetic and regenerated from a
  fixed seed.
- Passwords are bcrypt hashes, never plain text.
- Authorisation is enforced server-side on every protected page and API
  endpoint. Hiding a link in the UI is not access control.

---

## Layout

```
src/
  cli.py                    command line
  consolidate.py            report files -> one file per week
  database/db.py            all database access (19 tables)
  extraction/               parsing, validation, quarantine
  detection/                baseline, cusum, ewma, combined, confirmation
  evaluation_spatial.py     detection per street
  alerts/                   alert creation, message text, SMS dispatch
  sms/                      mock and live providers
  web/                      FastAPI app, auth, pages
tests/                      250+ tests
generate_dataset.py         rebuild the dataset
setup.py                    one-command setup
run_all.py                  whole pipeline
```

---

## Troubleshooting

**`python` not found, or the wrong packages** — make sure the virtual
environment is active. The prompt should start with `(.venv)`.

**Login fails** — if you edited `.env` with PowerShell `echo >>`, it was written
as UTF-16 and cannot be read. Rewrite it as UTF-8, or delete it and re-run
`python setup.py`.

**Port 8000 already in use** — `python -m uvicorn src.web.main:app --port 8001`

**Ollama warnings** — harmless. Alert messages fall back to a template.

**Tests take ~2 minutes** — expected. Several run a full 84-week evaluation.

---

## Status

Working: data generation, ingestion, extraction with quarantine, street-level
detection, all four detectors, both fusion modes, evaluation against ground
truth, alerts with optional AI phrasing, SMS with deduplication, the web
interface with authentication, uploading new weeks, and Docker.

Not done, stated plainly:

- **Live SMS has never been tested against a real provider.** Only mock mode is
  proven. Treat the live path as untested.
- **The RAG investigation layer** from the original design is not implemented.
  It was dropped as speculative — nothing in the workflow consumes it.
- **Detection precision is low** (best 0.119). Measured, explained above, not
  hidden.
- Eight functions exceed the 50-line guideline, the largest being
  `db.init_schema` (432 lines of table definitions). All are covered by tests.

---

## Project

Major Project Phase-I — Dayananda Sagar University, Dept. of CSE.

Full design rationale and references: **[ARCHITECTURE.md](ARCHITECTURE.md)**.
