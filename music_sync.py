#!/usr/bin/env python3
"""
keyboard-music-lightsync: music-reactive lighting for
  * any VIA-enabled QMK keyboard   (stock firmware; whole-board brightness + colour)
  * Razer mice/keyboards that speak the "extended matrix" protocol
    (tested: Basilisk V3, 11 addressable LEDs)

No firmware changes. Nothing is written to either device's flash; the
previous lighting is read at start and restored on Ctrl-C.

Audio comes from a loopback device (BlackHole on macOS) when present, else
the default input. See README.md for setup, credits and references.

Modes
  music  (default) brightness = loudness + bass punch + onset flashes,
         colour drifts and follows timbre (spectral centroid)
  meter  music, plus a 9-band spectrum across the mouse underglow
  beat   colour jumps on detected onsets
  pulse  brightness follows loudness only, fixed colour

Usage
  python music_sync.py --probe
  python music_sync.py --mode music
  python music_sync.py --mode meter --no-keyboard
"""
import argparse
import collections
import colorsys
import math
import signal
import sys
import threading
import time

import numpy as np
import sounddevice as sd

try:
    import hid
except ImportError:  # pragma: no cover
    hid = None


# ============================================================ Womier / VIA
RAW_USAGE_PAGE, RAW_USAGE = 0xFF60, 0x61      # QMK raw HID (VIA) usage page/id
CMD_GET_PROTOCOL_VERSION, CMD_CUSTOM_SET_VALUE, CMD_CUSTOM_GET_VALUE = 0x01, 0x07, 0x08
CH_RGB_MATRIX = 0x03
VAL_BRIGHTNESS, VAL_EFFECT, VAL_SPEED, VAL_COLOR = 1, 2, 3, 4
EFFECT_SOLID = 1


class ViaKeyboard:
    name = "keyboard"

    def __init__(self, vid=None, pid=None):
        cands = [d for d in hid.enumerate(vid or 0)
                 if d["usage_page"] == RAW_USAGE_PAGE and d["usage"] == RAW_USAGE
                 and (pid is None or d["product_id"] == pid)]
        # try the board itself before any wireless dongle that mirrors its descriptor
        cands.sort(key=lambda d: "dongle" in (d["product_string"] or "").lower())
        if not cands:
            raise RuntimeError("no VIA raw-HID interface found (keyboard must be wired)")
        self.lock = threading.Lock()
        err = None
        for d in cands:
            self.h = hid.device()
            try:
                self.h.open_path(d["path"])
            except OSError as e:
                err = e
                continue
            ver = self.xfer([CMD_GET_PROTOCOL_VERSION])
            if ver:
                self.info = d
                break
            self.h.close()
            err = RuntimeError(f"{d['product_string']} does not answer VIA (wireless dongle? use the cable)")
        else:
            raise err or RuntimeError("no VIA device answered")
        self.protocol = (ver[1] << 8) | ver[2]
        self.label = f"{self.info['product_string']} (VIA v{self.protocol})"
        self.last_b, self.last_h = -1, -1

    def xfer(self, pkt, timeout_ms=300):
        buf = [0x00] + pkt + [0] * (32 - len(pkt))
        with self.lock:
            self.h.write(buf)
            return self.h.read(32, timeout_ms=timeout_ms)

    def send(self, pkt):
        buf = [0x00] + pkt + [0] * (32 - len(pkt))
        with self.lock:
            self.h.write(buf)
            self.h.read(32, timeout_ms=20)

    def get(self, value_id, n=1):
        r = self.xfer([CMD_CUSTOM_GET_VALUE, CH_RGB_MATRIX, value_id])
        return list(r[3:3 + n]) if r else None

    def set(self, value_id, *data):
        self.send([CMD_CUSTOM_SET_VALUE, CH_RGB_MATRIX, value_id, *data])

    def snapshot(self):
        return {"brightness": self.get(VAL_BRIGHTNESS), "effect": self.get(VAL_EFFECT),
                "speed": self.get(VAL_SPEED), "color": self.get(VAL_COLOR, 2)}

    def start(self, hue, sat):
        self.snap = self.snapshot()
        self.set(VAL_EFFECT, EFFECT_SOLID)
        self.set(VAL_COLOR, hue, sat)
        self.set(VAL_BRIGHTNESS, 0)

    def update(self, f, brightness, hue, sat, mode):
        if hue != self.last_h:
            self.set(VAL_COLOR, hue, sat)
            self.last_h = hue
        if brightness != self.last_b:
            self.set(VAL_BRIGHTNESS, brightness)
            self.last_b = brightness

    def restore(self):
        for vid_, key in ((VAL_EFFECT, "effect"), (VAL_SPEED, "speed"),
                          (VAL_COLOR, "color"), (VAL_BRIGHTNESS, "brightness")):
            if self.snap.get(key):
                self.set(vid_, *self.snap[key])
                time.sleep(0.02)

    def close(self):
        self.h.close()


# ============================================================ Razer mouse
RAZER_VID = 0x1532
RAZER_TID = 0x1F
NOSTORE, VARSTORE = 0x00, 0x01
FX_STATIC, FX_SPECTRUM, FX_CUSTOM = 0x01, 0x03, 0x08
DEFAULT_MOUSE_LEDS = 11          # Basilisk V3: 0 scroll wheel, 1 logo, 2..10 underglow


class RazerMouse:
    name = "mouse"

    def __init__(self, pid=None, leds=DEFAULT_MOUSE_LEDS):
        cands = [d for d in hid.enumerate(RAZER_VID) if pid is None or d["product_id"] == pid]
        if not cands:
            raise RuntimeError("no Razer device on USB")
        self.h = None
        # The control endpoint is whichever interface accepts a feature report.
        for d in sorted(cands, key=lambda d: d["interface_number"] != 3):
            h = hid.device()
            try:
                h.open_path(d["path"])
            except OSError:
                continue
            self.h = h
            try:
                st, args = self._cmd(0x00, 0x81, [0, 0])
            except OSError:
                h.close(); self.h = None; continue
            if st == 0x02:
                self.info = d
                self.fw = f"{args[0]}.{args[1]}"
                break
            h.close(); self.h = None
        if self.h is None:
            raise RuntimeError("Razer device found but no interface answers the Razer protocol")
        self.lock = threading.Lock()
        self.label = f"{self.info['product_string']} (fw {self.fw})"
        self.n = leds
        self.last_rgb, self.last_b, self.last_frame = None, -1, None

    @staticmethod
    def _packet(cls, cid, args, size):
        p = bytearray(90)
        p[1], p[5], p[6], p[7] = RAZER_TID, size, cls, cid
        p[8:8 + len(args)] = bytes(args)
        crc = 0
        for b in p[2:88]:
            crc ^= b
        p[88] = crc
        return bytes(p)

    def _cmd(self, cls, cid, args, size=None, read=True):
        pkt = self._packet(cls, cid, args, len(args) if size is None else size)
        self.h.send_feature_report(b"\x00" + pkt)
        if not read:
            return None, None
        time.sleep(0.004)
        r = self.h.get_feature_report(0, 91)
        return r[1], list(r[9:89])

    def cmd(self, *a, **k):
        with getattr(self, "lock", threading.Lock()):
            return self._cmd(*a, **k)

    # --- lighting primitives
    def set_brightness(self, v):
        self.cmd(0x0F, 0x04, [NOSTORE, 0x00, v], read=False)

    def set_static(self, r, g, b):
        self.cmd(0x0F, 0x02, [NOSTORE, 0x00, FX_STATIC, 0, 0, 1, r, g, b], read=False)

    def set_effect_raw(self, args):
        self.cmd(0x0F, 0x02, [NOSTORE, 0x00] + list(args), size=0x0C, read=False)

    def set_frame(self, rgb):            # rgb: list of (r,g,b) per LED
        flat = [c for px in rgb for c in px]
        self.cmd(0x0F, 0x03, [0x00, 0x00, 0x00, len(rgb) - 1] + flat, size=0x47, read=False)

    def use_custom(self):
        self.cmd(0x0F, 0x02, [NOSTORE, 0x00, FX_CUSTOM] + [0] * 9, size=0x0C, read=False)

    # --- lifecycle
    def snapshot(self):
        st, a = self.cmd(0x0F, 0x84, [VARSTORE, 0x01, 0])
        bright = a[2] if st == 0x02 else 179
        st, a = self.cmd(0x0F, 0x82, [VARSTORE, 0x01] + [0] * 10, size=0x0C)
        effect = a[2:12] if st == 0x02 else [FX_SPECTRUM] + [0] * 9
        return {"brightness": bright, "effect": effect}

    def start(self, hue, sat):
        self.snap = self.snapshot()
        self.set_brightness(0)
        self.set_static(*hsv_to_rgb(hue, sat, 255))

    def update(self, f, brightness, hue, sat, mode):
        if mode == "meter":
            # underglow = 9-band spectrum (bass -> treble), scroll wheel + logo = energy
            v = brightness / 255.0
            frame = [hsv_to_rgb(hue, sat, int(255 * v)), hsv_to_rgb(hue, sat, int(255 * v))]
            spec = f["spectrum9"]
            for k in range(self.n - 2):
                lvl = spec[min(len(spec) - 1, int(k * len(spec) / max(1, self.n - 2)))]
                h = int((hue + 30 * k) % 256)                       # colour walks with frequency
                frame.append(hsv_to_rgb(h, sat, int(255 * gamma(float(lvl), 1.3))))
            if frame != self.last_frame:
                self.set_frame(frame)
                self.last_frame = frame
                if not getattr(self, "custom_on", False):
                    self.use_custom()
                    self.custom_on = True
            if self.last_b != 255:
                self.set_brightness(255)
                self.last_b = 255
            return
        rgb = hsv_to_rgb(hue, sat, 255)
        if rgb != self.last_rgb:
            self.set_static(*rgb)
            self.last_rgb = rgb
        if brightness != self.last_b:
            self.set_brightness(brightness)
            self.last_b = brightness

    def restore(self):
        self.set_effect_raw(self.snap["effect"])
        time.sleep(0.02)
        self.set_brightness(self.snap["brightness"])

    def close(self):
        self.h.close()


# ============================================================ helpers
def hsv_to_rgb(h, s, v):
    r, g, b = colorsys.hsv_to_rgb(h / 255.0, s / 255.0, v / 255.0)
    return (int(r * 255), int(g * 255), int(b * 255))


def gamma(x, g=1.6):
    return max(0.0, min(1.0, x)) ** g


class Analyzer:
    """Per-block music analysis, borrowing what the established visualizers do:

    * log-spaced bands + log magnitude (KeyboardVisualizer / MilkDrop style)
    * bass / mid / treb normalised by their own slow running average, plus
      attenuated (smoothed) copies  -> MilkDrop's bass, bass_att, ...
    * perceptual loudness in dB, auto-ranged between the recent quiet and loud
      percentiles so it works at any volume and tracks song dynamics
    * onsets by spectral flux with an adaptive (median) threshold and a
      refractory period  -> the standard beat-detection recipe
    * tempo estimate by autocorrelating the onset envelope
    * spectral centroid (timbre brightness) for colour
    """

    NBANDS = 24
    RATE = 50                    # analysis frames per second (20 ms hop)

    def __init__(self, sr, nfft=2048):
        self.sr, self.nfft = sr, nfft
        self.buf = np.zeros(nfft, dtype=np.float32)
        self.win = np.hanning(nfft).astype(np.float32)
        freqs = np.fft.rfftfreq(nfft, 1 / sr)
        top = min(16000.0, sr / 2 * 0.95)
        edges = np.geomspace(40.0, top, self.NBANDS + 1)
        self.bands = []
        for i in range(self.NBANDS):
            idx = np.where((freqs >= edges[i]) & (freqs < edges[i + 1]))[0]
            if idx.size == 0:
                idx = np.array([np.argmin(np.abs(freqs - edges[i]))])
            self.bands.append(idx)
        self.centers = np.sqrt(edges[:-1] * edges[1:])
        self.grp = {"bass": self.centers < 250, "mid": (self.centers >= 250) & (self.centers < 4000),
                    "treb": self.centers >= 4000}
        # A-weighting (IEC 61672) per bin, as linear power gain
        f = np.maximum(freqs, 1.0)
        ra = (12194 ** 2 * f ** 4) / ((f ** 2 + 20.6 ** 2) * np.sqrt((f ** 2 + 107.7 ** 2) * (f ** 2 + 737.9 ** 2)) * (f ** 2 + 12194 ** 2))
        self.aw = (ra / ra[np.argmin(np.abs(freqs - 1000))]) ** 2
        # 9 display bands for the mouse spectrum (40 Hz .. 12 kHz)
        e9 = np.geomspace(40.0, min(12000.0, top), 10)
        self.bands9 = [np.where((self.centers >= e9[i]) & (self.centers < e9[i + 1]))[0] for i in range(9)]
        for i, b in enumerate(self.bands9):
            if b.size == 0:
                self.bands9[i] = np.array([min(self.NBANDS - 1, int(i * self.NBANDS / 9))])
        self.peak9 = np.full(9, -60.0)
        # state
        self.prev_logb = np.full(self.NBANDS, -12.0)
        self.avg = {"bass": 1e-6, "mid": 1e-6, "treb": 1e-6}
        self.att = {"bass": 1.0, "mid": 1.0, "treb": 1.0}
        self.flux_hist = collections.deque(maxlen=self.RATE)           # 1 s
        self.onset_env = collections.deque(maxlen=self.RATE * 6)       # 6 s
        self.loud_hist = collections.deque(maxlen=self.RATE * 8)       # 8 s
        self.prev_flux = 0.0
        self.last_onset = -1.0
        self.frames = 0
        self.bpm = 0.0
        self.centroid = 0.5
        self.silent = True

    def feed(self, block):
        mono = block.mean(axis=1) if block.ndim > 1 else block
        n = len(mono)
        self.buf = np.concatenate((self.buf[n:], mono.astype(np.float32)))
        self.frames += 1
        t = self.frames / self.RATE

        spec = np.abs(np.fft.rfft(self.buf * self.win)) / self.nfft
        power = spec * spec
        total = float(np.sum(power * self.aw))
        loud_db = 10 * math.log10(total + 1e-12)
        self.silent = loud_db < -75
        out = {"t": t, "silent": self.silent}

        # --- loudness auto-ranged between recent quiet/loud (5th..95th pct)
        if not self.silent:
            self.loud_hist.append(loud_db)
        if len(self.loud_hist) > self.RATE:
            lo, hi = np.percentile(self.loud_hist, [8, 97])
            hi = max(hi, lo + 12)                       # at least 12 dB of range
            loud = (loud_db - lo) / (hi - lo)
        else:
            loud = 0.0
        out["loud"] = 0.0 if self.silent else max(0.0, min(1.0, loud))

        # --- 24 log bands, log magnitude
        bands = np.array([power[idx].mean() for idx in self.bands])
        logb = np.log10(bands + 1e-12)

        # --- MilkDrop-style bass/mid/treb: instantaneous / slow average
        for k, mask in self.grp.items():
            v = float(bands[mask].mean())
            if not self.silent:
                self.avg[k] += (v - self.avg[k]) * (1 / (self.RATE * 3.0))    # 3 s average
            rel = v / (self.avg[k] + 1e-9)
            self.att[k] += (rel - self.att[k]) * 0.25
            out[k] = rel
            out[k + "_att"] = self.att[k]

        # --- spectral flux onset detection (half-wave rectified, log domain)
        flux = float(np.sum(np.maximum(0.0, logb - self.prev_logb)[2:]))  # skip lowest bins (DC-ish)
        self.prev_logb = logb
        self.flux_hist.append(flux)
        self.onset_env.append(flux)
        thr = 1.4 * float(np.median(self.flux_hist)) + 0.6 if len(self.flux_hist) > 10 else 1e9
        onset = (not self.silent and flux > thr and flux >= self.prev_flux and (t - self.last_onset) > 0.12)
        if onset:
            self.last_onset = t
        out["onset"] = onset
        out["onset_strength"] = max(0.0, min(1.0, (flux - thr) / (thr + 1e-9))) if onset else 0.0
        self.prev_flux = flux

        # --- tempo: autocorrelation of onset envelope, 60..180 BPM, every second
        if self.frames % self.RATE == 0 and len(self.onset_env) >= self.RATE * 4:
            env = np.array(self.onset_env) - np.mean(self.onset_env)
            ac = np.correlate(env, env, "full")[len(env) - 1:]
            lo_lag, hi_lag = int(self.RATE * 60 / 180), int(self.RATE * 60 / 60)
            seg = ac[lo_lag:hi_lag + 1]
            if seg.size and ac[0] > 0:
                lag = lo_lag + int(np.argmax(seg))
                if seg.max() / ac[0] > 0.15:
                    self.bpm = 60.0 * self.RATE / lag
        out["bpm"] = self.bpm

        # --- spectral centroid (timbre brightness) on log-frequency scale
        if total > 1e-10:
            c = float(np.sum(self.centers * bands) / (np.sum(bands) + 1e-12))
            c01 = (math.log10(max(c, 100.0)) - 2.0) / (math.log10(6000.0) - 2.0)   # 100 Hz..6 kHz
            self.centroid += (max(0.0, min(1.0, c01)) - self.centroid) * 0.1
        out["centroid"] = self.centroid

        # --- 9-band spectrum 0..1 for the mouse (per-band auto-gain, 30 dB window)
        db9 = np.array([10 * math.log10(bands[idx].mean() + 1e-12) for idx in self.bands9])
        self.peak9 = np.maximum(db9, self.peak9 - 0.15)             # decays ~7.5 dB/s
        out["spectrum9"] = np.clip((db9 - (self.peak9 - 30.0)) / 30.0, 0.0, 1.0) if not self.silent else np.zeros(9)
        return out


def open_devices(args):
    devs = []
    if not args.no_keyboard:
        try:
            devs.append(ViaKeyboard(vid=args.kb_vid, pid=args.kb_pid))
        except (RuntimeError, OSError) as e:
            print(f"keyboard: skipped ({e})")
    if not args.no_mouse:
        try:
            devs.append(RazerMouse(pid=args.mouse_pid, leds=args.mouse_leds))
        except (RuntimeError, OSError) as e:
            print(f"mouse: skipped ({e})")
    return devs


def pick_input(device):
    if device is not None:
        return device
    bh = [i for i, d in enumerate(sd.query_devices())
          if "blackhole" in d["name"].lower() and d["max_input_channels"] > 0]
    return bh[0] if bh else None


class Mapper:
    """Turns analyzer features into brightness / hue / spectrum for the devices."""

    def __init__(self, args):
        self.a = args
        self.b_smooth = float(args.floor)
        self.hue_base = float(args.hue)
        self.flash = 0.0
        self.last_jump = 0.0
        self.hue = float(args.hue)

    def step(self, f, dt):
        a = self.a
        m = a.mode
        # --- energy: slow loudness + bass punch (instant vs attenuated) + onset flash
        punch = max(0.0, min(1.0, (f.get("bass", 1.0) - f.get("bass_att", 1.0)) / 0.8))
        self.flash = max(self.flash * math.exp(-dt / 0.12), f["onset_strength"] * 0.8 if f["onset"] else 0.0)
        if m == "pulse":
            energy = f["loud"]
        else:
            energy = 0.65 * f["loud"] + 0.35 * punch + 0.35 * self.flash
        energy = 0.0 if f["silent"] else max(0.0, min(1.0, energy))
        target = a.floor + (a.max_brightness - a.floor) * gamma(energy)
        k = (1 - a.smooth) if target > self.b_smooth else (1 - a.smooth) * 0.5
        self.b_smooth += (target - self.b_smooth) * k
        brightness = int(round(self.b_smooth))

        # --- colour
        if m == "beat":
            if f["onset"] and f["onset_strength"] > 0.15 and (f["t"] - self.last_jump) > 0.25:
                self.hue_base = (self.hue_base + 40 + 30 * f["onset_strength"]) % 256
                self.last_jump = f["t"]
            self.hue = self.hue_base
        elif m == "pulse":
            self.hue = self.hue_base
        else:   # music / meter: slow drift, pushed by timbre and treble energy
            self.hue_base = (self.hue_base + a.hue_rate * dt * (0.3 + f["loud"])) % 256
            shift = 45 * (f["centroid"] - 0.5) + 12 * max(0.0, f.get("treb", 1.0) - f.get("treb_att", 1.0))
            self.hue += ((self.hue_base + shift) % 256 - self.hue) * 0.15 if abs((self.hue_base + shift) % 256 - self.hue) < 128 else 0
            self.hue %= 256
        return brightness, int(self.hue) % 256, energy


def run(args):
    devs = [] if args.dry_run else open_devices(args)
    if not args.dry_run and not devs:
        sys.exit("No devices to drive. Keyboard wired (Fn+5)? Mouse plugged in?")
    for d in devs:
        print(f"{d.name}: {d.label}")
        d.start(args.hue, args.sat)

    dev = pick_input(args.device)
    info = sd.query_devices(dev, "input") if dev is not None else sd.query_devices(kind="input")
    sr, ch = int(info["default_samplerate"]), min(2, int(info["max_input_channels"]))
    print(f"audio: {info['name']} @ {sr} Hz  (mode={args.mode})")

    an = Analyzer(sr)
    latest = {"f": None}
    pending_onset = {"on": False, "strength": 0.0}
    lock = threading.Lock()

    def cb(indata, frames, t, status):
        f = an.feed(indata.copy())
        with lock:
            if f["onset"]:
                pending_onset["on"] = True
                pending_onset["strength"] = max(pending_onset["strength"], f["onset_strength"])
            latest["f"] = f

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    mapper = Mapper(args)
    period = 1.0 / args.fps
    bars = " ▁▂▃▄▅▆▇█"
    last = time.monotonic()
    onsets_seen = 0
    lost, last_retry = [], 0.0
    with sd.InputStream(device=dev, samplerate=sr, channels=ch, blocksize=int(sr / Analyzer.RATE), callback=cb):
        print("Running. Ctrl-C to stop and restore previous lighting.\n")
        while not stop.is_set():
            t0 = time.monotonic()
            with lock:
                f = latest["f"]
                if f is not None:
                    f = dict(f)
                    f["onset"], f["onset_strength"] = pending_onset["on"], pending_onset["strength"]
                    pending_onset["on"], pending_onset["strength"] = False, 0.0
            if f is None:
                time.sleep(period); continue
            dt, last = t0 - last, t0
            b, h, energy = mapper.step(f, dt)
            if f["onset"]:
                onsets_seen += 1
            if args.dry_run:
                n = int(energy * (len(bars) - 1))
                spec = "".join(bars[int(v * (len(bars) - 1))] for v in f["spectrum9"])
                sys.stdout.write(f"\r{bars[n] * 12:<12} loud={f['loud']:4.2f} bass={f.get('bass', 0):4.2f} "
                                 f"bright={b:3d} hue={h:3d} bpm={f['bpm']:5.1f} spec[{spec}] "
                                 f"{'ONSET' if f['onset'] else '     '} t={f['t']:5.1f}\n" if args.verbose else
                                 f"\r{bars[n] * 12:<12} loud={f['loud']:4.2f} bright={b:3d} hue={h:3d} bpm={f['bpm']:5.1f} "
                                 f"spec[{spec}] onsets={onsets_seen} {'ONSET' if f['onset'] else '     '}")
                sys.stdout.flush()
            else:
                for d in list(devs):
                    try:
                        d.update(f, b, h, args.sat, args.mode)
                    except OSError as e:
                        # device unplugged / mode switched / slept: park it and retry later
                        print(f"\n{d.name}: write failed ({e}); will reconnect when it's back")
                        try:
                            d.close()
                        except OSError:
                            pass
                        lost.append((d, d.snap))
                        devs.remove(d)
                if lost and t0 - last_retry > 2.0:
                    last_retry = t0
                    for d, snap in list(lost):
                        try:
                            nd = ViaKeyboard(vid=args.kb_vid, pid=args.kb_pid) if d.name == "keyboard" \
                                else RazerMouse(pid=args.mouse_pid, leds=args.mouse_leds)
                        except (RuntimeError, OSError):
                            continue
                        nd.start(args.hue, args.sat)
                        nd.snap = snap                       # keep the lighting we first saw
                        devs.append(nd)
                        lost.remove((d, snap))
                        print(f"{d.name}: reconnected")
            el = time.monotonic() - t0
            if el < period:
                time.sleep(period - el)

    print("\nStopping.")
    for d in devs:
        try:
            d.restore()
            d.close()
            print(f"{d.name}: previous lighting restored")
        except OSError as e:
            print(f"{d.name}: restore failed ({e})")
    for d, snap in lost:
        # one last try for devices that dropped out mid-session
        try:
            nd = ViaKeyboard(vid=args.kb_vid, pid=args.kb_pid) if d.name == "keyboard" \
                else RazerMouse(pid=args.mouse_pid, leds=args.mouse_leds)
            nd.snap = snap
            nd.restore()
            nd.close()
            print(f"{d.name}: reconnected and previous lighting restored")
        except (RuntimeError, OSError) as e:
            print(f"{d.name}: still unreachable, lighting not restored ({e})")


def probe(args):
    for d in open_devices(args):
        print(f"{d.name}: {d.label}")
        print("   current:", d.snapshot())
        d.close()


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--list-audio", action="store_true")
    p.add_argument("--probe", action="store_true", help="check device links and print current lighting")
    p.add_argument("--dry-run", action="store_true", help="audio only, print a meter")
    p.add_argument("--verbose", action="store_true", help="dry-run: one line per frame with onsets")
    p.add_argument("--device", help="audio input name/index (default: BlackHole if present, else system input)")
    p.add_argument("--no-keyboard", action="store_true")
    p.add_argument("--no-mouse", action="store_true")
    p.add_argument("--kb-vid", type=lambda s: int(s, 0), help="USB vendor id of the VIA keyboard (default: any)")
    p.add_argument("--kb-pid", type=lambda s: int(s, 0), help="USB product id of the VIA keyboard (default: any)")
    p.add_argument("--mouse-pid", type=lambda s: int(s, 0), help="USB product id of the Razer device (default: any)")
    p.add_argument("--mouse-leds", type=int, default=DEFAULT_MOUSE_LEDS,
                   help="addressable LEDs on the Razer device, first two = accent, rest = strip")
    p.add_argument("--mode", choices=["music", "meter", "beat", "pulse"], default="music",
                   help="music: loudness+bass punch+onset flashes, colour follows timbre; "
                        "meter: same, plus 9-band spectrum on the mouse underglow; "
                        "beat: colour jumps on onsets; pulse: loudness only")
    p.add_argument("--hue", type=int, default=170, help="starting hue 0-255 (170 = blue)")
    p.add_argument("--sat", type=int, default=255)
    p.add_argument("--hue-rate", type=float, default=4.0, help="hue drift per second (music mode)")
    p.add_argument("--floor", type=int, default=70, help="minimum brightness when quiet (never fully off)")
    p.add_argument("--max-brightness", type=int, default=255)
    p.add_argument("--smooth", type=float, default=0.35, help="output smoothing 0-1 (higher = slower glide)")
    p.add_argument("--fps", type=float, default=40)
    args = p.parse_args()
    if args.list_audio:
        print(sd.query_devices()); return
    if hid is None and not args.dry_run:
        sys.exit("python 'hidapi' package missing")
    if args.probe:
        probe(args); return
    run(args)


if __name__ == "__main__":
    main()
