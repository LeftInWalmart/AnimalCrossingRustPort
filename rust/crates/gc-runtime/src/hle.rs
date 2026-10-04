//! Function-level high-level emulation.
//!
//! Every generated function asks `gc_core::hooked` on entry; functions listed in
//! `sheets/hle.csv` (installed at boot via `gc_core::install_hooks`) are routed to [`call`]. Handlers are matched by the
//! function's name from `sheets/functions.csv`, can run the original recompiled body with
//! [`Ctx::call_orig`], and set r3/r4 as the return value. Returning `false` falls through to
//! the original code. Adding an HLE function only needs this file + the sheet, not a rebuild of
//! the generated crates.

/// Probe counters for the watchdog's pacing line (audio streaming health).
pub static DVD_READS: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
pub static ARQ_REQUESTS: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
static PADREADS: std::sync::atomic::AtomicU32 = std::sync::atomic::AtomicU32::new(0);
use crate::{Ctx, CtxExt};

/// `AC_PAD_SCRIPT=600:0,4:1000,60:0,...`: scripted port-0 buttons for automated runs. Each
/// `reads:hexmask` segment holds the mask for that many PADRead calls (one per frame); after the
/// last segment the script releases everything. Segment changes are logged with the frame count.
fn pad_script(n: u32) -> Option<u16> {
    static SCRIPT: std::sync::OnceLock<Vec<(u32, u16)>> = std::sync::OnceLock::new();
    let segs = SCRIPT.get_or_init(|| {
        std::env::var("AC_PAD_SCRIPT")
            .unwrap_or_default()
            .split(',')
            .filter_map(|seg| {
                let (k, m) = seg.trim().split_once(':')?;
                Some((k.parse().ok()?, u16::from_str_radix(m, 16).ok()?))
            })
            .collect()
    });
    if segs.is_empty() {
        return None;
    }
    let mut start = 0;
    for (i, &(len, mask)) in segs.iter().enumerate() {
        if n < start + len {
            if n == start {
                crate::diag::log(format!("pad script: frame {n} segment {i} mask {mask:#06x}"));
            }
            return Some(mask);
        }
        start += len;
    }
    Some(0)
}

/// The CPU's HID2 (bit 32 set once some thread has published it).
static HID2: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);

/// Payload of the host unwind that implements longjmp (must match tools/gen_rust.py).
const LONGJMP_MAGIC: u32 = 0x4C4A_4D50;
const DVD_STATE_END: u64 = 0;
const DVD_STATE_BUSY: u64 = 1;
const DVD_STATE_CANCELED: u64 = 10;
const DVD_RESULT_CANCELED: u32 = 0xFFFF_FFFD; // -3
const DVD_COMMAND_READ: u64 = 1;

/// The hottest hooks (hundreds of thousands of calls per second in 3D scenes), resolved by
/// address once so they skip the name lookup and the string match below.
#[derive(Clone, Copy)]
enum Hot {
    Disable,
    Enable,
    Restore,
    Mtx(&'static str),
}

fn hot(addr: u32) -> Option<Hot> {
    static T: std::sync::OnceLock<Vec<(u32, Hot)>> = std::sync::OnceLock::new();
    let t = T.get_or_init(|| {
        let m = crate::machine();
        let mut v: Vec<(u32, Hot)> = [
            ("OSDisableInterrupts", Hot::Disable),
            ("OSEnableInterrupts", Hot::Enable),
            ("OSRestoreInterrupts", Hot::Restore),
            ("PSMTXCopy", Hot::Mtx("PSMTXCopy")),
            ("PSMTXConcat", Hot::Mtx("PSMTXConcat")),
            ("PSMTXMultVec", Hot::Mtx("PSMTXMultVec")),
            ("PSVECCrossProduct", Hot::Mtx("PSVECCrossProduct")),
            ("PSVECMag", Hot::Mtx("PSVECMag")),
            ("PSMTXInverse", Hot::Mtx("PSMTXInverse")),
        ]
        .iter()
        .filter_map(|(n, h)| (m.addr_of)(n).map(|a| (a, *h)))
        .collect();
        v.sort_by_key(|e| e.0);
        v
    });
    t.binary_search_by_key(&addr, |e| e.0).ok().map(|i| t[i].1)
}

pub fn call(c: &mut Ctx, addr: u32) -> bool {
    if let Some(h) = hot(addr) {
        let msr = c.r(gc_core::REG_MSR, 4);
        let was = ((msr & gc_core::MSR_EE) != 0) as u32;
        match h {
            Hot::Disable => {
                c.wr(gc_core::REG_MSR, 4, msr & !gc_core::MSR_EE);
                c.set_gpr(3, was);
            }
            // Setting MSR.EE with an interrupt pending takes it right away, as on the CPU.
            Hot::Enable => {
                c.wr(gc_core::REG_MSR, 4, msr | gc_core::MSR_EE);
                c.set_gpr(3, was);
                c.poll();
            }
            Hot::Restore => {
                let new = if c.gpr(3) != 0 { msr | gc_core::MSR_EE } else { msr & !gc_core::MSR_EE };
                c.wr(gc_core::REG_MSR, 4, new);
                c.set_gpr(3, was);
                c.poll();
            }
            Hot::Mtx(name) => {
                crate::psmtx::call(c, name);
            }
        }
        return true;
    }
    let m = c.m();
    let s = &m.sched;
    match m.name(addr) {
        // ---- threads ----
        "OSCreateThread" => {
            let t = c.gpr(3);
            c.call_orig(addr); // fills the OSThread/OSContext; returns TRUE in r3
            s.create(c, t);
        }
        "OSResumeThread" => {
            crate::diag::log(format!("OSResumeThread {:#x} from {:#x} (lr {:#x})", c.gpr(3), c.thread, c.r(0x1020, 4) as u32));
            let prev = s.resume(c, c.gpr(3));
            c.set_gpr(3, prev as u32);
        }
        "OSSuspendThread" => {
            crate::diag::log(format!("OSSuspendThread {:#x} from {:#x} (lr {:#x})", c.gpr(3), c.thread, c.r(0x1020, 4) as u32));
            let prev = s.suspend(c, c.gpr(3));
            c.set_gpr(3, prev as u32);
        }
        "OSSleepThread" => s.sleep(c, c.gpr(3)),
        "OSWakeupThread" => s.wakeup(c, c.gpr(3)),
        "OSYieldThread" => s.yield_now(c),
        "__OSReschedule" => s.reschedule(c),
        "OSExitThread" => {
            let t = c.thread;
            m.write_be(t + 0x2D8, 4, c.gpr(3) as u64);
            s.exit(c);
            park_forever();
        }
        "OSCancelThread" => {
            if c.gpr(3) == c.thread {
                s.exit(c);
                park_forever();
            }
            s.cancel(c, c.gpr(3));
        }
        "SelectThread" | "OSLoadContext" => {
            crate::diag::log(format!("{} reached (unexpected with HLE scheduler)", m.name(addr)));
        }

        // ---- alarms ----
        "OSSetAlarm" => {
            let (alarm, handler) = (c.gpr(3), c.gpr(7));
            let fire = crate::sched::timebase() + arg64(c, 5);
            m.write_be(alarm, 4, handler as u64);
            m.write_be(alarm + 0x08, 8, fire);
            m.write_be(alarm + 0x18, 8, 0);
            s.set_alarm(alarm, handler, fire, 0);
        }
        "OSSetPeriodicAlarm" => {
            let (alarm, handler) = (c.gpr(3), c.gpr(9));
            let (start, period) = (arg64(c, 5), arg64(c, 7).max(1));
            let now = crate::sched::timebase();
            let fire = if start > now { start } else { start + ((now - start) / period + 1) * period };
            m.write_be(alarm, 4, handler as u64);
            m.write_be(alarm + 0x08, 8, fire);
            m.write_be(alarm + 0x18, 8, period);
            m.write_be(alarm + 0x20, 8, start);
            s.set_alarm(alarm, handler, fire, period);
        }
        "OSCancelAlarm" => {
            s.cancel_alarm(c.gpr(3));
            m.write_be(c.gpr(3), 4, 0);
        }

        // ---- DVD (reads go straight to the disc image) ----
        "DVDInit" => {
            if let Some(a) = (m.addr_of)("__DVDFSInit") {
                c.call(a as u64);
            }
        }
        "DVDReadAbsAsyncPrio" => {
            let (block, dst, len, off, cb) = (c.gpr(3), c.gpr(4), c.gpr(5), c.gpr(6), c.gpr(7));
            DVD_READS.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
            dvd_read(c, off as u64, dst, len);
            for (o, v) in [(0x08, DVD_COMMAND_READ), (0x0C, DVD_STATE_BUSY), (0x10, off as u64),
                           (0x14, len as u64), (0x18, dst as u64), (0x1C, len as u64),
                           (0x20, len as u64), (0x28, cb as u64)] {
                m.write_be(block + o, 4, v);
            }
            // The data is in RAM now; the block stays BUSY for the drive's seek + transfer
            // time, then turns END and the callback runs (see sched::dvd_complete).
            dvd_done(block, cb, len, dvd_latency(off as u64, len));
            c.set_gpr(3, 1);
        }
        "DVDReadDiskID" => {
            let (block, dst, cb) = (c.gpr(3), c.gpr(4), c.gpr(5));
            dvd_read(c, 0, dst, 0x20);
            m.write_be(block + 0x0C, 4, DVD_STATE_BUSY);
            m.write_be(block + 0x28, 4, cb as u64);
            dvd_done(block, cb, 0x20, dvd_latency(0, 0x20));
            c.set_gpr(3, 1);
        }
        "DVDInquiryAsync" => {
            let (block, info, cb) = (c.gpr(3), c.gpr(4), c.gpr(5));
            for i in 0..8 {
                m.write_be(info + 4 * i, 4, 0);
            }
            m.write_be(block + 0x0C, 4, DVD_STATE_BUSY);
            m.write_be(block + 0x28, 4, cb as u64);
            dvd_done(block, cb, 0x20, dvd_latency(0, 0));
            c.set_gpr(3, 1);
        }
        "DVDCancelAsync" => {
            let (block, cb) = (c.gpr(3), c.gpr(4));
            if let Some(read_cb) = dvd_cancel(c, block) {
                // As after the DI interrupt of a canceled command: the command's callback gets
                // DVD_RESULT_CANCELED, then the cancel callback runs.
                let now = crate::sched::timebase();
                for (callback, result) in [(read_cb, DVD_RESULT_CANCELED), (cb, 0)] {
                    crate::sched::dvd_complete(crate::sched::DvdDone { callback, result, block, state: None, due: now });
                }
            } else if cb != 0 {
                // Nothing in flight: the SDK calls the cancel callback right away.
                c.call_guest(cb, &[0, block]);
            }
            c.set_gpr(3, 1);
        }
        "DVDCancel" => {
            // Synchronous: when it returns the command is canceled and its callback has run.
            let block = c.gpr(3);
            if let Some(read_cb) = dvd_cancel(c, block) {
                if read_cb != 0 {
                    c.call_guest(read_cb, &[DVD_RESULT_CANCELED, block]);
                }
            }
            c.set_gpr(3, 0);
        }
        "DVDClose" => {
            // A DVDFileInfo starts with its command block: a command still in flight on a file
            // being closed must not complete into the caller's freed buffers.
            if !crate::sched::dvd_withdraw(c.gpr(3)).is_empty() {
                m.write_be(c.gpr(3) + 0x0C, 4, DVD_STATE_CANCELED);
            }
            c.call_orig(addr);
        }
        "DVDGetDriveStatus" | "DVDReset" | "DVDResume" => c.set_gpr(3, 0),

        // ---- foresta.rel (see rel_link / link_foresta below) ----
        "OSLink" => {
            let (module, bss) = (c.gpr(3), c.gpr(4));
            if module != crate::REL_BASE && is_foresta(m, module) {
                link_foresta(c, addr, module, bss);
            } else {
                c.call_orig(addr);
            }
        }
        "OSUnlink" => {
            if c.gpr(3) == LINKED.load(std::sync::atomic::Ordering::Relaxed) && c.gpr(3) != 0 {
                c.set_gpr(3, crate::REL_BASE);
            }
            c.call_orig(addr);
        }
        "emu64_texture_cache_data_entry_set" => {
            // LoadLink registers the module buffer as static texture data just before OSLink
            // (emu64 keeps textures from those ranges in its persistent cache). The module's data
            // is used at REL_BASE, so that is the range to register.
            let (start, end) = (c.gpr(3), c.gpr(4));
            if start != crate::REL_BASE && is_foresta(m, start) {
                let moved = crate::REL_BASE.wrapping_sub(start);
                c.set_gpr(3, crate::REL_BASE);
                c.set_gpr(4, end.wrapping_add(moved));
                crate::diag::log(format!(
                    "emu64 texture data range {start:#010x}..{end:#010x} -> {:#010x}..{:#010x} (foresta.rel)",
                    c.gpr(3), c.gpr(4)
                ));
            }
            c.call_orig(addr);
        }

        // ---- jaudio DSP driver (dsp.rs) ----
        "DspBoot__Fv" => {
            // The ucode upload and its first mail are skipped; DSPInit2's interrupt unmask is kept.
            if let Some(f) = (m.addr_of)("__OSUnmaskInterrupts") {
                c.call_guest(f, &[0x0100_0000]); // OS_INTERRUPTMASK_DSP_DSP
            }
            m.hw().dsp.booted = true;
            crate::diag::log("DSP: jaudio ucode booted (HLE)".into());
        }
        "DSPSendCommands__FPUlUl" => {
            let (cmds, count) = (c.gpr(3), c.gpr(4));
            m.hw().dsp.command(m, cmds, count);
            c.set_gpr(3, 0);
        }
        "DSPWaitFinish__Fv" => {}
        "DSPReleaseHalt__Fv" => {
            if m.hw().dsp.release_halt(m) {
                crate::sched::raise(crate::sched::IRQ_DSP_DSP);
            }
            c.set_gpr(3, crate::dsp::MAIL_SUBFRAME_DONE);
        }

        // ---- setjmp / longjmp ----
        "longjmp" => {
            // The original reloads LR, CR, r1, r2, r13-r31 and f14-f31 from the jmp_buf and
            // sets r3; its final blr would only return to longjmp's caller here, on the restored
            // (older) stack frame. Unwind the host stack instead, to the recompiled function
            // that called setjmp: tools/gen_rust.py wraps it in a catch that resumes at LR.
            c.call_orig(addr);
            let pc = c.r(0x1020, 4) as u32;
            std::panic::resume_unwind(Box::new([LONGJMP_MAGIC, pc]));
        }
        "DVDCheckDisk" => c.set_gpr(3, 1),
        "DVDGetCommandBlockStatus" => {
            let st = m.read_be(c.gpr(3) + 0x0C, 4) as u32;
            c.set_gpr(3, st);
        }

        // ---- controllers ----
        "PADInit" | "PADReset" | "PADRecalibrate" => c.set_gpr(3, 1),
        "PADControlMotor" => {}
        "PADSetSamplingCallback" => c.set_gpr(3, 0),
        "PADRead" => {
            let dst = c.gpr(3);
            let mut ports = *m.pad.ports.lock().unwrap();
            if let Ok(v) = std::env::var("AC_FAKE_PAD") {
                // Diagnostic: hold a button mask on port 0 (hex), e.g. 1000 = Start.
                let n = PADREADS.load(std::sync::atomic::Ordering::Relaxed);
                if (n / 30) % 2 == 1 {
                    ports[0].button = u16::from_str_radix(&v, 16).unwrap_or(0);
                }
                ports[0].connected = true;
            }
            let n = PADREADS.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
            if let Some(mask) = pad_script(n) {
                ports[0].button = mask;
                ports[0].connected = true;
            }
            for (i, p) in ports.iter().enumerate() {
                for (j, b) in p.to_bytes().iter().enumerate() {
                    m.write_be(dst + 12 * i as u32 + j as u32, 1, *b as u64);
                }
            }
            c.set_gpr(3, 0);
        }

        "SIProbe" | "SIGetType" => {
            // SI_GC_CONTROLLER for connected ports, SI_ERROR_NO_RESPONSE otherwise.
            let ch = (c.gpr(3) & 3) as usize;
            let connected = m.pad.ports.lock().unwrap()[ch].connected
                || ch == 0 && (std::env::var_os("AC_FAKE_PAD").is_some() || std::env::var_os("AC_PAD_SCRIPT").is_some());
            c.set_gpr(3, if connected { 0x0900_0000 } else { 0x08 });
        }

        // ---- GX ----
        "__GXFifoLink" => {
            m.hw().gx_link = c.gpr(3) & 0xFF != 0;
            c.call_orig(addr);
        }

        // ---- audio: the IPL-ucode handshake and AI sample-rate calibration are skipped; the
        // jaudio ucode is emulated at the command level (dsp.rs) ----
        "__AI_SRC_INIT" | "__OSInitAudioSystem" | "__OSStopAudioSystem" => {}
        // ---- diagnostics / system ----
        // ---- MTX library (native; top of the profile in 3D scenes) ----
        "PSMTXCopy" | "PSMTXConcat" | "PSMTXMultVec" | "PSVECCrossProduct" | "PSVECMag" | "PSMTXInverse" => {
            crate::psmtx::call(c, m.name(addr));
        }
        // ---- MSR.EE ----
        // Ghidra's mtmsr p-code keeps the old EE (and ME) bits, so the recompiled versions of
        // these never change the interrupt-enable state: every OS critical section could be
        // interrupted (the ARAM queue's chunk bookkeeping got corrupted that way).
        "OSDisableInterrupts" => {
            let msr = c.r(gc_core::REG_MSR, 4);
            c.wr(gc_core::REG_MSR, 4, msr & !gc_core::MSR_EE);
            c.set_gpr(3, ((msr & gc_core::MSR_EE) != 0) as u32);
        }
        "OSEnableInterrupts" => {
            let msr = c.r(gc_core::REG_MSR, 4);
            c.wr(gc_core::REG_MSR, 4, msr | gc_core::MSR_EE);
            c.set_gpr(3, ((msr & gc_core::MSR_EE) != 0) as u32);
            c.poll();
        }
        "OSRestoreInterrupts" => {
            let msr = c.r(gc_core::REG_MSR, 4);
            let new = if c.gpr(3) != 0 { msr | gc_core::MSR_EE } else { msr & !gc_core::MSR_EE };
            c.wr(gc_core::REG_MSR, 4, new);
            c.set_gpr(3, ((msr & gc_core::MSR_EE) != 0) as u32);
            c.poll();
        }
        "PPCMtmsr" => {
            c.wr(gc_core::REG_MSR, 4, c.gpr(3) as u64);
            c.poll();
        }
        // HID2 (locked cache enable, paired-single enable, ...) belongs to the CPU, not to a
        // thread, but every host thread has its own register file. The few functions that
        // touch it see the shared copy on entry and publish it on exit.
        "PPCMfhid2" | "PPCMthid2" | "__OSFPRInit" | "__LCEnable" | "LCDisable" | "LCQueueWait"
        | "__OSLoadFPUContext" | "__OSSaveFPUContext" | "OSFillFPUContext"
        => {
            const REG_HID2: usize = 0x1E60;
            let g = HID2.load(std::sync::atomic::Ordering::SeqCst);
            if g >> 32 != 0 {
                c.wr(REG_HID2, 4, g & 0xFFFF_FFFF);
            }
            c.call_orig(addr);
            HID2.store(1 << 32 | c.r(REG_HID2, 4), std::sync::atomic::Ordering::SeqCst);
        }
        "OSReport" => {
            let msg = format_guest(c, c.gpr(3), 4);
            crate::diag::log(format!("OSReport: {}", msg.trim_end()));
        }
        "OSPanic" => {
            let file = read_cstr(c, c.gpr(3));
            let line = c.gpr(4);
            let msg = format_guest(c, c.gpr(5), 6);
            crate::diag::log(format!("OSPanic {file}:{line}: {}", msg.trim_end()));
            park_forever();
        }
        "OSResetSystem" | "__OSDoHotReset" => {
            crate::diag::log(format!("{} called (ignored)", m.name(addr)));
        }
        "PPCHalt" => {
            crate::diag::log("PPCHalt".into());
            park_forever();
        }
        name if name.starts_with("CARD") || name.starts_with("__CARD") => match crate::memcard::call(c, name) {
            Some(r) => {
                crate::diag::log(format!("{name} -> {r}"));
                c.set_gpr(3, r as u32);
            }
            None => return false,
        },
        _ => return false,
    }
    true
}

/// Game buffer foresta.rel was loaded into (what LoadLink / UnLink hold), once linked.
static LINKED: std::sync::atomic::AtomicU32 = std::sync::atomic::AtomicU32::new(0);

/// Does `p` hold the foresta.rel module header (module id 1, 20 sections, REL version 2)?
fn is_foresta(m: &crate::Machine, p: u32) -> bool {
    p & 3 == 0
        && m.read_be(p, 4) == 1
        && m.read_be(p + 0x0C, 4) == 20
        && m.read_be(p + 0x1C, 4) == 2
        && m.read_be(p + 0x10, 4) == 0x48
}

/// Size of the REL image (header through the end of the import table).
fn rel_image_size(m: &crate::Machine, p: u32) -> u32 {
    let (rel_off, imp_off, imp_size) = (m.read_be(p + 0x24, 4) as u32, m.read_be(p + 0x28, 4) as u32, m.read_be(p + 0x2C, 4) as u32);
    let mut end = rel_off.max(imp_off + imp_size);
    let (n, info) = (m.read_be(p + 0x0C, 4) as u32, m.read_be(p + 0x10, 4) as u32);
    for i in 0..n {
        let (off, size) = (m.read_be(p + info + 8 * i, 4) as u32 & !1, m.read_be(p + info + 8 * i + 4, 4) as u32);
        if off != 0 {
            end = end.max(off + size);
        }
    }
    end
}

/// OSLink(foresta.rel): LoadLink read the module into a heap buffer (the heap is used exactly as
/// on the console). Its code was recompiled for one fixed address (tools/rel_link.py), so the
/// image is copied to REL_BASE, above the 24 MiB of MEM1 the game sees, and linked there with the
/// game's own OSLink, its BSS right after the image. LoadLink then calls the prolog through the
/// header of its buffer, and UnLink reads the epilog from it, so the linked header and section
/// table are mirrored back into the buffer.
fn link_foresta(c: &mut Ctx, addr: u32, module: u32, game_bss: u32) {
    let m = c.m();
    let size = rel_image_size(m, module);
    let bss = (crate::REL_BASE + size + 31) & !31;
    let bss_size = m.read_be(module + 0x20, 4) as u32;
    let (src, dst) = (crate::Machine::ram_off(module).unwrap(), crate::Machine::ram_off(crate::REL_BASE).unwrap());
    if src + size as usize > crate::MEM1_SIZE || dst + (bss - crate::REL_BASE + bss_size) as usize > crate::RAM_SIZE {
        crate::diag::log(format!("OSLink: foresta.rel at {module:#010x} ({size:#x} bytes) does not fit"));
        c.call_orig(addr);
        return;
    }
    if bss != REL_BSS || size != REL_IMAGE_SIZE {
        crate::diag::log(format!(
            "OSLink: foresta.rel image {size:#x} bytes / BSS {bss:#010x} differ from the recompiled build ({REL_IMAGE_SIZE:#x} / {REL_BSS:#010x}); wrong disc revision?"
        ));
    }
    // OSLink turns the section-info offset into an address: measure the header before linking.
    let header = m.read_be(module + 0x10, 4) as u32 + 8 * m.read_be(module + 0x0C, 4) as u32;
    m.ram().copy_within(src..src + size as usize, dst);
    c.set_gpr(3, crate::REL_BASE);
    c.set_gpr(4, bss);
    c.call_orig(addr);
    let ok = c.gpr(3);
    let h = dst..dst + header as usize;
    m.ram().copy_within(h, src);
    LINKED.store(module, std::sync::atomic::Ordering::Relaxed);
    crate::diag::log(format!(
        "OSLink: foresta.rel loaded at {module:#010x} (game BSS {game_bss:#010x}) linked at {:#010x}, BSS {bss:#010x}+{bss_size:#x}, prolog {:#010x} -> {ok}",
        crate::REL_BASE, m.read_be(module + 0x34, 4)
    ));
}

/// The foresta.rel layout the recompiled code was generated for (sheets/rel_layout.csv); checked
/// against `ac_recomp::table` at boot.
pub const REL_IMAGE_SIZE: u32 = 15_640_056;
pub const REL_BSS: u32 = 0x826E_A600;

fn park_forever() -> ! {
    loop {
        std::thread::park();
    }
}

/// 64-bit argument in a register pair (PPC EABI: rN = high word, rN+1 = low word).
fn arg64(c: &Ctx, n: usize) -> u64 {
    ((c.gpr(n) as u64) << 32) | c.gpr(n + 1) as u64
}

/// Queue the completion of a DVD command: at `due` the block turns END and `cb(result, block)`
/// runs, like the DI interrupt.
fn dvd_done(block: u32, cb: u32, result: u32, due: u64) {
    crate::sched::dvd_complete(crate::sched::DvdDone {
        callback: cb,
        result,
        block,
        state: Some(DVD_STATE_END as u32),
        due,
    });
}

/// GameCube drive timing (the drive runs one command at a time): command overhead, a seek
/// (20 ms, well under the real drive's average) unless the read continues where the previous one
/// ended, and ~3 MB/s transfer.
fn dvd_latency(off: u64, len: u32) -> u64 {
    use std::sync::atomic::{AtomicU64, Ordering::Relaxed};
    static DRIVE_FREE: AtomicU64 = AtomicU64::new(0);
    static NEXT_OFF: AtomicU64 = AtomicU64::new(u64::MAX);
    const TB: u64 = crate::sched::TB_HZ;
    let start = crate::sched::timebase().max(DRIVE_FREE.load(Relaxed));
    let seek = if NEXT_OFF.swap(off + len as u64, Relaxed) == off { 0 } else { TB * 20 / 1000 };
    let due = start + TB / 2000 + seek + len as u64 * TB / 3_000_000;
    DRIVE_FREE.store(due, Relaxed);
    due
}

/// Cancel the command in flight on `block`, if any: it becomes CANCELED and its pending
/// completion is withdrawn. Returns the command's callback (0 if none) when one was in flight.
fn dvd_cancel(c: &mut Ctx, block: u32) -> Option<u32> {
    let gone = crate::sched::dvd_withdraw(block);
    if gone.is_empty() {
        return None;
    }
    c.m().write_be(block + 0x0C, 4, DVD_STATE_CANCELED);
    Some(gone.iter().map(|d| d.callback).find(|&cb| cb != 0).unwrap_or(0))
}

fn dvd_read(c: &mut Ctx, off: u64, dst: u32, len: u32) {
    let m = c.m();
    let Some(o) = crate::Machine::ram_off(dst) else { return };
    let len = (len as usize).min(crate::RAM_SIZE - o);
    match &m.disc {
        Some(d) => d.read(off, &mut m.ram()[o..o + len]),
        None => crate::diag::log(format!("DVD read {off:#x}+{len:#x} but no disc image is loaded")),
    }
}

fn read_cstr(c: &Ctx, addr: u32) -> String {
    let mut out = Vec::new();
    for i in 0..1024 {
        let b = c.m().read_be(addr + i, 1) as u8;
        if b == 0 {
            break;
        }
        out.push(b);
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// Minimal printf for OSReport/OSPanic: integer, char and string conversions.
fn format_guest(c: &Ctx, fmt_addr: u32, first_arg: usize) -> String {
    let fmt = read_cstr(c, fmt_addr);
    let mut out = String::new();
    let mut arg = first_arg;
    let mut next = |c: &Ctx| {
        let v = if arg <= 10 { c.gpr(arg) } else { 0 };
        arg += 1;
        v
    };
    let mut chars = fmt.chars().peekable();
    while let Some(ch) = chars.next() {
        if ch != '%' {
            out.push(ch);
            continue;
        }
        let mut spec = String::new();
        while let Some(&n) = chars.peek() {
            chars.next();
            if n.is_ascii_alphabetic() && !matches!(n, 'l' | 'h') || n == '%' {
                spec.push(n);
                break;
            }
            spec.push(n);
        }
        match spec.chars().last() {
            Some('d') | Some('i') => out += &(next(c) as i32).to_string(),
            Some('u') => out += &next(c).to_string(),
            Some('x') | Some('p') => out += &format!("{:x}", next(c)),
            Some('X') => out += &format!("{:X}", next(c)),
            Some('c') => out.push(next(c) as u8 as char),
            Some('s') => {
                let a = next(c);
                out += &read_cstr(c, a);
            }
            Some('%') => out.push('%'),
            _ => out += &format!("%{spec}"),
        }
    }
    out
}

