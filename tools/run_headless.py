#!/usr/bin/env python3
"""Run the conversion on a board file without opening KiCad.

Must be run with KiCad's bundled interpreter -- ``pcbnew`` imports nowhere else:

    /Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/\\
3.9/bin/python3 tools/run_headless.py <board.kicad_pcb> [options]

With no resolver available there is nobody to ask about ambiguous footprints, so by
default they are reported and skipped rather than guessed. ``--auto`` accepts the
top-ranked candidate instead, which is useful for bulk checks but should not be
trusted blindly.
"""

import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "plugins"))

if os.environ.get("P2S_QUIET", "1") == "1":
    # KiCad's SWIG layer writes wxApp warnings and image-handler noise to stderr
    # before anything of ours runs.
    sys.stderr.flush()
    os.dup2(os.open(os.devnull, os.O_WRONLY), 2)

import pcbnew  # noqa: E402
from core import annotate, matcher, preflight, symlib, sync  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board", help="path to a .kicad_pcb")
    ap.add_argument("--auto", action="store_true",
                    help="accept the best candidate for ambiguous footprints")
    ap.add_argument("--auto-tag", action="store_true",
                    help="auto-increment references and net names that are missing "
                         "(never renames anything already named)")
    ap.add_argument("--stage", choices=("all", "symbols", "nets"), default="all",
                    help="'symbols' places parts without wiring them, so you can "
                         "arrange the sheet first; 'nets' then wires it up, keeping "
                         "wherever you put things")
    ap.add_argument("--dry-run", action="store_true", help="report without writing")
    ap.add_argument("--no-pcb", action="store_true",
                    help="write the schematic but leave the board untouched")
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args()

    if not os.path.isfile(args.board):
        sys.exit("no such board: %s" % args.board)

    def note(msg):
        if not args.quiet:
            print("  %s" % msg)

    started = time.time()
    project_dir, stem, _sch, _state = sync.project_paths(args.board)

    # Unlike the GUI plugin, this writes the board file itself, so a PCB held open by
    # pcbnew is a genuine conflict here.
    writes_pcb = not (args.no_pcb or args.dry_run)
    locks = annotate.find_lock_files(project_dir, stem, include_pcb=writes_pcb)
    if locks:
        for lock in locks:
            which = "schematic" if lock.endswith(".kicad_sch.lck") else "board"
            print("The %s is open in a KiCad editor (%s)."
                  % (which, os.path.basename(lock)))
        print("Close it first, or use --dry-run / --no-pcb.")
        return 3

    board = pcbnew.LoadBoard(args.board)

    note("Indexing symbol libraries...")
    index = symlib.SymbolIndex.build(project_dir)
    note("%d symbols in %d libraries" % (len(index), len(index.libraries())))

    def resolver(info, candidates, forced=False):
        if not forced and candidates and matcher.is_confident(candidates):
            return candidates[0]
        if args.auto and candidates:
            note("auto-picking %s for %s" % (candidates[0].lib_id, info.lib_id))
            return candidates[0]
        return None

    outcome = sync.sync_project(
        board, args.board, resolver=resolver, index=index,
        progress=note, write_pcb=not args.no_pcb, dry_run=args.dry_run,
        tag_policy=preflight.AUTO if args.auto_tag else preflight.REQUIRE,
        stage=args.stage,
    )

    if outcome.blocked:
        print("\nThe board is not fully tagged, so nothing was written.\n")
        print(outcome.result.tag_issues.describe())
        print("\nTag these in the PCB editor, or re-run with --auto-tag to have "
              "them auto-incremented.\n(Auto-tagging never renames anything that "
              "already has a name.)")
        return 2

    if not args.dry_run and not args.no_pcb:
        board.Save(args.board)

    result = outcome.result
    print("\n%s" % outcome.summary())
    if outcome.drift.any:
        print("Board has changed since the last run:")
        for line in outcome.drift.describe().splitlines():
            print("  %s" % line)
    print("Board: %s" % outcome.report.summary())
    for info, symdef, ref, _pin_map in result.symbols:
        print("  %-6s %-34s <- %s" % (ref, symdef.lib_id, info.lib_id))
    if result.stage != sync.STAGE_SYMBOLS:
        for net in result.nets:
            members = ", ".join(
                "%s.%s" % (result.reference_map.get(p.fp_uuid, "?"), p.number)
                for p in net.pads)
            print("  net %-10s %s" % (net.name, members))
    for info in result.unresolved:
        print("  UNRESOLVED  %s (pads: %s)" % (info.lib_id, ",".join(info.pads)))
    for warning in result.warnings:
        print("  ! %s" % warning)
    if result.labelled_nets:
        print("  labelled instead of routed: %s" % ", ".join(sorted(result.labelled_nets)))
    if args.dry_run:
        print("\n(dry run -- nothing written)")
    print("took %.1fs" % (time.time() - started))
    return 1 if result.unresolved else 0


if __name__ == "__main__":
    sys.exit(main())
