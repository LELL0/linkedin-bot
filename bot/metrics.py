"""Timing records for each accept and reply. Stored outside the repository, never with message text."""

import json
import logging
import statistics
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from .config import CONFIG

log = logging.getLogger(__name__)


def record(kind: str, path: Path | None = None, **fields) -> None:
    entry = {"at": round(time.time(), 3), "kind": kind, **fields}
    with (path or CONFIG.metrics_path).open("a") as file:
        file.write(json.dumps(entry) + "\n")


def load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def percentile(values: list[float], share: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(share * len(ordered)))]


def timing_line(label: str, entries: list[dict], field: str) -> str:
    values = [e[field] for e in entries if isinstance(e.get(field), (int, float))]
    if not values:
        return f"  {label:<22} no data"
    return (
        f"  {label:<22} median {statistics.median(values):6.1f}s"
        f"   p95 {percentile(values, 0.95):6.1f}s   max {max(values):6.1f}s"
    )


def counts(entries: list[dict], field: str) -> str:
    found = Counter(str(e.get(field, "unknown")) for e in entries)
    return ", ".join(f"{name} {count}" for name, count in found.most_common())


def report(entries: list[dict]) -> str:
    accepts = [e for e in entries if e["kind"] == "accept"]
    replies = [e for e in entries if e["kind"] == "reply"]
    lines = [f"Accepted invitations: {len(accepts)}"]
    if accepts:
        lines += [
            timing_line("sent to accepted", accepts, "total_s"),
            timing_line("sent to noticed", accepts, "detect_s"),
            timing_line("accept call", accepts, "action_s"),
            f"  via: {counts(accepts, 'via')}",
            f"  noticed by: {counts(accepts, 'trigger')}",
        ]
    lines.append(f"Replies: {len(replies)} answering {sum(e.get('messages', 1) for e in replies)} messages")
    if replies:
        lines += [
            timing_line("first message to reply", replies, "total_s"),
            timing_line("last message to reply", replies, "since_last_s"),
            timing_line("noticed", replies, "detect_s"),
            timing_line("waiting for quiet", replies, "quiet_s"),
            timing_line("model draft", replies, "draft_s"),
            timing_line("draft check", replies, "check_s"),
            timing_line("sending", replies, "send_s"),
            f"  outcome: {counts(replies, 'outcome')}",
            f"  model: {counts(replies, 'model')}",
            f"  noticed by: {counts(replies, 'trigger')}",
        ]
    recent = (accepts + replies)[-10:]
    if recent:
        lines.append("Latest:")
        for entry in sorted(recent, key=lambda e: e["at"]):
            when = datetime.fromtimestamp(entry["at"]).strftime("%d/%m %H:%M:%S")
            detail = entry.get("via") or entry.get("outcome", "")
            lines.append(f"  {when} {entry['kind']:<6} {entry.get('total_s', 0):6.1f}s {detail}")
    return "\n".join(lines)


def print_stats() -> None:
    print(report(load(CONFIG.metrics_path)))
