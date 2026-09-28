from bridge import sync


def test_deep_merge_recurses_into_dicts_and_replaces_lists_wholesale():
    merged = sync.deep_merge({"a": {"b": 1, "c": 2}, "deny": ["x", "y"]},
                             {"a": {"c": 3, "d": 4}, "deny": ["z"]})
    assert merged == {"a": {"b": 1, "c": 3, "d": 4}, "deny": ["z"]}
