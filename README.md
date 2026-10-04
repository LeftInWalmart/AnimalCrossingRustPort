# AnimalCrossingRustPort: Animal Crossing (GAFE01), Spreadsheet Method

The CSVs in `sheets/` are the **single source of truth** for the whole game: `main.dol` *and*
`foresta.rel`, the relocatable module that holds the game itself. Everything else is a projection:
`src/` (Ghidra C, for reading) and `rust/` (compilable Rust, for running) are generated from the
sheets and never edited by hand.

```
C:\...\Animal Crossing (USA).ciso
   │ tools/ciso_extract.py, tools/extract_files.py
   ▼
../AnimalCrossing/extracted/  main.dol, files/foresta.rel.szs, files/static.map, files/foresta.map
   │ tools/rel_link.py  (Yaz0 → foresta.rel → linked at 0x81800000 → forest.dol + symbols.csv)
   ▼
forest.dol ──ghidra-cli import──► ../AnimalCrossing/ghidra/AC.gpr
   │ tools/ApplySymbols.java → analyze → FixNoReturn.java, CreateGapFunctions.java,
   │ FixBodies.java, ExportPcode.java, ExportSheets.java  (via `ghidra script run`)
   ▼
../AnimalCrossing/export/ ──tools/build_sheets.py──► sheets/*.csv ──► src/units/*.c (readable C)
                                     tools/resolve_hle.py (hle.csv addresses)
                                                         └─tools/gen_rust.py──► rust/ (Bevy app)
```

## What this repository contains

Only the tools, the hand-written runtime (`gc-core`, `gc-runtime`, `ac-recomp/src/lib.rs`,
`ac-bevy`) and hand-curated metadata (`sheets/hle.csv`, `meta.csv`, `rel_layout.csv`). Everything
generated from the game contains its code and data, so it is **not** included and is regenerated
from your own disc image: the other sheets, `src/`, the 48 recompiled crates, the boot memory image,
`table.rs` and the workspace manifests. You need Python 3, Ghidra 12.1 with the GameCubeLoader
extension, ghidra-cli 0.2 and a Rust toolchain. The work folder
(`..\AnimalCrossing`, next to this repository) holds the extracted disc, the Ghidra project and
the raw export.

Rebuild everything from the disc (from `tools/`):

```bat
python ciso_extract.py "%USERPROFILE%\Downloads\Emus\GAMECUBE\Animal Crossing (USA).ciso" ..\..\AnimalCrossing\extracted
python extract_files.py "%USERPROFILE%\Downloads\Emus\GAMECUBE\Animal Crossing (USA).ciso" ..\..\AnimalCrossing\extracted
python rel_link.py ..\..\AnimalCrossing
set WORK=%CD%\..\..\AnimalCrossing
set G=ghidra --projects-dir %WORK%/ghidra
%G% import %WORK%/extracted/forest.dol --project AC --program forest.dol --no-analyze
%G% script run ApplySymbols.java --project AC --program forest.dol -- %WORK%/extracted/symbols.csv
%G% analyze --project AC --program forest.dol
%G% script run FixNoReturn.java --project AC --program forest.dol
%G% script run CreateGapFunctions.java --project AC --program forest.dol
%G% script run FixBodies.java --project AC --program forest.dol -- %WORK%/extracted/symbols.csv
%G% script run ExportPcode.java --project AC --program forest.dol -- %WORK%/export
%G% script run ExportSheets.java --project AC --program forest.dol -- %WORK%/export
python build_sheets.py && python resolve_hle.py && python gen_rust.py
```

`ghidra import` finishes in about a minute, but the CLI doesn't return (the bridge stays up):
check `ghidra jobs --project AC`. `ExportSheets.java` decompiles all 20,329 functions and takes
longer than the CLI's reply timeout; set `GHIDRA_CLI_READ_TIMEOUT=0`.

## foresta.rel

Almost all of Animal Crossing is in `foresta.rel.szs`: 2.9 MB of code, 11 MB of data (models,
textures, tables) and 2.3 MB of BSS. On the console, boot.c's `LoadLink` reads it into a heap
buffer (decompressing the Yaz0), puts its BSS in the sound arena or behind the image, and links it
in place with `OSLink`. Static recompilation needs code at a fixed address, so:

- `tools/rel_link.py` links the module the way `OSLink` does, at **`REL_BASE` = 0x81800000** (just
  above the 24 MiB of MEM1) with its BSS at 0x826EA600, and folds it into `forest.dol`. In that DOL,
  text slot 2 holds the header, `.text`, `.ctors` and `.dtors`, data slot 15 holds `.rodata` +
  `.data`, and data slot 16 holds the zeroed BSS. GameCubeLoader rejects 32-byte-rounded
  overlaps, and `.text` ends mid-line where `.ctors` begins.
- The runtime's guest RAM is 48 MiB. The game is still told it has 24 MiB, which is all its OS
  arena and heaps ever use. The `OSLink` hook (`gc-runtime/src/hle.rs`) lets `LoadLink` run
  unchanged, so the heap is used exactly as on the console. It then copies the loaded image to
  `REL_BASE` and runs the game's own `OSLink` there. It also mirrors the linked header back into
  the game's buffer, because `LoadLink` calls the prolog and `UnLink` the epilog through it.
  `OSUnlink` is translated the same way.
- `LoadLink` registers the module's buffer as static texture data with the N64 graphics layer
  (`emu64_texture_cache_data_entry_set`). That range is moved to `REL_BASE`, so module textures
  stay in emu64's persistent cache.
- `ac-recomp` checks at boot that the generated layout (`table.rs`, from
  `sheets/rel_layout.csv`) matches the runtime's.

## Sheets

| Sheet | Rows | Key | Contents |
|---|---|---|---|
| `meta.csv` | 13 | `key` | Game ID, entry point, BSS, DOL/REL hashes, REL base, toolchain |
| `sections.csv` | 27 | `name` | Memory blocks (DOL sections, linked REL sections, MMIO) |
| `functions.csv` | 20,329 | `addr` | name, size, **module** (static / foresta), section, **unit, status, owner**, fan-in/out, signature, C path, notes |
| `units.csv` | 867 | `unit` | Work partitions = the game's real translation units from the linker maps (`m_player_lib`, `ac_npc`, `jaudio_NES/dspproc.c`, `os/OSLink.c`, ...), with status roll-ups |
| `symbols.csv` | 70,013 | `addr` | Every symbol of `static.map` + `foresta.map` (functions *and* data) at its final address, with size, section and object file |
| `rel_layout.csv` | 10 | `section` | Where each foresta.rel section is linked |
| `hle.csv` | 92 | `name` | Functions replaced by high-level emulation in the Rust runtime |
| `calls.csv` | 44,151 | `caller,callee` | Static call graph |
| `instructions.csv` | 904,457 | `addr` | Every instruction: owning function, bytes, disassembly, **raw p-code semantics** |
| `memory.csv` | 275,454 | `addr` | Every initialized byte of main.dol and the linked REL, 64 bytes/row hex |
| `registers.csv` | 2,220 | `name` | Gekko register file layout (offsets used by p-code `R` varnodes) |
| `strings.csv` | 2,437 | `addr` | Defined strings + referencing functions |
| `globals.csv` | 55,990 | `addr` | Data labels, types, sizes, xref counts |
| `disc_files.csv` | 10 | `path` | Every file on the disc with offset and size |

`instructions.csv`, `memory.csv`, `registers.csv`, `rel_layout.csv` and `meta.csv` fully describe
the program, and they are what `gen_rust.py` consumes. P-code varnodes are written
`<space><hexoffset>:<size>`, where the space is `R` register, `U` temporary, `C` constant or `M` RAM.

### Names
The disc ships the original CodeWarrior linker maps (`static.map`, `foresta.map`). Every function
and global has its real name and object file. 20,285 of the 20,329 functions are `named`. The 44
`raw` ones are stray code Ghidra found outside any map symbol.

Auto-analysis truncated about 5,000 functions after calls it took for non-returning
(`OSDVDFatalError` cut `LoadLink` in half, for example). `FixBodies.java` therefore sets every
function body to exactly its map range. Without that, a loop crossing such a cut would recurse in
the recompiled code.

### Status lifecycle (`functions.csv:status`)
`raw` (auto name `FUN_…`) → `named` → `typed` → `clean` → `verified`.
To see what is left, read the per-status columns in `units.csv`.

## Rust / Bevy (`rust/`)

| Crate | What |
|---|---|
| `gc-core` | Stable ABI for generated code: CPU context, p-code ops, paired singles, inline RAM access (48 MiB backing: MEM1 + the REL region), and the `Host` function table |
| `gc-runtime` | The console behind `Host`: MMIO devices, scheduler, HLE, GX decoder, jaudio DSP, disc, pads, memory card, diagnostics |
| `recomp/part_00..47` | **Generated.** One `pub fn f_<ADDR>(c, entry)` per function, from `instructions.csv`, with one match arm per basic block. Cross-part calls are direct (`#[no_mangle]` + `extern "Rust"`). Loop back-edges poll for interrupts |
| `ac-recomp` | Dispatch table, boot image (`mem/*.bin`, main.dol sections only: the game loads foresta.rel itself), boot (apploader-equivalent low memory, FST, BI2), `preflight` binary |
| `ac-bevy` | Bevy 0.19 app: runs the game, draws its GX output on an emulated EFB, plays its audio and feeds keyboard/gamepad input into `PADRead` |

Run with `play.bat` (or `cd rust && cargo run -p ac-bevy`). The disc image is read from `AC_DISC`,
which defaults to `%USERPROFILE%\Downloads\Emus\GAMECUBE\Animal Crossing (USA).ciso`. Both `.iso`
and `.ciso` work. `preflight.bat` boots the game headlessly and writes a report to
`rust/preflight_report.txt`.

The first build compiles about 130 MB of generated Rust in 48 crates at opt-level 3. That takes
much longer than later builds, which only rebuild what changed. `AC_PART_OPT=1 python tools\gen_rust.py`
builds faster but the game then runs slower.

### Runtime specifics for Animal Crossing
- **Real-time clock.** The town runs on `OSGetTime` (`lbRTC_GetHardTime`). As on the console
  (whose IPL loads it from the RTC), the CPU time base starts at the local date and time, counted
  in seconds since 2000-01-01 at 40.5 MHz. `AC_RTC_OFFSET_SECS` shifts it, like changing the
  console clock.
- **Audio.** `jaudio_NES` mixes most sound on the CPU (`cpubuf.c`): the N64 "Nas" engine through
  its RSP simulator (`rspsim.c`), and the NES APU for the Famicom games (`emusound.c`). That is all
  recompiled code. JAudio's own DSP voices use a light-protocol Zelda-family ucode, which is HLE'd
  at the driver level (`dsp.rs`: `DspBoot`, `DSPSendCommands`, `DSPWaitFinish`,
  `DSPReleaseHalt`). `0x81` sets up the 0x180-byte channel blocks (`CH_BUF`), `0x82` queues a frame
  of 80-sample sub-frames, and each `DSPReleaseHalt` renders one sub-frame and raises the DSP
  interrupt. The game's `__DspSync` then drives the next one through the audio thread, as on
  hardware.
- **Memory card.** Slot A is emulated at the CARD API level (59 blocks, like the Memory Card 59
  bundled with the game; the town save needs 57). Files keep their permission bits (the game sets
  no-copy through `__CARDGetStatusEx`/`__CARDSetStatusEx`). The card is saved to
  `rust/memcard/slotA.bin` (override with `AC_MEMCARD`).
- **Memory budget.** The game gets all of MEM1 below the FST/BI2, as on the console. The stack for
  interrupt handlers run while every guest thread sleeps lives in the extended RAM.
- **Threads, alarms, DVD, PAD, interrupts, MTX** are HLE'd through `sheets/hle.csv` as in the
  earlier ports. The DVD drive has real timing (seek + ~3 MB/s), and callbacks arrive as
  interrupts. The libultra layer (`osCreateThread2`, `osRecvMesg`, ...) wraps the Dolphin OS calls
  and runs recompiled.

### Controls, settings, rendering
- Keyboard, mouse and gamepads all work at once. F2 rebinds controls (`%APPDATA%\AnimalCrossingRustPort\controls.json`).
- P opens the settings: volume, window mode, gamma, VSync, internal resolution 1x-6x and the FPS counter.
- F1 shows the debug overlay. F11 or Alt+Enter toggles borderless fullscreen.
- Rendering replays every decoded GX frame on an emulated EFB with TEV shaders.
- The NES emulator (`ksNesDraw*`, for the collectible Famicom games) draws each scanline as a
  GX line, with indirect texturing from the nametable into the CHR tiles, and sets texcoord scales
  manually. The renderer covers all three:
  - **Lines/points** are expanded to screen-space quads of `linewidth/6` pixels (BP 0x22),
    widened along the minor axis as Dolphin does.
  - **Setup-unit texcoord scale**: vertex texture coordinates are in texels (normalised ×
    BP 0x30+2i/0x31+2i) and are sampled at texel / map size. The SDK sets the scale to the texture
    size, so ordinary draws are unchanged.
  - **Indirect texturing** in `gx_efb.wgsl` follows Dolphin's integer model: s.7 coordinates,
    RAS1_SS scale, format masks and bias, static and S/T-dynamic matrices with their scale, wrap,
    add-previous, and s24 wrap.

### Diagnostics
- `ac_trace.log` (written every 5 s): hottest functions, thread states, audio and frame pacing,
  the OSLink / DSP setup lines, and OSReport output.
- When an interval produces no frame, the log gets a `stack of thread …` line: the running guest
  function and its callers.
- When the frame rate drops (under ~56 FPS over 5 s), the performance probe starts by itself and
  appends a host profile every 5 s to `ac_probe.txt`.
- If the game hangs, the log names the stuck function. Fix it by adding the SDK function to
  `sheets/hle.csv` with a handler in `gc-runtime/src/hle.rs`, then rerun `resolve_hle.py` and
  `gen_rust.py`. That rewrites only changed files, so the generated crates are not rebuilt.

### Known gaps
- No rumble. The GBA link (island and e-Reader features), the DSP's GBA/card-unlock tasks, and
  JAudio DSP reverb/filters are not emulated.
