# Data Settings — deploy help screenshots

These images are shown inside the **info tooltips** of the *New deployment → Google VM*
form (App Settings → Data Settings → Deployment). They're referenced by the Google VM
provider descriptor (`app/deploy/providers/google_vm.py`) as absolute `/ui/...` paths and
rendered by the field-help bubble in
`ui/main-panel/app/settings/data-settings/deploy.js` (`_showTip` → `.ac-tip-gallery`).

Drop the four screenshots in here with these exact names:

| File | Shown in the tooltip for… | What it should show |
|------|---------------------------|----------------------|
| `gcp-project-welcome.png` | **Google Cloud project ID** | The console "Welcome, …" banner with the project **ID** highlighted |
| `gcp-project-picker.png`  | **Google Cloud project ID** | The "Select a project" dialog, ID column highlighted |
| `gcp-sa-create-role.png`  | **Service-account key (JSON)** | Create service account → Permissions → searching the **Compute Admin** role |
| `gcp-sa-keys-addkey.png`  | **Service-account key (JSON)** | The service account **Keys** tab → **Add key ▸ Create new key** |

PNG or WEBP are both fine (update the paths in `google_vm.py` if you change the
extension). Keep them reasonably small — they render at ~320 px wide in the bubble.
