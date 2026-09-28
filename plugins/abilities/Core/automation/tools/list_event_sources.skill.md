Call this **before** subscribing. It returns each external source with an
`enabled` flag and the `event_type`s it supports. Use only a source whose
`enabled` is true, and only an `event_type` it lists. Don't invent sources or
event types — if Gmail push isn't on, don't promise "I'll watch your inbox."
