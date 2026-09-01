#!/usr/bin/env python3
"""Run the test suite under KiCad's bundled interpreter.

pytest is not available inside the KiCad Python framework and we don't want a
dependency the plugin's own runtime lacks, so this is a deliberately small
discover-and-call runner: every ``test_*`` function in every ``tests/test_*.py``.

Usage (the interpreter matters -- ``pcbnew`` only imports under KiCad's own):

    /Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/\\
Versions/3.9/bin/python3 tools/run_tests.py [name-filter ...]
"""

import importlib
import os
import sys
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "plugins"))  # -> core.*
sys.path.insert(0, os.path.join(ROOT, "tests"))  # -> fixtures, test_*

# KiCad's SWIG layer chatters on stderr about wxApp and duplicate image handlers
# before anything of ours runs. Silence it so failures are actually visible.
if os.environ.get("P2S_QUIET", "1") == "1":
    sys.stderr.flush()
    os.dup2(os.open(os.devnull, os.O_WRONLY), 2)


def main(argv):
    filters = argv[1:]
    modules = sorted(
        f[:-3]
        for f in os.listdir(os.path.join(ROOT, "tests"))
        if f.startswith("test_") and f.endswith(".py")
    )

    passed, failed = 0, []
    started = time.time()

    for mod_name in modules:
        module = importlib.import_module(mod_name)
        for attr in sorted(dir(module)):
            if not attr.startswith("test_"):
                continue
            label = "%s.%s" % (mod_name, attr)
            if filters and not any(f in label for f in filters):
                continue
            t0 = time.time()
            try:
                getattr(module, attr)()
            except Exception:
                failed.append((label, traceback.format_exc()))
                print("FAIL  %s" % label)
            else:
                passed += 1
                print("ok    %-52s %5.2fs" % (label, time.time() - t0))

    print("\n%d passed, %d failed in %.1fs" % (passed, len(failed), time.time() - started))
    for label, tb in failed:
        print("\n" + "=" * 70 + "\nFAIL %s\n%s" % (label, tb))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
