"""The legacy Paperclip Teams surfaces are deleted (Phase 1 task 7, inventory §4e groups 2 and 3):
no route path may start with one of these prefixes again."""

from main import app

GONE = (
    *(f"/api/v1/teams/{p}" for p in ("issues", "inbox", "runs", "approvals", "routines", "goals", "cases")),
    "/api/v1/internal/paperclip",  # the hermes_gateway run facade (task 8b.6)
)


def test_no_paperclip_teams_routes():
    # Routers are included lazily (app.routes holds _IncludedRouter wrappers), so read the schema.
    app.openapi_schema = None
    assert not [p for p in app.openapi()["paths"] if p.startswith(GONE)]
