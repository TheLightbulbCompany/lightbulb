"""Helpers for AWS Secrets Manager writes that tolerate the post-delete window.

``DeleteSecret`` with ``ForceDeleteWithoutRecovery`` frees the secret name
*asynchronously*, so a disconnect immediately followed by a reconnect of the
same provider can race: ``create_secret`` raises ``InvalidRequestException``
(the deterministic name is still held by the pending deletion) until AWS
finishes. We retry the create with a short backoff so the common quick-delete
case recovers transparently; on the rare slow case the error surfaces and the
caller retries.
"""

from __future__ import annotations

import time

# Total worst-case wait across retries is ~5s (0.5+1+1.5+2); the final attempt
# does not sleep before surfacing the error.
_RETRY_BACKOFFS = (0.5, 1.0, 1.5, 2.0)


def create_or_update_secret(sm, name: str, value: str, *, create_only: bool = False) -> str:
    """Create the secret, or update it if it already exists; return the value it now holds.

    ``create_only`` makes the FIRST writer win: an existing secret is left alone and its
    stored value is returned, so concurrent writers of a value that cannot be re-derived
    all converge on one.

    Retries past the async name-reuse window left by a prior
    ``ForceDeleteWithoutRecovery``. ``sm`` is a boto3 ``secretsmanager`` client;
    this is sync — call it via ``run_in_thread``.
    """
    for attempt in range(len(_RETRY_BACKOFFS) + 1):
        try:
            sm.create_secret(Name=name, SecretString=value)
            return value
        except sm.exceptions.ResourceExistsException:
            if create_only:
                return sm.get_secret_value(SecretId=name)["SecretString"]
            sm.put_secret_value(SecretId=name, SecretString=value)
            return value
        except sm.exceptions.InvalidRequestException as exc:
            # Only the "scheduled for deletion" case is a transient race worth
            # retrying; any other InvalidRequest is a genuine bad request.
            if "delet" not in str(exc).lower() or attempt >= len(_RETRY_BACKOFFS):
                raise
            time.sleep(_RETRY_BACKOFFS[attempt])
