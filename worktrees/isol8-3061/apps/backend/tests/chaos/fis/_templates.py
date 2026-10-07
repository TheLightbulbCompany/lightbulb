"""Resolve a deployed SP2 FIS experiment template by its ``Name`` tag.

The scenario tests do not hard-code template ids — those rotate every time the
FIS stack is redeployed. Instead each scenario resolves its template at run
time by the stable ``Name`` tag the CDK stack stamps on it
(``isol8-${env}-fis-s{N}-{slug}``, plus the ``isol8:chaos=sp2`` tag), via
``fis:ListExperimentTemplates``.

NO boto3 call happens at import — ``resolve_template_id`` is invoked only from
inside a test body (after the dev-only guard has passed), keeping collection
network- and AWS-free.
"""


def resolve_template_id(fis_client, name_tag: str) -> str:
    """Return the experiment-template id whose ``Name`` tag equals ``name_tag``.

    Pages through ``list_experiment_templates`` and matches on the ``Name``
    tag (the CDK stack also stamps ``isol8:chaos=sp2``, which we assert as a
    belt-and-braces guard against matching a stray template). Raises
    AssertionError if zero or more than one template matches — both are a
    deploy/config problem the game-day operator must see, not silently work
    around.
    """
    matches: list[dict] = []
    next_token: str | None = None
    while True:
        kwargs = {"maxResults": 100}
        if next_token:
            kwargs["nextToken"] = next_token
        resp = fis_client.list_experiment_templates(**kwargs)
        for tmpl in resp.get("experimentTemplates", []):
            tags = tmpl.get("tags", {})
            if tags.get("Name") == name_tag:
                matches.append(tmpl)
        next_token = resp.get("nextToken")
        if not next_token:
            break

    assert matches, f"no FIS template tagged Name={name_tag!r} (is the dev FIS stack deployed?)"
    assert len(matches) == 1, f"expected exactly one template tagged Name={name_tag!r}, got {len(matches)}"
    tmpl = matches[0]
    assert tmpl.get("tags", {}).get("isol8:chaos") == "sp2", (
        f"template {name_tag!r} is missing the isol8:chaos=sp2 tag — refusing to start a non-SP2 template"
    )
    return tmpl["id"]
