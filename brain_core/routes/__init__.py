"""brain_core/routes — FastAPI router auto-discovery.

Before 2026-05-12 server.py manually imported and mounted each of the
28 route modules individually (56 lines of boilerplate). New modules
required two coordinated edits in server.py.

iter_routers() walks this package, imports every sibling module, and
yields each module's `router` attribute. Result: adding a new route
file `routes/foo.py` with a `router` attribute is enough — server.py
auto-mounts it.

Stable iteration order: alphabetical by module name. Two modules
registering the same path produce a deterministic winner (the
alphabetically-earlier one). Any explicit ordering needs should
either be documented as a path collision or resolved by renaming.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from collections.abc import Iterator
from typing import Any

log = logging.getLogger("brain.routes")


def iter_routers() -> Iterator[tuple[str, Any]]:
    """Yield (module_name, router) for every sibling module that defines `router`.

    Module load failures are logged and skipped — a broken side module
    must not take down the whole brain server.
    """
    pkg = __name__
    for mod_info in sorted(pkgutil.iter_modules(__path__), key=lambda m: m.name):
        if mod_info.ispkg or mod_info.name.startswith("_"):
            continue
        full_name = f"{pkg}.{mod_info.name}"
        try:
            module = importlib.import_module(full_name)
        except Exception as exc:
            log.error("failed to import route module %s: %s", full_name, exc)
            continue
        router = getattr(module, "router", None)
        if router is None:
            continue
        yield mod_info.name, router
