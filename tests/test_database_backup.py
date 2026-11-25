import sqlite3

import pytest

import main as procureai_main


def test_database_backup_is_consistent_and_restorable(tmp_path, monkeypatch):
    database = tmp_path / "procureai.db"
    backup_dir = tmp_path / "backups"
    monkeypatch.setenv("PROCUREAI_DB_PATH", str(database))
    monkeypatch.setenv("PROCUREAI_BACKUP_DIR", str(backup_dir))
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE procurement_records (id INTEGER PRIMARY KEY, reference TEXT NOT NULL)")
        db.execute("INSERT INTO procurement_records(reference) VALUES(?)", ("PR-TEST-001",))

    result = procureai_main._create_database_backup()
    backup_path = backup_dir / result["filename"]
    assert backup_path.is_file()
    assert result["integrity"] == "ok"
    with sqlite3.connect(backup_path) as restored:
        assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert restored.execute("SELECT reference FROM procurement_records").fetchone()[0] == "PR-TEST-001"


def test_database_backup_retention_keeps_seven_latest(tmp_path, monkeypatch):
    database = tmp_path / "procureai.db"
    backup_dir = tmp_path / "backups"
    monkeypatch.setenv("PROCUREAI_DB_PATH", str(database))
    monkeypatch.setenv("PROCUREAI_BACKUP_DIR", str(backup_dir))
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE records (id INTEGER PRIMARY KEY)")

    for _ in range(9):
        procureai_main._create_database_backup()

    backups = list(backup_dir.glob("procureai-*.sqlite3"))
    assert len(backups) == 7
    assert len(procureai_main._list_database_backups()) == 7


def test_restore_verification_uses_isolated_copy_and_preserves_live_database(tmp_path, monkeypatch):
    database = tmp_path / "procureai.db"
    backup_dir = tmp_path / "backups"
    monkeypatch.setenv("PROCUREAI_DB_PATH", str(database))
    monkeypatch.setenv("PROCUREAI_BACKUP_DIR", str(backup_dir))
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE procurement_records (id INTEGER PRIMARY KEY, reference TEXT NOT NULL)")
        db.execute("INSERT INTO procurement_records(reference) VALUES(?)", ("PR-LIVE-001",))
    backup = procureai_main._create_database_backup()
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO procurement_records(reference) VALUES(?)", ("PR-LIVE-002",))

    result = procureai_main._verify_database_backup(backup["filename"])
    assert result["restore_test"] == "passed"
    assert result["integrity"] == "ok"
    assert result["live_database_changed"] is False
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT COUNT(*) FROM procurement_records").fetchone()[0] == 2


def test_backup_path_traversal_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("PROCUREAI_DB_PATH", str(tmp_path / "procureai.db"))
    with pytest.raises(Exception):
        procureai_main._resolve_database_backup("../secrets.sqlite3")


def test_recovery_inventory_counts_core_procurement_tables(tmp_path, monkeypatch):
    database = tmp_path / "procureai.db"
    monkeypatch.setenv("PROCUREAI_DB_PATH", str(database))
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE requests (id INTEGER PRIMARY KEY, code TEXT)")
        db.execute("INSERT INTO requests(code) VALUES('PR-1'),('PR-2')")
        db.execute("CREATE TABLE quotes (id INTEGER PRIMARY KEY)")
        db.execute("INSERT INTO quotes DEFAULT VALUES")
    inventory = procureai_main._database_inventory(database)
    assert inventory["integrity"] == "ok"
    assert inventory["table_counts"]["requests"] == 2
    assert inventory["table_counts"]["quotes"] == 1
    assert inventory["table_counts"]["orders"] == 0


def test_restore_preserves_live_database_snapshot_and_restores_data_richer_backup(tmp_path, monkeypatch):
    database = tmp_path / "procureai.db"
    backup_dir = tmp_path / "backups"
    monkeypatch.setenv("PROCUREAI_DB_PATH", str(database))
    monkeypatch.setenv("PROCUREAI_BACKUP_DIR", str(backup_dir))
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE requests (id INTEGER PRIMARY KEY, code TEXT NOT NULL)")
        db.execute("INSERT INTO requests(code) VALUES('PR-OLD-001'),('PR-OLD-002')")
    backup = procureai_main._create_database_backup()
    with sqlite3.connect(database) as db:
        db.execute("DELETE FROM requests WHERE code='PR-OLD-002'")
    result = procureai_main._restore_database_backup(backup["filename"])
    assert result["integrity"] == "ok"
    assert result["safety_snapshot"].startswith("before-restore-")
    assert "requests" in result["improved_tables"]
    with sqlite3.connect(database) as db:
        restored = [row[0] for row in db.execute("SELECT code FROM requests ORDER BY code")]
    assert restored == ["PR-OLD-001", "PR-OLD-002"]
    snapshot = tmp_path / "recovery-snapshots" / result["safety_snapshot"]
    assert snapshot.is_file()
    with sqlite3.connect(snapshot) as db:
        preserved = [row[0] for row in db.execute("SELECT code FROM requests ORDER BY code")]
    assert preserved == ["PR-OLD-001"]
