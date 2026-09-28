"""Deploy subsystem — the App Settings → Deploy panel's backend.

Live one-click deploy of WebAgent onto a cloud target (Google VM today; AWS, a
plain Linux box and Docker are drop-in targets to follow). Each target is a
self-describing drop-in under ``providers/`` (see registry.py); cloud keys live
in the encrypted vault and are auto-discarded after a successful deploy
(credentials.py). The HTTP surface is ``app/api/deploy.py``.
"""
