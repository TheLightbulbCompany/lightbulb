from scripts.reindex_sessions import jobs_from_rows

SID = "33333333-3333-4333-8333-333333333333"
PK = f"LOCALSESSION#org_a#{SID}"


def test_only_text_prompts_and_replies_are_enqueued():
    rows = [
        {"pk": PK, "sk": "EVENT#00000000000000000001", "kind": "text", "role": "user"},
        {"pk": PK, "sk": "EVENT#00000000000000000002", "kind": "text", "role": "assistant"},
        {"pk": PK, "sk": "EVENT#00000000000000000003", "kind": "tool", "role": "tool"},
        {"pk": PK, "sk": "EVENT#00000000000000000004", "kind": "text", "role": "system"},
        {"pk": PK, "sk": "META"},
        {"pk": "ROOM#r", "sk": "EVENT#00000000000000000001", "kind": "text", "role": "user"},
    ]
    assert jobs_from_rows(rows) == [
        {"ownerId": "org_a", "sessionId": SID, "sk": "EVENT#00000000000000000001"},
        {"ownerId": "org_a", "sessionId": SID, "sk": "EVENT#00000000000000000002"},
    ]
