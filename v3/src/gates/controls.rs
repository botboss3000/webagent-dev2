//! The loop calls gate behavior through these two integral modules.
#[path = "initial/mod.rs"] mod initial;
#[path = "closing/mod.rs"] mod closing;
pub use initial::{VisibilityConfig, working_schema, working_text};
pub use closing::{ClosingConfig, closing_schema, closing_status, fallback_status};
