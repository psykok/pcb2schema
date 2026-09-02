"""The pcbnew action plugin: toolbar button and run flow.

Everything substantive lives in :mod:`core`; this module is the GUI shell. It resolves
the project paths, refuses to run when an editor holds the files open, drives the
conversion with a wx-backed symbol picker, and reports what happened.
"""

import os
import traceback

import pcbnew
import wx

from .core import annotate, matcher, preflight, symlib, sync
from .core import state as sync_state
from .ui.dialog_symbol import SymbolPickerDialog

ICON = os.path.join(os.path.dirname(__file__), "icon.png")


class Pcb2SchemaAction(pcbnew.ActionPlugin):
    def defaults(self):
        self.name = "PCB to Schematic"
        self.category = "Generate"
        self.description = (
            "Generate a schematic from this PCB: match footprints to symbols, "
            "recover the netlist from copper, and route it."
        )
        self.show_toolbar_button = True
        if os.path.isfile(ICON):
            self.icon_file_name = ICON

    def Run(self):
        try:
            self._run()
        except Exception:
            wx.MessageBox(
                "pcb2schema failed:\n\n%s" % traceback.format_exc(),
                "PCB to Schematic", wx.OK | wx.ICON_ERROR,
            )

    # -- flow ---------------------------------------------------------------

    def _run(self):
        board = pcbnew.GetBoard()
        pcb_path = board.GetFileName()
        if not pcb_path:
            wx.MessageBox("Save the board before generating a schematic.",
                          "PCB to Schematic", wx.OK | wx.ICON_WARNING)
            return

        project_dir = os.path.dirname(pcb_path)
        stem = os.path.splitext(os.path.basename(pcb_path))[0]
        sch_path = os.path.join(project_dir, stem + ".kicad_sch")

        # Only the schematic lock matters here. This plugin runs inside pcbnew, so the
        # board's own lock is always present and is not a reason to refuse; the board
        # is edited in memory and saved by the user through the editor.
        locks = annotate.find_lock_files(project_dir, stem)
        if locks:
            wx.MessageBox(
                "The schematic editor has this project open:\n\n  %s\n\n"
                "Close the schematic editor and run this again. Leaving it open would "
                "mean the generated schematic is discarded the next time eeschema "
                "saves.\n\nThe PCB editor can stay open -- the board is updated in "
                "memory and saved by you as usual."
                % "\n  ".join(os.path.basename(p) for p in locks),
                "PCB to Schematic", wx.OK | wx.ICON_WARNING,
            )
            return

        stage = self._ask_stage()
        if stage is None:
            return

        known = sync_state.SyncState.load(
            sync_state.state_path(project_dir, stem)).symbol_choices
        untracked = (os.path.isfile(sch_path) and os.path.getsize(sch_path) > 0
                     and not known)
        if untracked and not self._confirm_untracked(sch_path):
            return

        progress = wx.ProgressDialog(
            "PCB to Schematic", "Indexing symbol libraries...",
            maximum=100, style=wx.PD_APP_MODAL | wx.PD_AUTO_HIDE,
        )
        try:
            progress.Update(5, "Indexing symbol libraries...")
            index = symlib.SymbolIndex.build(project_dir)

            ui = {"n": 10, "skip_all": False, "cancelled": False}

            def report(msg):
                ui["n"] = min(ui["n"] + 12, 90)
                progress.Update(ui["n"], msg)

            def resolver(info, candidates, forced=False):
                if not forced and candidates and matcher.is_confident(candidates):
                    return candidates[0]
                # A choice made on a previous run is not worth asking about again.
                remembered = known.get(info.lib_id)
                if remembered:
                    for cand in candidates:
                        if cand.lib_id == remembered:
                            return cand
                if ui["skip_all"]:
                    return None
                progress.Hide()
                dlg = SymbolPickerDialog(None, info, candidates, index)
                try:
                    code = dlg.ShowModal()
                    if code == wx.ID_OK:
                        return dlg.result()
                    if code == wx.ID_CANCEL:
                        ui["cancelled"] = True
                    ui["skip_all"] = ui["skip_all"] or dlg.skip_all
                    return None
                finally:
                    dlg.Destroy()
                    progress.Show()

            # Dry run first: this reports what is untagged without writing anything,
            # so the tagging question can be asked before any decision is acted on.
            probe = sync.sync_project(
                board, pcb_path, resolver=resolver, index=index, progress=report,
                dry_run=True, tag_policy=preflight.REQUIRE, stage=stage,
            )
            if ui["cancelled"]:
                return

            policy = preflight.REQUIRE
            if probe.blocked:
                progress.Hide()
                policy = self._ask_tagging(probe.result.tag_issues)
                progress.Show()
                if policy is None:
                    return

            outcome = sync.sync_project(
                board, pcb_path, resolver=resolver, index=index, progress=report,
                tag_policy=policy, stage=stage,
            )
            if ui["cancelled"]:
                return
            progress.Update(98, "Updating the board...")
            pcbnew.Refresh()
        finally:
            progress.Destroy()

        self._show_summary(outcome)

    # -- dialogs ------------------------------------------------------------

    _STAGES = (
        ("Symbols and nets", sync.STAGE_ALL,
         "Place every part and wire it up in one pass."),
        ("Symbols only", sync.STAGE_SYMBOLS,
         "Place the parts and stop, so you can arrange the sheet yourself first. "
         "Existing wiring is left alone."),
        ("Nets only", sync.STAGE_NETS,
         "Wire up the sheet, keeping the parts exactly where you put them."),
    )

    @classmethod
    def _ask_stage(cls):
        """Let the user split the conversion into arrange-then-wire.

        The grid placement is deliberately mechanical, and on anything but a small
        board a person will want to lay the sheet out themselves. Splitting the run
        means the router works with their arrangement instead of one they are about
        to throw away.
        """
        choices = ["%s -- %s" % (label, blurb) for label, _stage, blurb in cls._STAGES]
        dlg = wx.SingleChoiceDialog(
            None, "What should this run do?", "PCB to Schematic", choices)
        dlg.SetSelection(0)
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return None
            return cls._STAGES[dlg.GetSelection()][1]
        finally:
            dlg.Destroy()

    @staticmethod
    def _ask_tagging(issues):
        """Offer to auto-tag, or let the user go and do it by hand.

        Returns a tag policy, or ``None`` if the user wants to handle it themselves.
        """
        components, duplicates, nets = issues.counts()
        summary = []
        if components:
            summary.append("%d footprint(s) with no reference designator" % components)
        if duplicates:
            summary.append("%d duplicated reference(s)" % duplicates)
        if nets:
            summary.append("%d unnamed net(s)" % nets)

        message = (
            "This board is not fully tagged:\n\n  %s\n\n"
            "%s\n\n"
            "Tag them automatically?\n\n"
            "Auto-tagging assigns the next free number to each untagged item and "
            "never renames anything that already has one. Choose No to stop here and "
            "tag them yourself in the PCB editor."
            % ("\n  ".join(summary), issues.describe(limit=6))
        )
        if duplicates:
            message += ("\n\nDuplicated references cannot be fixed automatically -- "
                        "they need deciding by hand.")

        dlg = wx.MessageDialog(None, message, "PCB to Schematic",
                               wx.YES_NO | wx.CANCEL | wx.ICON_QUESTION)
        dlg.SetYesNoCancelLabels("Auto-tag and continue", "I will tag them", "Cancel")
        try:
            answer = dlg.ShowModal()
        finally:
            dlg.Destroy()
        return preflight.AUTO if answer == wx.ID_YES else None

    @staticmethod
    def _confirm_untracked(sch_path):
        dlg = wx.MessageDialog(
            None,
            "%s already has content, and there is no record of this plugin having "
            "generated it.\n\n"
            "Generated symbols and wires will be added to it. Nothing already in the "
            "file will be moved or deleted, and a timestamped backup is written "
            "first.\n\nContinue?" % os.path.basename(sch_path),
            "PCB to Schematic",
            wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING,
        )
        try:
            return dlg.ShowModal() == wx.ID_YES
        finally:
            dlg.Destroy()

    @staticmethod
    def _show_summary(outcome):
        result = outcome.result
        lines = [
            outcome.summary(),
            "",
            "Board updated: %s" % outcome.report.summary(),
        ]
        if outcome.drift.any:
            lines += ["", "The board changed since the last run:"]
            lines += ["  " + l for l in outcome.drift.describe().splitlines()]
        if result.labelled_nets:
            lines += ["", "Routed as net labels instead of wires (no clear path):",
                      "  " + ", ".join(sorted(result.labelled_nets)[:12])]
        if result.unresolved:
            lines += ["", "No symbol chosen for:"]
            lines += ["  " + i.lib_id for i in result.unresolved[:12]]
        if result.auto_tagged_components:
            lines += ["", "References auto-assigned: "
                      + ", ".join(result.auto_tagged_components[:12])]
        if result.auto_tagged_nets:
            lines += ["Nets auto-named: " + ", ".join(result.auto_tagged_nets[:12])]
        if result.warnings:
            lines += [""] + ["! " + w for w in result.warnings[:12]]
        if outcome.backups:
            lines += ["", "Backups: " + ", ".join(
                os.path.basename(b) for b in outcome.backups)]
        lines += ["", "Open the schematic editor to review the result."]

        wx.MessageBox("\n".join(lines), "PCB to Schematic", wx.OK | wx.ICON_INFORMATION)
