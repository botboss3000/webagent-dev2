"""Small, dependency-free shared utilities for the backend.

Currently holds ``config_io`` — the single place that knows how to read and write
the app's JSON config files safely (create-on-demand, atomic, merge-one-key). Keep
this package free of app-specific imports so any module can use it without cycles.
"""
