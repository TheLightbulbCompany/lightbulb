use lightbulb_bridge::instances::Instances;
use lightbulb_bridge::{http, lease, store, Cfg, Result};
use std::sync::Arc;
use std::time::Duration;
use tokio::signal::unix::{signal, SignalKind};

#[tokio::main]
async fn main() -> Result<()> {
    let var = |k: &str| std::env::var(k).map_err(|_| format!("{k} is not set"));
    let aws_cfg = aws_config::load_from_env().await;
    let aws = Arc::new(store::Aws {
        secrets: aws_sdk_secretsmanager::Client::new(&aws_cfg),
        s3: aws_sdk_s3::Client::new(&aws_cfg),
        bucket: var("OWNER_STATE_BUCKET")?,
    });
    let cfg = Arc::new(Cfg {
        backend_url: var("BACKEND_INTERNAL_URL")?,
        token: var("BRIDGE_INTERNAL_TOKEN")?,
        env: var("LIGHTBULB_ENV")?,
        relay: std::env::var("NETWORK_RELAY_URL")
            .ok()
            .filter(|s| !s.is_empty()),
        issuer: var("NETWORK_ISSUER_URL")?,
        holder: ulid::Ulid::generate().to_string(),
        scratch: std::env::temp_dir().join("lightbulb-bridge"),
        tick: Duration::from_secs(10),
        secrets: aws.clone(),
        blobs: aws,
        lease: Arc::new(lease::Dynamo {
            client: aws_sdk_dynamodb::Client::new(&aws_cfg),
            table: var("CONTAINERS_TABLE")?,
        }),
    });
    let listener = tokio::net::TcpListener::bind("0.0.0.0:8080").await?;
    let router = http::router(cfg.token.clone());
    tokio::spawn(async move { axum::serve(listener, router).await });
    let mut term = signal(SignalKind::terminate())?;
    lease::run(cfg.clone(), Instances::new(cfg), async move {
        term.recv().await;
    })
    .await;
    Ok(())
}
