import json
import shutil
from pathlib import Path

import psycopg
import pytest
from psycopg.types.json import Jsonb

from campaign import seed, store
from campaign.manifest import load_manifest_file
from db import migrate as mig
from db.connection import connect

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "campaign" / "fixtures" / "testnet-cxr-01.json"
FIXTURE_HASH = "sha256:cce1ee17af7dc01cf65f99ff3d85726f7aeacdfe7820b15fd181aa85044c5ab2"


@pytest.fixture
def migrated(db_url):
    assert mig.main([]) == 0
    return db_url


def seed_out(capsys, *args):
    code = seed.main(["--manifest", str(FIXTURE), *args])
    return code, capsys.readouterr().out.strip()


def test_migrations_apply_once_and_detect_edits(db_url, tmp_path):
    assert mig.migrate(db_url) == ["0001_campaigns"]
    assert mig.migrate(db_url) == []
    edited = tmp_path / "m"
    shutil.copytree(mig.MIGRATIONS_DIR, edited)
    (edited / "0001_campaigns.sql").write_text((edited / "0001_campaigns.sql").read_text() + "\n-- edit\n")
    with pytest.raises(mig.MigrationError, match="changed"):
        mig.migrate(db_url, edited)


def test_readonly_role_can_read_but_not_write(migrated, capsys):
    assert seed_out(capsys)[0] == 0
    import os

    role = os.environ["RADIANT_DB_READONLY_ROLE"]
    ro_url = migrated.replace("postgres:test@", f"{role}:ro-test@")
    with psycopg.connect(ro_url) as c:
        assert c.execute("SELECT count(*) FROM campaigns").fetchone()[0] == 1
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            c.execute("UPDATE campaigns SET title = 'x'")
    with psycopg.connect(ro_url, autocommit=True) as c:
        c.execute("SET default_transaction_read_only = off")  # even with read-only lifted, no grant
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("UPDATE campaigns SET title = 'x'")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("CREATE TABLE evil (x int)")


def test_seed_is_idempotent_and_hash_stable(migrated, capsys):
    assert seed_out(capsys) == (0, f"seeded testnet-cxr-01 {FIXTURE_HASH}")
    assert seed_out(capsys) == (0, f"unchanged testnet-cxr-01 {FIXTURE_HASH}")
    with connect() as conn:
        rows = store.list_campaigns(conn)
    assert len(rows) == 1 and store.stored_hash_matches(rows[0])
    assert rows[0]["submission_fee_history"] == [
        {"amount_tao": "0.01", "recipient": "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
         "effective_from_block": 0}]


def test_fee_override_does_not_move_the_hash(migrated, capsys):
    code, out = seed_out(capsys, "--fee-recipient", "5OwnerColdkey", "--fee-tao", "0.5")
    assert (code, out) == (0, f"seeded testnet-cxr-01 {FIXTURE_HASH}")
    with connect() as conn:
        fee = store.get_campaign(conn, "testnet-cxr-01")["submission_fee_history"][0]
    assert fee["recipient"] == "5OwnerColdkey" and fee["amount_tao"] == "0.5"


def test_changed_manifest_needs_replace_draft_and_is_frozen_once_open(migrated, capsys, tmp_path):
    assert seed_out(capsys)[0] == 0
    changed = json.loads(FIXTURE.read_text())
    changed["limits"]["model_load_timeout_s"] = 90
    path = tmp_path / "changed.json"
    path.write_text(json.dumps(changed))
    assert seed.main(["--manifest", str(path)]) == 3
    assert seed.main(["--manifest", str(path), "--replace-draft"]) == 0
    capsys.readouterr()
    with connect() as conn:
        store.set_status(conn, "testnet-cxr-01", "open")
    assert seed.main(["--manifest", str(FIXTURE), "--replace-draft"]) == 3
    with connect() as conn, pytest.raises(psycopg.errors.RaiseException, match="frozen"):
        conn.execute("UPDATE campaigns SET manifest = manifest || '{\"labels\":[\"x\"]}' WHERE id = 'testnet-cxr-01'")


def test_status_transitions(migrated, capsys):
    assert seed_out(capsys)[0] == 0
    with connect() as conn:
        store.set_status(conn, "testnet-cxr-01", "open")
        store.set_status(conn, "testnet-cxr-01", "closed")
        store.set_status(conn, "testnet-cxr-01", "open")
    with connect() as conn, pytest.raises(psycopg.errors.RaiseException, match="cannot move"):
        store.set_status(conn, "testnet-cxr-01", "draft")


def test_fee_history_is_append_only_in_sql(migrated, capsys):
    assert seed_out(capsys)[0] == 0
    with connect() as conn:
        store.append_fee(conn, "testnet-cxr-01", {"amount_tao": "0.02", "recipient": "5A"}, 500)
    with connect() as conn, pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        conn.execute("UPDATE campaigns SET submission_fee_history = jsonb_build_array(submission_fee_history->0) "
                     "WHERE id = 'testnet-cxr-01'")
    with connect() as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("UPDATE campaigns SET submission_fee_history = submission_fee_history || "
                     "'[{\"amount_tao\":\"1\",\"recipient\":\"5A\",\"effective_from_block\":10}]'::jsonb "
                     "WHERE id = 'testnet-cxr-01'")


def test_open_start_weights_cannot_exceed_one(migrated):
    base = json.loads(FIXTURE.read_text())
    with connect() as conn:
        for i, w in enumerate((0.6, 0.5)):
            raw = {**base, "campaign_id": f"c{i}x", "emission_rule": {**base["emission_rule"], "start_weight": w}}
            from campaign.manifest import build_manifest

            store.insert_campaign(conn, build_manifest(raw), status="draft")
        store.set_status(conn, "c0x", "open")
    with connect() as conn, pytest.raises(psycopg.errors.RaiseException, match="over 1.0"):
        store.set_status(conn, "c1x", "open")


def test_db_rejects_bad_rows_written_by_hand(migrated):
    m = load_manifest_file(FIXTURE)
    with connect() as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("INSERT INTO campaigns (id, title, manifest_version, manifest, manifest_hash, "
                     "submission_fee_history, visibility) VALUES (%s,%s,1,%s,%s,%s,%s)",
                     ("other-id", "t", Jsonb(m.pinned), m.manifest_hash,
                      Jsonb([{"amount_tao": "0.01", "recipient": "5A", "effective_from_block": 0}]),
                      Jsonb(m.visibility)))
