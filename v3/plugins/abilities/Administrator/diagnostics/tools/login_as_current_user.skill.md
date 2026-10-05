# Sign in the private V3 browser

Call with an optional exact relative `path`, normally the registered route for the current agent or conversation. Rust supplies the current session, caller, agent, short-lived cookie and authorization generation through trusted metadata. No credential argument or user selector is accepted.

The backend verifies the current caller through `/api/v3/auth/me` and returns only identity verification and route metadata. A profile belongs to one caller/session and is unavailable after expiry or rotation. This is V3 authentication, not V2 JWT or localStorage login.
