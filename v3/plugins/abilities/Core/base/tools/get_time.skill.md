# Get time

Read the current time from the Rust runtime clock. Use `{"timezone":"UTC"}` or a fixed offset such as `{"timezone":"UTC+05:30"}`. UTC is the default. Offsets are limited to -14:00 through +14:00. IANA timezone names and daylight-saving calculations are unsupported; do not silently approximate them.

The result contains text in 24-hour HH:MM:SS form, the requested timezone, and Unix seconds. This is a read-only native operation; live account, agent, tier and session policy still authorize every call.
