# Read native V3 diagnostics

Use Rust's bounded current-session/run diagnostic view. The authenticated caller and selected session determine scope; another user's session or tenant-private installation data is unavailable.

The view exposes sanitized decision, revision, timing and failure metadata implemented by V3. It never opens encrypted database files or returns raw model text, tool arguments/results, authentication data or unrestricted logs. Historical diagnostics are evidence, not permission to resume or replay an uncertain operation.
