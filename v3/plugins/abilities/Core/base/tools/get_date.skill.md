# Get date

Read the Gregorian date from the Rust runtime clock. Timezone accepts only UTC or UTC+HH:MM/UTC-HH:MM fixed offsets within ±14:00. IANA timezone names and daylight-saving calculations are unsupported.

Format is `full` (default), `short` (YYYY-MM-DD), or `iso` (date/time with explicit offset). The result contains text, requested timezone, Unix seconds and seconds precision. This is read-only and uses the same live Rust access policy as every tool.
