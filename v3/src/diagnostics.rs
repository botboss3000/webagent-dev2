//! Optional installation plugin. Removing its directory and rebuilding leaves safe no-op hooks.
#[cfg(diagnostics_plugin)]
#[path = "../plugins/diagnostics/mod.rs"]
mod installed;
#[cfg(diagnostics_plugin)]
pub(crate) use installed::*;
#[cfg(not(diagnostics_plugin))]
pub(crate) async fn init(_: &std::path::Path) {}
#[cfg(not(diagnostics_plugin))]
pub(crate) fn failure(_: &serde_json::Value, _: &anyhow::Error, _: &str) {}
#[cfg(not(diagnostics_plugin))]
pub(crate) fn backend_error() {}
#[cfg(not(diagnostics_plugin))]
pub(crate) fn router() -> axum::Router<crate::App> { axum::Router::new() }

#[cfg(not(diagnostics_plugin))]
pub(crate) async fn http_errors(request:axum::extract::Request,next:axum::middleware::Next)->axum::response::Response {next.run(request).await}
