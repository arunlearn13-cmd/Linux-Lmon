"""Paths for lmon's small per-user files (theme, timeline), the same under `sudo lmon` as without it."""
from __future__ import annotations

import os
import pwd


def _sudo_user():
    name = os.getenv("SUDO_USER")
    if os.geteuid() == 0 and name and name != "root":
        try:
            return pwd.getpwnam(name)
        except KeyError:
            return None
    return None


def home() -> str:
    u = _sudo_user()
    return u.pw_dir if u else os.path.expanduser("~")


def give_back(path: str):
    """Under sudo, hand a file (and the dirs we made for it) back to the real user so a later plain `lmon` can write it."""
    u = _sudo_user()
    if not u:
        return
    try:
        d = os.path.dirname(path)
        for p in (path, d, os.path.dirname(d)):
            if p.startswith(u.pw_dir + os.sep) and os.path.exists(p) and os.stat(p).st_uid == 0:
                os.chown(p, u.pw_uid, u.pw_gid)
    except OSError:
        pass
