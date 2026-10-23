//! One live bridge at a time: the containers-table row `__bridge__#lease` `{holder,
//! expires_at}`. Without it a task starts no instance; losing it stops them all.

use crate::instances::Instances;
use crate::{log, Cfg, Fut};
use aws_sdk_dynamodb::error::{ProvideErrorMetadata, SdkError};
use aws_sdk_dynamodb::types::AttributeValue;
use std::future::Future;
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use tokio::time::Instant;

pub const ROW: &str = "__bridge__#lease";
/// How long a put holds the lease.
pub const TTL_S: i64 = 30;

pub trait Table: Send + Sync {
    /// Takes or renews the lease for `holder` until `expires_at`: `false` while another
    /// holder's has not expired.
    fn put(&self, holder: &str, now: i64, expires_at: i64) -> Fut<'_, bool>;
    /// Frees the lease if `holder` has it.
    fn delete(&self, holder: &str) -> Fut<'_, ()>;
}

/// A put's outcome: refused means another holder has a live lease (never flush then).
enum Put {
    Taken,
    Refused,
    Failed,
}

async fn put(cfg: &Cfg) -> Put {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs() as i64;
    let put = cfg.lease.put(&cfg.holder, now, now + TTL_S);
    match tokio::time::timeout(cfg.tick, put).await {
        Ok(Ok(true)) => Put::Taken,
        Ok(Ok(false)) => Put::Refused,
        Ok(Err(_)) | Err(_) => Put::Failed,
    }
}

/// Until when an upload is safe after a good put sent at `sent`: our put holds the lease for
/// `TTL_S`, less one `tick` of margin for the clocks.
fn valid_until(cfg: &Cfg, sent: Instant) -> Instant {
    sent + Duration::from_secs(TTL_S as u64).saturating_sub(cfg.tick)
}

/// Renews every `tick` on its own, whatever boot and the routes are doing, moving the
/// instances' upload deadline with each good put; returns the first put that did not take.
async fn renew(cfg: Arc<Cfg>, instances: Arc<Instances>) -> Put {
    let mut tick = tokio::time::interval_at(Instant::now() + cfg.tick, cfg.tick);
    tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    loop {
        tick.tick().await;
        let sent = Instant::now();
        match put(&cfg).await {
            Put::Taken => instances.set_until(valid_until(&cfg, sent)),
            p => return p,
        }
    }
}

/// Boots, and again every `tick` while the workspaces list cannot be read.
async fn boot(instances: Arc<Instances>, tick: Duration) {
    while let Err(e) = instances.boot().await {
        log!("boot failed: {e}");
        tokio::time::sleep(tick).await;
    }
}

/// Waits for the lease (a put every `tick`), then boots while a separate task renews it
/// every `tick`. A renewal that does not take stops every instance and goes back to waiting.
/// `stop` (SIGTERM) stops every instance with its final snapshot, then frees the lease. Every
/// upload checks the deadline the last good put set, so none lands once the lease may be gone.
pub async fn run(cfg: Arc<Cfg>, instances: Arc<Instances>, stop: impl Future<Output = ()>) {
    tokio::pin!(stop);
    let mut tick = tokio::time::interval(cfg.tick);
    tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    loop {
        tokio::select! {
            biased;
            _ = &mut stop => return,
            _ = tick.tick() => {}
        }
        let sent = Instant::now();
        if !matches!(put(&cfg).await, Put::Taken) {
            continue;
        }
        log!("lease taken holder={}", cfg.holder);
        instances.set_until(valid_until(&cfg, sent));
        instances.set_active(true);
        let mut renewer = tokio::spawn(renew(cfg.clone(), instances.clone()));
        let booter = tokio::spawn(boot(instances.clone(), cfg.tick));
        tokio::select! {
            biased;
            _ = &mut stop => {
                instances.set_active(false);
                booter.abort();
                // Still renewing (and moving the deadline) while the final snapshots go up.
                instances.stop_all().await;
                renewer.abort();
                if let Err(e) = cfg.lease.delete(&cfg.holder).await {
                    log!("lease release failed: {e}");
                }
                log!("lease released holder={}", cfg.holder);
                return;
            }
            lost = &mut renewer => {
                instances.set_active(false);
                booter.abort();
                // Refused: another holder is live, so our copies are stale: no upload at all.
                // Failed: uploads go on until the last good put's deadline.
                let refused = !matches!(lost, Ok(Put::Failed));
                if refused {
                    instances.set_until(Instant::now());
                }
                log!("lease lost holder={} refused={refused}", cfg.holder);
                instances.stop_all().await;
                tick.reset();
            }
        }
    }
}

/// Whether `e` is DynamoDB's "the condition did not hold".
fn cond_failed<E: ProvideErrorMetadata, R>(e: &SdkError<E, R>) -> bool {
    e.as_service_error().and_then(ProvideErrorMetadata::code)
        == Some("ConditionalCheckFailedException")
}

/// The lease row in the containers table.
pub struct Dynamo {
    pub client: aws_sdk_dynamodb::Client,
    pub table: String,
}

impl Table for Dynamo {
    fn put(&self, holder: &str, now: i64, expires_at: i64) -> Fut<'_, bool> {
        let (s, n) = (
            |v: &str| AttributeValue::S(v.into()),
            |v: i64| AttributeValue::N(v.to_string()),
        );
        let req = self
            .client
            .put_item()
            .table_name(&self.table)
            .item("owner_id", s(ROW))
            .item("holder", s(holder))
            .item("expires_at", n(expires_at))
            // Renewing is taking again: our own row qualifies too.
            .condition_expression(
                "attribute_not_exists(owner_id) OR expires_at < :now OR holder = :me",
            )
            .expression_attribute_values(":now", n(now))
            .expression_attribute_values(":me", s(holder));
        Box::pin(async move {
            match req.send().await {
                Ok(_) => Ok(true),
                Err(e) if cond_failed(&e) => Ok(false),
                Err(e) => Err(e.into()),
            }
        })
    }

    fn delete(&self, holder: &str) -> Fut<'_, ()> {
        let req = self
            .client
            .delete_item()
            .table_name(&self.table)
            .key("owner_id", AttributeValue::S(ROW.into()))
            .condition_expression("holder = :me")
            .expression_attribute_values(":me", AttributeValue::S(holder.into()));
        Box::pin(async move {
            match req.send().await {
                Err(e) if !cond_failed(&e) => Err(e.into()),
                _ => Ok(()),
            }
        })
    }
}
