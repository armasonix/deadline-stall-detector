# deadline-stall-detector

Watchdog that detects stalled render jobs in Deadline 10 and takes
automatic recovery actions with Telegram notifications.

## How it works

1. Polls all jobs with `Rendering` status every N seconds
2. Detects a stall when **both** signals fire simultaneously:
   - Job progress has not changed since the previous snapshot
   - No new files written to `output_dir` within the threshold window
3. Applies tiered recovery based on stall count:

| Stall count | Action |
|-------------|--------|
| 1 | Requeue job, send warning |
| 2 | Blacklist worker + requeue, send warning |
| 3+ | Suspend job, send critical alert |

## Quick start

```bash
# 1. Copy config
cp config.example.yaml config.yaml

# 2. Set credentials (or edit config.yaml)
export DEADLINE_HOST=your-deadline-server
export TELEGRAM_BOT_TOKEN=your-token
export TELEGRAM_CHAT_ID=your-chat-id

# 3. Install dependencies
pip install -r requirements.txt

# 4. Run
python -m deadline_tools
python -m deadline_tools --config path/to/config.yaml
python -m deadline_tools --once   # single check, for cron
```

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DEADLINE_HOST` | `localhost` | Deadline WebService host |
| `DEADLINE_PORT` | `8082` | Deadline WebService port |
| `DEADLINE_REPO_PATH` | `C:\DeadlineRepository10` | Path to Deadline repo |
| `TELEGRAM_BOT_TOKEN` | - | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | - | Target chat or channel ID |

## Project structure
deadline_tools/
connection.py - Deadline API connection
stall_detector.py - JobSnapshot, StallHistory, StallDetector
recovery.py - Tiered recovery actions
notifier.py - Telegram notifications
monitor_cli.py - Rich dashboard + polling loop
__main__.py - Entry point
tests/
unit/ - Unit tests (no Deadline required)
integration/ - Integration tests (requires live Deadline)


text

## Running tests

```bash
python -m pytest tests/unit -v
```
