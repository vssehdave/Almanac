"""Binds the getplaybook package to Almanac's home, settings and wording. Imported by every module that uses it.

Playbooks stay where they always were (`<almanac home>/playbooks`, with `auto.json` and `auto.key` beside them), and the
`playbooks.*` settings in Almanac's config keep their names.
"""
import sqlite3
import sys

from getplaybook import config as pc
from getplaybook import mine as pm

from . import config

pc.home_fn = config.home
pc.load_fn = config.load
pc.NAME = "Almanac"
pc.CLI = "almanac playbook"
pc.SPAWN = [sys.executable, "-m", "getalmanac", "playbook", "work"]


def library_candidates(found, cfg):
    """The prompt library is one more source of candidates for the miner; none is made by looking."""
    if not (config.home() / "almanac.db").exists():
        return []
    from . import library
    from .store import Store

    try:
        store = Store(config.home() / "almanac.db")
        try:
            library.setup(store)
            return library.candidates(store, found, library.min_tools(cfg))
        finally:
            store.close()
    except sqlite3.Error:  # a locked or damaged database costs the library candidates, not the transcript ones
        return []


if library_candidates not in pm.FEEDS:
    pm.FEEDS.append(library_candidates)
