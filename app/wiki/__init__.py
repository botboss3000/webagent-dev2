"""Company-wide Wiki — a shared, searchable, agent-managed knowledge base.

The Wiki is one global collection of articles (company info, policies, contacts,
band details — any reference data). Unlike Pages it is NOT per-user: every article
is visible to everyone and searched together. See ``app/wiki/store.py`` for the
async facade and ``app/db/local.py`` (``wiki_*`` methods) for storage + search.
"""
