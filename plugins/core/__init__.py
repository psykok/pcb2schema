"""Headless core of pcb2schema.

Nothing in this package may import ``wx``. Everything here must be runnable
outside the pcbnew GUI so the pipeline can be driven and tested from a plain
interpreter (see ``tools/run_headless.py``). User interaction is injected as a
callback by the caller rather than raised from inside the core.
"""
