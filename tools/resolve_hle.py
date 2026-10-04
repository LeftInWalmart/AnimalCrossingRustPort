"""Fill sheets/hle.csv addresses from sheets/functions.csv by function name.

hle.csv lists SDK functions the Rust runtime replaces (gc-runtime/src/hle.rs dispatches on the
name). The addresses are per-game, so after naming (import_sdk_names.py) this resolves each name
to its address here. Rows whose name is not found are kept with an empty address (gen_rust.py
skips them) and reported.
"""
import csv
from pathlib import Path

SHEETS = Path(__file__).resolve().parent.parent / "sheets"


def main():
    with open(SHEETS / "functions.csv", newline="", encoding="utf-8") as fh:
        by_name = {}
        for r in csv.DictReader(fh):
            by_name.setdefault(r["name"], []).append(r["addr"])
    with open(SHEETS / "hle.csv", newline="", encoding="utf-8") as fh:
        rd = csv.DictReader(fh)
        cols, rows = rd.fieldnames, list(rd)
    missing = []
    for r in rows:
        addrs = by_name.get(r["name"], [])
        if len(addrs) == 1:
            r["addr"] = "0x" + addrs[0][2:].upper()
        else:
            r["addr"] = ""
            missing.append(f"{r['name']} ({len(addrs)} matches)")
    with open(SHEETS / "hle.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, cols)
        w.writeheader()
        w.writerows(rows)
    print(f"{len(rows) - len(missing)}/{len(rows)} HLE functions resolved")
    for m in missing:
        print("  unresolved:", m)


if __name__ == "__main__":
    main()
