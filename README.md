# AIS Station (receive-only)

Turns your VHF radios into a **receive-only AIS station**. It tunes the radios to the AIS channels over CAT and decodes the 9600-baud AIS signal from their audio output. Ships appear on a live chart and in a list (name, MMSI, call sign, type, position, speed, course, destination, ETA, range and bearing from your station). The data is also fed to OpenCPN or other chart software as standard NMEA.

**This program never transmits.** It only sets frequency and FM mode, has no PTT or transmit commands, and leaves the RTS/DTR serial lines off by default so interfaces that use them for PTT stay unkeyed. AIS transmissions must come from type-approved AIS equipment.

## Supported radios

| Radio | Control | Audio into the computer |
|---|---|---|
| **Icom IC-9100** | CI-V over USB (default address `0x7C`) | Built-in USB audio codec, or the DATA jack's 9600 output |
| **Yaesu FT-991A** | CAT over USB (Enhanced COM port) | Built-in USB audio codec, or the DATA jack's 9600 output |
| **Yaesu FTM-510DR** | Tune by hand (no full CAT) | DATA jack (9600) into a sound card |
| **Kenwood** (FA/MD ASCII CAT) | CAT over serial/USB | DATA jack or USB audio |
| Any AIS receiver with NMEA output | `nmea_in_serial` / `nmea_in_udp` | — |

The AIS channels are **AIS 1 / 87B = 161.975 MHz** and **AIS 2 / 88B = 162.025 MHz**, both FM. Each radio can stay on one channel or **scan** between them. If you have two radios, put one on each channel and you'll hear everything.

### Getting good audio (important)
AIS is 9600-baud data. The **speaker or headphone jack usually filters the audio too much** for reliable decoding. Use the radio's **9600 data output**: the DATA jack's 9600 pin, or the USB audio codec with the radio set for 9600-bps data. Check your radio manual's data/packet settings for 9600 bps. Then:
1. Run `python aisrx.py --list-audio` to find the radio's sound input.
2. Set the computer's input level so the meter in the display sits around **-20 to -10 dBFS** on a busy channel. Turn off any "audio enhancements" or automatic gain on that input.
3. Make sure your radio's receive range covers 162 MHz. Some regional versions are restricted.

A **€25–30 RTL-SDR dongle** running a decoder such as AIS-catcher also works well. It can listen to both channels at once and feed this station over UDP (`nmea_in_udp`).

Your **Icom IC-M330** has no AIS receiver as far as I know, so it doesn't feed this station. If you later add a marine AIS receiver or transponder, connect its NMEA output (usually 38400 baud) with `nmea_in_serial` and its targets will appear too.

## Install and run
Requires Python 3.9+.
```bash
pip install -r requirements.txt
python aisrx.py --demo          # try it with simulated ships, no radio needed
python aisrx.py --list-audio    # find your radio's sound input
python aisrx.py                 # first run creates config.json; edit it, then run again
```
The display opens at `http://127.0.0.1:8100`. Add `--lan` to view it from other computers on your network.

### config.json
```json
"station": { "name": "8PFX AIS MONITOR", "lat": 13.0975, "lon": -59.6167 }
```
Set `lat`/`lon` to your antenna's position; range and bearing are calculated from it. The example coordinates are only a placeholder.

Each entry in `receivers`:
| Key | Meaning |
|---|---|
| `rig` | `icom`, `yaesu`, `kenwood` or `manual` |
| `port`, `baud` | CAT serial port (e.g. `COM5`, `/dev/ttyUSB0`) and speed; must match the radio's CI-V / CAT RATE menu |
| `civ_address` | Icom only (IC-9100: `0x7C`) |
| `stopbits`, `rts`, `dtr` | Serial options. Leave `rts`/`dtr` unset unless your interface needs them for CAT |
| `audio_device` | Name (or number from `--list-audio`) of the sound input |
| `audio_channel` | 0 = left, 1 = right (some USB codecs carry data on one side) |
| `channel` | `A` (161.975), `B` (162.025) or `SCAN`; plus `scan_seconds` |

Outputs:
- **`nmea_udp`:** chart programs listening on UDP. OpenCPN: add a network connection, UDP, port 10110.
- **`nmea_tcp_port`:** a TCP server other programs can connect to.
- **`log_file`:** every sentence is saved, so you can replay it later.

### Decode a recording
`python aisrx.py --wav capture.wav` decodes a recording of the radio's 9600 audio. This is useful for checking levels.

## How it decodes
Audio → low-pass filter → clock recovery locked to the 9600-baud bit timing → NRZI decoding → HDLC framing with bit-unstuffing → CRC-16 check → AIS message decoding. Message types 1–5, 11, 18, 19, 21, 24 and 27 are decoded. Polarity doesn't matter, and 48 kHz or 44.1 kHz sound cards both work.

Run the tests with `python tests/test_pipeline.py`. They synthesize AIS bursts and check they decode correctly. In testing, decoding was 100% reliable down to about 8.5 dB signal-to-noise.

## Ready-made apps
Pushing to GitHub builds Windows, macOS and Linux executables (see `.github/workflows/build.yml`). Download them from the run's **Artifacts**, or push a `v1.0.0` tag to publish a release.

## License
MIT
