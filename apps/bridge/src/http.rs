//! The bridge's HTTP door (8080, private subnets, the backend SG only): `GET /health` for
//! anyone, every other route only with the internal token. Later tasks add routes.

use axum::extract::{Request, State};
use axum::http::StatusCode;
use axum::middleware::{self, Next};
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use axum::Router;
use std::sync::Arc;
use std::time::Instant;
use subtle::ConstantTimeEq;

pub const HEADER: &str = "x-lightbulb-bridge-internal";

pub fn router(token: String) -> Router {
    Router::new()
        .route("/health", get(|| async { "ok" }))
        .fallback(|| async { StatusCode::NOT_FOUND })
        .layer(middleware::from_fn_with_state(
            Arc::<str>::from(token),
            guard,
        ))
}

/// The token check and the access log (method, path, status, time: no query, header or body).
async fn guard(State(token): State<Arc<str>>, req: Request, next: Next) -> Response {
    let (method, path, t0) = (
        req.method().clone(),
        req.uri().path().to_string(),
        Instant::now(),
    );
    let allowed = path == "/health"
        || req
            .headers()
            .get(HEADER)
            .is_some_and(|v| !token.is_empty() && bool::from(v.as_bytes().ct_eq(token.as_bytes())));
    let res = if allowed {
        next.run(req).await
    } else {
        StatusCode::UNAUTHORIZED.into_response()
    };
    crate::log!(
        "http {method} {path} {} {}ms",
        res.status().as_u16(),
        t0.elapsed().as_millis()
    );
    res
}
