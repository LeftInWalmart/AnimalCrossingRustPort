"""Build the Spreadsheet-Method tables from the raw Ghidra export.

Inputs  (../AnimalCrossing/export/):    functions_raw.csv calls.csv strings.csv globals.csv sections.csv
                                        instructions.csv memory.csv registers.csv raw_c/*.c
        (../AnimalCrossing/extracted/): symbols.csv rel_layout.csv disc_files.csv (rel_link.py, ciso_extract.py)
Outputs (sheets/):  meta.csv functions.csv units.csv symbols.csv rel_layout.csv calls.csv strings.csv
                    globals.csv sections.csv instructions.csv memory.csv registers.csv disc_files.csv
        (src/units/, src/raw_c/): one C file per unit, projected from the sheet (regenerable)

Animal Crossing ships its linker maps (static.map, foresta.map), so every function has its real
name and translation unit. Functions Ghidra found that the maps don't list (padding stubs,
compiler-generated code) inherit the unit of the function before them: CodeWarrior keeps each
translation unit contiguous. Re-running preserves hand-edited columns (name, unit, status, owner,
notes) in sheets/functions.csv, so the sheet stays the source of truth across re-exports.
"""
import csv
import hashlib
import os
import re
import shutil
import struct
from collections import Counter, defaultdict
from pathlib import Path

csv.field_size_limit(1 << 30)
OUT = Path(__file__).resolve().parent.parent                       # AnimalCrossingRustPort/
WORK = Path(os.environ.get("AC_WORK", OUT.parent / "AnimalCrossing"))  # Ghidra project, export/, extracted/
EXP, EX, SHEETS, SRC = WORK / "export", WORK / "extracted", OUT / "sheets", OUT / "src" / "units"
STATUSES = ["raw", "named", "typed", "clean", "verified"]


def read(p):
    with open(p, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def write(p, rows, cols):
    with open(p, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def unit_name(obj):
    """Map object column ("jaudio_NES.o dspproc.c", "boot.o", "os.a OSLink.c") -> unit id."""
    parts = obj.split()
    if len(parts) == 2:
        lib, src = parts
        lib = re.sub(r"\.(a|o)$", "", lib)
        return f"{lib}/{src}"
    return re.sub(r"\.(o|obj)$", "", obj) if parts else ""


def main():
    SHEETS.mkdir(parents=True, exist_ok=True)
    funcs = read(EXP / "functions_raw.csv")
    syms = read(EX / "symbols.csv")
    prev = {r["addr"]: r for r in read(SHEETS / "functions.csv")} if (SHEETS / "functions.csv").exists() else {}

    # Map symbols by address (text only; a few addresses carry several names: keep the first).
    map_fn = {}
    for s in syms:
        if s["section"] in (".text", ".init"):
            map_fn.setdefault(int(s["addr"], 16), s)

    rel_base = int(next(r for r in read(EX / "rel_layout.csv") if r["section"] == "header")["addr"], 16)
    funcs.sort(key=lambda r: int(r["addr"], 16))
    rows, last_unit = [], ""
    for f in funcs:
        a = int(f["addr"], 16)
        old = prev.get(f["addr"], {})
        ms = map_fn.get(a)
        module = "foresta" if a >= rel_base else "static"
        if ms:
            unit = unit_name(ms["unit"])
            last_unit = unit
            name = ms["name"]
            status = "named"
            note = ""
        else:
            unit = last_unit or "unknown"
            name = f["name"]
            status = "raw" if name.startswith(("FUN_", "LAB_")) else "named"
            note = "not in the linker map (unit inherited from the preceding function)"
        rows.append({
            **f,
            "name": old.get("name") or name,
            "module": module,
            "unit": old.get("unit") or unit,
            "status": old.get("status") or status,
            "owner": old.get("owner", ""),
            "notes": old.get("notes") or note,
            "c_path": f"src/raw_c/{f['addr'][2:]}.c",
        })
    fcols = ["addr", "name", "size", "module", "section", "unit", "status", "owner", "calls_out", "called_by",
             "decomp_lines", "decomp_ok", "signature", "c_path", "notes"]
    write(SHEETS / "functions.csv", rows, fcols)

    by_unit = defaultdict(list)
    for r in rows:
        by_unit[r["unit"]].append(r)
    units = []
    for u, rs in by_unit.items():
        st = Counter(r["status"] for r in rs)
        units.append({
            "unit": u, "module": rs[0]["module"], "library": u.split("/")[0] if "/" in u else "",
            "first_addr": rs[0]["addr"], "last_addr": rs[-1]["addr"],
            "n_funcs": len(rs), "total_size": sum(int(r["size"]) for r in rs),
            **{s: st.get(s, 0) for s in STATUSES},
            "owner": "", "src_path": f"src/units/{u.replace('/', '__')}.c",
        })
    units.sort(key=lambda r: int(r["first_addr"], 16))
    write(SHEETS / "units.csv", units, ["unit", "module", "library", "first_addr", "last_addr", "n_funcs",
                                        "total_size", *STATUSES, "owner", "src_path"])

    for name in ["calls.csv", "strings.csv", "globals.csv", "sections.csv", "instructions.csv", "memory.csv",
                 "registers.csv"]:
        shutil.copy(EXP / name, SHEETS / name)
    for name in ["symbols.csv", "rel_layout.csv", "disc_files.csv"]:
        shutil.copy(EX / name, SHEETS / name)

    raw = OUT / "src" / "raw_c"
    if raw.exists():
        shutil.rmtree(raw)
    shutil.copytree(EXP / "raw_c", raw)

    # Projection: concatenate each unit's functions into one C file.
    if SRC.exists():
        shutil.rmtree(SRC)
    SRC.mkdir(parents=True)
    for u, rs in by_unit.items():
        with open(OUT / f"src/units/{u.replace('/', '__')}.c", "w", encoding="utf-8") as fh:
            fh.write(f"// unit {u} ({rs[0]['module']}): {len(rs)} functions {rs[0]['addr']}..{rs[-1]['addr']}\n"
                     f"// GENERATED from sheets/functions.csv - edit the sheet, not this file.\n\n")
            for r in rs:
                fh.write((OUT / r["c_path"]).read_text(encoding="utf-8"))
                fh.write("\n")

    dol = (EX / "main.dol").read_bytes()
    rel = (EX / "foresta.rel").read_bytes()
    bss, bss_size, entry = struct.unpack_from(">3I", dol, 0xD8)
    write(SHEETS / "meta.csv", [
        {"key": "game_id", "value": "GAFE01"},
        {"key": "title", "value": "Animal Crossing (USA)"},
        {"key": "engine", "value": "Nintendo EAD 'GCFOREST' (N64 Doubutsu no Mori port: libultra/libforest emulation layer), "
                                   "JSystem (JKernel/JUtility/JFramework/J2D), jaudio_NES, Famicom emulator, Dolphin SDK, Metrowerks runtime"},
        {"key": "entry_point", "value": f"0x{entry:08X}"},
        {"key": "bss", "value": f"0x{bss:08X}+0x{bss_size:X}"},
        {"key": "dol_sha1", "value": hashlib.sha1(dol).hexdigest()},
        {"key": "dol_size", "value": str(len(dol))},
        {"key": "rel", "value": "foresta.rel.szs (Yaz0) -> foresta.rel, module id 1"},
        {"key": "rel_base", "value": f"0x{rel_base:08X}"},
        {"key": "rel_sha1", "value": hashlib.sha1(rel).hexdigest()},
        {"key": "rel_size", "value": str(len(rel))},
        {"key": "symbols", "value": "static.map + foresta.map from the disc (original CodeWarrior linker maps)"},
        {"key": "toolchain", "value": "Ghidra 12.1.4 + GameCubeLoader via ghidra-cli 0.2.2"},
    ], ["key", "value"])

    tot = Counter(r["status"] for r in rows)
    mods = Counter(r["module"] for r in rows)
    print(f"{len(rows)} functions ({dict(mods)}), {len(units)} units, status {dict(tot)}")


if __name__ == "__main__":
    main()
