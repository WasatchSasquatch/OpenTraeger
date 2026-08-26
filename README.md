# OpenTraeger

A Claude Code harness for controlling your Traeger WiFIRE grill. Chat with Claude about what you're cooking, and it will manage your grill, monitor temperatures in real time, and ping your phone when it's time to flip or pull.

Built on the [go-traeger](https://github.com/bemeek-io/go-traeger) unofficial API — no Go required, pure Python.

## How it works

Open a Claude Code session in this repo. Claude reads `CLAUDE.md` and becomes your grill assistant:

1. Connects to your grill on startup and reports current status
2. You describe what you're cooking
3. Claude proposes a cook plan (temp, flip schedule, probe target) — you confirm
4. Claude sets the grill and starts monitoring via MQTT
5. You get phone notifications when it's time to flip, when the probe hits temp, or if anything needs attention

## Setup

**1. Clone and install dependencies**
```bash
git clone https://github.com/WasatchSasquatch/OpenTraeger
cd OpenTraeger
pip install -r requirements.txt
```

**2. Create your `.env`**
```bash
cp .env.example .env
```
Fill in your Traeger app credentials:
```
TRAEGER_EMAIL=your@email.com
TRAEGER_PASSWORD=yourpassword
```
If your password contains special characters like `#`, wrap it in single quotes.

**3. Get your grill's thing name** (one-time setup)
```bash
python traeger.py list-grills
```
Copy the `thingName` value into `.env`:
```
TRAEGER_THING_NAME=80342873E0F0
```

**4. Start a session**
```bash
claude
```
Claude will connect, report grill status, and ask what you're cooking.

## Tools

### `traeger.py` — Grill commands

```bash
python traeger.py set-temp 375        # Set grill temperature
python traeger.py set-probe 160       # Set probe alarm
python traeger.py set-timer 2700      # Set a 45-minute timer (seconds)
python traeger.py clear-timer         # Clear the timer
python traeger.py shutdown            # Initiate cool-down
python traeger.py super-smoke on      # Enable Super Smoke
python traeger.py keep-warm on        # Enable Keep Warm
python traeger.py list-grills         # List grills on your account
```

All output is JSON.

### `mqtt_monitor.py` — Real-time monitoring

```bash
# Get current grill status and exit
python mqtt_monitor.py --one-shot

# Monitor a cook with threshold alerts
python mqtt_monitor.py \
  --probe-alert 160 \
  --flip-minutes 45,45 \
  --grill-target 375
```

Outputs structured lines to stdout:

| Prefix | Meaning |
|---|---|
| `STATUS` | Current temps, state, pellet level (every 5 min) |
| `ALERT probe_reached` | Probe hit target — pull the food |
| `ALERT flip_time` | Time to flip |
| `ALERT grill_swing` | Grill temp drifted >25°F from target |
| `ALERT low_pellets` | Pellets below 20% |
| `ERROR` | Connection or auth issue |

`--flip-minutes 45,45` means: alert at 45 minutes, then again 45 minutes later (90 min total).

## Phone notifications

Claude uses Claude Code's [session linking](https://docs.anthropic.com/en/docs/claude-code/session-linking) and the built-in `PushNotification` tool to send alerts to your phone. Start a session on your Mac, continue it on your phone — alerts follow you.

## Requirements

- Python 3.11+
- A Traeger WiFIRE-enabled grill
- A Traeger account (same login as the Traeger app)
- [Claude Code](https://claude.ai/code)

## Credits

API reverse-engineered by [bemeek-io/go-traeger](https://github.com/bemeek-io/go-traeger).
