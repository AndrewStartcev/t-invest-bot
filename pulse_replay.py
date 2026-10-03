"""Replay sanitized Pulse snapshots by instrument trade counts. No network or orders."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path


def read_snapshot(path: Path) -> tuple[str, list[dict]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("profile"), str) or not data["profile"]:
        raise ValueError("snapshot.profile must be a nonempty string")
    instruments = data.get("instruments")
    if not isinstance(instruments, list):
        raise ValueError("snapshot.instruments must be a list")
    keys = set()
    for item in instruments:
        if not isinstance(item, dict):
            raise ValueError("instrument must be an object")
        ticker, class_code = item.get("ticker"), item.get("classCode")
        count, history = item.get("totalOperationsCount"), item.get("history")
        if not all(isinstance(value, str) and value for value in (ticker, class_code)):
            raise ValueError("instrument needs ticker and classCode")
        if type(count) is not int or count < 0 or not isinstance(history, list):
            raise ValueError("instrument needs nonnegative totalOperationsCount and history list")
        key = f"{ticker}:{class_code}"
        if key in keys:
            raise ValueError(f"duplicate instrument {key}")
        keys.add(key)
        for trade in history:
            if not isinstance(trade, dict) or not all(
                isinstance(trade.get(field), str) and trade[field]
                for field in ("tradeDateTime", "action", "currency")
            ) or trade["action"] not in ("buy", "sell") or not isinstance(trade.get("averagePrice"), (int, float)):
                raise ValueError(f"invalid history item for {key}")
    return data["profile"], instruments


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
    profile, instruments = read_snapshot(snapshot_path)
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    if not isinstance(state, dict):
        raise ValueError("state must be an object")
    profile_state = state.setdefault(profile, {})
    if not isinstance(profile_state, dict):
        raise ValueError("invalid profile state")
    fresh = []
    updates = {}
    for item in instruments:
        ticker, class_code = item["ticker"], item["classCode"]
        key = f"{ticker}:{class_code}"
        count = item["totalOperationsCount"]
        previous = profile_state.get(key)
        if previous is not None and (type(previous) is not int or previous < 0):
            raise ValueError(f"invalid state count for {key}")
        if previous is not None:
            delta = count - previous
            if delta < 0:
                raise ValueError(f"count decreased for {key}; manual review needed")
            if delta > len(item["history"]):
                raise ValueError(f"history incomplete for {key}: need {delta}, got {len(item['history'])}")
            for offset, trade in enumerate(reversed(item["history"][:delta]), start=1):
                fresh.append({
                    "profile": profile,
                    "ticker": ticker,
                    "classCode": class_code,
                    "sequence": previous + offset,
                    "tradeDateTime": trade["tradeDateTime"],
                    "action": trade["action"],
                    "currency": trade["currency"],
                    "averagePrice": trade["averagePrice"],
                })
        updates[key] = count
    profile_state.update(updates)
    write_state(state_path, state)
    return sorted(fresh, key=lambda trade: (trade["tradeDateTime"], trade["ticker"], trade["sequence"]))


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
