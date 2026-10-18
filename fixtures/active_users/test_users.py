from users import active_emails


def test_active_record_after_inactive_record():
    records = [{"email": "a@example.test", "active": False},
               {"email": "a@example.test", "active": True}]
    assert active_emails(records) == ["a@example.test"]


def test_duplicate_active_records():
    record = {"email": "a@example.test", "active": True}
    assert active_emails([record, record]) == ["a@example.test"]


def test_inactive_only():
    assert active_emails([{"email": "a@example.test", "active": False}]) == []


def test_active_order():
    records = [{"email": email, "active": True} for email in ["b", "a", "c"]]
    assert active_emails(records) == ["b", "a", "c"]


def test_empty_records():
    assert active_emails([]) == []
