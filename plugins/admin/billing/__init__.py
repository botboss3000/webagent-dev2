"""Platform (marketplace) billing — optional, private add-on.

Dropping this package in adds the **platform tier** on top of the always-present
**agent tier** in ``plugins/billing``: a platform-wide config + a cut of every
charge, payout (Connect) routing for subscriptions, and the platform-admin
endpoints. On import it registers its extension into the core billing seam
(``plugins.billing.extensions``) and exposes a ``router`` the host mounts.

A public / agent-only edition ships **without** this package, so the seam stays
dormant and the agent admin keeps 100%. The dependency only ever points
platform → agent; ``plugins/billing`` never imports this package, so removing it
leaves the agent tier fully working with no dangling reference. See
``scripts/build_edition.py`` for how the public build strips it.
"""

from plugins.billing.extensions import register_billing_extension
from plugins.admin.billing.extension import PlatformBillingExtension
from plugins.admin.billing.api import router  # re-exported; the host mounts it

register_billing_extension(PlatformBillingExtension())

__all__ = ["router"]
