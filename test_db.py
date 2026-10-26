from pathlib import Path

from app.db import Database


def test_inactive_and_delete_recording(tmp_path: Path):
    database = Database(tmp_path / "test.db")
    database.initialize()
    database.create_recording("done", "https://live.douyin.com/1", "完成", "a")
    database.create_recording("busy", "https://live.douyin.com/2", "处理中", "b")
    database.create_recording("live", "https://live.douyin.com/3", "录制中", "c")
    database.update_recording("done", record_status="recorded")
    database.update_recording("busy", record_status="recorded", process_status="processing")
    database.update_recording("live", record_status="recording")

    inactive_ids = [item["id"] for item in database.inactive_recordings()]
    assert inactive_ids == ["done"]

    database.delete_recording("done")
    try:
        database.get_recording("done")
    except KeyError:
        pass
    else:
        raise AssertionError("deleted recording still exists")

