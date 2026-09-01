"""Paths to the checked-in test boards."""

import os

# Checked-in boards. Tests must not read the developer's live KiCad project: it
# changes under them, and assertions about "3 footprints" quietly become wrong when
# it grows to ninety.
DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
SIMPLE_BOARD = os.path.join(DATA, "simple.kicad_pcb")   # 3 parts, fully tagged
DEMO_BOARD = os.path.join(DATA, "demo.kicad_pcb")       # ~90 parts, multi-unit ICs
