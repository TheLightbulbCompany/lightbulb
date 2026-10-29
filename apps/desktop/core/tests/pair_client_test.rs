use lightbulb_core::pair_client::{pair, revoke, PairRequest, PairResponse};
use wiremock::matchers::{header, method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

#[tokio::test]
async fn pair_sends_clerk_jwt_and_parses_response() {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/api/v1/desktop/pair"))
        .and(header("authorization", "Bearer test_jwt"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
            "gatewayUrl": "wss://ws-dev.isol8.co",
            "relayUrl": "wss://bv-dev.thelightbulb.company/api/v1/gateway/ws",
            "gatewayToken": "tok_xyz",
            "ownerId": "org_test",
            "deviceIdMac": "dev_mac_hash",
            "deviceIdCli": "dev_cli_hash",
            "commandsMac": ["system.notify", "device.info"],
            "commandsCli": ["system.run", "browser.proxy"],
        })))
        .mount(&server)
        .await;

    let req = PairRequest {
        tauri_mac_pub_key: "PUB_MAC".to_string(),
        cli_pub_key: "PUB_CLI".to_string(),
        permissions: None,
    };
    let resp: PairResponse = pair(&server.uri(), "test_jwt", &req).await.unwrap();
    assert_eq!(resp.gateway_url, "wss://ws-dev.isol8.co");
    assert_eq!(resp.relay_url, "wss://bv-dev.thelightbulb.company/api/v1/gateway/ws");
    assert_eq!(resp.gateway_token, "tok_xyz");
    assert_eq!(resp.owner_id, "org_test");
    assert_eq!(resp.device_id_mac, "dev_mac_hash");
    assert_eq!(resp.device_id_cli, "dev_cli_hash");
    assert_eq!(resp.commands_mac, vec!["system.notify", "device.info"]);
    assert_eq!(resp.commands_cli, vec!["system.run", "browser.proxy"]);
}

#[tokio::test]
async fn pair_returns_error_on_4xx() {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/api/v1/desktop/pair"))
        .respond_with(ResponseTemplate::new(400).set_body_string("bad pem"))
        .mount(&server)
        .await;

    let req = PairRequest {
        tauri_mac_pub_key: "PUB_MAC".to_string(),
        cli_pub_key: "PUB_CLI".to_string(),
        permissions: None,
    };
    let err = pair(&server.uri(), "test_jwt", &req).await.unwrap_err();
    assert!(err.contains("400"), "expected 400 in error: {err}");
}

#[tokio::test]
async fn revoke_sends_delete_with_bearer() {
    let server = MockServer::start().await;
    Mock::given(method("DELETE"))
        .and(path("/api/v1/desktop/devices/dev_abc"))
        .and(header("authorization", "Bearer test_jwt"))
        .respond_with(ResponseTemplate::new(204))
        .mount(&server)
        .await;
    revoke(&server.uri(), "test_jwt", "dev_abc").await.unwrap();
}
