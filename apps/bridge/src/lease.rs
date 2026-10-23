//! One live bridge at a time: the containers-table row `__bridge__#lease` `{holder,
//! expires_at}`. Without it a task starts no instance; losing it stops them all.

use crate::instances::Instances;
use crate::{log, Cfg, Fut};
use aws_sdk_dynamodb::types::AttributeValue;
use std::future::Future;
use std::sync::{Arc, Mutex};
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

/// Renews every `tick` on its own, whatever boot and the routes are doing; returns the first
/// put that did not take. `last_ok` is when the last good put was sent.
async fn renew(cfg: Arc<Cfg>, last_ok: Arc<Mutex<Instant>>) -> Put {
    let mut tick = tokio::time::interval_at(Instant::now() + cfg.tick, cfg.tick);
    tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    loop {
        tick.tick().await;
        let sent = Instant::now();
        match put(&cfg).await {
            Put::Taken => *last_ok.lock().unwrap() = sent,
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
/// `stop` (SIGTERM) stops every instance with its final snapshot, then frees the lease.
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
        let last_ok = Arc::new(Mutex::new(sent));
        instances.set_active(true);
        let mut renewer = tokio::spawn(renew(cfg.clone(), last_ok.clone()));
        let booter = tokio::spawn(boot(instances.clone(), cfg.tick));
        tokio::select! {
            biased;
            _ = &mut stop => {
                instances.set_active(false);
                booter.abort();
                // Still renewing while the final snapshots go up.
                instances.stop_all(true).await;
                renewer.abort();
                if let Err(e) = crate::store::within(cfg.lease.delete(&cfg.holder)).await {
                    log!("lease release failed: {e}");
                }
                log!("lease released holder={}", cfg.holder);
                return;
            }
            lost = &mut renewer => {
                instances.set_active(false);
                booter.abort();
                // Refused: another holder is live, so our copies are stale. Failed: they are
                // the newest only while our last good put has time to run.
                let left = Duration::from_secs(TTL_S as u64).saturating_sub(cfg.tick);
                let flush = matches!(lost, Ok(Put::Failed))
                    && last_ok.lock().unwrap().elapsed() < left;
                log!("lease lost holder={} flush={flush}", cfg.holder);
                instances.stop_all(flush).await;
                tick.reset();
            }
        }
    }
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
                Err(e)
                    if e.as_service_error()
                        .is_some_and(|e| e.is_conditional_check_failed_exception()) =>
                {
                    Ok(false)
                }
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
                Ok(_) => Ok(()),
                Err(e)
                    if e.as_service_error()
                        .is_some_and(|e| e.is_conditional_check_failed_exception()) =>
                {
                    Ok(())
                }
                Err(e) => Err(e.into()),
            }
        })
    }
}
