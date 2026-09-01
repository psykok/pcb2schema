#!/usr/bin/env python3
"""Package the plugin as a KiCad PCM archive.

Produces ``dist/pcb2schema-<version>.zip`` containing only what the Plugin and Content
Manager expects at the archive root -- ``metadata.json``, ``plugins/``, ``resources/``
-- and fills in the ``download_sha256`` / ``download_size`` / ``install_size`` fields
that the official repository requires.

    python3 tools/build_pcm.py

The archive can be installed locally via PCM's "Install from File...".
"""

import argparse
import hashlib
import json
import os
import shutil
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INCLUDE = ("plugins", "resources")
EXCLUDE_DIRS = {"__pycache__", ".git", ".pytest_cache"}
EXCLUDE_SUFFIX = (".pyc", ".pyo", ".orig", ".rej", ".bak")


def _files():
    for top in INCLUDE:
        base = os.path.join(ROOT, top)
        if not os.path.isdir(base):
            continue
        for folder, dirs, names in os.walk(base):
            dirs[:] = sorted(d for d in dirs if d not in EXCLUDE_DIRS)
            for name in sorted(names):
                if name.endswith(EXCLUDE_SUFFIX) or name.startswith("."):
                    continue
                full = os.path.join(folder, name)
                yield full, os.path.relpath(full, ROOT)


def build(outdir):
    meta_path = os.path.join(ROOT, "metadata.json")
    with open(meta_path, "r", encoding="utf-8") as fh:
        meta = json.load(fh)
    version = meta["versions"][0]["version"]

    os.makedirs(outdir, exist_ok=True)
    archive = os.path.join(outdir, "pcb2schema-%s.zip" % version)
    if os.path.exists(archive):
        os.remove(archive)

    install_size = 0
    payload = list(_files())
    if not payload:
        sys.exit("nothing to package: plugins/ and resources/ are empty")

    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        # metadata.json must sit at the archive root.
        zf.write(meta_path, "metadata.json")
        install_size += os.path.getsize(meta_path)
        for full, rel in payload:
            zf.write(full, rel)
            install_size += os.path.getsize(full)

    data = open(archive, "rb").read()
    meta["versions"][0].update({
        "download_sha256": hashlib.sha256(data).hexdigest(),
        "download_size": len(data),
        "install_size": install_size,
    })
    with open(os.path.join(outdir, "metadata.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=4)
        fh.write("\n")

    print("%s  (%d files, %.1f kB packed, %.1f kB installed)"
          % (archive, len(payload) + 1, len(data) / 1024.0, install_size / 1024.0))
    print("sha256 %s" % meta["versions"][0]["download_sha256"])
    return archive


PACKAGE_DIR = "pcb2schema"  # must be a valid Python identifier: KiCad imports it by name


def install(archive, version_dir=None):
    """Unpack into KiCad's 3rdparty plugin folder, bypassing PCM.

    The layout matters and is easy to get wrong. KiCad's loader walks
    ``<KICAD_3RD_PARTY>/plugins/``, and for each *directory* it imports the directory
    itself as a Python module -- but only if that directory contains ``__init__.py``
    at its top level. Extracting the archive wholesale puts our ``__init__.py`` one
    level down, so KiCad silently skips the whole plugin and no button ever appears.

    So the archive's ``plugins/`` *contents* go into ``plugins/<PACKAGE_DIR>/``, and
    the directory name has to be a legal module name -- no dots, which rules out
    using the reverse-DNS identifier here.
    """
    base = os.path.expanduser("~/Documents/KiCad")
    if version_dir is None:
        # Sort numerically, not lexically: "10.0" must beat "7.0".
        def as_version(name):
            try:
                return [int(p) for p in name.split(".")]
            except ValueError:
                return [-1]

        versions = [d for d in os.listdir(base)
                    if os.path.isdir(os.path.join(base, d)) and d[0].isdigit()]
        if not versions:
            sys.exit("no KiCad version directory under %s" % base)
        version_dir = max(versions, key=as_version)
    third_party = os.path.join(base, version_dir, "3rdparty")
    plugin_dir = os.path.join(third_party, "plugins", PACKAGE_DIR)
    resource_dir = os.path.join(third_party, "resources", PACKAGE_DIR)

    for path in (plugin_dir, resource_dir):
        shutil.rmtree(path, ignore_errors=True)
        os.makedirs(path, exist_ok=True)

    with zipfile.ZipFile(archive) as zf:
        for member in zf.namelist():
            if member.endswith("/"):
                continue
            parts = member.split("/")
            if parts[0] == "plugins":
                dest = os.path.join(plugin_dir, *parts[1:])
            elif parts[0] == "resources":
                dest = os.path.join(resource_dir, *parts[1:])
            else:
                continue  # metadata.json is PCM bookkeeping, not runtime content
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with zf.open(member) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)

    marker = os.path.join(plugin_dir, "__init__.py")
    if not os.path.isfile(marker):
        sys.exit("install produced no %s -- KiCad would skip the plugin" % marker)
    print("installed to %s" % plugin_dir)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-o", "--outdir", default=os.path.join(ROOT, "dist"))
    ap.add_argument("--install", action="store_true",
                    help="also unpack into the local KiCad plugin folder")
    args = ap.parse_args()
    archive = build(args.outdir)
    if args.install:
        install(archive)


if __name__ == "__main__":
    main()
