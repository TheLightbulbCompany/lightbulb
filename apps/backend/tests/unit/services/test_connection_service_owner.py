import boto3
from moto import mock_aws

from core.services.connection_service import ConnectionService


@mock_aws
def test_store_connection_writes_owner_id_org():
    client = boto3.client("dynamodb", region_name="us-east-1")
    client.create_table(
        TableName="ws-conn-test",
        KeySchema=[{"AttributeName": "connectionId", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "connectionId", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    svc = ConnectionService(table_name="ws-conn-test", region_name="us-east-1")
    # org member: ownerId must be the org id
    svc.store_connection(connection_id="c1", user_id="user_a", org_id="org_x", connection_type="chat")
    item = client.get_item(TableName="ws-conn-test", Key={"connectionId": {"S": "c1"}})["Item"]
    assert item["ownerId"]["S"] == "org_x"
    # personal: ownerId == userId
    svc.store_connection(connection_id="c2", user_id="user_b", org_id=None, connection_type="chat")
    item2 = client.get_item(TableName="ws-conn-test", Key={"connectionId": {"S": "c2"}})["Item"]
    assert item2["ownerId"]["S"] == "user_b"
