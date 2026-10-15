"""A run's numbers, written as report.json and a short report.md."""

from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .transfers import Transfer


@dataclass(frozen=True)
class Balances:
    sender_before: int
    sender_after: int
    receiver_before: int
    receiver_after: int


def percentile(values: list[float], q: float) -> float | None:
    """Nearest rank, so p95 of ten values is the largest."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


def balance_check(transfers: list[Transfer], amount: int, balances: Balances | None) -> str:
    if any(t.outcome == "unresolved" for t in transfers):
        return "skipped: an unresolved transfer can still land"
    if balances is None:
        return "not read"
    moved = amount * sum(t.outcome == "landed" for t in transfers)
    sent = balances.sender_before - balances.sender_after
    got = balances.receiver_after - balances.receiver_before
    if sent == moved and got == moved:
        return "ok"
    return f"mismatch: {moved} expected to move, sender lost {sent}, receiver gained {got}"


def summarize(
    transfers: list[Transfer], ended: str, amount: int, balances: Balances | None
) -> dict[str, Any]:
    landings = [(t.due_at, t.landed_at) for t in transfers if t.landed_at is not None]
    completion = [landed - due for due, landed in landings]
    reply_s = [t.reply_at - t.sent_at for t in transfers if t.reply is not None]
    # From the first transfer's due time until the run moved on from the last one, stalls included.
    ends = [t.done_at for t in transfers if t.done_at]
    window = max(ends) - transfers[0].due_at if ends else None
    check = balance_check(transfers, amount, balances)
    outcomes = Counter(t.outcome for t in transfers)
    failed_landed = sum(
        1 for t in transfers if t.landed_at is not None and t.reply and t.reply.kind != "ok"
    )
    return {
        "verdict": "PASS" if ended == "completed" and check == "ok" else "FAIL",
        "ended": ended,
        "sent": len(transfers),
        "landed": outcomes["landed"],
        "refused": outcomes["refused"],
        "unresolved": outcomes["unresolved"],
        "replies": dict(Counter(t.reply.kind for t in transfers if t.reply is not None)),
        "failed_replies_that_landed": failed_landed,
        "connections_that_never_opened": sum(
            t.reply.connect_failures for t in transfers if t.reply is not None
        ),
        "window_s": _round(window),
        "landed_per_minute": _round(len(landings) / window * 60) if window else None,
        "completion_s_median": _round(percentile(completion, 0.5)),
        "completion_s_p95": _round(percentile(completion, 0.95)),
        "reply_s_median": _round(percentile(reply_s, 0.5)),
        "balances": check,
        "balances_units": asdict(balances) if balances else None,
    }


_SHOWN_ABOVE = ("benchmark", "started_at", "n", "amount", "symbol", "sender", "receiver")


def to_markdown(meta: dict[str, Any], summary: dict[str, Any], transfers: list[Transfer]) -> str:
    def s(key: str) -> str:
        value = summary[key]
        return "n/a" if value is None else f"{value} s"

    per_minute = summary["landed_per_minute"]
    lines = [
        f"# Transfers, N = {meta['n']}: {summary['verdict']}",
        "",
        f"{meta['started_at']}: {meta['n']} transfers of {meta['amount']} {meta['symbol']}, "
        f"serial, {meta['sender']} to {meta['receiver']}.",
        "",
        "| Landed per minute | Median completion | p95 completion | Median API reply |",
        "| --- | --- | --- | --- |",
        f"| {'n/a' if per_minute is None else per_minute} | {s('completion_s_median')} "
        f"| {s('completion_s_p95')} | {s('reply_s_median')} |",
        "",
        f"Landed {summary['landed']} of {summary['sent']} sent, refused {summary['refused']}, "
        f"unresolved {summary['unresolved']}. Replies: {summary['replies']}. "
        f"Failed replies that landed: {summary['failed_replies_that_landed']}. "
        f"Connections that never opened: {summary['connections_that_never_opened']}. "
        f"Balances: {summary['balances']}. Ended: {summary['ended']}.",
        "",
        "Completion is counted from when each transfer was due to the first nonce read "
        "that showed it used.",
        "",
        "Setup: " + ", ".join(f"{k} {v}" for k, v in meta.items() if k not in _SHOWN_ABOVE),
    ]
    notes = [f"- transfer {t.k} (nonce {t.nonce}): {t.note}" for t in transfers if t.note]
    if notes:
        lines += ["", "Notes:", *notes]
    return "\n".join(lines) + "\n"


def write_report(
    run_dir: Path, meta: dict[str, Any], summary: dict[str, Any], transfers: list[Transfer]
) -> str:
    run_dir.mkdir(parents=True, exist_ok=True)
    data = {"meta": meta, "summary": summary, "transfers": [asdict(t) for t in transfers]}
    (run_dir / "report.json").write_text(json.dumps(data, indent=2) + "\n")
    markdown = to_markdown(meta, summary, transfers)
    (run_dir / "report.md").write_text(markdown)
    return markdown
