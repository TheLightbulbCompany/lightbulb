from scripts import reindex_messages


def test_jobs_from_rows_keeps_messages_and_points_at_the_room_owner():
    rows = [
        {"pk": "ROOM#r1", "sk": "MSG#00000000000000000001", "roomId": "r1", "ownerId": "org_a"},
        {"pk": "ROOM#r1", "sk": "META", "roomId": "r1", "ownerId": "org_a"},
    ]
    assert reindex_messages.jobs_from_rows(rows) == [
        {"ownerId": "org_a", "roomId": "r1", "sk": "MSG#00000000000000000001"}
    ]
