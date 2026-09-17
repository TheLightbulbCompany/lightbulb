"""Task 28 (M2-R11): the node-invoke error codes' HTTP status mapping."""

from core.services import messaging as service


def test_api_error_status_maps_node_codes():
    assert service.API_ERROR_STATUS["NODE_NOT_ATTACHED"] == 409
    assert service.API_ERROR_STATUS["COMMAND_NOT_ALLOWED"] == 403
    assert service.API_ERROR_STATUS["NODE_TIMEOUT"] == 504
    assert service.API_ERROR_STATUS["NODE_MOVED"] == 409
    for code, status in (
        ("NODE_NOT_ATTACHED", 409),
        ("COMMAND_NOT_ALLOWED", 403),
        ("NODE_TIMEOUT", 504),
        ("NODE_MOVED", 409),
    ):
        assert service.ApiProblem(code, "x").status_code == status
