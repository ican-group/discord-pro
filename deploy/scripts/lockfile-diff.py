#!/usr/bin/env python3
"""Compare two Yarn v1 lockfiles. Prints added/removed entries and any entry whose
resolved version changed. Exit 1 only if an EXISTING entry would resolve to a
different version. Removed entries (ranges nothing depends on any more, pruned
by Yarn) are reported but do not change any installed version.

    python3 deploy/scripts/lockfile-diff.py old/yarn.lock new/yarn.lock
"""
import re
import sys


def parse(path):
    entries, current = {}, None
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            if not line.startswith(" ") and line.endswith(":"):
                current = [k.strip().strip('"') for k in line[:-1].split(", ")]
                for key in current:
                    entries[key] = None
                continue
            m = re.match(r'^  version "?([^"]+)"?$', line)
            if m and current:
                for key in current:
                    entries[key] = m.group(1)
    return entries


def main(old_path, new_path):
    old, new = parse(old_path), parse(new_path)
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed = sorted(k for k in set(old) & set(new) if old[k] != new[k])
    print(f"entries: old={len(old)} new={len(new)}")
    print(f"added ({len(added)}):")
    for k in added:
        print(f"  + {k} -> {new[k]}")
    print(f"removed ({len(removed)}):")
    for k in removed:
        print(f"  - {k} ({old[k]})")
    print(f"existing entries with a different resolved version ({len(changed)}):")
    for k in changed:
        print(f"  ~ {k}: {old[k]} -> {new[k]}")
    ok = not changed
    print("RESULT:", "EXISTING VERSIONS PRESERVED" + (" (unused entries pruned)" if removed else "")
          if ok else "EXISTING ENTRIES CHANGED VERSION - review")
    return 0 if ok else 1


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    sys.exit(main(sys.argv[1], sys.argv[2]))
