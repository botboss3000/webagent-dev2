# Private Access page

This directory owns the app-shell page shown to signed-out visitors when the
installation uses the **Private** (`admin_approval`) access policy.

It is deliberately separate from `ui/shared/` in the same spirit as the
segregated welcome experience under `ui/splash/splash-page/`. It is not a
navigable main-panel page and is therefore not registered in the page catalog.
The shell loads its HTML into `#app-restricted-overlay` before `main.js` runs.

## Files

- `private-access-page.html` — accessible page markup and administrator-approval copy.
- `private-access-page.css` — the complete restricted shell layout and responsive styling.
- `js/private-access-page.js` — visibility presentation and shared account form mounting.

## Security boundary

This directory does not decide who is authorized. The authoritative access-mode
and identity checks remain in `ui/shared/js/main.js`, backed by
`GET /api/v1/auth/access-mode`. The shared layer passes only the final visible /
hidden decision into this page module.

In Private mode:

1. A visitor can sign in or register directly in the card using the same instance-scoped form as the account menu.
2. The account is created in a pending state.
3. The visitor remains restricted until an administrator approves the account.
4. An approved member can sign in and the shared auth layer removes this page.

The static header remains visible above the page so the normal account menu is
still available as an alternative entry point.
