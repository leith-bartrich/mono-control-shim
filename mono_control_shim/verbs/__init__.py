"""The verb packs the broker serves beyond Step 1's ``ping`` / ``broker.info``.

Importing this package registers every pack: each module's ``@verb`` decorators
run at import, populating :data:`mono_control_shim.broker._VERBS`. ``cli`` imports
this before starting the broker so a container run finds the git + mono_config
verbs live; ``broker.info`` then lists them.

Kept as granular modules (``git`` / ``mono_config``) so a future pack slots in by
adding a module and one import here, with no change to the transport core.
"""

from __future__ import annotations

from mono_control_shim.verbs import git, mono_config  # noqa: F401  (import = register)

__all__ = ["git", "mono_config"]
