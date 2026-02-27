"""Tests for block-state retry status transitions."""

import json

from src.services.block_state import BlockStateManager


def test_retry_state_transitions_and_persistence(tmp_path):
    """Processing -> failed -> processing -> dead transitions should persist."""
    manager = BlockStateManager(state_dir=str(tmp_path))
    manager.initialize()

    chain_id = 1
    tx_hash = "0x" + "ab" * 32

    manager.add_pending_tx(
        chain_id=chain_id,
        tx_hash=tx_hash,
        block_number=123,
        from_address="0x1234567890123456789012345678901234567890",
    )

    pending = manager.get_pending_txs(chain_id)[tx_hash]
    assert pending.status == "processing"
    assert pending.processing_started_at > 0

    manager.mark_failed(chain_id, tx_hash, "temporary error")
    failed = manager.get_failed_txs(chain_id)[tx_hash]
    assert failed.status == "failed"
    assert failed.last_error == "temporary error"
    assert failed.processing_started_at == 0.0

    retry_count = manager.claim_retry(chain_id, tx_hash)
    assert retry_count == 1
    pending = manager.get_pending_txs(chain_id)[tx_hash]
    assert pending.status == "processing"
    assert pending.retry_count == 1
    assert pending.processing_started_at > 0
    assert pending.last_error is None

    manager.mark_dead(chain_id, tx_hash, "permanent error")
    pending = manager.get_pending_txs(chain_id)[tx_hash]
    assert pending.status == "dead"
    assert pending.last_error == "permanent error"
    assert tx_hash not in manager.get_failed_txs(chain_id)

    # Verify persistence by reloading from disk.
    reloaded = BlockStateManager(state_dir=str(tmp_path))
    reloaded.initialize()
    persisted = reloaded.get_pending_txs(chain_id)[tx_hash]
    assert persisted.status == "dead"
    assert persisted.last_error == "permanent error"
    assert persisted.retry_count == 1


def test_claim_retry_requires_failed_status(tmp_path):
    """Retry claim should only work for failed transactions."""
    manager = BlockStateManager(state_dir=str(tmp_path))
    manager.initialize()

    chain_id = 1
    tx_hash = "0x" + "cd" * 32

    manager.add_pending_tx(chain_id=chain_id, tx_hash=tx_hash, block_number=1)
    assert manager.claim_retry(chain_id, tx_hash) is None

    manager.mark_failed(chain_id, tx_hash, "rpc timeout")
    assert manager.claim_retry(chain_id, tx_hash) == 1


def test_earliest_pending_block_excludes_dead_entries(tmp_path):
    """Dead entries should not force historical backfill scans."""
    manager = BlockStateManager(state_dir=str(tmp_path))
    manager.initialize()

    chain_id = 1
    tx_dead = "0x" + "de" * 32
    tx_active = "0x" + "ac" * 32

    manager.add_pending_tx(chain_id=chain_id, tx_hash=tx_dead, block_number=10)
    manager.mark_dead(chain_id, tx_dead, "terminal")

    manager.add_pending_tx(chain_id=chain_id, tx_hash=tx_active, block_number=100)

    assert manager.get_earliest_pending_block(chain_id) == 100


def test_legacy_pending_list_migrates_to_failed(tmp_path):
    """Legacy list format should migrate to failed entries, not processing."""
    state_file = tmp_path / "block_state.json"
    state_file.write_text(
        json.dumps(
            {
                "version": 1,
                "chains": {
                    "1": {
                        "last_processed_block": 100,
                        "pending_txs": ["0x" + "ef" * 32],
                    }
                },
            }
        )
    )

    manager = BlockStateManager(state_dir=str(tmp_path))
    manager.initialize()

    failed = manager.get_failed_txs(1)
    assert "0x" + "ef" * 32 in failed
    assert failed["0x" + "ef" * 32].status == "failed"
