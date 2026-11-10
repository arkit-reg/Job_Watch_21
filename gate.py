"""Decides in a second whether this run should do the full check. Standard library only, so it runs
before anything is installed. Full checks happen once per time slot (every 3 hours on the clock,
Eastern time: 12 AM, 3, 6, 9 AM, 12 PM, 3, 6, 9 PM). GitHub's timer wakes the workflow every
15 minutes; the first wake-up in each slot does the check, the rest stop here."""
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/New_York")


def every_hours() -> int:
    try:
        import tomllib
        with open("config.toml", "rb") as f:
            return max(1, int(tomllib.load(f).get("run_every_hours", 3)))
    except Exception:
        return 3


def slot_key(now: datetime | None = None, every: int | None = None) -> str:
    every = every or every_hours()
    local = (now or datetime.now(TZ)).astimezone(TZ)
    start = local.replace(hour=local.hour - local.hour % every, minute=0, second=0, microsecond=0)
    return start.strftime("%Y-%m-%d %H:00")


def should_run(event: str, state: dict, now: datetime | None = None) -> bool:
    return event != "schedule" or state.get("last_slot") != slot_key(now)


if __name__ == "__main__":
    try:
        with open("state.json", encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        state = {}
    run = should_run(os.environ.get("GITHUB_EVENT_NAME", ""), state)
    slot = slot_key()
    print(f"Time slot {slot} (Eastern): " + ("running the full check." if run else
          f"already checked (last slot {state.get('last_slot')}), so this wake-up stops here."))
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as out:
            out.write(f"run={'true' if run else 'false'}\n")
