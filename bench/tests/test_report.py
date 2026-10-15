from __future__ import annotations

import json
from pathlib import Path

from privana_bench.privana import Reply
from privana_bench.report import (
    Balances,
    balance_check,
    percentile,
    summarize,
    to_markdown,
    write_report,
)
from privana_bench.transfers import Transfer

AMOUNT = 10_000


def landed(k: int, due: float, landed_at: float, reply: str = "ok") -> Transfer:
    return Transfer(
        k=k,
        nonce=k,
        due_at=due,
        sent_at=due,
        reply=Reply(reply, 200 if reply == "ok" else 500),  # type: ignore[arg-type]
        reply_at=due + 1.0,
        landed_at=landed_at,
        seen_nonce=k + 1,
        done_at=landed_at,
        outcome="landed",
    )


def moved(n: int) -> Balances:
    return Balances(100 * AMOUNT, (100 - n) * AMOUNT, 0, n * AMOUNT)


def test_percentile_is_nearest_rank() -> None:
    values = [float(v) for v in range(1, 11)]
    assert percentile([], 0.5) is None
    assert percentile([3.0], 0.95) == 3.0
    assert percentile(values, 0.5) == 5.0
    assert percentile(values, 0.95) == 10.0


def test_balance_check() -> None:
    two = [landed(0, 0, 6), landed(1, 6, 12)]
    assert balance_check(two, AMOUNT, moved(2)) == "ok"
    assert balance_check(two, AMOUNT, None) == "not read"
    assert balance_check(two, AMOUNT, moved(3)).startswith("mismatch: 20000 expected to move")
    unresolved = [*two, Transfer(k=2, nonce=2, due_at=12)]
    assert balance_check(unresolved, AMOUNT, moved(2)).startswith("skipped")


def test_balance_check_counts_landed_only_and_needs_both_sides() -> None:
    refused = Transfer(k=1, nonce=1, due_at=6, outcome="refused")
    assert balance_check([landed(0, 0, 6), refused], AMOUNT, moved(1)) == "ok"
    sender_only = Balances(100 * AMOUNT, 99 * AMOUNT, 0, 0)
    receiver_only = Balances(100 * AMOUNT, 100 * AMOUNT, 0, AMOUNT)
    for balances in (sender_only, receiver_only):
        assert balance_check([landed(0, 0, 6)], AMOUNT, balances).startswith("mismatch")


def test_summary_of_a_clean_run() -> None:
    transfers = [landed(0, 0, 6), landed(1, 6, 12, reply="unknown"), landed(2, 12, 24)]
    s = summarize(transfers, "completed", AMOUNT, moved(3))
    assert s["verdict"] == "PASS"
    assert (s["sent"], s["landed"], s["refused"], s["unresolved"]) == (3, 3, 0, 0)
    assert s["replies"] == {"ok": 2, "unknown": 1}
    assert s["failed_replies_that_landed"] == 1
    assert s["window_s"] == 24.0
    assert s["landed_per_minute"] == 7.5
    assert s["completion_s_median"] == 6.0
    assert s["completion_s_p95"] == 12.0
    assert s["reply_s_median"] == 1.0


def test_throughput_counts_a_stall() -> None:
    stalled = Transfer(k=1, nonce=1, due_at=6, sent_at=6, reply=Reply("ok", 200), done_at=306)
    s = summarize([landed(0, 0, 6), stalled], "stopped at transfer 1", AMOUNT, None)
    assert (s["window_s"], s["landed_per_minute"]) == (306.0, 0.2)
    refused = Transfer(k=0, nonce=0, due_at=0, reply=Reply("refused", 400), done_at=30)
    s = summarize([refused], "stopped at transfer 0", AMOUNT, None)
    assert s["landed_per_minute"] == 0.0
    meta = {"started_at": "t", "n": 1, "amount": "0.01", "symbol": "USDC", "sender": "S"}
    assert "| 0.0 |" in to_markdown({**meta, "receiver": "R"}, s, [refused])


def test_summary_fails_on_a_stop_or_a_mismatch() -> None:
    transfers = [landed(0, 0, 6)]
    assert summarize(transfers, "stopped at transfer 1: refused", AMOUNT, moved(1))["verdict"] == (
        "FAIL"
    )
    assert summarize(transfers, "completed", AMOUNT, moved(2))["verdict"] == "FAIL"
    assert summarize(transfers, "completed", AMOUNT, None)["verdict"] == "FAIL"


def test_summary_with_nothing_landed() -> None:
    s = summarize([], "aborted", AMOUNT, None)
    assert s["verdict"] == "FAIL"
    assert s["window_s"] is None and s["landed_per_minute"] is None


def test_report_files(tmp_path: Path) -> None:
    transfers = [landed(0, 0, 6)]
    transfers[0].note = "a note"
    meta = {
        "started_at": "2026-10-14T12:00:00+00:00",
        "n": 1,
        "amount": "0.01",
        "symbol": "USDC",
        "sender": "0xS",
        "receiver": "0xR",
        "poll_s": 1.0,
    }
    summary = summarize(transfers, "completed", AMOUNT, moved(1))
    markdown = write_report(tmp_path, meta, summary, transfers)
    assert markdown.startswith("# Transfers, N = 1: PASS")
    assert "Setup: poll_s 1.0" in markdown
    assert "- transfer 0 (nonce 0): a note" in markdown
    assert (tmp_path / "report.md").read_text() == markdown
    data = json.loads((tmp_path / "report.json").read_text())
    assert data["summary"]["balances_units"]["receiver_after"] == AMOUNT
    assert data["transfers"][0]["outcome"] == "landed"
