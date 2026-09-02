"""Ownership record for incremental sync.

Re-running must preserve the user's manual schematic work, which means knowing which
items in the file are ours to regenerate and which are theirs to leave alone. Wires
and junctions cannot carry a marker property, so instead the plugin keeps a sidecar
``<project>.pcb2schema.json`` listing the UUID of everything it created.

Anything in the schematic whose UUID is absent from that list was put there by the
user and is never touched. If the sidecar is missing -- deleted, or a first run
against a hand-built schematic -- the plugin owns nothing, and existing content is
left strictly alone rather than assumed to be disposable.
"""

import json
import os

__all__ = ["SyncState", "state_path"]

STATE_VERSION = 1


def state_path(project_dir, stem):
    return os.path.join(project_dir, "%s.pcb2schema.json" % stem)


class SyncState(object):
    """UUIDs of the items this plugin generated, plus the user's symbol choices."""

    def __init__(self, data=None):
        data = data or {}
        # footprint uuid -> {"symbol": lib_id, "units": {unit: symbol uuid}}
        self.components = data.get("components", {})
        # UUIDs of wires/junctions/labels we drew
        self.routing = set(data.get("routing", []))
        # footprint lib_id -> symbol lib_id, so a choice is only ever asked once
        self.symbol_choices = data.get("symbol_choices", {})
        self.paper = data.get("paper", "")

    # -- persistence --------------------------------------------------------

    @classmethod
    def load(cls, path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return cls()
        if data.get("version") != STATE_VERSION:
            return cls()
        return cls(data)

    def save(self, path):
        payload = {
            "version": STATE_VERSION,
            "comment": "Written by pcb2schema. Tracks which schematic items the "
                       "plugin generated so re-runs leave your own edits alone. "
                       "Safe to delete; doing so makes the plugin treat the whole "
                       "schematic as hand-made and stop managing it.",
            "paper": self.paper,
            "components": self.components,
            "routing": sorted(self.routing),
            "symbol_choices": self.symbol_choices,
        }
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)

    # -- queries ------------------------------------------------------------

    def owns(self, uuid):
        if uuid in self.routing:
            return True
        for entry in self.components.values():
            if uuid in entry.get("units", {}).values():
                return True
        return False

    def owned_symbol_uuids(self):
        out = set()
        for entry in self.components.values():
            out.update(entry.get("units", {}).values())
        return out

    def symbol_uuid(self, fp_uuid, unit):
        return self.components.get(fp_uuid, {}).get("units", {}).get(str(unit))

    # -- updates ------------------------------------------------------------

    def record_component(self, fp_uuid, lib_id, units, reference=""):
        """*units* maps unit number to the symbol UUID placed for it.

        The reference is stored purely so a part that later disappears from the board
        can be named in the report -- a bare UUID tells the user nothing.
        """
        self.components[fp_uuid] = {
            "symbol": lib_id,
            "reference": reference,
            "units": {str(k): v for k, v in units.items()},
        }

    def record_choice(self, footprint_lib_id, symbol_lib_id):
        self.symbol_choices[footprint_lib_id] = symbol_lib_id

    def forget_component(self, fp_uuid):
        self.components.pop(fp_uuid, None)

    def set_routing(self, uuids):
        self.routing = set(uuids)
