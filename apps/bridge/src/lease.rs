//! One live bridge at a time: the containers-table row `__bridge__#lease` `{holder,
//! expires_at}`. Without it a task starts no instance; losing it stops them all.

use crate::instances::Instances;
use crate::{log, Cfg, Fut};
use aws_sdk_dynamodb::types::AttributeValue;
use std::future::Future;
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

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

/// Waits for the lease (every `tick`), boots the instances, renews every `tick`; a renewal
/// that fails stops them all and goes back to waiting. `stop` (SIGTERM) flushes every final
/// snapshot, then frees the lease.
pub async fn run(cfg: Arc<Cfg>, instances: Arc<Instances>, stop: impl Future<Output = ()>) {
    tokio::pin!(stop);
    let mut tick = tokio::time::interval(cfg.tick);
    tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    // When the last put succeeded, while we hold the lease.
    let mut held: Option<Instant> = None;
    loop {
        tokio::select! {
            _ = &mut stop => break,
            _ = tick.tick() => {}
        }
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_secs() as i64;
        let put = cfg.lease.put(&cfg.holder, now, now + TTL_S);
        let ok = matches!(tokio::time::timeout(cfg.tick, put).await, Ok(Ok(true)));
        match (ok, held) {
            (true, None) => {
                log!("lease taken holder={}", cfg.holder);
                match instances.boot().await {
                    Ok(()) => held = Some(Instant::now()),
                    Err(e) => {
                        log!("boot failed: {e}");
                        instances.stop_all(true).await;
                    }
                }
            }
            (true, Some(_)) => held = Some(Instant::now()),
            (false, Some(last)) => {
                // Our copies are still the newest only while our last put has time to run.
                let flush = last.elapsed() < Duration::from_secs(TTL_S as u64) - cfg.tick;
                log!("lease lost holder={} flush={flush}", cfg.holder);
                instances.stop_all(flush).await;
                held = None;
            }
            (false, None) => {}
        }
    }
    if held.is_some() {
        instances.stop_all(true).await;
        if let Err(e) = cfg.lease.delete(&cfg.holder).await {
            log!("lease release failed: {e}");
        }
        log!("lease released holder={}", cfg.holder);
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
