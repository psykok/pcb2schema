"""Write results back into the PCB.

Generating a schematic is only half a conversion. Until the board carries the same
references and a ``(path ...)`` link to each generated symbol, KiCad treats the two
files as unrelated: "Update PCB from Schematic" would add a second copy of every part
rather than recognising the ones already there.

Three things are written:

* **references** -- ``REF**`` becomes ``R1``/``D1``/``J1``;
* **the symbol link** -- each footprint's path is set to ``/<symbol uuid>``;
* **nets** -- the recovered netlist is applied to pads and tracks, so the board gains
  a real ratsnest and DRC becomes meaningful.

Every write is optional and reversible: :func:`backup` snapshots the file first, and
:func:`apply_to_board` reports exactly what it changed.
"""

import os
import shutil
import time

import pcbnew

__all__ = ["AnnotationReport", "apply_to_board", "backup", "find_lock_files"]


class AnnotationReport(object):
    def __init__(self):
        self.references = []   # (old, new)
        self.paths = []        # (reference, symbol uuid)
        self.nets = []         # (net name, pad count)
        self.skipped = []

    def summary(self):
        return "%d reference(s), %d link(s), %d net(s)" % (
            len(self.references), len(self.paths), len(self.nets)
        )


def find_lock_files(project_dir, stem, include_pcb=False):
    """Editor lock files that would make our writes pointless.

    KiCad writes ``~<name>.lck`` beside a file it has open.

    The **schematic** lock always matters: the ``.kicad_sch`` is written directly, and
    eeschema would discard our version the moment it saves.

    The **PCB** lock normally does not, and must not be treated as a blocker. The
    plugin's usual home is a toolbar button inside pcbnew, which holds that lock by
    definition -- refusing on it would mean the plugin could never run from the very
    UI it lives in. In that mode the board is modified in memory and saved by the user
    through the editor, so nothing is written behind its back.

    Pass ``include_pcb=True`` only when about to write the ``.kicad_pcb`` from outside
    the editor, as the headless runner does.
    """
    suffixes = [".kicad_sch"]
    if include_pcb:
        suffixes.append(".kicad_pcb")

    found = []
    for suffix in suffixes:
        candidate = os.path.join(project_dir, "~%s%s.lck" % (stem, suffix))
        if os.path.exists(candidate):
            found.append(candidate)
    return found


def backup(path, keep=10):
    """Copy *path* aside with a timestamp; returns the backup path or ``None``."""
    if not os.path.isfile(path):
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = "%s.%s.bak" % (path, stamp)
    shutil.copy2(path, target)

    # Keep the directory from filling up over repeated runs.
    base = os.path.basename(path)
    folder = os.path.dirname(path) or "."
    backups = sorted(
        f for f in os.listdir(folder)
        if f.startswith(base + ".") and f.endswith(".bak")
    )
    for stale in backups[:-keep]:
        try:
            os.remove(os.path.join(folder, stale))
        except OSError:
            pass
    return target


def apply_to_board(board, result, set_references=True, set_paths=True, set_nets=True):
    """Apply a :class:`~core.convert.ConversionResult` to *board* in place."""
    report = AnnotationReport()
    by_uuid = {fp.m_Uuid.AsString(): fp for fp in board.GetFootprints()}

    if set_references:
        for fp_uuid, ref in sorted(result.reference_map.items()):
            fp = by_uuid.get(fp_uuid)
            if fp is None:
                continue
            old = fp.GetReference()
            if old != ref:
                fp.SetReference(ref)
                report.references.append((old, ref))

    if set_paths:
        for fp_uuid, sym_uuid in sorted(result.uuid_map.items()):
            fp = by_uuid.get(fp_uuid)
            if fp is None:
                continue
            try:
                fp.SetPath(pcbnew.KIID_PATH("/" + sym_uuid))
                report.paths.append((fp.GetReference(), sym_uuid))
            except Exception as exc:  # pragma: no cover - SWIG variation
                report.skipped.append("path for %s: %r" % (fp.GetReference(), exc))

    if set_nets:
        _apply_nets(board, result, report)

    return report


def _apply_nets(board, result, report):
    """Create net objects and attach pads and tracks to them."""
    pads_by_key = {}
    for fp in board.GetFootprints():
        fp_uuid = fp.m_Uuid.AsString()
        for pad in fp.Pads():
            if pad.GetNumber():
                pads_by_key.setdefault((fp_uuid, pad.GetNumber()), []).append(pad)

    tracks_by_uuid = {t.m_Uuid.AsString(): t for t in board.GetTracks()}
    zones_by_uuid = {z.m_Uuid.AsString(): z for z in board.Zones()}

    existing = board.GetNetsByName()

    for net in result.nets:
        try:
            if net.name in existing:
                info = board.FindNet(net.name)
            else:
                info = pcbnew.NETINFO_ITEM(board, net.name)
                board.Add(info)
        except Exception as exc:  # pragma: no cover
            report.skipped.append("net %s: %r" % (net.name, exc))
            continue

        count = 0
        for pad_ref in net.pads:
            for pad in pads_by_key.get((pad_ref.fp_uuid, pad_ref.number), ()):
                pad.SetNet(info)
                count += 1
        for item_uuid in net.items:
            item = tracks_by_uuid.get(item_uuid) or zones_by_uuid.get(item_uuid)
            if item is not None:
                item.SetNet(info)

        report.nets.append((net.name, count))

    board.BuildConnectivity()
