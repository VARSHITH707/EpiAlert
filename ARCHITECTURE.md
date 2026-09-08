# EpiAlert — Architecture and How It Works

Plain-English explanation of the whole system: what each part does, why it
exists, and how the pieces fit together.

Written for someone who has to explain this project to a panel.

---

## 1. What problem this solves

A village has 1,000 people. Every week, each person reports whether they are
sick. Somewhere in that village, on one street, dengue cases start rising.

Two things can go wrong:

**Nobody notices.** Three extra cases on one street disappears inside a village
of 1,000 people. By the time it is obvious, it is an outbreak.

**Everybody panics.** The system spots three cases on Street 4 and alerts all
1,000 villagers. It does this every week, for streets that turn out to be fine.
After a month, people ignore the messages. Now when a real outbreak comes, the
warning arrives and nobody reads it.

These two problems pull in opposite directions. Make the system more sensitive
and you get more false alarms. Make it stricter and you miss real outbreaks.

**EpiAlert tries to handle both**: watch each street separately so small rises
are visible, and alert only the affected street so people keep trusting the
messages.

---

## 2. The whole system in one picture

```
   Weekly reports (312,000 text files)
              |
              v
    [1] CONSOLIDATE  -> one JSONL file per week
              |
              v
    [2] EXTRACT      -> pull out: who, where, when, what disease
              |
              v
    [3] VALIDATE     -> check it makes sense; bad rows go to quarantine
              |
              v
    [4] DATABASE     -> store as rows (SQLite)
              |
              v
    [5] AGGREGATE    -> count cases per street per week
              |
              v
    [6] DETECT       -> baseline, CUSUM, EWMA, combined
              |
              v
    [7] ALERT        -> create an alert, write the warning message
              |
              v
    [8] SMS          -> send to affected street only, deduplicated
              |
              v
    [9] WEBSITE      -> a person logs in and sees all of it
```

Each stage only talks to the one after it. That means you can fix or replace
any stage without breaking the rest.

---

## 3. Each stage explained

### Stage 1 — Consolidate

**Problem it solves.** The dataset is 312,000 separate small text files. Opening
312,000 files one at a time is slow — it took minutes just to walk the folder.

**What it does.** Reads all of them once and writes 104 JSONL files (one per
week, one report per line). Everything downstream reads those instead.

**Where:** `src/consolidate.py`, output in `data/consolidated/`

---

### Stage 2 — Extract

**Problem it solves.** A report is a sentence written by a person, like
*"Ramesh from Street 4, Village A, reported fever on 12 January."* A computer
cannot count that. It needs structured fields.

**What it does.** Turns each sentence into four fields:

| Field | Example |
|---|---|
| person_id | P0001 |
| village | Village A |
| street | Street 4 |
| reporting_date | 2025-01-12 |
| disease | Dengue |

**Two ways of doing it.** This is an important design decision:

| Extractor | Speed | Used for |
|---|---|---|
| Rule-based | Fast — 312,000 reports in minutes | All 104 historical weeks |
| Ollama (phi3 AI model) | Slow — seconds each | New incoming weeks, and accuracy checking |

**Why two?** Running an AI model on 312,000 reports would take days. It would
never finish, so the project would never run. The rule-based one handles the
bulk. The AI one handles new data and is checked against the rule-based one on a
sample, to prove it works.

Both are interchangeable — they follow the same interface, so any other
extractor can be dropped in without changing anything else.

**Where:** `src/extraction/` — `interface.py`, `rule_based.py`,
`ollama_extractor.py`, `schema.py`

---

### Stage 3 — Validate

**Problem it solves.** If extraction gets something wrong, that error flows into
the statistics and corrupts every number after it.

**What it does.** Four checks on every record:

1. Does this person exist?
2. Does this village and street exist?
3. Is the person actually registered at that street?
4. Is the date real, and is it a Sunday? (all reports are weekly, on Sundays)

Anything failing goes to a **quarantine** table with its original text kept, and
is excluded from the statistics until a human looks at it.

**Why quarantine instead of deleting or guessing?** A wrong location is worse
than a missing one. Guessing puts a case on the wrong street, which corrupts two
streets' numbers at once — the one that gets a case it should not have, and the
one that loses a case it should have had.

**Where:** `src/extraction/validation.py`

---

### Stage 4 — Database

19 tables in SQLite. The important ones:

| Table | Holds | Rows |
|---|---|---|
| `people` | who lives where, plus phone number | 3,000 |
| `reports` | every validated weekly report | 312,000 |
| `detection_results` | detector output per street per week | 21,710 |
| `alerts` | confirmed alerts with warning text | 1,000 |
| `sms_messages` | every dispatch attempt (audit trail) | 954 |
| `quarantine` | rejected records | — |
| `users` | website logins | 2 |

**Why SQLite and not MySQL?** The original plan said MySQL. SQLite needs no
server to install, so the project runs anywhere immediately. All database code
sits in one file, so switching to MySQL later means changing that one file.

**Where:** `src/database/db.py`

---

### Stage 5 — Aggregate

Counts cases per **(disease, village, street, week)**.

**One critical detail.** There are 10 street names but **9 of them appear in more
than one village.** "Street 1" exists in Village A, Village B and Village C —
three completely unrelated streets.

If the code grouped by street name alone, those three would be merged into one
number. An outbreak in Village A's Street 1 would pollute Village C's Street 1
baseline, and both would produce nonsense.

So every key in the entire system is the **pair (village, street)**, never street
alone.

---

### Stage 6 — Detect

This is the heart of the system. **26 spatial units** (village+street pairs), each
watched independently, each with its own memory.

#### The baseline: what is normal here?

Weeks 1–20 establish what a normal week looks like **for that specific street**.
Street 4 in Village A might normally have 1 case a week; Street 9 in Village C
might normally have 0.

Two refinements matter:

**Alert weeks are excluded.** If a street had an outbreak in week 30, week 30 is
removed when calculating what is normal. Otherwise a long outbreak slowly
becomes "normal" and the system stops noticing it — the outbreak hides itself.

**The exclusion removes the week from the division too.** If you have 13 weeks
and exclude one, you divide by 12, not 13. Treating the excluded week as a zero
would drag the average down and invent variation that is not there.

#### Four detectors

| Detector | In plain terms |
|---|---|
| **Baseline** | Is this week much higher than normal? |
| **CUSUM** | Have small rises been *adding up* over several weeks? |
| **EWMA** | Is the smoothed recent average drifting upward? |
| **Combined** | Fuses all of the above into one decision |

**CUSUM** catches a slow steady climb that never spikes — 2, 3, 3, 4, 4 cases.
No single week looks alarming, but the total keeps growing.

**EWMA** is a weighted average that remembers recent weeks more than old ones.
Its alarm threshold **widens in the early weeks**, because with only a few weeks
of history you cannot be confident yet. Without that widening you get false
alarms in weeks 1–10 that are just noise.

#### The combined detector: two ways to combine

This is the most interesting design decision, and it is measured.

| Mode | Rule | Result |
|---|---|---|
| **Union** | Any 2 of 4 signals agree | Fires on **16.6%** of street-weeks |
| **Confirmation** | CUSUM **and** EWMA must both fire | Fires on **2.9%** of street-weeks |

The true outbreak rate is **1.3%** of street-weeks.

Union alerts 12 times more often than outbreaks actually happen. Confirmation is
close to correct. **Confirmation is the default.**

Why the difference? Union fires when *any* two detectors are wrong together, so
their mistakes add up. Confirmation requires *both* to agree, so a mistake by one
alone is not enough.

Output is one of four states:

| State | Meaning |
|---|---|
| `NORMAL` | nothing unusual |
| `WATCH` / `PROVISIONAL` | one detector fired — logged, but **no alert sent** |
| `ALERT` | both fired — notify the street |
| `HIGH_ALERT` | both fired and the rise is large |

**Where:** `src/detection/` — `baseline.py`, `cusum.py`, `ewma.py`,
`combined.py`, `confirmation.py`, `config.py`

---

### Stage 7 — Alert

Turns a detection result into something a person can read.

The statistics produce: *Village B, Street 8, Influenza, week 24, observed 4,
expected 0.75, CUSUM fired, EWMA fired.*

The alert layer turns that into:

> "EpiAlert Warning: Increased Influenza/ARI activity has been detected in
> Village B, Street 8. Observed 4 cases this week against an expected 0.75
> cases. Status: HIGH_ALERT. Please take appropriate precautions."

**The AI writes the sentence, not the numbers.** Ollama receives the already-
computed values and phrases them. It is never allowed to decide what the disease
is, where it is, or how many cases there are. If Ollama is switched off, a fixed
template produces the same information in slightly plainer wording.

**Why that split?** An AI that invents a case count would be putting false
medical information in front of villagers.

**Where:** `src/alerts/` — `service.py`, `message.py`

---

### Stage 8 — SMS

**The problem this stage exists to solve.** An alert on one street affects
roughly 100–180 people. Sending one message per person means 180 messages for
one alert.

In this dataset all 3,000 people share **3 test phone numbers**. So 180 people
means the same 3 phones getting 60 messages each.

**What it does:**

```
alert
  -> find people on that (village, street)
  -> collect their phone numbers
  -> convert to standard +91 format
  -> REMOVE DUPLICATES          <- 180 numbers become 3
  -> check: already sent this alert to this number?
  -> send
  -> write an audit row
```

Result: **318 alerts produced 954 messages** — exactly 3 per alert, never 180.

**Two safety rules:**

*The duplicate check happens before the message is sent, not after.* The database
can stop a duplicate row being saved, but it cannot un-send a message that
already went out. Checking first means re-running the pipeline sends nothing new.

*Mock mode is the default.* Nothing real is sent. The full audit trail is still
written, so you can demonstrate the whole flow safely. Live sending needs
provider credentials to be configured deliberately.

**Where:** `src/sms/service.py` (providers),
`src/alerts/service.py` (dispatch logic)

---

### Stage 9 — Website

Where a health worker actually uses the system.

| Page | Shows |
|---|---|
| Login | username and password |
| Dashboard | live totals, recent alerts |
| Results | every street-week, with filters |
| Alerts | alert list, and detail per alert |
| Statistics | how the detectors compare |
| Evaluation | how accurate the system actually is |
| SMS | the full dispatch audit trail |

**Security:**

- Passwords are stored as **bcrypt hashes**, never as text. Bcrypt is
  deliberately slow, so a stolen password table cannot be cracked quickly.
- Login state is a **signed cookie**. Editing it to say "I am admin" fails,
  because the signature no longer matches.
- **Every protected page and API endpoint checks on the server.** Hiding a menu
  link is not security — anyone can type the address. The server refuses the
  request itself.

**Where:** `src/web/` — `main.py` (routes), `auth.py` (login), `data.py`
(queries), `templates/` (pages)

---

## 4. How accurate is it, honestly?

Measured over weeks 21–104, 26 streets, 6 known outbreaks:

| | Union | Confirmation |
|---|---|---|
| False alarms | 1,737 | **280** |
| Alerts fired on | 16.6% of street-weeks | **2.9%** |
| Precision (PPV) | 0.042 | **0.119** |
| Sensitivity | 0.531 | 0.262 |
| Outbreaks caught in week 1 | 5 of 6 | 5 of 6 |

**The good result.** Confirmation cuts false alarms by **84%** and still catches
outbreaks just as early. That is the design working as intended.

**The honest limitation.** Even at its best, **88% of alerts are still false.**
A street has 0–4 cases in a normal week. The difference between "quiet week" and
"outbreak starting" is one or two cases, which is well inside ordinary random
variation. A parameter sweep found no settings reaching usable precision.

**A further caveat.** In confirmation mode, the combined detector performs almost
identically to EWMA on its own (38 true positives either way). Requiring both
detectors mostly just inherits the stricter one's caution. The gain is real, but
it is not coming from clever fusion.

**This is a genuine finding, not a failure.** It matches published work where
sophisticated ensembles failed to beat simple control charts. Detecting outbreaks
from very small counts is genuinely hard, and saying so is more defensible than
claiming success.

---

## 5. Code layout

```
src/
  cli.py                          command line
  consolidate.py                  312,000 files -> 104 JSONL files
  database/db.py                  all database access (19 tables)
  extraction/
    interface.py                  the shared contract
    rule_based.py                 fast extractor
    ollama_extractor.py           AI extractor
    validation.py                 4 checks + quarantine
  detection/
    baseline.py                   what is normal here
    cusum.py                      cumulative rises
    ewma.py                       smoothed drift
    combined.py                   fuses the four signals
    config.py                     every threshold, in one place
  evaluation_spatial.py           run detection per street
  evaluation_spatial_metrics.py   score it against ground truth
  alerts/
    service.py                    create alerts, dispatch SMS
    message.py                    write the warning text
  sms/service.py                  mock and live providers
  web/
    main.py                       routes
    auth.py                       login and access control
    data.py                       page queries
    templates/                    the HTML pages
tests/                            13 files, 242 tests
run_all.py                        run everything
```

About **12,200 lines** of Python, plus 242 tests.

---

## 6. Development standards used

Three Claude Code plugins are enabled at **user scope**, meaning they apply
automatically to **every project on this machine** — nothing needs configuring
per project.

| Plugin | Version | What it does |
|---|---|---|
| **ponytail** | 4.8.4 | Governs how code gets written |
| **code-review** | official | Reviews code for quality and security |
| **claude-mem** | 13.10.2 | Remembers context between sessions |

### Ponytail — writing less code

The rule is: **stop at the first solution that works.** In order —

1. Does this need to exist at all? If it is speculative, skip it.
2. Does something in this codebase already do it? Reuse it.
3. Does Python's standard library do it? Use that.
4. Can it be one line? Make it one line.
5. Only then write new code.

Other rules it enforces:

- No abstraction with only one implementation
- Fix the **root cause**, not the one place the bug was noticed
- Boring code over clever code — someone has to debug it at 3am

**Where this showed up in EpiAlert:** the SMS bug where messages were sent before
the duplicate check. The obvious fix was patching the one function that reported
it. The root-cause fix was moving the check before the provider call, which fixed
every caller at once.

### Code review — the checklist

Applied to changed code:

- No passwords or keys written into source files
- Functions under 50 lines, files under 800
- Nesting no deeper than 4 levels
- Every error handled explicitly, never silently swallowed
- Database queries use parameters, never string joining (stops SQL injection)
- Tests exist and actually check behaviour

**Where this showed up:** a hardcoded database password was found as a default
value in `src/config.py` and removed. The value is deliberately not repeated
here -- writing it into the documentation would put the credential back into
the repository. `src/database/db.py` grew past 800 lines and the
alert code was moved out into `src/alerts/`.

### The rule that mattered most

Beyond any plugin, one working rule shaped this project:

> **Never report something as working without running it.**

During the build, eight bugs were found where code existed, tests passed, and it
was reported complete — but nothing actually called it, or it ran with different
values than documented. Examples:

- A combined detector with 57 passing tests that nothing imported
- Detector parameters that were stored but ignored, so changing a threshold
  tenfold changed nothing
- A test suite reported as "20/20 passing" when the real suite had 4 failures

Each was caught by checking the actual behaviour, not the report. Two habits
prevent this:

1. **Grep for the caller.** If nothing outside its own file imports the new
   code, it is not finished.
2. **Run the whole test suite**, and quote the exact summary line.

---

## 7. Running it

```bash
# everything, end to end
.\.venv\Scripts\python.exe run_all.py

# the website
.\.venv\Scripts\python.exe -m uvicorn src.web.main:app --reload

# the tests
.\.venv\Scripts\python.exe -m pytest tests\ -q
```

Always use `.\.venv\Scripts\python.exe`. A bare `python` on this machine
resolves to a different environment that lacks the project's packages.

---

## 8. What is not built

Stated plainly, because a reviewer will ask:

- **Live SMS has never been tested against a real provider.** Only mock mode
  works and is tested. The live path exists but is unproven.
- **The RAG investigation layer** from the original design — retrieving public
  health guidance to attach to alerts — is not implemented.
- **Docker packaging** is not written.
- **Detection precision is low** (best PPV 0.119). Measured, explained above, and
  not hidden.
