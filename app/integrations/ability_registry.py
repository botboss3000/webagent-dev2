"""OAuth ability registry — the per-capability scope catalogue.

An "ability" is a fine-grained capability on a provider (e.g. `google.gmail_read`,
`google.gmail_send`, `microsoft.calendar_write`). Each ability maps to one or
more OAuth scopes. Three layers consult this registry:

  - The app admin picks which abilities exist + which OAuth flow can grant them
    (platform creds vs agent-admin BYO).
  - The agent admin enables specific abilities on their agent.
  - The end user authorizes per-ability when they sign in.

Tools (gmail_list_messages, etc.) declare which ability they require via
`@requires_ability("google.gmail_read")`; the OAuth helper checks the user's
token coverage at call time and surfaces a `not_connected_payload` when the
required ability isn't authorized.

`profile` is implicit for every provider — openid/email/profile (or the
provider equivalent) is always requested so we can identify the connected
user. It can't be disabled.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class AbilityDef:
    id: str                                     # "google.gmail_read"
    provider: str                               # "google"
    display_name: str
    description: str
    scopes: tuple[str, ...] = field(default_factory=tuple)
    implicit: bool = False                      # profile-level, always granted


def _gscope(s: str) -> str:
    return f"https://www.googleapis.com/auth/{s}"


# ── Registry ──────────────────────────────────────────────────────────────
#
# Provider scope groupings mirror the legacy bundles in app/admin/integrations.py
# (GOOGLE_SCOPES, MICROSOFT_SCOPES, ...) split into capability-shaped pieces.

ABILITIES: dict[str, AbilityDef] = {
    # ── Google ────────────────────────────────────────────────────────────
    "google.profile": AbilityDef(
        id="google.profile", provider="google", implicit=True,
        display_name="Sign-in",
        description="Identify the connected Google account (always granted).",
        scopes=("openid", "email", "profile"),
    ),
    "google.gmail_read": AbilityDef(
        id="google.gmail_read", provider="google",
        display_name="Gmail · read",
        description="Read Gmail messages, threads, and labels.",
        scopes=(_gscope("gmail.readonly"),),
    ),
    "google.gmail_send": AbilityDef(
        id="google.gmail_send", provider="google",
        display_name="Gmail · send",
        description="Compose and send mail on the user's behalf.",
        scopes=(_gscope("gmail.compose"),),
    ),
    "google.gmail_modify": AbilityDef(
        id="google.gmail_modify", provider="google",
        display_name="Gmail · modify",
        description="Label, archive, mark read, and subscribe to push notifications.",
        scopes=(_gscope("gmail.modify"),),
    ),
    "google.calendar_read": AbilityDef(
        id="google.calendar_read", provider="google",
        display_name="Calendar · read",
        description="Read events from the user's calendars.",
        scopes=(_gscope("calendar.readonly"),),
    ),
    "google.calendar_write": AbilityDef(
        id="google.calendar_write", provider="google",
        display_name="Calendar · write",
        description="Create, update, and delete calendar events.",
        scopes=(_gscope("calendar"),),
    ),
    "google.drive_read": AbilityDef(
        id="google.drive_read", provider="google",
        display_name="Drive & Docs · read",
        description="Read files from Drive and Google Docs.",
        scopes=(_gscope("drive.readonly"), _gscope("docs.readonly")),
    ),
    "google.drive_write": AbilityDef(
        id="google.drive_write", provider="google",
        display_name="Drive & Docs · write",
        description="Create, edit, and share files in Drive and Google Docs.",
        scopes=(_gscope("drive"), _gscope("docs"), _gscope("drive.file")),
    ),

    # ── Microsoft ─────────────────────────────────────────────────────────
    "microsoft.profile": AbilityDef(
        id="microsoft.profile", provider="microsoft", implicit=True,
        display_name="Sign-in",
        description="Identify the connected Microsoft account (always granted).",
        scopes=("openid", "email", "profile", "offline_access", "User.Read"),
    ),
    "microsoft.mail_read": AbilityDef(
        id="microsoft.mail_read", provider="microsoft",
        display_name="Outlook · read",
        description="Read Outlook messages and folders.",
        scopes=("Mail.ReadWrite",),  # MS doesn't ship a separate ReadOnly tier
    ),
    "microsoft.mail_send": AbilityDef(
        id="microsoft.mail_send", provider="microsoft",
        display_name="Outlook · send",
        description="Send Outlook mail on the user's behalf.",
        scopes=("Mail.Send",),
    ),
    "microsoft.calendar_write": AbilityDef(
        id="microsoft.calendar_write", provider="microsoft",
        display_name="Calendar",
        description="Read and write events in Outlook Calendar.",
        scopes=("Calendars.ReadWrite",),
    ),
    "microsoft.files_read": AbilityDef(
        id="microsoft.files_read", provider="microsoft",
        display_name="OneDrive & SharePoint · read",
        description="Read files in OneDrive and SharePoint without modifying them.",
        scopes=("Files.Read.All", "Sites.Read.All"),
    ),
    "microsoft.files_write": AbilityDef(
        id="microsoft.files_write", provider="microsoft",
        display_name="OneDrive & SharePoint · write",
        description="Create and modify files in OneDrive and SharePoint.",
        scopes=("Files.ReadWrite.All", "Sites.ReadWrite.All"),
    ),

    # ── Yahoo ─────────────────────────────────────────────────────────────
    "yahoo.profile": AbilityDef(
        id="yahoo.profile", provider="yahoo", implicit=True,
        display_name="Sign-in",
        description="Identify the connected Yahoo account.",
        scopes=("openid", "email", "profile"),
    ),
    "yahoo.mail_read": AbilityDef(
        id="yahoo.mail_read", provider="yahoo",
        display_name="Yahoo Mail · read",
        description="Read Yahoo Mail messages (where API supports it).",
        scopes=("mail-r",),
    ),
    "yahoo.mail_write": AbilityDef(
        id="yahoo.mail_write", provider="yahoo",
        display_name="Yahoo Mail · write",
        description="Send and modify Yahoo Mail (where API supports it).",
        scopes=("mail-w",),
    ),

    # ── Dropbox ───────────────────────────────────────────────────────────
    "dropbox.profile": AbilityDef(
        id="dropbox.profile", provider="dropbox", implicit=True,
        display_name="Sign-in",
        description="Identify the connected Dropbox account.",
        scopes=("account_info.read",),
    ),
    "dropbox.files_read": AbilityDef(
        id="dropbox.files_read", provider="dropbox",
        display_name="Files · read",
        description="Read metadata and contents of files in Dropbox.",
        scopes=("files.metadata.read", "files.content.read"),
    ),
    "dropbox.files_write": AbilityDef(
        id="dropbox.files_write", provider="dropbox",
        display_name="Files · write",
        description="Upload, move, and modify files in Dropbox.",
        scopes=("files.metadata.write", "files.content.write"),
    ),
    "dropbox.sharing": AbilityDef(
        id="dropbox.sharing", provider="dropbox",
        display_name="Sharing",
        description="Read and create shared links + folders.",
        scopes=("sharing.read", "sharing.write"),
    ),

    # ── Meta (Facebook + Instagram share one OAuth app) ──────────────────
    "meta.profile": AbilityDef(
        id="meta.profile", provider="meta", implicit=True,
        display_name="Sign-in",
        description="Identify the connected Meta account.",
        scopes=("public_profile", "email"),
    ),
    "meta.facebook_post": AbilityDef(
        id="meta.facebook_post", provider="meta",
        display_name="Facebook · post",
        description="Read engagement and post to Facebook Pages.",
        scopes=("pages_manage_posts", "pages_read_engagement"),
    ),
    "meta.instagram_read": AbilityDef(
        id="meta.instagram_read", provider="meta",
        display_name="Instagram · read",
        description="Read Instagram profile, comments, and insights.",
        scopes=("instagram_basic", "instagram_manage_comments", "instagram_manage_insights"),
    ),
    "meta.instagram_publish": AbilityDef(
        id="meta.instagram_publish", provider="meta",
        display_name="Instagram · publish",
        description="Publish content to an Instagram Business account.",
        scopes=("instagram_content_publish",),
    ),

    # ── Twitter / X ───────────────────────────────────────────────────────
    "twitter.profile": AbilityDef(
        id="twitter.profile", provider="twitter", implicit=True,
        display_name="Sign-in",
        description="Identify the connected X account (refresh requires offline.access).",
        scopes=("users.read", "offline.access"),
    ),
    "twitter.tweet_read": AbilityDef(
        id="twitter.tweet_read", provider="twitter",
        display_name="Tweets · read",
        description="Read tweets, likes, and follows.",
        scopes=("tweet.read", "follows.read", "like.read"),
    ),
    "twitter.tweet_write": AbilityDef(
        id="twitter.tweet_write", provider="twitter",
        display_name="Tweets · write",
        description="Post tweets, like, and follow.",
        scopes=("tweet.write", "tweet.moderate.write", "follows.write", "like.write"),
    ),
    "twitter.dm": AbilityDef(
        id="twitter.dm", provider="twitter",
        display_name="DMs",
        description="Read and send direct messages.",
        scopes=("dm.read", "dm.write"),
    ),

    # ── LinkedIn ──────────────────────────────────────────────────────────
    "linkedin.profile": AbilityDef(
        id="linkedin.profile", provider="linkedin", implicit=True,
        display_name="Sign-in",
        description="Identify the connected LinkedIn account.",
        scopes=("openid", "profile", "email", "r_liteprofile", "r_emailaddress"),
    ),
    "linkedin.post": AbilityDef(
        id="linkedin.post", provider="linkedin",
        display_name="Post",
        description="Share posts as the connected member or company.",
        scopes=("w_member_social",),
    ),

    # ── TikTok ────────────────────────────────────────────────────────────
    "tiktok.profile": AbilityDef(
        id="tiktok.profile", provider="tiktok", implicit=True,
        display_name="Sign-in",
        description="Identify the connected TikTok account.",
        scopes=("user.info.basic", "user.info.profile"),
    ),
    "tiktok.video_read": AbilityDef(
        id="tiktok.video_read", provider="tiktok",
        display_name="Videos · read",
        description="List the user's videos.",
        scopes=("video.list",),
    ),
    "tiktok.video_publish": AbilityDef(
        id="tiktok.video_publish", provider="tiktok",
        display_name="Videos · publish",
        description="Upload and publish videos.",
        scopes=("video.upload", "video.publish"),
    ),

    # ── Pinterest ────────────────────────────────────────────────────────
    "pinterest.profile": AbilityDef(
        id="pinterest.profile", provider="pinterest", implicit=True,
        display_name="Sign-in",
        description="Identify the connected Pinterest account.",
        scopes=("user_accounts:read",),
    ),
    "pinterest.boards_read": AbilityDef(
        id="pinterest.boards_read", provider="pinterest",
        display_name="Boards & pins · read",
        description="Read boards and pins.",
        scopes=("boards:read", "pins:read"),
    ),
    "pinterest.boards_write": AbilityDef(
        id="pinterest.boards_write", provider="pinterest",
        display_name="Boards & pins · write",
        description="Create and modify boards and pins.",
        scopes=("boards:write", "pins:write"),
    ),

    # ── Reddit ────────────────────────────────────────────────────────────
    "reddit.profile": AbilityDef(
        id="reddit.profile", provider="reddit", implicit=True,
        display_name="Sign-in",
        description="Identify the connected Reddit account.",
        scopes=("identity",),
    ),
    "reddit.read": AbilityDef(
        id="reddit.read", provider="reddit",
        display_name="Read",
        description="Read posts, history, and subscribed subreddits.",
        scopes=("read", "history", "mysubreddits"),
    ),
    "reddit.submit": AbilityDef(
        id="reddit.submit", provider="reddit",
        display_name="Submit",
        description="Submit posts and comments.",
        scopes=("submit",),
    ),
    "reddit.dm": AbilityDef(
        id="reddit.dm", provider="reddit",
        display_name="Private messages",
        description="Read and send private messages.",
        scopes=("privatemessages",),
    ),

    # ── Snapchat ──────────────────────────────────────────────────────────
    "snapchat.profile": AbilityDef(
        id="snapchat.profile", provider="snapchat", implicit=True,
        display_name="Sign-in",
        description="Identify the connected Snapchat account.",
        scopes=(
            "https://auth.snapchat.com/oauth2/api/user.display_name",
            "https://auth.snapchat.com/oauth2/api/user.bitmoji.avatar",
            "https://auth.snapchat.com/oauth2/api/user.external_id",
        ),
    ),

    # ── Twitch ────────────────────────────────────────────────────────────
    "twitch.profile": AbilityDef(
        id="twitch.profile", provider="twitch", implicit=True,
        display_name="Sign-in",
        description="Identify the connected Twitch account.",
        scopes=("user:read:email",),
    ),
    "twitch.channel_read": AbilityDef(
        id="twitch.channel_read", provider="twitch",
        display_name="Channel · read",
        description="Read follows, subscriptions, and chat.",
        scopes=("user:read:follows", "channel:read:subscriptions", "chat:read"),
    ),
    "twitch.channel_write": AbilityDef(
        id="twitch.channel_write", provider="twitch",
        display_name="Channel · write",
        description="Manage broadcast info, clips, and chat.",
        scopes=("channel:manage:broadcast", "clips:edit", "chat:edit"),
    ),

    # ── eBay ──────────────────────────────────────────────────────────────
    "ebay.profile": AbilityDef(
        id="ebay.profile", provider="ebay", implicit=True,
        display_name="Sign-in",
        description="Identify the connected eBay account.",
        scopes=("https://api.ebay.com/oauth/api_scope",),
    ),
    "ebay.buy_read": AbilityDef(
        id="ebay.buy_read", provider="ebay",
        display_name="Buy · read",
        description="Read the buyer's order history.",
        scopes=("https://api.ebay.com/oauth/api_scope/buy.order.readonly",),
    ),
    "ebay.sell_read": AbilityDef(
        id="ebay.sell_read", provider="ebay",
        display_name="Sell · read",
        description="Read seller inventory, accounts, fulfillment, and marketing data.",
        scopes=(
            "https://api.ebay.com/oauth/api_scope/sell.inventory.readonly",
            "https://api.ebay.com/oauth/api_scope/sell.account.readonly",
            "https://api.ebay.com/oauth/api_scope/sell.fulfillment.readonly",
            "https://api.ebay.com/oauth/api_scope/sell.marketing.readonly",
        ),
    ),
    "ebay.sell_write": AbilityDef(
        id="ebay.sell_write", provider="ebay",
        display_name="Sell · write",
        description="Manage seller inventory, accounts, fulfillment, and marketing.",
        scopes=(
            "https://api.ebay.com/oauth/api_scope/sell.inventory",
            "https://api.ebay.com/oauth/api_scope/sell.account",
            "https://api.ebay.com/oauth/api_scope/sell.fulfillment",
            "https://api.ebay.com/oauth/api_scope/sell.marketing",
        ),
    ),

    # ── Etsy ──────────────────────────────────────────────────────────────
    "etsy.profile": AbilityDef(
        id="etsy.profile", provider="etsy", implicit=True,
        display_name="Sign-in",
        description="Identify the connected Etsy account.",
        scopes=("email_r", "profile_r"),
    ),
    "etsy.shop_read": AbilityDef(
        id="etsy.shop_read", provider="etsy",
        display_name="Shop · read",
        description="Read the user's Etsy shop and transactions.",
        scopes=("shops_r", "transactions_r"),
    ),
    "etsy.shop_write": AbilityDef(
        id="etsy.shop_write", provider="etsy",
        display_name="Shop · write",
        description="Modify the user's Etsy shop.",
        scopes=("shops_w",),
    ),
    "etsy.listings_read": AbilityDef(
        id="etsy.listings_read", provider="etsy",
        display_name="Listings · read",
        description="Read listings.",
        scopes=("listings_r",),
    ),
    "etsy.listings_write": AbilityDef(
        id="etsy.listings_write", provider="etsy",
        display_name="Listings · write",
        description="Create, update, and delete listings.",
        scopes=("listings_w", "listings_d"),
    ),

    # ── Shopify ───────────────────────────────────────────────────────────
    "shopify.products_read": AbilityDef(
        id="shopify.products_read", provider="shopify",
        display_name="Products · read",
        description="Read products and inventory.",
        scopes=("read_products", "read_inventory"),
    ),
    "shopify.products_write": AbilityDef(
        id="shopify.products_write", provider="shopify",
        display_name="Products · write",
        description="Create, update, and delete products and inventory.",
        scopes=("write_products", "write_inventory"),
    ),
    "shopify.orders_read": AbilityDef(
        id="shopify.orders_read", provider="shopify",
        display_name="Orders · read",
        description="Read orders.",
        scopes=("read_orders", "read_locations"),
    ),
    "shopify.orders_write": AbilityDef(
        id="shopify.orders_write", provider="shopify",
        display_name="Orders · write",
        description="Modify orders.",
        scopes=("write_orders",),
    ),

    # ── Amazon SP-API ─────────────────────────────────────────────────────
    "amazon.spapi": AbilityDef(
        id="amazon.spapi", provider="amazon", implicit=True,
        display_name="Selling Partner API",
        description="Access SP-API with the LWA-issued refresh token. Per-API roles "
                    "are granted via the Seller Central app registration, not OAuth scopes.",
        scopes=("sellingpartnerapi::client_credential:refresh_token",),
    ),
}


# ── Helpers ───────────────────────────────────────────────────────────────

def get_ability(ability_id: str) -> Optional[AbilityDef]:
    return ABILITIES.get(ability_id)


def abilities_for_provider(provider: str) -> list[str]:
    """Return ability IDs for a provider in registry order."""
    p = (provider or "").lower()
    return [aid for aid, ab in ABILITIES.items() if ab.provider == p]


def all_scopes_for_provider(provider: str) -> list[str]:
    """Union of every scope across every ability for this provider."""
    p = (provider or "").lower()
    seen: list[str] = []
    for ab in ABILITIES.values():
        if ab.provider != p:
            continue
        for s in ab.scopes:
            if s not in seen:
                seen.append(s)
    return seen


def scopes_for_abilities(ability_ids: list[str]) -> list[str]:
    """Union of scopes across the given ability IDs (de-duplicated, order-preserving)."""
    seen: list[str] = []
    for aid in ability_ids:
        ab = ABILITIES.get(aid)
        if not ab:
            continue
        for s in ab.scopes:
            if s not in seen:
                seen.append(s)
    return seen


def implicit_abilities(provider: str) -> list[str]:
    """Provider's always-on abilities (sign-in / profile). These are added automatically."""
    p = (provider or "").lower()
    return [aid for aid, ab in ABILITIES.items() if ab.provider == p and ab.implicit]


def cover_abilities(provider: str, granted_scopes: list[str]) -> list[str]:
    """Return the ability IDs whose scope set is fully present in `granted_scopes`.

    An ability with zero scopes is treated as always covered (defensive default —
    not currently used by any registered ability). The match is exact: substring /
    suffix matches don't count, because providers like Google use full URI scopes.
    """
    p = (provider or "").lower()
    granted = set(granted_scopes or [])
    out: list[str] = []
    for aid, ab in ABILITIES.items():
        if ab.provider != p:
            continue
        if not ab.scopes:
            out.append(aid)
            continue
        if all(s in granted for s in ab.scopes):
            out.append(aid)
    return out
