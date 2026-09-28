"""Domain / DNS management subsystem.

The sibling of ``app/deploy/``: where deploy CREATES a server, this subsystem
POINTS a domain at one. A DNS provider (Cloudflare, Namecheap, Porkbun, …) is a
DROP-IN — one file under ``app/dns/providers/<id>.py`` exposing a module-level
``PROVIDER`` (a ``BaseDNSProvider`` instance) + a ``FEATURE`` header — auto-
discovered by ``app/dns/registry.py`` with no central edit, exactly like the
deploy targets and scheduler providers.

Layout mirrors ``app/deploy/`` deliberately so the two read the same:

  * ``base.py``        — the provider contract (test / list_zones / list_records /
                          upsert_record / delete_record).
  * ``registry.py``    — drop-in discovery of ``providers/``.
  * ``credentials.py`` — the API keys, in the SAME encrypted vault every secret
                          uses (keyed ``dns_cred:<id>``); never plaintext.
  * ``store.py``       — the NON-secret settings + the last domain that was
                          pointed, in ``data/config/dns.json``.
  * ``verify.py``      — provider-independent DNS resolution check.
  * ``manager.py``     — the glue the router drives: build the catalog, connect,
                          point a domain at an IP (streaming), verify.
"""
