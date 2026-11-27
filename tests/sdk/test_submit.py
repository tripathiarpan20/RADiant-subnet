"""Unit tests for radiant_sdk.submit fee resolution and CLI edge cases."""

from __future__ import annotations

from pathlib import Path

import pytest

from radiant_sdk import cli
from radiant_sdk.submit import active_fee_at, submit


def test_active_fee_at_ignores_future_scheduled_changes():
    history = [
        {"amount_tao": "0.01", "recipient": "5OldRecipient", "effective_from_block": 0},
        {"amount_tao": "0.50", "recipient": "5FutureRecipient", "effective_from_block": 2000},
    ]
    assert active_fee_at(history, 1500) == {"amount_tao": "0.01", "recipient": "5OldRecipient"}
    assert active_fee_at(history, 2000) == {"amount_tao": "0.50", "recipient": "5FutureRecipient"}
    with pytest.raises(ValueError, match="no fee"):
        active_fee_at([{"amount_tao": "0.01", "recipient": "5A", "effective_from_block": 10}], 5)


def test_submit_rejects_mismatched_payment_ref_flags(tmp_path):
    zp = tmp_path / "solution.zip"
    zp.write_bytes(b"")
    with pytest.raises(ValueError, match="together"):
        submit(zp, "testnet-cxr-01", api_url="http://localhost:8080", network="test", netuid=292,
               wallet_name="w", wallet_hotkey="h", payment_block=100, payment_tx=None)
    with pytest.raises(ValueError, match="together"):
        submit(zp, "testnet-cxr-01", api_url="http://localhost:8080", network="test", netuid=292,
               wallet_name="w", wallet_hotkey="h", payment_block=None, payment_tx=3)


def test_miner_env_strips_surrounding_quotes(tmp_path, monkeypatch):
    env_file = tmp_path / "miner.env"
    env_file.write_text('RADIANT_API_URL="https://api.example.org"\nWALLET_NAME=\'my_wallet\'\n')
    monkeypatch.delenv("RADIANT_API_URL", raising=False)
    monkeypatch.delenv("WALLET_NAME", raising=False)
    cli._miner_env(str(env_file))
    import os

    assert os.environ["RADIANT_API_URL"] == "https://api.example.org"
    assert os.environ["WALLET_NAME"] == "my_wallet"


def test_cli_submit_requires_netuid_on_finney(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("RADIANT_NETUID", raising=False)
    zp = tmp_path / "solution.zip"
    zp.write_bytes(b"")
    code = cli.main(["submit", str(zp), "--campaign", "c1", "--network", "finney"])
    assert code == 2
    assert "RADIANT_NETUID" in capsys.readouterr().err
