"""
cook_timer.py — Clock-accurate flip alerts + regular probe polling.

Flip alerts use threading.Timer so they fire at exactly the right wall-clock
time, independent of MQTT message frequency. Probe temps are fetched every
--poll-interval seconds via a one-shot MQTT query.

Usage:
  python cook_timer.py --probe-target 165 --flip-minutes 10
  python cook_timer.py --probe-target 165 --flip-minutes 10,10 --poll-interval 60

  # Phase 2 of a two-phase cook — start it right when you crank the heat
  python cook_timer.py --probe-target 165 --flip-minutes 10

Output (one JSON line per event, always flushed):
  INFO  {"cook_timer_started": true, ...}
  FLIP  {"flip_number": 1, "elapsed_min": 10.0, "message": "..."}
  POLL  {"probe_temp": 155, "grill_temp": 400, "elapsed_min": 8.5, "state": "cooking"}
  DONE  {"probe_temp": 166, "elapsed_min": 22.3, "message": "Pull it off!"}
  ERROR {"error": "..."}
"""

import argparse
import json
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone


def emit(kind: str, data: dict) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"{kind} {json.dumps({**data, '_ts': ts})}", flush=True)


def get_grill_status() -> dict | None:
    """Fetch current grill status via a one-shot MQTT query."""
    try:
        result = subprocess.run(
            [sys.executable, "mqtt_monitor.py", "--one-shot"],
            capture_output=True, text=True, timeout=20,
        )
        for line in result.stdout.splitlines():
            if line.startswith("STATUS "):
                return json.loads(line[7:])
    except Exception:
        pass
    return None


class CookTimer:
    def __init__(self, args: argparse.Namespace):
        self.probe_target = args.probe_target
        self.poll_interval = args.poll_interval
        self.flip_times: list[float] = (
            [float(m) for m in args.flip_minutes.split(",")]
            if args.flip_minutes else []
        )
        self.start_time = time.time()
        self._done = threading.Event()
        self._flip_timers: list[threading.Timer] = []

    def _elapsed_min(self) -> float:
        return (time.time() - self.start_time) / 60

    def _schedule_flips(self) -> None:
        cumulative = 0.0
        for i, minutes in enumerate(self.flip_times):
            cumulative += minutes
            flip_num = i + 1
            t = threading.Timer(
                cumulative * 60,
                self._fire_flip,
                args=(flip_num, cumulative),
            )
            t.daemon = True
            t.start()
            self._flip_timers.append(t)

    def _fire_flip(self, flip_num: int, target_min: float) -> None:
        if not self._done.is_set():
            emit("FLIP", {
                "flip_number": flip_num,
                "elapsed_min": round(self._elapsed_min(), 1),
                "message": f"Time to flip! Flip #{flip_num} — {target_min:.0f} min elapsed.",
            })

    def _poll_loop(self) -> None:
        # Poll immediately on start, then every poll_interval seconds
        while not self._done.is_set():
            status = get_grill_status()

            if status is None:
                emit("ERROR", {"error": "Could not reach grill — check WiFi"})
            else:
                probe = status.get("probe_temp")
                grill = status.get("grill_temp")
                elapsed = round(self._elapsed_min(), 1)

                emit("POLL", {
                    "probe_temp": probe,
                    "grill_temp": grill,
                    "state": status.get("state"),
                    "elapsed_min": elapsed,
                })

                if probe is not None and probe >= self.probe_target:
                    emit("DONE", {
                        "probe_temp": probe,
                        "elapsed_min": elapsed,
                        "message": f"Probe hit {probe:.0f}°F — pull it off the grill!",
                    })
                    self._done.set()
                    return

            self._done.wait(timeout=self.poll_interval)

    def run(self) -> None:
        emit("INFO", {
            "cook_timer_started": True,
            "probe_target": self.probe_target,
            "flip_schedule_minutes": self.flip_times,
            "poll_interval_sec": self.poll_interval,
        })
        self._schedule_flips()
        try:
            self._poll_loop()
        except KeyboardInterrupt:
            pass
        finally:
            for t in self._flip_timers:
                t.cancel()


def main():
    parser = argparse.ArgumentParser(
        description="Clock-accurate cook timer with probe polling"
    )
    parser.add_argument("--probe-target", type=float, required=True,
                        help="Pull temp °F — timer exits when probe reaches this")
    parser.add_argument("--flip-minutes", metavar="MIN[,MIN...]",
                        help="Flip intervals e.g. '10,10' = flip at 10 min, again at 20 min")
    parser.add_argument("--poll-interval", type=int, default=60,
                        help="Seconds between probe polls (default: 60)")
    args = parser.parse_args()
    CookTimer(args).run()


if __name__ == "__main__":
    main()
