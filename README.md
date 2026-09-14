# keyboard-music-lightsync

Music-reactive RGB for a **VIA keyboard** and a **Razer mouse**, driven from
system audio, with no custom firmware and nothing written to device flash.

Tested on macOS with a Womier RD75 Pro (stock VIA firmware, wired) and a
Razer Basilisk V3. Should work with any VIA-enabled QMK keyboard and any
Razer device that speaks the "extended matrix" lighting protocol.

Why this exists: [KeyboardVisualizer](https://github.com/CalcProgrammer1/KeyboardVisualizer)
is the usual answer, but it needs OpenRGB, and OpenRGB only supports QMK boards
running a patched firmware. Vendor QMK forks (Womier, Epomaker, ...) don't have
that, and flashing a tri-mode wireless board is risky. This talks to the stock
firmware through the protocols it already has.

## What it does

| Device | Channel | Control |
|---|---|---|
| VIA keyboard | QMK raw HID, VIA "lighting" channel | whole-board brightness + hue (VIA has no per-key command) |
| Razer mouse | Razer HID feature reports (the protocol OpenRazer documents) | per-LED frames: 9-band spectrum on the underglow, accent LEDs pulse |

Previous effect/brightness/colour are read at start and restored on Ctrl-C.
Only `NOSTORE` / non-EEPROM commands are used, so nothing wears the flash.

## Setup (macOS)

```bash
git clone https://github.com/ronstercodes/keyboard-music-lightsync && cd keyboard-music-lightsync
./setup.sh                              # venv + deps, builds the audio helper
brew install --cask blackhole-2ch       # loopback so the script can hear system audio
sudo killall coreaudiod                 # load the driver without a reboot
```

Keyboard: plug in the USB cable and put it in wired mode (on Womier boards
that's Fn+5; VIA does not answer over 2.4 GHz or Bluetooth).

## Run

```bash
./musicsync.sh                   # music mode on every device it finds
./musicsync.sh --mode meter      # + 9-band spectrum across the mouse underglow
./musicsync.sh --mode beat       # colour jumps on detected beats
./musicsync.sh --mode pulse      # loudness only, fixed colour
./musicsync.sh off               # restore audio output if a run got killed
```

`musicsync.sh` builds a Multi-Output Device (your speakers + BlackHole) so you
still hear the music, starts the sync, and reverts the output on Ctrl-C.
macOS disables the volume keys while a Multi-Output Device is selected, which
is why the wrapper only enables it for the duration of a session.

Direct use, any platform with a loopback input:

```bash
.venv/bin/python music_sync.py --probe                 # show device links + current lighting
.venv/bin/python music_sync.py --dry-run --verbose     # watch the analysis, no devices
.venv/bin/python music_sync.py --device "BlackHole 2ch" --mode music
```

Useful flags: `--hue 0-255` starting colour, `--hue-rate` drift speed,
`--floor` minimum brightness (never fully off), `--smooth` glide (0.2 snappy,
0.5 lazy), `--mouse-leds N` for other Razer devices, `--kb-vid/--kb-pid` to pin
a specific keyboard, `--no-keyboard` / `--no-mouse`.

## How the audio is analysed

The mapping is deliberately modelled on what mature visualizers do rather than
raw volume. All of it is in `Analyzer` and `Mapper` in `music_sync.py`.

1. **Spectrum**: 2048-point Hann-windowed FFT at 50 frames/s, folded into 24
   log-spaced bands (40 Hz to 16 kHz), log magnitude. Log spacing and log
   magnitude are what KeyboardVisualizer and MilkDrop both do so that treble
   doesn't vanish next to bass.
2. **Loudness**: A-weighted power in dB (IEC 61672 curve), then auto-ranged
   between the 8th and 97th percentile of the last 8 s. This is why it works
   at any volume and follows a song's dynamics instead of clipping.
3. **bass / mid / treb**: mean band energy in 20-250 / 250-4000 / 4000+ Hz,
   each divided by its own 3 s running average so 1.0 means "normal for this
   track", plus an attenuated (smoothed) copy. This is MilkDrop's `bass`,
   `bass_att` etc. **Bass punch** = instantaneous minus attenuated, which is
   a kick relative to the groove rather than an absolute level.
4. **Onsets**: spectral flux, i.e. the half-wave-rectified sum of positive
   log-magnitude differences between consecutive frames, peak-picked against
   an adaptive threshold (1.4 x median of the last second + offset) with a
   120 ms refractory period. This is the standard recipe from the onset
   detection literature.
5. **Tempo**: autocorrelation of the onset envelope over the last 6 s,
   searching 60-180 BPM. Locks a synthetic 120 BPM test track within 4 s.
6. **Timbre**: spectral centroid on a log-frequency axis, smoothed, used to
   steer hue (cymbal-heavy passages push one way, bassy sections the other).

Brightness = `0.65 x loudness + 0.35 x bass punch + 0.35 x onset flash`,
gamma-curved, smoothed asymmetrically (fast up, slow down), never below the
floor. Mouse underglow in `meter` mode shows 9 bands with per-band auto-gain
over a 30 dB window and a slow peak decay, like a hardware spectrum analyser.

## Credits and references

This is original code, but almost every idea in it came from somewhere:

- **[OpenRazer](https://github.com/openrazer/openrazer)** (GPL-2.0). The Razer
  USB report format (90-byte packet, transaction id, XOR checksum, class 0x0F
  extended-matrix commands for brightness / static / custom frame) and the
  Basilisk V3 LED layout were learned from `driver/razerchromacommon.c`,
  `driver/razermouse_driver.c` and the daemon's `hardware/mouse.py`. No
  OpenRazer code is copied here; the protocol was re-implemented in Python
  over hidapi feature reports.
- **[QMK](https://github.com/qmk/qmk_firmware) / [VIA](https://www.usevia.app/)**.
  The VIA raw-HID protocol (`id_custom_set_value`, the `rgb_matrix` channel,
  value ids for brightness / effect / speed / color) is from QMK's `via.h`
  and `via.c`.
- **[KeyboardVisualizer](https://github.com/CalcProgrammer1/KeyboardVisualizer)**
  by Adam Honse (GPL-2.0), for the overall idea and its processing choices:
  windowed FFT, log-compressed magnitudes, per-bin normalisation, a first-order
  filter constant for smoothing, peak decay.
- **MilkDrop** by Ryan Geiss. The `bass` / `mid` / `treb` normalisation by
  running average and the attenuated `*_att` copies come from the
  [MilkDrop preset authoring guide](https://www.geisswerks.com/milkdrop/milkdrop_preset_authoring.html).
- **Onset detection**: J. P. Bello, L. Daudet, S. Abdallah, C. Duxbury,
  M. Davies, M. B. Sandler, "A Tutorial on Onset Detection in Music Signals",
  *IEEE Trans. Speech and Audio Processing* 13(5), 2005; S. Dixon, "Onset
  Detection Revisited", *DAFx* 2006 (spectral flux with adaptive peak picking);
  S. Böck and G. Widmer, "Maximum Filter Vibrato Suppression for Onset
  Detection", *DAFx* 2013 (SuperFlux, the log-magnitude variant).
- **A-weighting**: IEC 61672-1 sound level meter standard.
- **[BlackHole](https://github.com/ExistentialAudio/BlackHole)** by Existential
  Audio (GPL-3.0), the macOS loopback driver this relies on (external
  dependency, not bundled).
- Libraries: [hidapi](https://github.com/libusb/hidapi) via
  [python-hidapi](https://github.com/trezor/cython-hidapi),
  [python-sounddevice](https://python-sounddevice.readthedocs.io/),
  [NumPy](https://numpy.org/).

Vendor firmware sources that were read (not used) to confirm what the stock
firmware can do: [womierkeyboard/RD75](https://github.com/womierkeyboard/RD75),
[FirmwareLeaks/Womier_RD75](https://github.com/FirmwareLeaks/Womier_RD75).

## Limitations

- VIA gives whole-board control only. Per-key spectrum bars on the keyboard
  would need custom firmware (see [Kasper24/QMK-OpenRGB](https://github.com/Kasper24/QMK-OpenRGB)).
- Keyboard must be on the cable. Wireless dongles that mirror the raw-HID
  descriptor do not forward VIA.
- Razer LED index order is assumed from OpenRazer's Basilisk V3 definition;
  other devices may need `--mouse-leds` and a different accent/strip split.
- The audio helper and wrapper are macOS-only (CoreAudio). `music_sync.py`
  itself is cross-platform given any loopback input device.

## License

MIT. See `LICENSE`. Protocol knowledge credited above comes from GPL projects;
none of their code is included.
