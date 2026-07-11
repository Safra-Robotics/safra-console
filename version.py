"""Single version source for the Safra Operator Console.

Bumped by hand per release; tools/build_installer.py stamps it into the
installer + update manifest, and the updater compares against the feed's
latest.json.
"""

VERSION = "0.5.0"


def parse(v):
    try:
        return tuple(int(x) for x in v.strip().lstrip("v").split("."))
    except (ValueError, AttributeError):
        return (0,)


def is_newer(candidate, current=VERSION):
    return parse(candidate) > parse(current)
