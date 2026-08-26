"""
Traeger MQTT Monitor — real-time grill status and threshold alerting.

Uses websocket-client to connect directly to AWS IoT's presigned WebSocket URL
with a minimal MQTT 3.1.1 implementation (subscribe + receive PUBLISH only).

Usage:
  # Get current status and exit (Claude uses this on startup)
  python mqtt_monitor.py --one-shot

  # Monitor a cook with alerts (Claude starts this as a background process)
  python mqtt_monitor.py \\
    --probe-alert 160 \\
    --flip-minutes 45,45 \\
    --grill-target 375

Output format (one per line, always flushed):
  INFO  {...}   Startup / informational
  STATUS {...}  Current grill state (on --one-shot, and every 5 min during monitoring)
  ALERT {...}   Action needed (probe reached, flip time, grill swing, low pellets)
  ERROR {...}   Something went wrong
"""

import argparse
import json
import os
import ssl
import struct
import sys
import threading
import time
import uuid
from datetime import datetime, timezone

import requests
import websocket
from dotenv import load_dotenv

load_dotenv()

AUTH_URL = "https://auth-api.iot.traegergrills.io/tokens"
API_BASE = "https://mobile-iot-api.iot.traegergrills.io"
HEADERS = {
    "Content-Type": "application/json",
    "Accept-Language": "en-us",
    "User-Agent": "Traeger/11 CFNetwork/1209 Darwin/20.2.0",
}
STATES = {
    2: "sleeping", 3: "idle", 4: "igniting", 5: "preheating",
    6: "cooking", 7: "custom_cook", 8: "cool_down", 9: "shutdown", 99: "offline",
}
STATUS_INTERVAL_SEC = 300
GRILL_SWING_THRESHOLD = 25
KEEPALIVE_SEC = 60


# ── Minimal MQTT 3.1.1 implementation ────────────────────────────────────────

def _encode_remaining(n: int) -> bytes:
    out = b""
    while True:
        byte = n % 128
        n //= 128
        if n:
            byte |= 0x80
        out += bytes([byte])
        if not n:
            return out


def _decode_remaining(data: bytes, offset: int) -> tuple[int, int]:
    value, multiplier = 0, 1
    while True:
        byte = data[offset]; offset += 1
        value += (byte & 0x7F) * multiplier
        multiplier *= 128
        if not (byte & 0x80):
            return value, offset


def mqtt_connect(client_id: str, keepalive: int = KEEPALIVE_SEC) -> bytes:
    proto = b"MQTT"
    vh = struct.pack("!H", len(proto)) + proto + bytes([4, 0x02]) + struct.pack("!H", keepalive)
    cid = client_id.encode()
    body = vh + struct.pack("!H", len(cid)) + cid
    return bytes([0x10]) + _encode_remaining(len(body)) + body


def mqtt_subscribe(topic: str, packet_id: int = 1) -> bytes:
    t = topic.encode()
    body = struct.pack("!H", packet_id) + struct.pack("!H", len(t)) + t + bytes([0])
    return bytes([0x82]) + _encode_remaining(len(body)) + body


def mqtt_pingreq() -> bytes:
    return bytes([0xC0, 0x00])


def mqtt_disconnect() -> bytes:
    return bytes([0xE0, 0x00])


def parse_packet(data: bytes) -> tuple[int, bytes]:
    """Return (packet_type, variable_header+payload bytes)."""
    if not data:
        return -1, b""
    ptype = (data[0] & 0xF0) >> 4
    length, offset = _decode_remaining(data, 1)
    return ptype, data[offset: offset + length]


def parse_publish_body(body: bytes) -> tuple[str, bytes]:
    """Extract topic and payload from a QoS-0 PUBLISH body."""
    tlen = struct.unpack("!H", body[:2])[0]
    topic = body[2: 2 + tlen].decode(errors="replace")
    payload = body[2 + tlen:]
    return topic, payload


# ── Helpers ───────────────────────────────────────────────────────────────────

def emit(kind: str, data: dict) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"{kind} {json.dumps({**data, '_ts': ts})}", flush=True)


# ── GrillMonitor ─────────────────────────────────────────────────────────────

class GrillMonitor:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.token: str | None = None
        self.token_expires: float = 0
        self.thing_name: str | None = args.thing_name or os.getenv("TRAEGER_THING_NAME")
        self.latest_status: dict = {}
        self.cook_start: float | None = None
        self.flip_index: int = 0
        self.flip_times: list[float] = []
        self.probe_alert_sent = False
        self.grill_swing_sent = False
        self.low_pellet_sent = False
        self.last_status_emit: float = 0
        self.status_received = threading.Event()
        self._ws: websocket.WebSocket | None = None
        self._ping_stop = threading.Event()

        if args.flip_minutes:
            self.flip_times = [float(m) for m in args.flip_minutes.split(",")]

    # ── Auth & API ────────────────────────────────────────────────────────────

    def _authenticate(self) -> None:
        email = os.getenv("TRAEGER_EMAIL")
        password = os.getenv("TRAEGER_PASSWORD")
        if not email or not password:
            emit("ERROR", {"error": "Missing TRAEGER_EMAIL or TRAEGER_PASSWORD in .env"})
            sys.exit(1)
        r = requests.post(AUTH_URL, json={"username": email, "password": password}, headers=HEADERS)
        r.raise_for_status()
        d = r.json()
        self.token = d["idToken"]
        self.token_expires = time.time() + float(d["expiresIn"]) - 60

    def _ensure_auth(self) -> None:
        if not self.token or time.time() >= self.token_expires:
            self._authenticate()

    def _api(self, method: str, path: str, **kwargs):
        self._ensure_auth()
        h = {**HEADERS, "Authorization": self.token}
        r = requests.request(method, f"{API_BASE}{path}", headers=h, **kwargs)
        r.raise_for_status()
        return r.json() if r.content else {}

    def _resolve_thing_name(self) -> None:
        if self.thing_name:
            return
        things = self._api("GET", "/users/self").get("things", [])
        if not things:
            emit("ERROR", {"error": "No grills found on account"})
            sys.exit(1)
        self.thing_name = things[0]["thingName"]
        emit("INFO", {
            "thing_name": self.thing_name,
            "friendly_name": things[0].get("friendlyName", ""),
            "note": "Add TRAEGER_THING_NAME to .env to skip this lookup next time",
        })

    def _get_mqtt_creds(self) -> tuple[str, float]:
        d = self._api("POST", "/mqtt-connections")
        return d["signedUrl"], float(d["expirationSeconds"])

    def _trigger_status_update(self) -> None:
        try:
            self._ensure_auth()
            h = {**HEADERS, "Authorization": self.token}
            requests.post(
                f"{API_BASE}/things/{self.thing_name}/commands",
                headers=h, json={"command": "90"}, timeout=5,
            )
        except Exception:
            pass

    # ── WebSocket + MQTT connection ───────────────────────────────────────────

    def _ws_connect(self, signed_url: str) -> websocket.WebSocket:
        ws = websocket.WebSocket(sslopt={"cert_reqs": ssl.CERT_NONE})
        ws.connect(signed_url, subprotocols=["mqtt"])

        # MQTT CONNECT
        ws.send_binary(mqtt_connect(client_id=str(uuid.uuid4())))
        resp = ws.recv()
        if isinstance(resp, str):
            resp = resp.encode()
        ptype, _ = parse_packet(resp)
        if ptype != 2:  # CONNACK
            raise RuntimeError(f"Expected CONNACK (type 2), got type {ptype}")

        # MQTT SUBSCRIBE
        topic = f"prod/thing/update/{self.thing_name}"
        ws.send_binary(mqtt_subscribe(topic))
        resp = ws.recv()
        if isinstance(resp, str):
            resp = resp.encode()
        ptype, _ = parse_packet(resp)
        if ptype != 9:  # SUBACK
            raise RuntimeError(f"Expected SUBACK (type 9), got type {ptype}")

        return ws

    def _start_ping_thread(self, ws: websocket.WebSocket) -> None:
        self._ping_stop.clear()

        def _ping():
            while not self._ping_stop.wait(KEEPALIVE_SEC - 5):
                try:
                    ws.send_binary(mqtt_pingreq())
                except Exception:
                    break

        t = threading.Thread(target=_ping, daemon=True)
        t.start()

    def _stop_ping_thread(self) -> None:
        self._ping_stop.set()

    # ── Message handling ──────────────────────────────────────────────────────

    def _on_mqtt_message(self, payload_bytes: bytes) -> None:
        try:
            payload = json.loads(payload_bytes)
            status = payload.get("status", payload)
            self.latest_status = status
            self.status_received.set()
            self._check_thresholds(status)
            now = time.time()
            if now - self.last_status_emit >= STATUS_INTERVAL_SEC:
                emit("STATUS", self._format_status(status))
                self.last_status_emit = now
        except Exception:
            pass

    def _recv_loop(self, ws: websocket.WebSocket, stop_after_first: bool = False) -> None:
        while True:
            try:
                data = ws.recv()
                if isinstance(data, str):
                    data = data.encode()
                if not data:
                    break
                ptype, body = parse_packet(data)
                if ptype == 3:    # PUBLISH
                    _, payload_bytes = parse_publish_body(body)
                    self._on_mqtt_message(payload_bytes)
                    if stop_after_first:
                        return
                elif ptype == 13:  # PINGRESP — ignore
                    pass
            except websocket.WebSocketConnectionClosedException:
                break
            except Exception:
                break

    # ── Status formatting ─────────────────────────────────────────────────────

    def _format_status(self, s: dict) -> dict:
        state_code = s.get("system_status", 0)
        units = "°F" if s.get("units", 1) == 1 else "°C"
        result = {
            "state": STATES.get(state_code, f"unknown({state_code})"),
            "grill_temp": s.get("grill"),
            "set_temp": s.get("set"),
            "probe_temp": s.get("probe") if s.get("probe_con") else None,
            "probe_connected": bool(s.get("probe_con")),
            "probe_alarm_set": s.get("probe_set") if s.get("probe_con") else None,
            "pellet_level": s.get("pellet_level"),
            "units": units,
        }
        if self.cook_start:
            result["elapsed_min"] = round((time.time() - self.cook_start) / 60, 1)
        return result

    # ── Threshold checks ──────────────────────────────────────────────────────

    def _check_thresholds(self, s: dict) -> None:
        state_code = s.get("system_status", 0)

        if self.args.probe_alert and not self.probe_alert_sent:
            probe = s.get("probe", 0)
            if s.get("probe_con") and probe >= self.args.probe_alert:
                emit("ALERT", {
                    "type": "probe_reached",
                    "probe_temp": probe,
                    "target": self.args.probe_alert,
                    "message": f"Probe hit {probe:.0f}°F — time to pull your food off the grill!",
                })
                self.probe_alert_sent = True

        if self.flip_times and self.cook_start and self.flip_index < len(self.flip_times):
            target_min = sum(self.flip_times[: self.flip_index + 1])
            elapsed_min = (time.time() - self.cook_start) / 60
            if elapsed_min >= target_min:
                flip_num = self.flip_index + 1
                emit("ALERT", {
                    "type": "flip_time",
                    "flip_number": flip_num,
                    "elapsed_min": round(elapsed_min, 1),
                    "message": f"Time to flip! Flip #{flip_num} — {elapsed_min:.0f} minutes elapsed.",
                })
                self.flip_index += 1

        if self.args.grill_target and state_code == 6:
            grill = s.get("grill", 0)
            swing = abs(grill - self.args.grill_target)
            if swing > GRILL_SWING_THRESHOLD and not self.grill_swing_sent:
                emit("ALERT", {
                    "type": "grill_swing",
                    "grill_temp": grill,
                    "target": self.args.grill_target,
                    "deviation": round(grill - self.args.grill_target, 1),
                    "message": f"Grill is at {grill:.0f}°F — {swing:.0f}° off your {self.args.grill_target:.0f}°F target.",
                })
                self.grill_swing_sent = True
            elif swing <= 15:
                self.grill_swing_sent = False

        pellet = s.get("pellet_level")
        if pellet is not None and pellet < 20 and not self.low_pellet_sent:
            emit("ALERT", {
                "type": "low_pellets",
                "pellet_level": pellet,
                "message": f"Pellet level is at {pellet}%. Refill soon to avoid a temperature drop.",
            })
            self.low_pellet_sent = True

    # ── Entry points ──────────────────────────────────────────────────────────

    def run_one_shot(self) -> None:
        self._ensure_auth()
        self._resolve_thing_name()
        signed_url, _ = self._get_mqtt_creds()

        ws = self._ws_connect(signed_url)
        self._trigger_status_update()

        # Wait up to 12s for the first PUBLISH
        done = threading.Event()

        def _recv():
            self._recv_loop(ws, stop_after_first=True)
            done.set()

        t = threading.Thread(target=_recv, daemon=True)
        t.start()
        got_it = done.wait(timeout=12)

        try:
            ws.send_binary(mqtt_disconnect())
            ws.close()
        except Exception:
            pass

        if got_it and self.latest_status:
            emit("STATUS", self._format_status(self.latest_status))
        else:
            emit("ERROR", {"error": "No status received within 12s — is the grill on and connected?"})

    def run(self) -> None:
        self._ensure_auth()
        self._resolve_thing_name()

        if self.flip_times:
            self.cook_start = time.time()
        emit("INFO", {
            "monitoring_started": True,
            "thing_name": self.thing_name,
            "flip_schedule_minutes": self.flip_times or None,
            "probe_alert": self.args.probe_alert,
            "grill_target": self.args.grill_target,
        })

        while True:
            try:
                signed_url, expires_in = self._get_mqtt_creds()
                ws = self._ws_connect(signed_url)
                self._trigger_status_update()
                self._start_ping_thread(ws)

                reconnect_at = time.time() + expires_in - 120

                def _recv_until_reconnect():
                    while time.time() < reconnect_at:
                        try:
                            data = ws.recv()
                            if isinstance(data, str):
                                data = data.encode()
                            if not data:
                                return
                            ptype, body = parse_packet(data)
                            if ptype == 3:
                                _, payload_bytes = parse_publish_body(body)
                                self._on_mqtt_message(payload_bytes)
                            elif ptype == 13:
                                pass
                        except Exception:
                            return

                recv_thread = threading.Thread(target=_recv_until_reconnect, daemon=True)
                recv_thread.start()
                recv_thread.join()
                self._stop_ping_thread()

                try:
                    ws.send_binary(mqtt_disconnect())
                    ws.close()
                except Exception:
                    pass

                emit("INFO", {"mqtt_reconnecting": True})

            except KeyboardInterrupt:
                break
            except Exception as e:
                emit("ERROR", {"error": str(e), "retrying_in": 10})
                time.sleep(10)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Traeger MQTT Monitor")
    parser.add_argument("--thing-name", help="Grill thing name (overrides TRAEGER_THING_NAME env var)")
    parser.add_argument("--probe-alert", type=float, metavar="TEMP",
                        help="ALERT when probe temp reaches this °F")
    parser.add_argument("--flip-minutes", metavar="MIN[,MIN...]",
                        help="Flip intervals in minutes, e.g. '45,45' = flip at 45 min then 90 min")
    parser.add_argument("--grill-target", type=float, metavar="TEMP",
                        help="ALERT if grill swings >25°F from this target")
    parser.add_argument("--one-shot", action="store_true",
                        help="Fetch current status once and exit")
    args = parser.parse_args()

    GrillMonitor(args).run_one_shot() if args.one_shot else GrillMonitor(args).run()


if __name__ == "__main__":
    main()
