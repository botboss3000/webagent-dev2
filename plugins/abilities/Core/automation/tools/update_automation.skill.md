Changes an existing automation — its prompt, timing, delivery or guardrails.
Requires the `automation_id` from `list_automations`. Read the current config
from that list first, so you change only what the user asked for and don't
silently drop a delivery target or a guardrail.
