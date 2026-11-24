import sqlite3

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
