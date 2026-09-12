"""Dimension sets published by ``put_metric``.

CloudWatch treats every dimension set as its own metric identity, not a
filterable view of a shared one. That single fact is why this is worth pinning:
an alarm bound to a set nothing publishes is not a noisy alarm, it is a silent
one, and silence is indistinguishable from health.

The status rollup in particular exists because CloudWatch rejects
``SEARCH()`` on alarms ("SEARCH is not supported on Metric Alarms"). The
aggregate an error alarm needs -- errors across every op/provider -- therefore
cannot be computed at the alarm layer and has to be published here.
"""

import json

from core.observability.metrics import put_metric


def _sets(capsys) -> list[list[str]]:
    line = capsys.readouterr().out.strip()
    return json.loads(line)["_aws"]["CloudWatchMetrics"][0]["Dimensions"]


def test_status_metric_publishes_full_status_and_fleet_sets(capsys):
    """A qualifier + status metric needs all three identities.

    The full set is too narrow for an "all providers" alarm (one identity per
    provider), and the fleet rollup is too wide (it folds ok and error
    together, so an error alarm would fire on success traffic). The status
    rollup is the only one that answers the question the alarm asks.
    """
    put_metric("channel.rpc", dimensions={"provider": "slack", "status": "error"})

    assert _sets(capsys) == [
        ["env", "service", "provider", "status"],
        ["env", "service", "status"],
        ["env", "service"],
    ]


def test_no_rollup_dimension_means_no_qualifier_rollup(capsys):
    put_metric("container.lifecycle.latency", dimensions={"op": "start"})

    assert _sets(capsys) == [
        ["env", "service", "op"],
        ["env", "service"],
    ]


def test_outcome_metric_with_a_second_dimension_publishes_the_outcome_rollup(capsys):
    """`messaging.worker.message{outcome, error}` is the shape that broke: an
    alarm binding {env, service, outcome} matched nothing, because the only
    published identities were the full set and the bare fleet rollup."""
    put_metric("messaging.worker.message", dimensions={"kind": "room_dispatch", "outcome": "retry", "error": "Work"})

    assert _sets(capsys) == [
        ["env", "service", "kind", "outcome", "error"],
        ["env", "service", "outcome"],
        ["env", "service"],
    ]


def test_status_class_metric_with_a_second_dimension_publishes_the_status_class_rollup(capsys):
    put_metric("messaging.ingress.post", dimensions={"status_class": "5xx", "event_kind": "user_request"})

    assert _sets(capsys) == [
        ["env", "service", "status_class", "event_kind"],
        ["env", "service", "status_class"],
        ["env", "service"],
    ]


def test_only_allowlisted_qualifiers_get_their_own_rollup(capsys):
    """Every published set costs money and CloudWatch caps a metric at 30
    dimensions, so this is an allowlist, not "one rollup per dimension"."""
    put_metric("chat.error", dimensions={"reason": "timeout", "outcome": "error"})

    assert _sets(capsys) == [
        ["env", "service", "reason", "outcome"],
        ["env", "service", "outcome"],
        ["env", "service"],
    ]


def test_every_allowlisted_qualifier_present_gets_a_rollup(capsys):
    """`chat.error{reason, reclassified, source}` carries one allowlisted
    qualifier among three dimensions; the {env, service, source} alarm binds
    the rollup, not the full set."""
    put_metric("chat.error", dimensions={"reason": "timeout", "reclassified": "false", "source": "webchat"})

    assert _sets(capsys) == [
        ["env", "service", "reason", "reclassified", "source"],
        ["env", "service", "source"],
        ["env", "service"],
    ]


def test_status_only_metric_does_not_duplicate_its_own_set(capsys):
    """A repeated set inside one record double-counts the event."""
    put_metric("container.provision", dimensions={"status": "ok"})

    sets = _sets(capsys)
    assert sets == [["env", "service", "status"], ["env", "service"]]
    assert len(sets) == len({tuple(s) for s in sets})


def test_dimensionless_metric_publishes_exactly_one_set(capsys):
    put_metric("gateway.running.count", value=3)

    assert _sets(capsys) == [["env", "service"]]
