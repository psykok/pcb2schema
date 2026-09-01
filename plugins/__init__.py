"""pcb2schema -- generate a KiCad schematic from a PCB layout.

pcbnew imports this package to discover action plugins. Registration is guarded
so that importing ``plugins.core.*`` from a plain interpreter (headless tests,
CI) still works when wxPython or the pcbnew GUI are unavailable.
"""

import sys as _sys


def _register():
    from .action import Pcb2SchemaAction

    Pcb2SchemaAction().register()


try:
    _register()
except ImportError:
    # Headless: no wx / no pcbnew GUI. Core modules stay importable.
    pass
except Exception as _exc:  # pragma: no cover - surfaced in the pcbnew console
    # A real failure inside the plugin. Never swallow it silently: without this
    # the toolbar button just fails to appear with no explanation.
    print("pcb2schema: failed to register action plugin: %r" % (_exc,), file=_sys.stderr)
