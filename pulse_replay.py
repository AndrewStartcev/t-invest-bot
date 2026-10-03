"""Replay normalized Pulse snapshots. No network or brokerage operations."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path


REQUIRED = {"id", "instrument", "side", "time"}


def read_snapshot(path: Path) -> tuple[str, list[dict]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("profile"), str) or not data["profile"]:
        raise ValueError("snapshot.profile must be a nonempty string")
    trades = data.get("trades")
    if not isinstance(trades, list):
        raise ValueError("snapshot.trades must be a list")
    seen = set()
    for trade in trades:
        if not isinstance(trade, dict) or not REQUIRED.issubset(trade):
            raise ValueError("each trade needs id, instrument, side and time")
        if not all(isinstance(trade[key], str) and trade[key] for key in REQUIRED):
            raise ValueError("required trade fields must be nonempty strings")
        if trade["side"] not in {"BUY", "SELL"}:
            raise ValueError("trade.side must be BUY or SELL")
        if trade["id"] in seen:
            raise ValueError("duplicate trade id in one snapshot")
        seen.add(trade["id"])
    return data["profile"], trades


def write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".pulse-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def replay(state_path: Path, snapshot_path: Path) -> list[dict]:
    profile, trades = read_snapshot(snapshot_path)
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    if not isinstance(state, dict):
        raise ValueError("state must be an object")
    baseline = profile not in state
    known = state.setdefault(profile, [])
    if not isinstance(known, list) or not all(isinstance(item, str) for item in known):
        raise ValueError("invalid state for profile")
    known_set = set(known)
    fresh = [] if baseline else [trade for trade in trades if trade["id"] not in known_set]
    known.extend(trade["id"] for trade in trades if trade["id"] not in known_set)
    write_state(state_path, state)
    return fresh


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--state", type=Path, default=Path(".local/pulse-state.json"))
    args = parser.parse_args()
    try:
        fresh = replay(args.state, args.snapshot)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    for trade in fresh:
        print(json.dumps(trade, ensure_ascii=False, sort_keys=True))
    print(f"new events: {len(fresh)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
