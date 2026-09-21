# EWBS Python Prototype

A small, license-free encoder/decoder prototype for Japan's **Emergency
Warning Broadcasting System (EWBS / 緊急警報放送)** - the analog-radio alarm
signaling scheme that has reliably woken up ordinary AM and FM radios from
standby since 1985, with no proprietary chipset, no licensing fees, and no
"next-generation" replacement needed every few years.

If you've ever wondered why newer digital broadcast standards (DAB+'s EWF,
now ASA) keep reinventing emergency alerting from scratch every decade or
so, this repository is a demonstration that a remarkably simple, purely
tone-based FSK protocol from the 1980s still does the job perfectly well
today - decoded here with nothing more exotic than a Goertzel filter running.

![main](https://github.com/radiolab81/EWBS_JP/blob/main/images/protocol-structure.png)


## What's in here

- **`ewbs_encoder.py`** - a Tkinter GUI that lets you pick a WAV file (your
  "broadcast program"), configure any EWBS alarm (start/end signal, type I
  earthquake or type II tsunami, area code, date/time, repetitions), and
  embed the resulting FSK tone into the program audio - either as a new WAV
  file or live through a sound card output (e.g. into an FM/AM modulator).

  ![enc](https://github.com/radiolab81/EWBS_JP/blob/main/images/encoder.png)

  
- **`ewbs_decoder.py`** - a Tkinter GUI that monitors a WAV file or a live
  audio input (line-in/microphone) for the EWBS tone and reacts the way a
  real receiver historically would: waking up, unmuting, and (since most
  real receivers had no display) additionally showing the fully decoded
  fields for protocol verification during development.

  ![dec1](https://github.com/radiolab81/EWBS_JP/blob/main/images/decoder_alert.png)

  ![dec2](https://github.com/radiolab81/EWBS_JP/blob/main/images/decoder_endsignal.png)
  
- **`ewbs_common.py`** - the shared protocol library: bit-exact tone
  generation/detection, the full area-code table (47 prefectures + wide-area + nationwide codes), date/time encoding, and two decoder strictness levels
  (a tolerant scanner that reliably recovers the area code even from noisy
  real-world recordings, and a strict parser that fully verifies an entire
  block).

## Status: verified against real recordings

This isn't just a synthetic round-trip test. The decoder has been checked
against several genuine EWBS recordings, and in each case the decoded
output matches independently known facts about the recording:

| Recording | Decoded result | Known fact |
|---|---|---|
| NHK Osaka monthly test broadcast (Dec 1, 2005) | End signal, Kinki region, Dec 1, hour 11/12 | Broadcast at 11:59 JST from Osaka (Kinki region) - the hour rollover across the two alternating "odd/even" blocks matches the minute-59 boundary exactly |
| Real Type-II (tsunami) start signal, Sept 2004 | Type II start, nationwide, day 4/6 Sept, hour 23/0 | Broadcast during the real earthquakes off the Kii peninsula on Sept 5-6, 2004, spanning midnight |
| Three EWBS demo sounds (English Wikipedia) | Start/end signals with plausible area/date/time | Match the expected signal types |

See the source comments in `ewbs_common.py` for the exact protocol
derivation and sources.

## Requirements

```
pip install numpy sounddevice
```

Tkinter is part of the Python standard library on Windows/macOS; on
Debian/Ubuntu, install it separately:

```
sudo apt install python3-tk libportaudio2 portaudio19-dev
```

## Using virtual python environment: 

```
sudo apt update
sudo apt install libportaudio2 portaudio19-dev python3-tk python3-venv
python3 -m venv ewbs_venv
source ewbs_venv/bin/activate
```

## Usage

```
python3 ewbs_encoder.py
python3 ewbs_decoder.py
```


1. In the encoder, pick a WAV file, configure the alarm, and either save a
   new WAV file or play it live through a chosen sound device/AM or FM modulator. 
2. In the decoder, point it at that WAV file (or a live line-in/microphone
   input [from a real radio]) and start monitoring. A message box appears when a start or end
   signal is recognized.

By default the decoder favors **speed over exhaustive analysis** - a real
warning system must react quickly. An optional "calibrate bit phase"
checkbox trades a few seconds of startup latency for more reliable decoding
of heavily degraded, off-air recordings (see the docstring of
`find_best_bit_phase()` in `ewbs_common.py`).

## Known open point

One area code (Iwate prefecture, 岩手県) differs by a few bits between our
transcription of the government notification and the value used in an
independently field-tested reference decoder (see `AREA_CODES` in
`ewbs_common.py`). We use the field-tested value; this could be settled
conclusively with an Iwate-specific real recording.

## Sources

- Ministry of Posts and Telecommunications Notification No. 405 (1985),
  "Structure of the Emergency Warning Signal under Article 9-3 Item 5 of the
  Radio Equipment Regulations" (published at tele.soumu.go.jp) - the
  authoritative bit-level specification.
- NHK STRL / Kazuyoshi Shogen et al., "Implementation of Emergency Warning
  Broadcasting System in the Asia Pacific Region", ITU/ESCAP Disaster
  Communications Workshop, Bangkok 2006 - block structure and FSK
  parameters.
- [ingen084/EwsDemodulator](https://github.com/ingen084/EwsDemodulator) - an
  independently developed C# decoder, field-tested against real broadcasts
  (including March 11, 2011). This was the key reference that resolved the
  exact date/time field layout used here.
- [radio1ban.com](https://radio1ban.com/jikken_ews1/) - a Japanese
  amateur-radio site with real off-air EWBS recordings and an independent
  write-up of the block structure.
- The English Wikipedia article ["Emergency Warning Broadcast System
  (Japan)"](https://en.wikipedia.org/wiki/Emergency_Warning_Broadcast_System_(Japan))
  for demo sound files used in testing.

