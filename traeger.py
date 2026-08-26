"""
Traeger Grill CLI — REST command wrapper for Claude to call.

Usage:
  python traeger.py list-grills
  python traeger.py set-temp <°F>
  python traeger.py set-probe <°F>
  python traeger.py set-timer <seconds>
  python traeger.py clear-timer
  python traeger.py shutdown
  python traeger.py super-smoke <on|off>
  python traeger.py keep-warm <on|off>
  python traeger.py request-status   (triggers MQTT push, use mqtt_monitor.py to read it)
"""

import json
import os
import sys
import time

import requests
from dotenv import load_dotenv

load_dotenv()

AUTH_URL = "https://auth-api.iot.traegergrills.io/tokens"
API_BASE = "https://mobile-iot-api.iot.traegergrills.io"
HEADERS = {
    "Content-Type": "application/json",
    "Accept-Language": "en-us",
    "User-Agent": "Traeger/11 CFNetwork/1209 Darwin/20.2.0",
}


def authenticate(email: str, password: str) -> tuple[str, float]:
    r = requests.post(AUTH_URL, json={"username": email, "password": password}, headers=HEADERS)
    r.raise_for_status()
    d = r.json()
    return d["idToken"], time.time() + float(d["expiresIn"]) - 60


def api(method: str, path: str, token: str, **kwargs):
    h = {**HEADERS, "Authorization": token}
    r = requests.request(method, f"{API_BASE}{path}", headers=h, **kwargs)
    r.raise_for_status()
    return r.json() if r.content else {}


def send_command(thing_name: str, command: str, token: str) -> None:
    api("POST", f"/things/{thing_name}/commands", token, json={"command": command})


def get_creds() -> tuple[str, str]:
    email = os.getenv("TRAEGER_EMAIL")
    password = os.getenv("TRAEGER_PASSWORD")
    if not email or not password:
        out({"error": "Set TRAEGER_EMAIL and TRAEGER_PASSWORD in .env"})
        sys.exit(1)
    return email, password


def get_thing(token: str) -> str:
    tn = os.getenv("TRAEGER_THING_NAME")
    if tn:
        return tn
    things = api("GET", "/users/self", token).get("things", [])
    if not things:
        out({"error": "No grills found on this account"})
        sys.exit(1)
    return things[0]["thingName"]


def out(data) -> None:
    print(json.dumps(data))


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    email, password = get_creds()
    token, _ = authenticate(email, password)

    if cmd == "list-grills":
        things = api("GET", "/users/self", token).get("things", [])
        out(things)

    elif cmd == "mqtt-creds":
        d = api("POST", "/mqtt-connections", token)
        out({"signed_url": d["signedUrl"], "expires_in": d["expirationSeconds"]})

    elif cmd == "set-temp":
        if len(sys.argv) < 3:
            out({"error": "Usage: set-temp <°F>"})
            sys.exit(1)
        tn = get_thing(token)
        temp = int(sys.argv[2])
        send_command(tn, f"11,{temp}", token)
        out({"ok": True, "set_temp": temp})

    elif cmd == "set-probe":
        if len(sys.argv) < 3:
            out({"error": "Usage: set-probe <°F>"})
            sys.exit(1)
        tn = get_thing(token)
        temp = int(sys.argv[2])
        send_command(tn, f"14,{temp}", token)
        out({"ok": True, "probe_alarm": temp})

    elif cmd == "set-timer":
        if len(sys.argv) < 3:
            out({"error": "Usage: set-timer <seconds>"})
            sys.exit(1)
        tn = get_thing(token)
        seconds = int(sys.argv[2])
        send_command(tn, f"12,{seconds:05d}", token)
        out({"ok": True, "timer_seconds": seconds, "timer_minutes": round(seconds / 60, 1)})

    elif cmd == "clear-timer":
        tn = get_thing(token)
        send_command(tn, "12,00000", token)
        out({"ok": True, "timer": "cleared"})

    elif cmd == "shutdown":
        tn = get_thing(token)
        send_command(tn, "17", token)
        out({"ok": True, "action": "shutdown initiated"})

    elif cmd == "super-smoke":
        if len(sys.argv) < 3:
            out({"error": "Usage: super-smoke <on|off>"})
            sys.exit(1)
        tn = get_thing(token)
        enabled = sys.argv[2].lower() == "on"
        send_command(tn, "21" if enabled else "20", token)
        out({"ok": True, "super_smoke": enabled})

    elif cmd == "keep-warm":
        if len(sys.argv) < 3:
            out({"error": "Usage: keep-warm <on|off>"})
            sys.exit(1)
        tn = get_thing(token)
        enabled = sys.argv[2].lower() == "on"
        send_command(tn, "19" if enabled else "18", token)
        out({"ok": True, "keep_warm": enabled})

    elif cmd == "request-status":
        tn = get_thing(token)
        send_command(tn, "90", token)
        out({"ok": True, "note": "Status update triggered — use mqtt_monitor.py --one-shot to read it"})

    else:
        out({
            "commands": [
                "list-grills",
                "set-temp <°F>",
                "set-probe <°F>",
                "set-timer <seconds>",
                "clear-timer",
                "shutdown",
                "super-smoke <on|off>",
                "keep-warm <on|off>",
                "request-status",
                "mqtt-creds",
            ]
        })


if __name__ == "__main__":
    main()
