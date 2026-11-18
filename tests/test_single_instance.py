from keep_to_bring.single_instance import LOCK_FILE_NAME, SingleInstance


def test_only_one_holder_at_a_time(tmp_path):
    first = SingleInstance(tmp_path / "data")
    second = SingleInstance(tmp_path / "data")
    assert first.acquire()
    assert not second.acquire()
    first.release()
    assert second.acquire()
    second.release()


def test_lock_file_records_pid_and_context_manager(tmp_path):
    import os

    with SingleInstance(tmp_path) as lock:
        assert lock.acquire()
    # Read after releasing: Windows forbids reading a locked byte range.
    assert (tmp_path / LOCK_FILE_NAME).read_text().strip() == str(os.getpid())
    assert SingleInstance(tmp_path).acquire()


def test_different_folders_do_not_conflict(tmp_path):
    a, b = SingleInstance(tmp_path / "a"), SingleInstance(tmp_path / "b")
    assert a.acquire() and b.acquire()
    a.release()
    b.release()
