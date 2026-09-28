# Wiki Control — the shared, company-wide knowledge base

The Wiki is **one shared, searchable knowledge base** that everyone in the
workspace sees: app features, internal processes, policies, contacts, project
facts — any reference knowledge worth keeping. It is **not** your private memory
(that's the `memory` tool) and **not** the chat transcript (`session_search`).
What you write here changes shared data **everyone** can read, so treat writes
with care.

You can both **read** it (search, list, get, backlinks, history) and **write**
it (create, update, set status, restore, delete).

On each substantive turn, Wiki Control automatically searches this knowledge
base and may place a few relevant excerpts in your BRAIN CONTEXT. Treat those as
leads, not necessarily complete articles: call `wiki_get` before relying on
details that may fall outside an excerpt. Greetings and command-only turns skip
automatic recall, and disabling Wiki Control removes both recall and its tools.

## Read FIRST — search before you answer, and before you write

1. **Search:** `wiki_search(query="…")` — meaning + keyword search. This is your
   first move whenever a question *might* be answered by shared knowledge, and
   **always** before creating an article (so you don't duplicate one).
2. **Browse:** `wiki_list()` — every visible article with snippets, newest first.
   Use this to see what exists when you don't have a precise query.
3. **Read:** `wiki_get(slug="…")` — the full article. Use a slug from a
   search/list result, never a guessed one.
4. **Synthesize and cite.** Combine what you found with the request and **name
   the articles you used**. If search returns nothing relevant, say so — do
   **not** invent an article title, slug, or fact that wasn't in the results.

### No-hallucination rule
Only cite or `wiki_get` slugs that actually appeared in a `wiki_search` /
`wiki_list` result. If you're unsure an article exists, search for it — never
assert it exists or quote content you didn't read back. A `wiki_get` on a
missing slug returns an error; that's your signal the article isn't there.

## Writing to the Wiki

- **`wiki_create(title, body, tags?, category?, status?)`** — add a new article.
  Search first. Bodies are **Markdown** (headings, lists, tables, bold, links).
  Link to another article with `[[Article Title]]` or `[[slug]]` — it renders as
  a live link and feeds backlinks. New articles default to **draft** (internal).
- **`wiki_update(slug, …)`** — edit an existing article. Only the fields you pass
  change; the slug is preserved. If you're rewriting the body, **`wiki_get` it
  first** so you build on the current text instead of clobbering it. The prior
  version is auto-saved to history.
- **`wiki_set_status(slug, status)`** — publish (`published` = public, visible to
  anonymous visitors) or unpublish (`draft` = internal, members only). Don't
  publish anything confidential or not meant for the public.

### Draft vs published — and who you're acting for
Every article is **draft** (internal, signed-in members only) or **published**
(public). When you serve a **signed-in member** you can read & write both. When
you serve an **anonymous visitor** you can only read **published** articles and
**all writes are refused** — if a create/update/delete comes back refused for
that reason, explain that editing needs a signed-in member rather than retrying.

## History, recovery, and the link graph

- **`wiki_history(slug)`** — list prior versions (newest first), each with a
  revision id, author, time, snippet.
- **`wiki_get_revision(revision_id)`** — read one past version without touching
  the live article.
- **`wiki_restore(slug, revision_id)`** — roll back to a past revision. The
  current version is snapshotted first, so a restore is itself reversible.
- **`wiki_backlinks(slug)`** — which articles link **to** this one. Check this
  **before** renaming or deleting an article so you don't orphan references.

## Deleting (the one destructive tool)
- **`wiki_delete(slug)`** — permanently removes the article for everyone. This is
  the only destructive Wiki tool (it triggers a confirmation). Prefer
  `wiki_set_status(…, "draft")` to "hide" something; reserve delete for content
  that is genuinely wrong or junk. Check `wiki_backlinks` first.

## Best practices
- Search first — to answer, and to avoid duplicate articles.
- Keep one topic per article; link related topics with `[[…]]` instead of
  repeating content.
- Edit (update + history) over delete-and-recreate, so the revision trail and
  the slug/backlinks survive.
- Cite what you read; never fabricate an article or its contents.
