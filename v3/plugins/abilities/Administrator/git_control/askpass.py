#!/usr/bin/python3 -I
"""Private Git HTTPS credential prompt. Never log prompts or credentials."""
import os
import re
import sys
from urllib.parse import urlsplit


def answer(prompt, environ):
    match = re.fullmatch(r"(Username|Password) for '([^']+)': ?", prompt)
    if not match:
        return None
    expected = urlsplit(environ.get("V3_GIT_URL", ""))
    actual = urlsplit(match[2])
    if expected.scheme != "https" or actual.scheme != "https" or actual.password or actual.query or actual.fragment:
        return None
    if (actual.hostname, actual.port or 443, actual.path.rstrip("/")) != (expected.hostname, expected.port or 443, expected.path.rstrip("/")):
        return None
    username = environ.get("V3_GIT_USERNAME", "")
    if actual.username and actual.username != username:
        return None
    value = username if match[1] == "Username" else environ.get("V3_GIT_TOKEN", "")
    if not value or not all(33 <= ord(c) <= 126 for c in value):
        return None
    return value


if __name__ == "__main__":
    try:
        value = answer(sys.argv[1], os.environ) if len(sys.argv) == 2 else None
    except (ValueError, TypeError):
        value = None
    if value is None:
        sys.exit(1)
    sys.stdout.write(value + "\n")
