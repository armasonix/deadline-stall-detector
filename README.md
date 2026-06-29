# Deadline Stall Detector

Autonomous watchdog for **Thinkbox Deadline 10.x** render farms.

Detects silently hung Maya + V-Ray jobs (progress frozen + no files written) and applies a **three-tier escalation** without human intervention. Operators receive Telegram alerts at each tier and can override at any time via the live dashboard or Deadline Monitor.

[![CI](https://github.com/armasonix/deadline-stall-detector/actions/workflows/ci.yml/badge.svg?branch=dev)](https://github.com/armasonix/deadline-stall-detector/actions/workflows/ci.yml)

---

## The Problem

On a 20-node render farm, Maya/V-Ray jobs occasionally hang silently — process alive, Deadline reports *Rendering*, progress frozen. Root causes vary: lost texture server connection, V-Ray hitting a memory ceiling, an unresponsive Alembic cache disk. The supervisor only discovers the lost render time on manual inspection, sometimes hours later.

## What This Tool Does

- Polls the Deadline WebService every N seconds (configurable).
- Declares a **stall** only when **both signals** are absent: no progress movement **and** no new files on disk.
- Distinguishes a job that a worker is **actively rendering** from one that is merely **queued** (for example, blacklisted off the only worker and waiting for a free machine). Only actively rendering jobs are eligible for stall detection.
- Applies automatic escalation, blacklisting a stalling worker before suspending the job.

---

## Architecture

```mermaid
flowchart TD
    A["monitor_cli.py\npolling loop"] --> B["StallDetector.check()"]
    B --> C["Deadline WebService\nlocalhost:8081"]
    C --> D{"Job Stat == Active (1)?"}
    D -- No --> E[skip]
    D -- Yes --> R{"RenderingChunks > 0?"}
    R -- No --> RQ["Queued / waiting\nrefresh baseline, no stall"]
    R -- Yes --> F["JobSnapshot\nprogress + output_dir mtime"]
    F --> G{"Previous\nsnapshot exists?"}
    G -- No --> H[Store baseline]
    G -- Yes --> I{"elapsed >\nthreshold?"}
    I -- No --> J[Too early - skip]
    I -- Yes --> K{"progress moved\nOR new file on disk?"}
    K -- Yes --> L["Reset stall_count\nupdate snapshot"]
    K -- No --> M["StallHistory\nstall_count++"]
    M --> N{stall_count?}
    N -- 1 --> O["RequeueJob\n-> any worker"]
    N -- 2 --> P["SetJobMachineLimit (blacklist)\n+ RequeueJob"]
    N -- ">=3" --> Q["SuspendJob\n+ escalate"]
    O & P & Q --> S["TelegramNotifier\nwarn / critical"]
    S --> T["event_log.csv\n+ rich dashboard / watchdog log"]
```

---

## Escalation Logic

| Stall # | Action | Telegram |
|---------|--------|----------|
| 1 | `RequeueJob` -> any available worker | STALLED: {job} — requeue attempt 1 |
| 2 | Blacklist previous worker (`SetJobMachineLimit`) + `RequeueJob` | STALLED AGAIN: {job} — blacklisting {worker} |
| >= 3 | `SuspendJob` — likely scene issue | SCENE ISSUE: {job} — suspended, manual review needed |

On a single-machine farm, when the only worker is already blacklisted, the tier-3 suspend is **skipped** for that job so it stays queued instead of being suspended off the farm entirely.

---

## Project Structure

```text
deadline-stall-detector/
├── deadline_tools/
│   ├── __init__.py
│   ├── __main__.py          # python -m deadline_tools
│   ├── connection.py        # DeadlineCon wrapper + env config
│   ├── stall_detector.py    # JobSnapshot, StallHistory, check()
│   ├── recovery.py          # Three-tier escalation
│   ├── event_log.py         # CSV audit log of recovery actions
│   ├── notifier.py          # Telegram Bot API
│   └── monitor_cli.py       # rich dashboard + watchdog log
├── tests/
│   ├── unit/
│   │   ├── test_stall_detector.py
│   │   ├── test_recovery.py
│   │   └── test_notifier.py
│   └── integration/
│       ├── conftest.py
│       └── test_full_cycle.py
├── test_assets/
│   └── stall_clean.ma       # Maya scene: cube + VRayMtl + Pre-Render sleep
├── .github/workflows/ci.yml
├── terminal-profile.json    # Windows Terminal dark profile
├── pyproject.toml
├── config.example.yaml
├── .env.example
└── requirements.txt
```

---

## Setup

### Requirements

- Python 3.10+
- Thinkbox Deadline 10.x with the WebService enabled

### Install

```bash
pip install -e .          # runtime only
pip install -e ".[dev]"   # runtime + test tooling
```

### Environment Variables

Copy `.env.example` -> `.env` and fill in your values. **Never commit `.env`.**

```bash
DEADLINE_HOST=localhost
DEADLINE_PORT=8081
DEADLINE_REPO_PATH=C:\DeadlineRepository10
TELEGRAM_BOT_TOKEN=        # from @BotFather - keep secret
TELEGRAM_CHAT_ID=          # numeric chat id
TELEGRAM_PROXY=            # optional: socks5h://host:port or http://host:port
POLL_INTERVAL_SEC=60
STALL_THRESHOLD_MIN=20
```

### Enable the Deadline WebService

Deadline Monitor -> Tools -> Configure Repository Options -> Web Service -> Enable.

Verify with: `curl http://localhost:8081/api/jobs`

---

## Usage

```bash
# Quiet watchdog (default): scrolling event log
python -m deadline_tools

# Live dashboard: single fixed header, spinner only on rendering jobs
python -m deadline_tools --dashboard

# Custom threshold and poll interval
python -m deadline_tools --threshold 15 --poll 30

# Verbose logging
python -m deadline_tools --log-level DEBUG
```

### Dashboard

- A **single** fixed header shows the active threshold, poll interval and hotkeys.
- The header never duplicates on stall events or window resize (alternate-screen mode).
- A spinner rotates **only** next to jobs Deadline reports as actively *Rendering*. Suspended and queued jobs show a static marker.
- Hotkeys: `R` requeue, `S` suspend, `Q` quit.

### Watchdog Output

```text
Deadline Stall Monitor - watchdog mode (threshold=20m, poll=60s)
------------------------------------------------------------
14:31:02  Monitoring 12 active jobs...
14:32:07  [STALL]: shot_042_beauty requeue #1
14:32:08  [REQUE ] -> render-node-05
14:47:15  [STALL] AGAIN: shot_042_beauty
14:47:16  [BLKLST] worker=render-node-03
15:09:44  [SUSP ]: shot_042_beauty
```

---

## Event Log

Every recovery action is appended to `logs/stall_events.csv` (override the directory with `STALL_LOG_DIR`):

```text
timestamp,job_id,job_name,event,worker,stall_count
```

---

## Tests

```bash
# All tests (no live Deadline required - everything is mocked)
python -m pytest tests -v

# With coverage
python -m pytest tests --cov=deadline_tools --cov-report=term-missing
```

18 tests: 16 unit + 2 integration, all mock-based.

---

## CI

GitHub Actions runs the full test suite on Python 3.10 / 3.11 / 3.12 for every push and pull request to `dev` and `main`. See `.github/workflows/ci.yml`.

---

## Windows Terminal Profile

Import `terminal-profile.json` into Windows Terminal settings for the Deadline dark theme.

---

## License

MIT
