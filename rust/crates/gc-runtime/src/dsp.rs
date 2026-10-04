//! High-level emulation of Animal Crossing's audio DSP ucode (the "light protocol" member of the
//! JAudio / Zelda ucode family, Ector CRC 0x4BE6A5CB in Dolphin's list).
//!
//! The game's DSP driver lives in `jaudio_NES` (dspboot.c, dspproc.c, dspbuf.c, audiothread.c).
//! It is hooked at the function level (`hle.rs`, `sheets/hle.csv`), because the protocol on top of
//! the mailbox is fully visible in that CPU code:
//! * `DspBoot` uploads the ucode and waits for its first mail; here the DSP simply counts as
//!   booted.
//! * `DSPSendCommands(cmds, n)` sends a command as `n` mails:
//!   - `0x80......` (DwaitFrame): no-op.
//!   - `0x81` DsetupTable: `cmd & 0xFFFF` channels, the channel block array (`CH_BUF`, 0x180
//!     bytes per channel), the resampling/pattern tables, the AFC coefficients, the reverb blocks.
//!   - `0x82` DsyncFrame: `(cmd >> 16) & 0xFF` sub-frames of 80 samples into the left/right
//!     output buffers, at output volume `cmd & 0xFFFF`.
//! * `DSPWaitFinish` waits for the command's acknowledge (immediate here).
//! * After `0x82` the DSP is halted. Each `DSPReleaseHalt` renders one sub-frame, after which the
//!   DSP interrupts the CPU. The interrupt handler (`__DspSync`) posts message 1 to the audio
//!   thread, which counts the sub-frames down (`intcount`, set by `DspSyncCountClear`) and
//!   releases the DSP again (`UpdateDSP`) until the frame is complete (`DspFrameEnd`).
//!
//! Channel mixing implements what the CPU side relies on: AFC 4-bit/2-bit ADPCM and 8/16-bit PCM
//! (from ARAM, where jaudio's wave system loads its archives; main-RAM addresses are accepted
//! too), pitch (4.12 fixed point), loops with stored predictor history, per-bus mixer volumes
//! (main L/R = buses 0x0D00/0x0D60, as in jaudio's connect table) or the auto-mixer, and the
//! finished flag. Not emulated: FX/reverb buses, IIR/FIR filters, oscillators.
//! Most of Animal Crossing's sound (the N64 "Nas" engine through `rspsim.c`, and the NES APU of
//! the Famicom games) is mixed by the CPU (`cpubuf.c`) and needs nothing from here.

use crate::{Machine, ARAM_SIZE};
use std::sync::atomic::{AtomicU64, Ordering::Relaxed};

/// Sub-frames rendered so far (for diagnostics / preflight).
pub static SUBFRAMES_RENDERED: AtomicU64 = AtomicU64::new(0);
/// Channels that had samples mixed in the last rendered sub-frame.
pub static VOICES_LAST: AtomicU64 = AtomicU64::new(0);
/// Most channels mixed in any sub-frame so far.
pub static VOICES_MAX: AtomicU64 = AtomicU64::new(0);
/// Channels seen active (enabled flag set), cumulative.
pub static ACTIVE_SEEN: AtomicU64 = AtomicU64::new(0);

const CH_SIZE: u32 = 0x180;
pub const SUBFRAME: usize = 80;
const BUS_MAIN_L: u16 = 0x0D00;
const BUS_MAIN_R: u16 = 0x0D60;
/// What `DSPReleaseHalt` returns (and the mail the ucode sends after each sub-frame).
pub const MAIL_SUBFRAME_DONE: u32 = 0x8888_1357;

#[derive(Clone, Default)]
struct Voice {
    /// Fractional resampling position between `prev` and `cur` input samples.
    frac: f64,
    prev: i32,
    cur: i32,
    /// Decoded AFC block, the index of the next sample to take from it and the end of its valid
    /// samples (`idx >= valid` = need a block).
    samples: [i16; 16],
    idx: usize,
    valid: usize,
    /// Samples to drop from the next decoded block (start / loop points inside a block).
    skip: usize,
    hist: (i32, i32),
}

struct PendingFrame {
    left: u32,
    right: u32,
    subframes: u32,
    next: u32,
    level: f32,
}

#[derive(Default)]
pub struct Dsp {
    pub booted: bool,
    ch_buf: u32,
    channels: u32,
    adpcm_table: u32,
    voices: Vec<Voice>,
    frame: Option<PendingFrame>,
}

impl Dsp {
    // The mailbox registers are not used: the driver functions above are hooked. These keep the
    // MMIO side (hw.rs) inert.
    pub fn reset(&mut self) {}
    pub fn set_halted(&mut self, _halted: bool) {}
    pub fn read_hi(&mut self) -> u32 {
        0
    }
    pub fn read_lo(&mut self) -> u32 {
        0
    }
    pub fn mail_to_dsp(&mut self, _m: &Machine, mail: u32) {
        crate::diag::log(format!("DSP: unexpected mail {mail:#010x} through the mailbox"));
    }

    /// `DSPSendCommands(cmds, count)`.
    pub fn command(&mut self, m: &Machine, cmds: u32, count: u32) {
        let rd = |i: u32| if i < count { m.read_be(cmds + 4 * i, 4) as u32 } else { 0 };
        if count == 0 {
            return;
        }
        let cmd = rd(0);
        match (cmd >> 24) & 0x7F {
            0x00 => {}
            0x01 => {
                self.channels = (cmd & 0xFFFF).min(256);
                self.ch_buf = rd(1);
                self.adpcm_table = rd(3);
                self.voices = vec![Voice { idx: 16, cur: i32::MIN, ..Default::default() }; self.channels as usize];
                crate::diag::log(format!(
                    "DSP: setup {} channels at {:#010x}, AFC coefficients {:#010x}",
                    self.channels, self.ch_buf, self.adpcm_table
                ));
            }
            0x02 => {
                let subframes = (cmd >> 16) & 0xFF;
                let level = (cmd & 0xFFFF) as f32 / 0x4000 as f32;
                if let Some(f) = &self.frame {
                    if f.next < f.subframes {
                        crate::diag::log(format!(
                            "DSP: new frame while {} of {} sub-frames are unrendered",
                            f.subframes - f.next, f.subframes
                        ));
                    }
                }
                self.frame = Some(PendingFrame { left: rd(1), right: rd(2), subframes, next: 0, level });
            }
            op => crate::diag::log(format!("DSP: unhandled command {op:#04x} ({cmd:#010x})")),
        }
    }

    /// `DSPReleaseHalt`: render the next sub-frame of the pending frame. Returns true if one was
    /// rendered (the DSP then interrupts the CPU).
    pub fn release_halt(&mut self, m: &Machine) -> bool {
        let Some(f) = self.frame.as_mut() else { return false };
        if f.next >= f.subframes {
            self.frame = None;
            return false;
        }
        let (k, left, right, level) = (f.next, f.left, f.right, f.level);
        f.next += 1;
        let mut l = [0f32; SUBFRAME];
        let mut r = [0f32; SUBFRAME];
        let mut voices = 0;
        for ch in 0..self.channels {
            voices += self.mix_channel(m, ch, &mut l, &mut r) as u64;
        }
        SUBFRAMES_RENDERED.fetch_add(1, Relaxed);
        VOICES_LAST.store(voices, Relaxed);
        VOICES_MAX.fetch_max(voices, Relaxed);
        let off = k * SUBFRAME as u32 * 2;
        for i in 0..SUBFRAME {
            let cl = (l[i] * level).clamp(-32768.0, 32767.0) as i16;
            let cr = (r[i] * level).clamp(-32768.0, 32767.0) as i16;
            m.write_be(left + off + 2 * i as u32, 2, cl as u16 as u64);
            m.write_be(right + off + 2 * i as u32, 2, cr as u16 as u64);
        }
        true
    }

    /// Mix one channel into the sub-frame; returns true if it produced samples.
    fn mix_channel(&mut self, m: &Machine, ch: u32, out_l: &mut [f32; SUBFRAME], out_r: &mut [f32; SUBFRAME]) -> bool {
        let b = self.ch_buf + ch * CH_SIZE;
        let r16 = |o: u32| m.read_be(b + o, 2) as u16;
        let r32 = |o: u32| m.read_be(b + o, 4) as u32;
        let w16 = |o: u32, v: u16| m.write_be(b + o, 2, v as u64);
        if r16(0x00) == 0 || r16(0x0C) != 0 {
            return false; // inactive or paused
        }
        if ACTIVE_SEEN.fetch_add(1, Relaxed) == 0 {
            crate::diag::log(format!(
                "DSP: first active channel {ch}: pitch {:#x} wave {:#x} type {} loop {} start {:#x} end {:#x} \
                 total {:#x} automix {} vol {:#x} mix0 {:#x}/{:#x} mix1 {:#x}/{:#x}",
                r16(0x04), r32(0x118), r16(0x100), r16(0x102), r32(0x110), r32(0x114),
                r32(0x11C), r16(0x58), r16(0x56), r16(0x10), r16(0x12), r16(0x18), r16(0x1A)
            ));
        }
        if r16(0x10A) != 0 {
            w16(0x02, 1); // forced stop: report finished
            return false;
        }
        let table = self.adpcm_table;
        let wave = r32(0x118);
        // Sample source type (VPB +0x100), which for ARAM sources is also the number of bytes
        // per 16 samples: 5 / 9 = AFC (2- / 4-bit), 8 = PCM8, 16 = PCM16. Oscillators (< 4) are
        // not emulated.
        let block_bytes = r16(0x100) as u32;
        let (afc, pcm_bytes) = match block_bytes {
            5 | 9 => (true, 0),
            8 => (false, 1),
            16 => (false, 2),
            _ => return false,
        };
        if wave == 0 {
            return false;
        }
        // Sample memory: ARAM offsets, or main RAM for cached/uncached addresses.
        let (mem, base_off): (&[u8], u32) = if wave >= 0x8000_0000 {
            (&m.ram()[..crate::MEM1_SIZE], 0x8000_0000 | (wave & 0x4000_0000))
        } else {
            (&m.aram()[..ARAM_SIZE], 0)
        };
        let wave_off = wave.wrapping_sub(base_off);
        let addr_of = |pos: u32| if afc { wave_off + pos / 16 * block_bytes } else { wave_off + pos * pcm_bytes };
        let looped = r16(0x102) != 0;
        let pitch = r16(0x04) as f64 / 4096.0;

        // Volumes: explicit main-bus mixers when routed, else the auto-mixer (volume + pan).
        let (mut vl, mut vr) = (0f32, 0f32);
        for i in 0..4 {
            let mo = 0x10 + 8 * i;
            let target = r16(mo + 2) as i16 as f32 / 32767.0;
            w16(mo + 4, r16(mo + 2)); // current volume reaches target
            match r16(mo) {
                BUS_MAIN_L => vl += target,
                BUS_MAIN_R => vr += target,
                _ => {}
            }
        }
        if vl == 0.0 && vr == 0.0 && r16(0x58) != 0 {
            let vol = r16(0x56) as f32 / 32767.0;
            let pan = ((r16(0x50) >> 8) as f32 / 127.0).clamp(0.0, 1.0);
            vl = vol * (1.0 - pan).sqrt();
            vr = vol * pan.sqrt();
        }

        // Playback state lives in the channel block like on the real DSP (the CPU reads it):
        // 0x70 = current sample address, 0x74 = samples left before the loop/end point,
        // 0x110 = loop position, 0x114 = loop/end position, 0x68 = start position.
        let voice = &mut self.voices[ch as usize];
        let (mut addr, mut remaining);
        if r16(0x08) != 0 {
            let start = r32(0x68);
            *voice = Voice { idx: 16, skip: if afc { (start % 16) as usize } else { 0 }, ..Default::default() };
            addr = addr_of(start);
            remaining = r32(0x114).wrapping_sub(start);
            w16(0x08, 0);
            voice.cur = i32::MIN; // prime on first use
        } else {
            addr = r32(0x70).wrapping_sub(base_off);
            remaining = r32(0x74);
        }
        let coef = |idx: usize| -> (i32, i32) {
            let a = table + 4 * idx as u32;
            (m.read_be(a, 2) as u16 as i16 as i32, m.read_be(a + 2, 2) as u16 as i16 as i32)
        };
        let loop_hist = (r16(0x104) as i16 as i32, r16(0x106) as i16 as i32);
        let (loop_pos, loop_end) = (r32(0x110), r32(0x114));
        let mut finished = false;
        // Next input sample, advancing the address / remaining count like the ucode (semantics as
        // in Dolphin's ZeldaAudioRenderer): for AFC `remaining` counts samples *decoded* (a block
        // at a time); at the end of a looping voice the source restarts at the loop block with
        // the loop history, and `remaining = end - cached - loop` in wrapping u32 arithmetic.
        let fetch = |voice: &mut Voice, addr: &mut u32, remaining: &mut u32| -> Option<i32> {
            if afc {
                if voice.idx >= voice.valid {
                    if *remaining == 0 {
                        if !looped {
                            return None;
                        }
                        *addr = wave_off.wrapping_add((loop_pos >> 4) * block_bytes);
                        voice.hist = loop_hist;
                        voice.samples = decode_afc(mem, *addr as usize, block_bytes, &mut voice.hist, &coef);
                        *addr = addr.wrapping_add(block_bytes);
                        let cached = 16 - (loop_pos & 0xF);
                        voice.idx = (loop_pos & 0xF) as usize;
                        voice.valid = 16;
                        voice.skip = 0;
                        *remaining = loop_end.wrapping_sub(cached).wrapping_sub(loop_pos);
                    } else {
                        voice.samples = decode_afc(mem, *addr as usize, block_bytes, &mut voice.hist, &coef);
                        *addr = addr.wrapping_add(block_bytes);
                        let start = voice.skip.min(15);
                        voice.skip = 0;
                        let n = (16 - start as u32).min(*remaining);
                        voice.idx = start;
                        voice.valid = start + n as usize;
                        *remaining -= n;
                    }
                }
                let s = voice.samples[voice.idx] as i32;
                voice.idx += 1;
                Some(s)
            } else {
                if *remaining == 0 {
                    if !looped {
                        return None;
                    }
                    *addr = addr_of(loop_pos);
                    *remaining = loop_end.wrapping_sub(loop_pos);
                }
                *remaining -= 1;
                let a = *addr as usize;
                *addr = addr.wrapping_add(pcm_bytes);
                Some(if pcm_bytes == 1 {
                    mem.get(a).map_or(0, |&v| (v as i8 as i32) << 8)
                } else if a + 1 < mem.len() {
                    i16::from_be_bytes([mem[a], mem[a + 1]]) as i32
                } else {
                    0
                })
            }
        };
        if voice.cur == i32::MIN {
            match fetch(voice, &mut addr, &mut remaining) {
                Some(s) => {
                    voice.prev = s;
                    voice.cur = s;
                }
                None => finished = true,
            }
        }
        if !finished {
            for i in 0..SUBFRAME {
                let s = voice.prev as f64 + (voice.cur - voice.prev) as f64 * voice.frac;
                out_l[i] += s as f32 * vl;
                out_r[i] += s as f32 * vr;
                voice.frac += pitch;
                while voice.frac >= 1.0 {
                    voice.frac -= 1.0;
                    match fetch(voice, &mut addr, &mut remaining) {
                        Some(n) => {
                            voice.prev = voice.cur;
                            voice.cur = n;
                        }
                        None => {
                            finished = true;
                            break;
                        }
                    }
                }
                if finished {
                    break;
                }
            }
        }
        m.write_be(b + 0x70, 4, addr.wrapping_add(base_off) as u64);
        m.write_be(b + 0x74, 4, remaining as u64);
        // VPB +0x64: decoded AFC samples not yet consumed (part of the play position).
        w16(0x64, if afc { voice.valid.saturating_sub(voice.idx) as u16 } else { 0 });
        if finished {
            w16(0x02, 1);
        }
        true
    }
}

/// Decode one AFC block (9 bytes = 4-bit, 5 bytes = 2-bit) into 16 samples.
fn decode_afc(mem: &[u8], a: usize, bytes: u32, hist: &mut (i32, i32), coef: impl Fn(usize) -> (i32, i32)) -> [i16; 16] {
    let mut out = [0i16; 16];
    if a + bytes as usize > mem.len() {
        return out;
    }
    let header = mem[a];
    let delta = 1i32 << ((header >> 4) & 0xF);
    let (c0, c1) = coef((header & 0xF) as usize);
    for (i, o) in out.iter_mut().enumerate() {
        let (val, shift) = if bytes == 9 {
            let byte = mem[a + 1 + i / 2];
            let nib = if i & 1 == 0 { byte >> 4 } else { byte & 15 };
            (((nib as i32) << 28) >> 28, 11)
        } else {
            let byte = mem[a + 1 + i / 4];
            let v = (byte >> (6 - 2 * (i % 4))) & 3;
            (((v as i32) << 30) >> 30, 13)
        };
        let mut s = (delta * val) << shift;
        s += hist.0 * c0 + hist.1 * c1;
        s >>= 11;
        let s = s.clamp(-32768, 32767);
        hist.1 = hist.0;
        hist.0 = s;
        *o = s as i16;
    }
    out
}
