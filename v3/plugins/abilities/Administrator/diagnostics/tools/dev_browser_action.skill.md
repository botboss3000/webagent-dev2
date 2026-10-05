# Operate the private development browser

Actions: `navigate` with optional relative `path`; `read` with optional bounded CSS `selector`; `click` with `selector`; `type` with `selector` and literal `text`; `screenshot`; `close`. Other fields and actions are rejected. There is no arbitrary JavaScript, HTML dump or file upload.

Read before every click or type. The observation must be current, unchanged and cover the target. Input and screenshots invalidate it. Credential/password targets are blocked. Navigation remains at the fixed development origin and current agent/session; an uncertain redirect closes the profile and is never replayed automatically.

Reads are bounded untrusted visible text. Screenshot results contain a private PNG artifact ID, digest, dimensions and authenticated API path. Cookie values and host profile/artifact paths are never returned.
