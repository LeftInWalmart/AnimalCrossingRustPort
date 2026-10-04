"""Project the sheets into a Rust workspace.

sheets/instructions.csv (p-code per instruction) -> rust/crates/recomp/part_NN/src/lib.rs
sheets/memory.csv                                 -> rust/crates/ac-recomp/mem/*.bin
sheets/functions.csv + meta.csv + hle.csv         -> rust/crates/ac-recomp/src/table.rs + HLE hooks

Every function becomes `pub fn f_<ADDR>(c: &mut Ctx, entry: u32)`: a loop over a `match` with one
arm per basic block (dense indices -> jump table). Straight-line code inside a block runs without
dispatch; direct branches set the block index; computed jumps and mid-function entries resolve
through a per-function leader table. Block leaders: function entries, every constant branch/call
target, code addresses referenced from data sections (switch jump tables), and the instruction
after any unconditional transfer. Branches back to an earlier address (loops) poll for pending
interrupts, so loops waiting on a flag set by an interrupt handler terminate. Generated code is disposable: edit the sheets (or this
generator), then rerun.
"""
import csv
import os
import io
import re
import shutil
import sys
from collections import defaultdict
from pathlib import Path

csv.field_size_limit(1 << 30)
ROOT = Path(__file__).resolve().parent.parent
SHEETS, RUST = ROOT / "sheets", ROOT / "rust"
CRATES = RUST / "crates"
PARTS = 48
# foresta.rel is linked here (tools/rel_link.py); the game loads it itself through LoadLink, so its
# sections are not part of the boot image.
REL_BASE = 0x81800000

VN = re.compile(r"([CRUMX])([0-9a-f]+):(\d+)")
BIN = {
    "INT_ADD", "INT_SUB", "INT_MULT", "INT_DIV", "INT_SDIV", "INT_REM", "INT_SREM", "INT_AND", "INT_OR",
    "INT_XOR", "INT_LEFT", "INT_RIGHT", "INT_SRIGHT", "INT_EQUAL", "INT_NOTEQUAL", "INT_LESS",
    "INT_LESSEQUAL", "INT_SLESS", "INT_SLESSEQUAL", "INT_CARRY", "INT_SCARRY", "INT_SBORROW",
    "BOOL_AND", "BOOL_OR", "BOOL_XOR", "FLOAT_ADD", "FLOAT_SUB", "FLOAT_MULT", "FLOAT_DIV",
    "FLOAT_EQUAL", "FLOAT_NOTEQUAL", "FLOAT_LESS", "FLOAT_LESSEQUAL",
}
UN = {
    "INT_NEGATE", "INT_2COMP", "INT_ZEXT", "BOOL_NEGATE", "FLOAT_NEG", "FLOAT_ABS", "FLOAT_SQRT",
    "FLOAT_NAN", "FLOAT_CEIL", "FLOAT_FLOOR", "FLOAT_ROUND", "POPCOUNT", "LZCOUNT",
}
CONV = {"FLOAT2FLOAT", "INT2FLOAT", "TRUNC"}
# mftb: the time base registers read the host clock.
TIMEBASE = {(0x1430, 4): "c.tb_lo()", (0x1434, 4): "c.tb_hi()"}
# mtspr DMAL: starts a locked-cache DMA.
DMAL = (0x1E6C, 4)
NOP_INTRINSICS = {
    "sync", "instructionSynchronize", "dataCacheBlockStore", "instructionCacheBlockInvalidate",
    "dataCacheBlockInvalidate", "dataCacheBlockFlush", "dataCacheBlockTouch", "eieio",
}


def read(name):
    with open(SHEETS / name, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def parse_vn(tok):
    if tok == "_":
        return None
    k, off, size = VN.fullmatch(tok).groups()
    return k, int(off, 16), int(size)


def parse_op(text):
    head, rest = text.split(" = ", 1) if " = " in text else (text, "")
    parts = head.split(" ")
    mnem = parts[0]
    name = None
    if mnem.startswith("CALLOTHER["):
        name = mnem[10:-1]
        mnem = "CALLOTHER"
    out = parse_vn(parts[1]) if len(parts) > 1 else None
    ins = [parse_vn(t) for t in rest.split()] if rest else []
    return mnem, name, out, ins


def write_if_changed(path, data):
    """Only touch files whose content changed, so cargo rebuilds just what changed."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    if path.exists() and path.read_bytes() == data:
        return
    path.write_bytes(data)


class Out(io.StringIO):
    """`with Out(path) as fh:` collects text and writes it with write_if_changed on exit."""

    def __init__(self, path):
        super().__init__()
        self.path = path

    def __exit__(self, *exc):
        if exc[0] is None:
            write_if_changed(self.path, self.getvalue())
        return super().__exit__(*exc)


def mask(v, s):
    return v & ((1 << (8 * s)) - 1)


def ends_block(pcode):
    """True if control never falls through past this instruction unconditionally (branch,
    computed jump or return; conditional returns count too, which only adds a block split)."""
    for text in pcode.split(" | "):
        m = text.split(" ", 1)[0]
        if m in ("BRANCH", "BRANCHIND", "RETURN"):
            return True
    return False


# setjmp/longjmp: longjmp (an HLE in gc-runtime) restores the registers saved by setjmp and then
# unwinds the host stack with this payload; a function that calls setjmp catches it and resumes
# at the instruction after its setjmp call (made a block leader) with r3 = longjmp's value.
LONGJMP_MAGIC = 0x4C4A4D50  # "LJMP"
SETJMP = set()  # entry addresses of setjmp (functions.csv name "setjmp")
ALL_ENTRIES = set()  # every generated function entry (all parts)


def calls_setjmp(text):
    """True if this p-code `CALL` goes to setjmp."""
    if not text.startswith("CALL "):
        return False
    ins = parse_op(text)[3]
    return bool(ins and ins[0] and ins[0][0] == "M" and ins[0][1] in SETJMP)


# FPSCR status/exception bits (Ghidra registers fp_fx..fp_ni, 1 byte each). Ghidra's FP p-code
# recomputes them after every arithmetic op (~30 ops per fadds), but in this game nothing reads
# them except mffs/mtfsf in setjmp/longjmp (no record-form FP ops, no mcrfs): the updates are dead.
FPSCR_LO, FPSCR_HI = 0x500, 0x520
FPSCR_INSNS = {"mffs", "mtfsf", "mtfsfi", "mtfsb0", "mtfsb1", "mcrfs"}
SIDE_EFFECTS = {"STORE", "BRANCH", "CBRANCH", "BRANCHIND", "CALL", "CALLIND", "CALLOTHER", "RETURN"}


def drop_dead_fpscr(ops, mnem):
    """Remove p-code ops of one instruction that only feed FPSCR bits nothing reads afterwards
    (FPSCR writes not read later in the same instruction, and the temporaries computing them)."""
    if mnem in FPSCR_INSNS:
        return ops
    parsed = [parse_op(o) for o in ops]
    is_fpscr = lambda v: v is not None and v[0] == "R" and FPSCR_LO <= v[1] < FPSCR_HI
    if not any(is_fpscr(p[2]) for p in parsed):
        return ops
    # Ops are indexed for intra-instruction relative branches: leave those instructions alone.
    if any(m in ("BRANCH", "CBRANCH") and ins and ins[0] and ins[0][0] == "C" for m, _, _, ins in parsed):
        return ops
    live_u = []        # (offset, size) of live unique temporaries
    live_f = set()     # live FPSCR bit offsets
    keep = []
    for text, (m, _, out, ins) in reversed(list(zip(ops, parsed))):
        needed = out is None or m in SIDE_EFFECTS
        if not needed:
            if out[0] == "U":
                needed = any(out[1] < o + z and o < out[1] + out[2] for o, z in live_u)
            elif is_fpscr(out):
                needed = out[1] in live_f
            else:
                needed = True
        if not needed:
            continue
        if out is not None:
            if out[0] == "U":
                live_u = [(o, z) for o, z in live_u if not (out[1] <= o and o + z <= out[1] + out[2])]
            elif is_fpscr(out):
                live_f.discard(out[1])
        for v in ins:
            if v is None:
                continue
            if v[0] == "U":
                live_u.append((v[1], v[2]))
            elif is_fpscr(v):
                live_f.add(v[1])
        keep.append(text)
    keep.reverse()
    return keep


def find_leaders(instructions, entries):
    """Global set of block leaders: function entries, constant branch/call targets, and code
    addresses stored in data sections (switch jump tables, callbacks)."""
    code = {int(r["addr"], 16) for r in instructions}
    leaders = set(entries)
    for r in instructions:
        for text in r["pcode"].split(" | "):
            if calls_setjmp(text):
                # longjmp resumes here (see LONGJMP_MAGIC).
                leaders.add(int(r["addr"], 16) + len(r["bytes"]) // 2)
            if not text.startswith(("BRANCH ", "CBRANCH ", "CALL ")):
                continue
            ins = parse_op(text)[3]
            if ins and ins[0] and ins[0][0] == "M":
                leaders.add(ins[0][1])
    data_refs = 0
    for r in read("memory.csv"):
        if "text" in r["section"]:
            continue
        base, raw = int(r["addr"], 16), bytes.fromhex(r["hex"])
        for i in range(0, len(raw) - 3, 4):
            w = int.from_bytes(raw[i:i + 4], "big")
            if w in code and w not in leaders:
                leaders.add(w)
                data_refs += 1
    print(f"block leaders: {len(leaders)} ({data_refs} from data references)", flush=True)
    return leaders


class Gen:
    def __init__(self, entries, func_of_addr, leaders):
        self.entries = entries          # all function entry addrs (this part)
        self.global_entries = func_of_addr
        self.leaders = leaders          # global block-leader addresses
        self.blk = {}                   # current function: leader address -> block index
        self.cur = 0                    # address of the instruction being translated
        self.extern = set()             # functions of other parts this part calls directly

    def rd(self, v):
        k, off, s = v
        if k == "C":
            return f"0x{mask(off, s):x}"
        if k == "R":
            if (off, s) in TIMEBASE:
                return TIMEBASE[(off, s)]
            return f"c.r(0x{off:x},{s})"
        if k == "U":
            return f"u{off:x}"
        return f"c.ld(0x{off:x},{s})"

    def wr(self, v, expr):
        k, off, s = v
        if k != "U" and ("c.ld(" in expr or "c.intrinsic(" in expr):
            return f"{{let v={expr};{self.wr(v, 'v')}}}"
        if k == "R":
            if (off, s) == DMAL:
                return f"{{let v={expr};c.wr(0x{off:x},{s},v);c.lc_dma();}}"
            return f"c.wr(0x{off:x},{s},{expr});"
        if k == "U":
            return f"u{off:x}={expr};"
        if k == "M":
            return f"c.st(0x{off:x},{s},{expr});"
        raise ValueError(v)

    def branch(self, target, idx, own, relative):
        k, off, s = target
        if k == "C":  # relative to this instruction's p-code ops
            rel = off - (1 << 32) if off >= 1 << 31 else off
            relative[0] = True
            return f"i={idx + rel};continue;"
        if off in own:
            # Loop back-edge: poll for interrupts like the CPU would (see Ctx::poll).
            poll = "c.poll();" if off <= self.cur else ""
            return f"{poll}b={self.blk[off]};continue 'f;"
        d = self.direct(off)
        return f"{d};return;" if d else f"c.call(0x{off:x});return;"

    def direct(self, off):
        """Rust call expression for a direct call to the function entry `off`, or None."""
        if off in self.entries:
            return f"f_{off:08X}(c,0x{off:x})"
        if off in ALL_ENTRIES:
            # Another part: linked by symbol name (#[no_mangle]), skipping the dispatcher.
            self.extern.add(off)
            return f"unsafe{{f_{off:08X}(c,0x{off:x})}}"
        return None

    def call(self, off):
        d = self.direct(off)
        return f"{d};" if d else f"c.call(0x{off:x});"

    def op(self, text, idx, own, asm, relative):
        mnem, name, out, ins = parse_op(text)
        if mnem == "COPY":
            return self.wr(out, self.rd(ins[0]))
        if mnem == "LOAD":
            return self.wr(out, f"c.ld({self.rd(ins[0])},{out[2]})")
        if mnem == "STORE":
            a, v = self.rd(ins[0]), self.rd(ins[1])
            if "c.ld(" in a + v:
                return f"{{let a={a};let v={v};c.st(a,{ins[1][2]},v);}}"
            return f"c.st({a},{ins[1][2]},{v});"
        if mnem in BIN:
            fn = mnem.lower()
            if mnem == "INT_MULT" and out[2] == 8 and asm.startswith("psq_l"):
                fn = "float_mult"  # GameCubeLoader spec multiplies the dequant scale with INT_MULT
            return self.wr(out, f"{fn}({self.rd(ins[0])},{self.rd(ins[1])},{ins[0][2]})")
        if mnem in UN:
            return self.wr(out, f"{mnem.lower()}({self.rd(ins[0])},{ins[0][2]})")
        if mnem == "INT_SEXT":
            return self.wr(out, f"mask(int_sext({self.rd(ins[0])},{ins[0][2]}),{out[2]})")
        if mnem in CONV:
            return self.wr(out, f"{mnem.lower()}({self.rd(ins[0])},{ins[0][2]},{out[2]})")
        if mnem == "SUBPIECE":
            return self.wr(out, f"subpiece({self.rd(ins[0])},{ins[1][1]},{out[2]})")
        if mnem == "PIECE":
            return self.wr(out, f"piece({self.rd(ins[0])},{self.rd(ins[1])},{ins[1][2]})")
        if mnem == "BRANCH":
            return self.branch(ins[0], idx, own, relative)
        if mnem == "CBRANCH":
            return f"if {self.rd(ins[1])}!=0{{{self.branch(ins[0], idx, own, relative)}}}"
        if mnem == "BRANCHIND":
            return (f"{{let t={self.rd(ins[0])} as u32;let k=blk(t);"
                    f"if k!=0{{b=k;continue 'f;}}c.call(t as u64);return;}}")
        if mnem == "CALL":
            k, off, _ = ins[0]
            return self.call(off) if k == "M" else f"c.call({self.rd(ins[0])});"
        if mnem == "CALLIND":
            return f"c.call({self.rd(ins[0])});"
        if mnem == "RETURN":
            return "return;"
        if mnem == "CALLOTHER":
            if name == "ldexpf":
                return self.wr(out, f"ldexp_pow2({self.rd(ins[0])},{ins[0][2]},{out[2]})")
            if name == "countLeadingZeros":
                return self.wr(out, f"lzcount({self.rd(ins[0])},{ins[0][2]})")
            if name in NOP_INTRINSICS:
                return ""
            args = ",".join(self.rd(v) for v in ins)
            call = f'c.intrinsic("{name}",&[{args}])'
            return self.wr(out, call) if out else call + ";"
        raise ValueError(f"unhandled p-code op {mnem}")

    def instruction(self, ins, nxt, own):
        self.cur = int(ins["addr"], 16)
        mnem = ins["asm"].split(" ", 1)[0].rstrip(".")
        if mnem.startswith(("ps_", "psq_")) or mnem == "dcbz_l":
            # Paired singles: the loader's p-code has aliasing bugs; use the native version.
            return f"c.ps(0x{ins['bytes']});"
        ops = drop_dead_fpscr([p for p in ins["pcode"].split(" | ") if p], mnem)
        uniq = sorted({int(m.group(2), 16) for m in VN.finditer(" | ".join(ops)) if m.group(1) == "U"})
        relative = [False]
        body = [self.op(o, i, own, ins["asm"], relative) for i, o in enumerate(ops)]
        decl = "".join(f"let mut u{u:x}=0u64;" for u in uniq)
        if relative[0]:
            arms = "".join(f"{i}=>{{{b}i={i + 1};}}" for i, b in enumerate(body))
            return f"{{{decl}let mut i:i64=0;loop{{match i{{{arms}_=>break}}}}}}"
        return "{" + decl + "".join(body) + "}"

    def function(self, entry, name, rows):
        own = {int(r["addr"], 16) for r in rows}
        # Block leaders inside this function.
        lead, prev_nxt, after_jump = [], None, False
        for r in rows:
            a = int(r["addr"], 16)
            if a == entry or a in self.leaders or after_jump or prev_nxt != a:
                lead.append(a)
            after_jump = ends_block(r["pcode"])
            prev_nxt = a + len(r["bytes"]) // 2
        self.blk = {a: i + 1 for i, a in enumerate(lead)}
        lead_set = set(lead)
        table = ",".join(f"0x{a:x}=>{i + 1}" for i, a in enumerate(lead))
        lines = [f"/// {name} ({len(rows)} instructions, {len(lead)} blocks)",
                 f"#[unsafe(no_mangle)]\npub fn f_{entry:08X}(c:&mut Ctx,entry:u32){{if c.enter(0x{entry:x},entry){{return;}}",
                 f"fn blk(t:u32)->usize{{match t{{{table},_=>0}}}}",
                 (f"let mut b:usize=if entry==0x{entry:x}{{{self.blk[entry]}}}else{{blk(entry)}};"
                  if entry in self.blk else "let mut b:usize=blk(entry);"),
                 f"if b==0{{c.block_miss(0x{entry:x},entry);return;}}",
                 "'f:loop{match b{"]
        jmp = any(calls_setjmp(t) for r in rows for t in r["pcode"].split(" | "))
        if jmp:
            # The block loop runs inside catch_unwind; a longjmp to a setjmp of this function
            # resumes at its return address (see LONGJMP_MAGIC).
            lines[-1] = ("let depth0=c.depth;loop{let r=std::panic::catch_unwind("
                         "std::panic::AssertUnwindSafe(||{'f:loop{match b{")
        for k, r in enumerate(rows):
            a = int(r["addr"], 16)
            nxt = a + len(r["bytes"]) // 2
            if a in lead_set:
                lines.append(f"{self.blk[a]}=>{{")
            lines.append(f"// {r['asm']}\n{self.instruction(r, nxt, own)}")
            last = k + 1 == len(rows) or int(rows[k + 1]["addr"], 16) in lead_set
            if last:
                # Fall through into the next block (or out of the function: tail-call it).
                if nxt in self.blk:
                    lines.append(f"b={self.blk[nxt]};continue 'f;}}")
                else:
                    lines.append(f"c.call(0x{nxt:x});return;}}")
        if jmp:
            lines.append("_=>return}}}));match r{Ok(())=>return,Err(p)=>{"
                         f"if let Some(&[0x{LONGJMP_MAGIC:x},pc])=p.downcast_ref::<[u32;2]>(){{"
                         "let k=blk(pc);if k!=0{c.depth=depth0;b=k;continue;}}"
                         "std::panic::resume_unwind(p)}}}}")
        else:
            lines.append("_=>return}}}")
        return "\n".join(lines)


# Optimisation level for the generated parts in dev builds (see the workspace profile comment).
PART_OPT = int(os.environ.get("AC_PART_OPT", "3"))


def main():
    funcs = {int(r["addr"], 16): r for r in read("functions.csv")}
    meta = {r["key"]: r["value"] for r in read("meta.csv")}
    hle = [(int(r["addr"], 16), r["name"]) for r in read("hle.csv") if r["addr"]]
    by_func = defaultdict(list)
    orphan_entry = None
    prev_end = None
    print("reading instructions.csv ...", flush=True)
    # The REL header and the .ctors/.dtors pointer tables share foresta.rel's executable block
    # (rel_link.py); CreateGapFunctions disassembles a few words there. They are data: skip them.
    lay = {r["section"]: int(r["addr"], 16) for r in read("rel_layout.csv")}
    not_code = [(lay["header"], lay[".text"]), (lay[".ctors"], lay[".rodata"])]
    instructions = [r for r in read("instructions.csv")
                    if not any(lo <= int(r["addr"], 16) < hi for lo, hi in not_code)]
    for r in instructions:
        a = int(r["addr"], 16)
        if r["func"]:
            f = int(r["func"], 16)
            orphan_entry = None
        else:
            # Code outside any Ghidra function (exception vectors, etc.): group contiguous runs.
            if orphan_entry is None or a != prev_end:
                orphan_entry = a
            f = orphan_entry
        prev_end = a + len(r["bytes"]) // 2
        by_func[f].append(r)

    entries = sorted(by_func)
    SETJMP.update(a for a, r in funcs.items() if r["name"] == "setjmp")
    ALL_ENTRIES.update(by_func)
    leaders = find_leaders(instructions, entries)
    del instructions
    total = sum(len(v) for v in by_func.values())
    per_part = total / PARTS
    parts, cur, cnt = [], [], 0
    for e in entries:
        cur.append(e)
        cnt += len(by_func[e])
        if cnt >= per_part * (len(parts) + 1) and len(parts) < PARTS - 1:
            parts.append(cur)
            cur = []
    parts.append(cur)

    recomp = CRATES / "recomp"
    keep = {f"part_{n:02}" for n in range(len(parts))}
    if recomp.exists():
        for stale in recomp.iterdir():
            if stale.name not in keep:
                shutil.rmtree(stale)
    members = []
    table = []
    for n, part in enumerate(parts):
        crate = f"ac-part-{n:02}"
        d = recomp / f"part_{n:02}"
        (d / "src").mkdir(parents=True, exist_ok=True)
        members.append(f"crates/recomp/part_{n:02}")
        write_if_changed(d / "Cargo.toml",
            f'[package]\nname = "{crate}"\nversion = "0.1.0"\nedition = "2024"\n\n'
            f'[dependencies]\ngc-core = {{ path = "../../gc-core" }}\n')
        g = Gen(set(part), funcs, leaders)
        with Out(d / "src" / "lib.rs") as fh:
            fh.write("//! GENERATED by tools/gen_rust.py from sheets/instructions.csv. Do not edit.\n"
                     "#![allow(unused, unreachable_code, non_snake_case, clippy::all)]\n"
                     "use gc_core::{Ctx, ops::*};\n\n")
            fh.write("pub static FUNCS: &[(u32, u32, fn(&mut Ctx, u32))] = &[\n")
            for e in part:
                rows = by_func[e]
                end = max(int(r["addr"], 16) + len(r["bytes"]) // 2 for r in rows)
                fh.write(f"    (0x{e:x}, 0x{end:x}, f_{e:08X}),\n")
                name = funcs[e]["name"] if e in funcs else f"orphan_{e:08X}"
                table.append((e, name))
            fh.write("];\n\n")
            for e in part:
                name = funcs[e]["name"] if e in funcs else f"orphan_{e:08X} (code outside any function)"
                fh.write(g.function(e, name, by_func[e]) + "\n\n")
            # Functions of other parts called directly (resolved by the linker).
            fh.write('unsafe extern "Rust" {\n')
            for e in sorted(g.extern):
                fh.write(f"    fn f_{e:08X}(c: &mut Ctx, entry: u32);\n")
            fh.write("}\n")
        print(f"{crate}: {len(part)} functions", flush=True)

    # Memory image from memory.csv.
    top = CRATES / "ac-recomp"
    mem = top / "mem"
    mem.mkdir(parents=True, exist_ok=True)
    blobs = defaultdict(bytearray)
    starts = {}
    for r in read("memory.csv"):
        sec = r["section"]
        starts.setdefault(sec, int(r["addr"], 16))
        blobs[sec] += bytes.fromhex(r["hex"])
    for sec in [s for s in starts if starts[s] >= REL_BASE]:
        del starts[sec], blobs[sec]
    for sec, data in blobs.items():
        write_if_changed(mem / f"{sec}.bin", bytes(data))

    regs = read("registers.csv")
    reg_space = max(int(r["offset"], 16) + int(r["size"]) for r in regs)
    deps = "\n".join(f'ac-part-{n:02} = {{ path = "../recomp/part_{n:02}" }}' for n in range(len(parts)))
    (top / "src").mkdir(parents=True, exist_ok=True)
    write_if_changed(top / "Cargo.toml",
        '[package]\nname = "ac-recomp"\nversion = "0.1.0"\nedition = "2024"\n\n'
        f'[dependencies]\ngc-core = {{ path = "../gc-core" }}\ngc-runtime = {{ path = "../gc-runtime" }}\n{deps}\n')
    with Out(top / "src" / "table.rs") as fh:
        fh.write("//! GENERATED by tools/gen_rust.py. Do not edit.\nuse gc_core::Ctx;\n\n")
        fh.write(f"pub const ENTRY_POINT: u32 = {meta['entry_point']};\n")
        fh.write(f"pub const REG_SPACE: usize = 0x{reg_space:x};\n")
        fh.write(f"pub const FUNCTION_COUNT: usize = {len(table)};\n")
        lay = {r["section"]: r for r in read("rel_layout.csv")}
        fh.write("/// foresta.rel as linked by tools/rel_link.py (checked against gc_runtime::hle at boot).\n"
                 f"pub const REL_BASE: u32 = {lay['header']['addr']};\n"
                 f"pub const REL_IMAGE_SIZE: u32 = {lay['header']['size']};\n"
                 f"pub const REL_BSS: u32 = {lay['.bss']['addr']};\n\n")
        fh.write("pub static SECTIONS: &[(u32, &[u8])] = &[\n")
        for sec, start in starts.items():
            fh.write(f'    (0x{start:08x}, include_bytes!("../mem/{sec}.bin")),\n')
        fh.write("];\n\npub fn parts() -> [&'static [(u32, u32, fn(&mut Ctx, u32))]; %d] {\n    [\n" % len(parts))
        for n in range(len(parts)):
            fh.write(f"        ac_part_{n:02}::FUNCS,\n")
        fh.write("    ]\n}\n\n/// Functions handled by gc_runtime::hle (from sheets/hle.csv).\n")
        fh.write("pub static HLE: &[(u32, &str)] = &[\n")
        for e, name in hle:
            fh.write(f'    (0x{e:08x}, "{name}"),\n')
        fh.write("];\n\npub static NAMES: &[(u32, &str)] = &[\n")
        for e, name in sorted(table):
            fh.write(f'    (0x{e:08x}, "{name}"),\n')
        fh.write("];\n")

    write_if_changed(RUST / "Cargo.toml",
        '[workspace]\nresolver = "3"\nmembers = [\n    "crates/gc-core",\n    "crates/gc-runtime",\n    "crates/ac-recomp",\n'
        '    "crates/ac-bevy",\n' + "".join(f'    "{m}",\n' for m in members) + "]\n\n"
        "# The game is played from this profile (play.bat). Debug assertions and integer overflow\n"
        "# checks would sit on every guest instruction and throughout Bevy, so they are off. No debug\n"
        "# info: it was ~3.5 GB of .rlib/.pdb in target/ (panics still print their message, just\n"
        "# without file:line backtraces).\n"
        "[profile.dev]\nopt-level = 1\ndebug = false\ndebug-assertions = false\noverflow-checks = false\n\n"
        "# Bevy and the runtime are small and hot: optimize them even in dev builds.\n"
        '[profile.dev.package."*"]\nopt-level = 3\n\n'
        "# The runtime (GX decoder, scheduler, DSP), dispatch and the app are workspace members,\n"
        "# which the wildcard above does not cover: optimize them fully (does not touch the parts).\n"
        '[profile.dev.package.gc-runtime]\nopt-level = 3\n\n'
        '[profile.dev.package.ac-recomp]\nopt-level = 3\n\n'
        '[profile.dev.package.ac-bevy]\nopt-level = 3\n\n'
        "# gc-core is the per-instruction hot path; inline and optimize it fully.\n"
        '[profile.dev.package.gc-core]\nopt-level = 3\n\n'
        "# The generated parts are huge. Full optimization with few codegen units (incremental\n"
        "# compilation would split each part into hundreds of units and block inlining between\n"
        "# guest functions) is what lets the game run at full speed; AC_PART_OPT=1 builds much\n"
        "# faster but runs slower.\n"
        + "".join(f'[profile.dev.package.ac-part-{n:02}]\nopt-level = {PART_OPT}\ndebug = false\ncodegen-units = 16\nincremental = false\n\n' for n in range(len(parts)))
        + "[profile.release]\ncodegen-units = 16\n")
    print(f"{len(table)} functions in {len(parts)} parts, {total} instructions")


if __name__ == "__main__":
    main()
