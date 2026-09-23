# EWBS Decoder — ATmega328P Proof of Concept

A bare-metal (pure avr-gcc/avr-libc, no Arduino core) feasibility port of
the decoder onto an ATmega328P, e.g. a stock Arduino Nano board used purely
as a cheap breakout board.

## Result

```
Program:    2562 bytes (7.8% of 32 KB flash)
Data:         27 bytes (1.3% of 2 KB SRAM)
```

Comfortably fits an ATmega328P with enormous headroom left for a display,
more area codes' worth of logic, a second decoder instance, etc.

## Hardware

```
Audio in --[analog low-pass, ~1.5-2 kHz corner]--+
                                                  |
Vcc --[R]--+--[R]-- GND    (bias to Vcc/2)  ------+---> ADC0 (A0 / PC0)

PB5 (D13, Nano onboard LED) -----------------------> alarm LED
PD0/PD1 (Nano onboard USB-serial) -----------------> 9600 8N1 console
```

- **ADC0 (A0)**: audio input, AC-coupled, biased to the middle of the ADC's
  input range by a resistor divider (as you described), preceded by a
  simple analog low-pass filter. The filter does not need to be sharp -
  see the sample-rate discussion below for why a 1-2 pole RC/Sallen-Key
  filter with a corner around 1.5-2 kHz is plenty.
- **PB5 / D13**: alarm LED. Lights on a decoded start signal, turns off on
  a decoded end signal. This is the Arduino Nano's onboard LED, so the
  very first bring-up test needs zero extra wiring.
- **USART0 (9600 8N1)**: human-readable status/alarm lines, e.g.:
  ```
  EWBS decoder PoC - ATmega328P, fs=4096Hz, N=64 samples/bit
  Waiting for signal...
  ALARM START area_idx=18 area_code=0xAAC fixed=0x0E6D
  INFO date(start) day=05 month=09 even_block=0
  INFO time(start) hour=00 year_cycle_idx=9
  CLEAR END area_idx=18 area_code=0xAAC
  ```
  `area_idx` is the index into the area table documented at the top of
  `main.c` and in `ewbs_common.py` (the PC-side source of truth) - cross
  reference it there to get the human-readable prefecture/region name.

## Why 4096 Hz sampling

One EWBS bit period is exactly 15.625 ms. Sampling at **4096 Hz** gives
**exactly 64 samples per bit** (no rounding at all), and both target tones
land exactly on a Goertzel bin for that window length:

```
k(640 Hz)  = 64 * 640  / 4096 = 10   (matches the spec's "10 cycles/bit")
k(1024 Hz) = 64 * 1024 / 4096 = 16   (matches the spec's "16 cycles/bit")
```

Integer bins mean no spectral leakage from the window length itself. As a
bonus, the Goertzel coefficient for k/N = 1/4 (the 1024 Hz "mark" tone
here) is `2*cos(pi/2) = 0` exactly, so that tone's detector doesn't need a
multiply at all. Nyquist margin is 2x versus the 1024 Hz tone, which is
comfortable for a simple analog anti-alias filter.

This is about as low a sample rate as you can reasonably go without
resorting to band-pass/undersampling tricks - which is exactly what was
asked for, and it happens to also be the mathematically cleanest choice.

## Building

Requires `gcc-avr`, `avr-libc`, `binutils-avr` (Debian/Ubuntu:
`sudo apt install gcc-avr avr-libc binutils-avr`) and `avrdude` for
flashing.

```
make            # builds main.hex, prints a flash/RAM usage report
make flash      # uploads via avrdude (edit PORT/BAUD in the Makefile
                # or override on the command line, e.g.:
                # make flash PORT=/dev/ttyUSB0 BAUD=57600)
make clean
```

No Arduino IDE, no Arduino core/libraries - just avr-gcc, avr-libc, and
avrdude for the upload step (avrdude is what the Arduino IDE itself uses
under the hood to talk to the Nano's bootloader, it's not an Arduino-core
dependency).

## Field test

This firmware has been validated end-to-end through a real FM and AM
modulator/receiver chain against four different recordings, including a
real, decades-old off-air tsunami-warning broadcast, with zero false
triggers.


## Tuning notes

- **`MIN_TONE_POWER`** in `main.c` is a raw Goertzel-power threshold used
  to reject silence/noise as "no tone" (mirroring `min_power` in the PC
  prototype's `ewbs_common.samples_to_bit()`). Its correct value depends
  on your analog front end's gain and the ADC's actual signal swing -
  start conservative and adjust based on real measurements (e.g. print
  raw Goertzel power values over UART temporarily while feeding a known
  test tone in).
- The **AC bias / half-swing resistor divider** you described is exactly
  what `sample = (int16_t)ADC - 512;` in `ISR(ADC_vect)` assumes: a
  signal centered on roughly half of the ADC's full-scale reading. If
  your bias point isn't exactly Vcc/2, adjust that constant to match your
  actual DC operating point (or measure and subtract a running DC
  estimate instead of a fixed constant, for extra robustness against
  component tolerances/drift).
- This firmware only implements the **tolerant subframe scanner**
  (equivalent to `scan_subframes()`/`interpret_payload()` in
  `ewbs_common.py`), not the strict full-block verifier - it reacts on
  the area field alone (the field most robust against bit errors) and
  reports date/time opportunistically whenever a nearby subframe happens
  to decode. This matches the PC prototype's deliberate speed-over-
  completeness design choice for a warning system (see the main project
  README).

## Known limitations / next steps

- No bit-error tolerance yet (an exact 16-bit match is required for the
  fixed code) - the PC prototype's majority-vote-over-repetitions idea
  from earlier discussion isn't ported yet.
- No automatic bit-phase calibration (again mirroring the PC decoder's
  fast-by-default path, not its optional calibration mode) - for a
  cleanly generated tone (e.g. from `ewbs_encoder.py` or a real broadcast
  received cleanly) the ADC's free-running 4096 Hz grid should lock on
  well within the first block regardless, since bit errors just cause a
  resync rather than a persistent misalignment.
- No sleep/power-down mode between samples - the MCU currently just spins
  in `main()`'s empty loop. For a battery-powered design, an idle sleep
  mode woken by the ADC/timer interrupts would cut power draw
  significantly with no logic changes needed.
