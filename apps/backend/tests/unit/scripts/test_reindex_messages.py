from scripts import reindex_messages


def test_jobs_from_rows_keeps_messages_and_points_at_the_meta_owner():
    rows = [
        {"pk": "ROOM#r1", "sk": "MSG#00000000000000000001", "roomId": "r1", "ownerId": "user_old"},
        {"pk": "ROOM#r1", "sk": "META", "roomId": "r1", "ownerId": "org_new"},
        {"pk": "ROOM#r2", "sk": "MSG#00000000000000000001", "roomId": "r2", "ownerId": "org_a"},
    ]
    owners = {"r1": "org_new"}  # r2 has no META
    assert reindex_messages.jobs_from_rows(rows, owners.get) == [
        {"ownerId": "org_new", "roomId": "r1", "sk": "MSG#00000000000000000001"}
    ]
