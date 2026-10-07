"""Factory for creating user test dicts (DynamoDB items)."""


def create_user(user_id: str = "user_test123") -> dict:
    return {"user_id": user_id, "created_at": "2026-01-01T00:00:00+00:00"}
