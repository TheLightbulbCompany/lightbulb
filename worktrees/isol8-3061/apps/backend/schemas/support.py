"""Request schema for the support-ticket endpoint."""

from pydantic import BaseModel, Field


class SupportTicketRequest(BaseModel):
    """Client-supplied (low-trust) fields. The backend derives identity +
    account/container state itself; the client only provides the message +
    benign context."""

    message: str = Field(min_length=1, max_length=8000)
    category: str = Field(default="Other", max_length=64)
    # Where the user was. Benign, client-supplied.
    route: str | None = Field(default=None, max_length=512)
    user_agent: str | None = Field(default=None, max_length=512)
    app_sha: str | None = Field(default=None, max_length=64)
    # When launched from the ErrorBoundary: the captured error.
    error_message: str | None = Field(default=None, max_length=2000)
    error_stack: str | None = Field(default=None, max_length=8000)
    # Recent backend X-Request-IDs the client captured from failed responses.
    recent_request_ids: list[str] = Field(default_factory=list, max_length=20)
