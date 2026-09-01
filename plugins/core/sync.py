"""Run a conversion against a project on disk, incrementally.

:func:`sync_project` is the entry point both the GUI and the headless runner use. It
decides whether this is a first generation or an update, preserves whatever the user
has done to the schematic in between, and writes the results out.

The preservation rule is deliberately conservative. The plugin only ever removes items
whose UUIDs it recorded in the sidecar state file; everything else -- symbols the user
added by hand, extra wires, labels, notes, graphics, title-block edits -- is carried
through untouched. If the sidecar is missing, the plugin owns nothing and will not
delete anything at all.
"""

import os

from . import annotate, convert, preflight, schematic, state

__all__ = ["SyncOutcome", "sync_project", "project_paths"]


class SyncOutcome(object):
    def __init__(self, result, report, sch_path, created, backups):
        self.result = result
        self.report = report
        self.sch_path = sch_path
        self.created = created  # True on first generation, False on update
        self.backups = backups

    @property
    def blocked(self):
        return self.result.blocked

    def summary(self):
        if self.result.blocked:
            return "Nothing written -- the board is not fully tagged"
        verb = "Created" if self.created else "Updated"
        parts = ["%s %s" % (verb, os.path.basename(self.sch_path)), self.result.summary()]
        if not self.created:
            parts.append("%d symbol position(s) preserved" % len(self.result.preserved))
        return " | ".join(parts)


def project_paths(pcb_path):
    """``(project_dir, stem, schematic path, state path)`` for a board file."""
    project_dir = os.path.dirname(os.path.abspath(pcb_path))
    stem = os.path.splitext(os.path.basename(pcb_path))[0]
    return (
        project_dir,
        stem,
        os.path.join(project_dir, stem + ".kicad_sch"),
        state.state_path(project_dir, stem),
    )


def _load_existing(sch_path, stem):
    """Existing schematic, or ``None`` if there is nothing usable to build on."""
    if not os.path.isfile(sch_path) or os.path.getsize(sch_path) == 0:
        return None
    try:
        return schematic.Schematic.load(sch_path, project_name=stem)
    except Exception:
        # An unreadable schematic is not something to silently overwrite.
        raise


def sync_project(board, pcb_path, resolver=None, index=None, progress=None,
                 write_pcb=True, dry_run=False, tag_policy=preflight.REQUIRE):
    """Generate or update the schematic beside *pcb_path*."""
    project_dir, stem, sch_path, st_path = project_paths(pcb_path)

    existing = _load_existing(sch_path, stem)
    sync_state = state.SyncState.load(st_path) if existing is not None else state.SyncState()

    # An existing schematic we have no record of is the user's work, not ours. Build
    # into it additively rather than replacing it.
    if existing is not None and not sync_state.components:
        progress and progress("Adding to the existing schematic")

    result = convert.convert(
        board,
        project_name=stem,
        project_dir=project_dir,
        resolver=resolver,
        index=index,
        progress=progress,
        existing=existing,
        sync_state=sync_state if existing is not None else None,
        tag_policy=tag_policy,
    )

    # The board is not fully tagged and the caller did not authorise auto-tagging.
    # Nothing has been written; the caller reports what needs naming.
    if result.blocked:
        return SyncOutcome(result, annotate.AnnotationReport(), sch_path,
                           existing is None, [])

    if dry_run:
        return SyncOutcome(result, annotate.AnnotationReport(), sch_path,
                           existing is None, [])

    backups = []
    if existing is not None:
        b = annotate.backup(sch_path)
        if b:
            backups.append(b)

    result.schematic.save(sch_path)

    for fp_uuid, units in result.component_units.items():
        lib_id = next((sd.lib_id for i, sd, _r, _m in result.symbols
                       if i.uuid == fp_uuid), "")
        sync_state.record_component(fp_uuid, lib_id, units)
    # Footprints that have gone from the board should stop being tracked, so their
    # symbols are removed rather than orphaned on the next run.
    for fp_uuid in list(sync_state.components):
        if fp_uuid not in result.component_units:
            sync_state.forget_component(fp_uuid)
    sync_state.set_routing(result.generated_routing)
    sync_state.paper = result.paper
    for info, symdef, _ref, _map in result.symbols:
        sync_state.record_choice(info.lib_id, symdef.lib_id)
    sync_state.save(st_path)

    report = annotate.AnnotationReport()
    if write_pcb:
        if board.GetFileName():
            b = annotate.backup(board.GetFileName())
            if b:
                backups.append(b)
        report = annotate.apply_to_board(board, result)

    return SyncOutcome(result, report, sch_path, existing is None, backups)
