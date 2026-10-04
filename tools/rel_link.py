"""Link foresta.rel at a fixed address and fold it into one DOL with main.dol.

Animal Crossing keeps almost all game code in foresta.rel.szs, which boot.c's LoadLink reads
into a heap buffer and OSLink relocates in place. Static recompilation needs fixed code
addresses, so the port links the module at REL_BASE (0x81800000, just above the 24 MiB of
MEM1, in the runtime's extended RAM) and its BSS right after the image. The runtime's OSLink
hook does the same at run time (copies the loaded image there and links it with the game's
own OSLink), so the addresses here and in the running game agree.

Outputs (in <workdir>/extracted):
  forest.dol      main.dol + relocated REL text (text slot 2), sections 2..5 (data slot 15)
                  and the zeroed REL BSS (data slot 16) -> imported into Ghidra
  rel_layout.csv  section -> address map of the linked module
  symbols.csv     every symbol from static.map and foresta.map with its final address

Usage: python rel_link.py <workdir>
"""
import csv
import re
import struct
import sys
from pathlib import Path

from yaz0 import decompress

REL_BASE = 0x81800000
SECTION_NAMES = {1: ".text", 2: ".ctors", 3: ".dtors", 4: ".rodata", 5: ".data", 6: ".bss"}


def align(v, a):
    return (v + a - 1) & ~(a - 1)


def parse_rel(img):
    h = struct.unpack_from(">12I4B6I", img, 0)
    keys = ("id next prev num_sections section_info name_off name_size version bss_size "
            "rel_off imp_off imp_size prolog_sec epilog_sec unresolved_sec bss_sec prolog "
            "epilog unresolved align bss_align fix_size").split()
    return dict(zip(keys, h))


def link(img, base, bss_addr):
    """Relocate a REL image the way OSLink (SDK v2) does; returns (image, section addresses)."""
    img = bytearray(img)
    hdr = parse_rel(img)
    secs = {}
    for i in range(hdr["num_sections"]):
        off, size = struct.unpack_from(">II", img, hdr["section_info"] + 8 * i)
        if off & ~1:
            secs[i] = (base + (off & ~1), size, off & 1)
            struct.pack_into(">I", img, hdr["section_info"] + 8 * i, base + off)
        elif size:  # the BSS section
            secs[i] = (bss_addr, size, 0)
            struct.pack_into(">I", img, hdr["section_info"] + 8 * i, bss_addr)
    for k in ("prolog", "epilog", "unresolved"):
        sec = hdr[k + "_sec"]
        if sec:
            struct.pack_into(">I", img, {"prolog": 0x34, "epilog": 0x38, "unresolved": 0x3C}[k],
                             secs[sec][0] + hdr[k])
    for i in range(hdr["imp_size"] // 8):
        mod, roff = struct.unpack_from(">II", img, hdr["imp_off"] + 8 * i)
        struct.pack_into(">I", img, hdr["imp_off"] + 8 * i + 4, base + roff)
        p, site = roff, 0
        while True:
            off, typ, sec, addend = struct.unpack_from(">HBBI", img, p)
            p += 8
            site += off
            if typ == 203:  # R_DOLPHIN_END
                break
            if typ == 202:  # R_DOLPHIN_SECTION
                site = secs[sec][0] - base
                continue
            if typ in (0, 201):
                continue
            s = addend if mod == 0 else secs[sec][0] + addend
            a = base + site
            o = site
            if typ == 1:  # ADDR32
                struct.pack_into(">I", img, o, s & 0xFFFFFFFF)
            elif typ == 2:  # ADDR24
                w = struct.unpack_from(">I", img, o)[0]
                struct.pack_into(">I", img, o, (w & ~0x03FFFFFC) | (s & 0x03FFFFFC))
            elif typ == 3 or typ == 4:  # ADDR16 / ADDR16_LO
                struct.pack_into(">H", img, o, s & 0xFFFF)
            elif typ == 5:  # ADDR16_HI
                struct.pack_into(">H", img, o, (s >> 16) & 0xFFFF)
            elif typ == 6:  # ADDR16_HA
                struct.pack_into(">H", img, o, ((s >> 16) + (1 if s & 0x8000 else 0)) & 0xFFFF)
            elif typ in (7, 8, 9):  # ADDR14
                w = struct.unpack_from(">I", img, o)[0]
                struct.pack_into(">I", img, o, (w & ~0xFFFC) | (s & 0xFFFC))
            elif typ == 10:  # REL24
                w = struct.unpack_from(">I", img, o)[0]
                struct.pack_into(">I", img, o, (w & ~0x03FFFFFC) | ((s - a) & 0x03FFFFFC))
            elif typ in (11, 12, 13):  # REL14
                w = struct.unpack_from(">I", img, o)[0]
                struct.pack_into(">I", img, o, (w & ~0xFFFC) | ((s - a) & 0xFFFC))
            else:
                raise ValueError(f"relocation type {typ}")
    return bytes(img), secs, hdr


MAP_LINE = re.compile(r"^\s+([0-9a-f]{8}) ([0-9a-f]{6}) ([0-9a-f]{8})\s+(\d+) (\S.*?)\s*\t(.*)$")


def parse_map(path):
    """CodeWarrior map -> [(section, start, size, vaddr, name, unit)]"""
    rows, sec = [], None
    for line in open(path, encoding="latin-1"):
        m = re.match(r"^(\S+) section layout", line)
        if m:
            sec = m.group(1)
            continue
        m = MAP_LINE.match(line.rstrip("\r\n"))
        if not m or sec is None:
            continue
        start, size, vaddr, al, name, unit = m.groups()
        if name == sec:  # per-unit section header ("1 .text  boot.o")
            continue
        name = name.split(" (entry of")[0].strip()
        rows.append((sec, int(start, 16), int(size, 16), int(vaddr, 16), name, unit.strip()))
    return rows


def build_dol(dol, extra_text, extra_data):
    offs = list(struct.unpack_from(">18I", dol, 0))
    addrs = list(struct.unpack_from(">18I", dol, 0x48))
    sizes = list(struct.unpack_from(">18I", dol, 0x90))
    out = bytearray(dol)
    out[0x100:] = dol[0x100:]
    pos = align(len(out), 32)
    for slots, items in ((range(0, 7), extra_text), (range(7, 18), extra_data)):
        free = [i for i in slots if sizes[i] == 0]
        for (addr, data), slot in zip(items, free):
            out += b"\0" * (pos - len(out))
            offs[slot], addrs[slot], sizes[slot] = pos, addr, align(len(data), 32)
            out += data + b"\0" * (sizes[slot] - len(data))
            pos = len(out)
    struct.pack_into(">18I", out, 0, *offs)
    struct.pack_into(">18I", out, 0x48, *addrs)
    struct.pack_into(">18I", out, 0x90, *sizes)
    return bytes(out)


def main(work):
    ex = Path(work) / "extracted"
    raw = (ex / "files" / "foresta.rel.szs").read_bytes()
    rel = decompress(raw)
    (ex / "foresta.rel").write_bytes(rel)
    bss_addr = align(REL_BASE + len(rel), 32)
    img, secs, hdr = link(rel, REL_BASE, bss_addr)
    (ex / "foresta_linked.bin").write_bytes(img)

    # GameCubeLoader rejects sections whose 32-byte-rounded ends overlap, and .text ends
    # mid-line where .ctors starts. So the text slot holds header + .text + .ctors + .dtors
    # (up to the aligned .rodata) and the data slot .rodata + .data.
    data_lo = secs[4][0] - REL_BASE
    assert data_lo % 32 == 0
    data_hi = max(s[0] + s[1] for i, s in secs.items() if i in (4, 5)) - REL_BASE
    dol = (ex / "main.dol").read_bytes()
    forest = build_dol(
        dol,
        [(REL_BASE, img[:data_lo])],
        [(REL_BASE + data_lo, img[data_lo:data_hi]), (bss_addr, b"\0" * hdr["bss_size"])],
    )
    (ex / "forest.dol").write_bytes(forest)
    # GameCubeLoader asks "Load symbols?" in a modal dialog unless a .map sits next to the DOL.
    (ex / "forest.map").write_text("")

    with open(ex / "rel_layout.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["section", "index", "addr", "size", "exec"])
        w.writerow(["header", "", f"0x{REL_BASE:08X}", len(rel), 0])
        for i, (a, s, e) in sorted(secs.items()):
            w.writerow([SECTION_NAMES.get(i, f"sec{i}"), i, f"0x{a:08X}", s, e])
        for k in ("prolog", "epilog", "unresolved"):
            w.writerow([k, hdr[k + "_sec"], f"0x{secs[hdr[k + '_sec']][0] + hdr[k]:08X}", 0, 1])

    name_to_idx = {v: k for k, v in SECTION_NAMES.items()}
    with open(ex / "symbols.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["addr", "name", "size", "section", "unit", "module"])
        n = 0
        for sec, start, size, vaddr, name, unit in parse_map(ex / "files" / "static.map"):
            w.writerow([f"0x{vaddr:08X}", name, size, sec, unit, "static"])
            n += 1
        for sec, start, size, vaddr, name, unit in parse_map(ex / "files" / "foresta.map"):
            idx = name_to_idx.get(sec)
            if idx not in secs:
                continue
            w.writerow([f"0x{secs[idx][0] + start:08X}", name, size, sec, unit, "foresta"])
            n += 1
    print(f"linked foresta.rel ({len(rel)} bytes) at {REL_BASE:#x}, bss {bss_addr:#x}+{hdr['bss_size']:#x}; "
          f"forest.dol {len(forest)} bytes; {n} symbols")


if __name__ == "__main__":
    main(sys.argv[1])
