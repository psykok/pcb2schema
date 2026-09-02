# pcb2schema

A KiCad addon that runs the design flow backwards: draw a PCB, get a schematic.

Drop footprints in the PCB editor, draw tracks between their pads, then run the
plugin. It works out which library symbol each footprint represents, recovers the
netlist from the copper you drew, places the symbols and routes the connections as
orthogonal wires. References are annotated back into the board and linked to the
generated symbols, so KiCad's normal schematic↔PCB sync keeps working afterwards.

Built and tested against **KiCad 10.0.6**.

## Why this is not straightforward

KiCad has no schematic API. `kicad-cli sch` only exports, and the IPC API covers the
board. So the `.kicad_sch` is generated directly as S-expressions, which means the
writer has to be exactly faithful to KiCad's own format — verified by round-tripping
every KiCad file on the system byte-for-byte.

The other half is that a freshly drawn board usually has **no nets at all**: every pad
and track sits on netcode 0. KiCad's ratsnest is useless in that state, but its
geometric hit-testing is not, and a union-find over pads, tracks, vias and zones
rebuilds the netlist from what is physically touching what.

## Installing

```sh
python3 tools/build_pcm.py            # -> dist/pcb2schema-<version>.zip
```

Then **Plugin and Content Manager → Install from File…** and pick the zip. Restart
pcbnew, or use *Tools → External Plugins → Refresh Plugins*.

This is the route to use. PCM reads `metadata.json` out of the archive as it installs
and records the result in `installed_packages.json`; it never reads it back from the
installed directory. So a package only has an author, description and version if PCM
put it there.

For development there is also

```sh
python3 tools/build_pcm.py --install  # copy straight into the plugin folder
```

which gets the code under the toolbar button without going through PCM. The trade-off
is that PCM has no record of it, so it appears in the package list with a blank author
and version 0.0. That listing is cosmetic -- the plugin itself works either way.

The installed layout matters and is easy to get wrong: KiCad imports each directory
under `<KICAD_3RD_PARTY>/plugins/` *as a Python module*, and skips any that has no
`__init__.py` at its top level. So the files must land at

```
~/Documents/KiCad/<ver>/3rdparty/plugins/pcb2schema/__init__.py
```

not one level deeper. The directory name also becomes the module name, so it has to be
a legal Python identifier — the dotted reverse-DNS identifier cannot be used here.
`build_pcm.py --install` does this and fails loudly if the marker file is missing.

## Using it

1. Open your board in the PCB editor and save it.
2. Close the **schematic** editor. The PCB editor can stay open — that's where the
   plugin runs from. Only eeschema is a conflict, because the `.kicad_sch` is written
   directly and would be discarded the next time eeschema saves. The board is updated
   in memory and saved by you as usual.
3. Click the **PCB to Schematic** toolbar button, or *Tools → External Plugins → PCB
   to Schematic*.
4. If anything is untagged, decide whether to auto-tag it (see below).
5. Confirm any footprints the matcher could not identify on its own.
6. Open the schematic to review.

Both files are backed up with a timestamp before anything is written.

### Two-step: arrange, then wire

The automatic placement is a grid. It echoes the board so the sheet is navigable, but
on any real design you will want to lay it out yourself — and the router should work
with *your* arrangement, not one you are about to discard. So the run can be split:

1. **Symbols only** — places every part and stops. Any existing wiring is left alone.
2. *Arrange the sheet in the schematic editor.*
3. **Nets only** — wires it up, keeping every symbol exactly where you put it.

The plugin asks which to do on each run; **Symbols and nets** does both in one pass and
is the default. From the command line:

```sh
tools/run_headless.py board.kicad_pcb --stage symbols   # place, then go arrange
tools/run_headless.py board.kicad_pcb --stage nets      # wire up your arrangement
```

Splitting the run changes only where things sit — the resulting netlist is identical
either way, which is asserted by a test.

Between the two steps the board may have moved on, so the second step reports what
changed: parts added, parts deleted, and footprints swapped for a different one.
Wiring against a stale sheet would otherwise produce a schematic that quietly
disagrees with the PCB.

### Re-running keeps what you have arranged

Symbol positions survive a re-run, and so does wiring. A net is redrawn only when
something about it actually changed — its name, its set of pins, or where those pins
sit. Everything else is left exactly as you arranged it, including routes you dragged
into shape by hand.

Move one symbol and only the nets touching it are redrawn; the rest of the sheet is
untouched. The run reports which is which:

```
Updated board.kicad_sch | 12 symbols, 10 nets, 8 net(s) left untouched
```

If part of a net's wiring has been deleted, that net is redrawn rather than left half
wired.

### Everything must be tagged first

A conversion is only as good as the identifiers it starts from. If footprints are
still `REF**` and nets are unnamed, the plugin would have to invent both — and then
write its inventions back onto your board. That is a naming decision, and it belongs
to you, so it is an explicit gate rather than a silent default.

Before generating anything the plugin checks for footprints with no reference
designator, duplicated references, and unnamed nets. If it finds any it stops, having
written nothing, and reports exactly what is missing. You can then either tag them
yourself in the PCB editor, or let the plugin do it:

```sh
tools/run_headless.py board.kicad_pcb            # reports and stops (exit 2)
tools/run_headless.py board.kicad_pcb --auto-tag # fills the gaps
```

Auto-tagging **only fills gaps**. It assigns the next free number to each untagged
item and never renames anything that already has one, so hand-assigned identifiers
survive untouched. Net numbering follows whatever convention the board already uses:
if your nets are called `1`, `2`, `3`, the next one is `4`, not `N$1`.

Duplicated references are reported but not fixed — which of two `R1`s should be
renamed is not something to guess at.

### Net names are preserved

Every net gets a **global** label carrying its name. This is not decoration: a wire
carries no name, so without a label KiCad renames the net `Net-(D1-A)`, and the next
*Update PCB from Schematic* pushes that back over the name you chose. Global rather
than local labels because KiCad prefixes a root-sheet local label with the sheet path
— a net called `1` would come back as `/1`.

### What it decides on its own

Everyday parts — resistors, capacitors, LEDs, diodes, crystals, fuses, switches, pin
headers, terminal blocks — are matched without asking, using the symbol's declared
footprint filters, its default footprint, and a curated hint table. Pin/pad
compatibility is a hard gate rather than a hint: a symbol whose pins cannot be
reconciled with the footprint's pads is never offered, however well it scores
otherwise.

Anything ambiguous goes to a picker showing the ranked candidates, why each was
suggested, and the pad-versus-pin comparison. A match that would need pads mapped to
pins positionally (`A`/`K` against `1`/`2`) is never auto-accepted — that is a guess
about polarity, and you should see it.

## Limitations

- **Placement is a grid, not a schematic layout.** Symbols keep their rough relative
  positions from the board, which makes the sheet navigable, but no attempt is made to
  produce a conventionally-drawn schematic (power at top, signal flow left to right).
- **Nets that cannot be routed become labels.** The result stays electrically correct;
  it just looks worse for that net. The run report lists them.
- **Genuinely unconnected pads stay unconnected.** No no-connect flags are inserted,
  so ERC will report them — that is usually a real layout problem worth seeing.
- **Duplicated references are not auto-fixed** — only reported.
- Placement of *new* symbols on an existing sheet is a grid slot, not a considered
  position; existing symbols never move.

## Development

Everything under `plugins/core/` is headless and must never import `wx`; the GUI layer
is confined to `plugins/action.py` and `plugins/ui/`. That is what lets the whole
pipeline be driven and tested without clicking through KiCad.

Tests run under KiCad's bundled interpreter, since `pcbnew` imports nowhere else, and
use a small built-in runner because that interpreter has no pytest:

```sh
/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/3.9/bin/python3 \
    tools/run_tests.py            # everything
    tools/run_tests.py netlist    # or filter by name
```

The load-bearing test is the **netlist round-trip** in `tests/test_convert.py`: a
generated schematic is fed back through KiCad's own netlist exporter and compared net
by net against the netlist recovered from the board. Wrong symbol, wrong pin mapping,
a pin snapped off-grid, a wire that doesn't quite touch, a missing junction — they all
surface there and essentially nowhere else.

`tests/fixtures.py` builds synthetic boards with known netlists. They exist because
the KiCad template projects, the obvious corpus, turn out to contain **zero tracks** —
they are unrouted shield outlines, so they exercise only the declared-net path and
cannot validate geometric recovery at all.

### Layout

| Path | |
|---|---|
| `plugins/core/sexpr.py` | lossless S-expression parse/serialise |
| `plugins/core/netlist.py` | netlist recovery from copper geometry |
| `plugins/core/symlib.py` | cached index of ~23k library symbols |
| `plugins/core/matcher.py` | footprint → symbol ranking |
| `plugins/core/symbody.py` | full symbol definitions, pin geometry |
| `plugins/core/place.py` | sheet placement |
| `plugins/core/route.py` | orthogonal wire router |
| `plugins/core/schematic.py` | `.kicad_sch` construction |
| `plugins/core/preflight.py` | the tagging gate and auto-increment |
| `plugins/core/annotate.py` | references, symbol links and nets into the PCB |
| `plugins/core/sync.py` | incremental update against a project on disk |
| `plugins/core/state.py` | ownership sidecar, so user edits survive re-runs |
| `plugins/core/convert.py` | the pipeline |

## Licence

MIT.
