# -*- coding: utf-8 -*-
"""
ewbs_encoder.py
===============

EWBS encoder prototype (transmitter side), with a Tkinter GUI.

What this program does
-----------------------
1. The user picks a WAV file representing the "broadcast program" - the
   regular programming into which the alarm signal will be embedded (much
   like a real broadcaster sending the EWBS tone ahead of a spoken
   announcement).
2. The GUI exposes every relevant EWBS parameter: start/end signal,
   category (type I earthquake/evacuation or type II tsunami), area code
   (all 47 prefectures + wide-area regions + nationwide), date/time
   (manual or automatic = current system time), number of block
   repetitions, and the alarm tone's amplitude.
3. On button press, this is turned into an FSK alarm signal following the
   bit- and tone-level specification implemented in ewbs_common.py
   (derived from the official Japanese regulation).
4. The resulting alarm signal can be:
     a) written into a NEW WAV file (alarm + program combined, according
        to the chosen insertion mode), or
     b) played back live through a chosen sound-card output - e.g. to feed
        an external FM/AM modulator's audio input for over-the-air testing.

Dependencies: numpy, sounddevice (pip install numpy sounddevice), and
Tkinter (on Debian/Ubuntu: `sudo apt install python3-tk`; usually already
bundled with Windows/macOS Python).
"""

import os
import wave
import threading
import datetime as dt

import numpy as np

try:
    import sounddevice as sd
    HAVE_SOUNDDEVICE = True
except Exception:
    # If sounddevice/PortAudio isn't installed/available, the program
    # should still start - only the live-output option gets disabled.
    HAVE_SOUNDDEVICE = False

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import ewbs_common as ewbs


# ---------------------------------------------------------------------------
# WAV I/O helpers
# ---------------------------------------------------------------------------

def load_wav_as_float(path: str):
    """Loads a WAV file, returning (samples_float32, sample_rate, n_channels).
    Samples are normalized to float32 in the range [-1, 1]. Multi-channel
    files stay interleaved (shape: (n_frames, n_channels)).
    """
    with wave.open(path, "rb") as wf:
        n_channels = wf.getnchannels()
        sample_rate = wf.getframerate()
        sample_width = wf.getsampwidth()
        n_frames = wf.getnframes()
        raw = wf.readframes(n_frames)

    if sample_width == 2:
        data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif sample_width == 1:
        # 8-bit WAV is unsigned
        data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif sample_width == 4:
        data = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"Unsupported sample width: {sample_width} bytes")

    if n_channels > 1:
        data = data.reshape(-1, n_channels)

    return data, sample_rate, n_channels


def save_wav_from_float(path: str, samples: np.ndarray, sample_rate: int, n_channels: int):
    """Writes float32 samples (range -1..1) as a 16-bit PCM WAV file."""
    clipped = np.clip(samples, -1.0, 1.0)
    int_samples = (clipped * 32767.0).astype(np.int16)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(n_channels)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(int_samples.tobytes())


def make_mono_like(tone_mono: np.ndarray, n_channels: int) -> np.ndarray:
    """Duplicates a mono alarm signal across n_channels so it matches a
    possibly multi-channel background program (e.g. stereo)."""
    if n_channels == 1:
        return tone_mono
    return np.tile(tone_mono.reshape(-1, 1), (1, n_channels))


# ---------------------------------------------------------------------------
# Core function: generate the alarm signal and embed it into the program
# ---------------------------------------------------------------------------

def apply_alarm_to_program(program_samples, sample_rate, n_channels,
                            alarm_tone_mono, insert_mode, insert_pos_s):
    """Embeds the alarm tone into the program signal.

    insert_mode:
      "prepend"  - the alarm is placed BEFORE the program (the classic
                   case: the radio wakes up, hears the alarm tone first,
                   then the announcement/program).
      "insert"   - the alarm is INSERTED at insert_pos_s (shifts the
                   remaining program later; nothing of the program is
                   lost, but the tone and program never overlap -
                   realistic for "tone briefly interrupts the broadcast").
      "overlay"  - the alarm is MIXED into the program at insert_pos_s
                   (added), and the program keeps playing underneath. In
                   practice EWBS is usually not overlaid but replaces/
                   precedes the program - this option is still useful for
                   testing decoders under realistic "music was still
                   playing in the background" conditions.
    """
    alarm = make_mono_like(alarm_tone_mono, n_channels)
    pos_samples = round(insert_pos_s * sample_rate)
    pos_samples = max(0, min(pos_samples, len(program_samples)))

    if insert_mode == "prepend":
        return np.concatenate([alarm, program_samples], axis=0)

    if insert_mode == "insert":
        before = program_samples[:pos_samples]
        after = program_samples[pos_samples:]
        return np.concatenate([before, alarm, after], axis=0)

    if insert_mode == "overlay":
        result = program_samples.copy()
        end = pos_samples + len(alarm)
        if end > len(result):
            # program is too short -> pad the end with silence
            pad_shape = (end - len(result),) if result.ndim == 1 else (end - len(result), n_channels)
            result = np.concatenate([result, np.zeros(pad_shape, dtype=np.float32)], axis=0)
        result[pos_samples:end] = result[pos_samples:end] + alarm
        return result

    raise ValueError(f"Unknown insert_mode: {insert_mode}")


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class EncoderApp:
    def __init__(self, root):
        self.root = root
        self.root.title("EWBS Encoder - Prototype (Emergency Warning Broadcasting System)")
        self.root.geometry("640x760")

        self.program_path = tk.StringVar()
        self.program_samples = None
        self.program_sr = None
        self.program_channels = None

        self._build_ui()

    # -- UI construction ----------------------------------------------------

    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        # --- File selection -----------------------------------------------
        frm_file = ttk.LabelFrame(self.root, text="1) Broadcast program (WAV)")
        frm_file.pack(fill="x", **pad)
        ttk.Entry(frm_file, textvariable=self.program_path, width=55).pack(side="left", padx=6, pady=6)
        ttk.Button(frm_file, text="Browse...", command=self.choose_wav).pack(side="left", padx=6)

        # --- Signal type -----------------------------------------------
        frm_type = ttk.LabelFrame(self.root, text="2) Signal type")
        frm_type.pack(fill="x", **pad)

        self.var_is_start = tk.StringVar(value="start")
        ttk.Radiobutton(frm_type, text="Start signal (triggers wake-up/alarm)",
                         variable=self.var_is_start, value="start",
                         command=self._on_signal_kind_changed).pack(anchor="w")
        ttk.Radiobutton(frm_type, text="End signal (clears the alarm state)",
                         variable=self.var_is_start, value="end",
                         command=self._on_signal_kind_changed).pack(anchor="w")

        self.var_category = tk.StringVar(value="1")
        frm_cat = ttk.Frame(frm_type)
        frm_cat.pack(anchor="w", pady=(4, 0))
        ttk.Label(frm_cat, text="Category (only relevant for start signals):").pack(side="left")
        self.rb_cat1 = ttk.Radiobutton(frm_cat, text="Type I - Earthquake / evacuation instruction",
                                        variable=self.var_category, value="1")
        self.rb_cat1.pack(side="left", padx=4)
        self.rb_cat2 = ttk.Radiobutton(frm_cat, text="Type II - Tsunami warning",
                                        variable=self.var_category, value="2")
        self.rb_cat2.pack(side="left", padx=4)

        # --- Area -----------------------------------------------
        frm_area = ttk.LabelFrame(self.root, text="3) Area code (地域符号)")
        frm_area.pack(fill="x", **pad)
        self.var_area = tk.StringVar(value="Nationwide (地域共通符号)")
        area_names = list(ewbs.AREA_CODES.keys())
        cb_area = ttk.Combobox(frm_area, textvariable=self.var_area, values=area_names,
                                width=45, state="readonly")
        cb_area.pack(padx=6, pady=6, anchor="w")

        # --- Date/time -----------------------------------------------
        frm_dt = ttk.LabelFrame(self.root, text="4) Date / time")
        frm_dt.pack(fill="x", **pad)

        self.var_auto_time = tk.BooleanVar(value=True)
        ttk.Checkbutton(frm_dt, text="Use current system time automatically",
                         variable=self.var_auto_time,
                         command=self._on_auto_time_toggled).pack(anchor="w", padx=6)

        grid = ttk.Frame(frm_dt)
        grid.pack(anchor="w", padx=6, pady=4)

        now = dt.datetime.now()
        self.var_day = tk.IntVar(value=now.day)
        self.var_month = tk.IntVar(value=now.month)
        self.var_hour = tk.IntVar(value=now.hour)
        self.var_year = tk.IntVar(value=now.year)

        ttk.Label(grid, text="Day:").grid(row=0, column=0, sticky="w")
        self.sp_day = ttk.Spinbox(grid, from_=1, to=31, textvariable=self.var_day, width=5)
        self.sp_day.grid(row=0, column=1, padx=4)

        ttk.Label(grid, text="Month:").grid(row=0, column=2, sticky="w")
        self.sp_month = ttk.Spinbox(grid, from_=1, to=12, textvariable=self.var_month, width=5)
        self.sp_month.grid(row=0, column=3, padx=4)

        ttk.Label(grid, text="Hour:").grid(row=0, column=4, sticky="w")
        self.sp_hour = ttk.Spinbox(grid, from_=0, to=23, textvariable=self.var_hour, width=5)
        self.sp_hour.grid(row=0, column=5, padx=4)

        ttk.Label(grid, text="Year:").grid(row=0, column=6, sticky="w")
        self.sp_year = ttk.Spinbox(grid, from_=1985, to=2099, textvariable=self.var_year, width=7)
        self.sp_year.grid(row=0, column=7, padx=4)

        self._on_auto_time_toggled()  # initial state (spinboxes disabled)

        # --- Repetitions / amplitude -----------------------------------------------
        frm_sig = ttk.LabelFrame(self.root, text="5) Signal parameters")
        frm_sig.pack(fill="x", **pad)

        row1 = ttk.Frame(frm_sig)
        row1.pack(anchor="w", padx=6, pady=2, fill="x")
        ttk.Label(row1, text="Block repetitions:").pack(side="left")
        self.var_repeats = tk.IntVar(value=ewbs.START_REPEATS_DEFAULT)
        self.sp_repeats = ttk.Spinbox(row1, from_=1, to=20, textvariable=self.var_repeats, width=5)
        self.sp_repeats.pack(side="left", padx=6)
        ttk.Label(row1, text="(spec: start 4-10, end 2-4)").pack(side="left")

        row2 = ttk.Frame(frm_sig)
        row2.pack(anchor="w", padx=6, pady=2, fill="x")
        ttk.Label(row2, text="Alarm tone amplitude:").pack(side="left")
        self.var_amp = tk.DoubleVar(value=0.85)
        ttk.Scale(row2, from_=0.05, to=1.0, orient="horizontal",
                  variable=self.var_amp, length=200).pack(side="left", padx=6)

        # --- Insertion mode -----------------------------------------------
        frm_ins = ttk.LabelFrame(self.root, text="6) Insertion mode into the program")
        frm_ins.pack(fill="x", **pad)
        self.var_insert_mode = tk.StringVar(value="prepend")
        ttk.Radiobutton(frm_ins, text="Place before the program (classic: alarm tone, then broadcast)",
                         variable=self.var_insert_mode, value="prepend").pack(anchor="w")
        row_pos = ttk.Frame(frm_ins)
        row_pos.pack(anchor="w", fill="x")
        ttk.Radiobutton(row_pos, text="Insert at position (sec.) (shifts the program):",
                         variable=self.var_insert_mode, value="insert").pack(side="left")
        self.var_pos = tk.DoubleVar(value=0.0)
        ttk.Entry(row_pos, textvariable=self.var_pos, width=8).pack(side="left", padx=6)
        row_pos2 = ttk.Frame(frm_ins)
        row_pos2.pack(anchor="w", fill="x")
        ttk.Radiobutton(row_pos2, text="Overlay at position (sec.) (mix, program keeps playing):",
                         variable=self.var_insert_mode, value="overlay").pack(side="left")

        # --- Output -----------------------------------------------
        frm_out = ttk.LabelFrame(self.root, text="7) Output")
        frm_out.pack(fill="x", **pad)
        self.var_output_mode = tk.StringVar(value="file")
        ttk.Radiobutton(frm_out, text="Save to a new WAV file",
                         variable=self.var_output_mode, value="file").pack(anchor="w")

        row_dev = ttk.Frame(frm_out)
        row_dev.pack(anchor="w", fill="x")
        ttk.Radiobutton(row_dev, text="Play live through sound card (e.g. into an FM/AM modulator):",
                         variable=self.var_output_mode, value="device",
                         state=("normal" if HAVE_SOUNDDEVICE else "disabled")).pack(side="left")

        self.device_names = []
        self.device_indices = []
        if HAVE_SOUNDDEVICE:
            for idx, d in enumerate(sd.query_devices()):
                if d["max_output_channels"] > 0:
                    self.device_names.append(f"[{idx}] {d['name']}")
                    self.device_indices.append(idx)
        else:
            self.device_names = ["sounddevice/PortAudio not available"]

        self.var_device = tk.StringVar(value=self.device_names[0] if self.device_names else "")
        cb_dev = ttk.Combobox(frm_out, textvariable=self.var_device, values=self.device_names,
                               width=50, state="readonly" if HAVE_SOUNDDEVICE else "disabled")
        cb_dev.pack(anchor="w", padx=6, pady=4)

        # --- Action -----------------------------------------------
        ttk.Button(self.root, text="Encode & apply alarm",
                   command=self.run_encode).pack(pady=10)

        self.status = tk.StringVar(value="Ready.")
        ttk.Label(self.root, textvariable=self.status, foreground="blue",
                  wraplength=600, justify="left").pack(fill="x", padx=8, pady=4)

    # -- Callback helpers -----------------------------------------------

    def _on_signal_kind_changed(self):
        is_start = self.var_is_start.get() == "start"
        state = "normal" if is_start else "disabled"
        self.rb_cat1.configure(state=state)
        self.rb_cat2.configure(state=state)
        # adopt the official repetition-count default for the chosen kind
        self.var_repeats.set(ewbs.START_REPEATS_DEFAULT if is_start else ewbs.END_REPEATS_DEFAULT)

    def _on_auto_time_toggled(self):
        state = "disabled" if self.var_auto_time.get() else "normal"
        for w in (self.sp_day, self.sp_month, self.sp_hour, self.sp_year):
            w.configure(state=state)

    def choose_wav(self):
        path = filedialog.askopenfilename(
            title="Select broadcast program (WAV)",
            filetypes=[("WAV files", "*.wav"), ("All files", "*.*")]
        )
        if not path:
            return
        try:
            samples, sr, ch = load_wav_as_float(path)
        except Exception as ex:
            messagebox.showerror("Error loading file", f"Could not load WAV file:\n{ex}")
            return
        self.program_path.set(path)
        self.program_samples = samples
        self.program_sr = sr
        self.program_channels = ch
        self.status.set(
            f"Program loaded: {os.path.basename(path)}  "
            f"({sr} Hz, {ch} channel(s), {len(samples) / sr:.1f} s)"
        )

    # -- Main action -----------------------------------------------

    def run_encode(self):
        if self.program_samples is None:
            messagebox.showwarning("No program loaded",
                                    "Please select a WAV file as the broadcast program first.")
            return

        try:
            is_start = self.var_is_start.get() == "start"
            signal_type = int(self.var_category.get())
            area_name = self.var_area.get()

            if self.var_auto_time.get():
                now = dt.datetime.now()
                day, month, hour, year = now.day, now.month, now.hour, now.year
            else:
                day, month, hour, year = (self.var_day.get(), self.var_month.get(),
                                           self.var_hour.get(), self.var_year.get())

            repeats = int(self.var_repeats.get())
            amplitude = float(self.var_amp.get())
            insert_mode = self.var_insert_mode.get()
            insert_pos_s = float(self.var_pos.get())

            sr = self.program_sr

            # --- Generate the alarm tone per the EWBS specification ---
            alarm_mono = ewbs.build_full_signal(
                is_start=is_start, signal_type=signal_type, area_name=area_name,
                day=day, month=month, hour=hour, year=year,
                repeats=repeats, sample_rate=sr,
            ) * amplitude

            # --- Embed it into the program ---
            result = apply_alarm_to_program(
                self.program_samples, sr, self.program_channels,
                alarm_mono, insert_mode, insert_pos_s
            )

            block_bits = ewbs.build_block(is_start, signal_type, area_name, day, month, hour, year)

        except Exception as ex:
            messagebox.showerror("Encoding error", str(ex))
            return

        info = (
            f"Signal: {'START' if is_start else 'END'}"
            + (f" (type {signal_type})" if is_start else "")
            + f" | Area: {area_name} | {day:02d}.{month:02d}.{year} {hour:02d}:00\n"
            f"Block bits: {block_bits}\n"
            f"Alarm duration: {len(alarm_mono) / sr:.2f} s with {repeats} repetitions"
        )

        if self.var_output_mode.get() == "file":
            out_path = filedialog.asksaveasfilename(
                title="Choose output file", defaultextension=".wav",
                filetypes=[("WAV files", "*.wav")]
            )
            if not out_path:
                return
            save_wav_from_float(out_path, result, sr, self.program_channels)
            self.status.set(f"Saved: {out_path}\n\n{info}")
            messagebox.showinfo("Done", f"WAV file written:\n{out_path}")
        else:
            if not HAVE_SOUNDDEVICE:
                messagebox.showerror("Not available",
                                      "sounddevice/PortAudio is not installed on this system.")
                return
            sel = self.var_device.get()
            dev_index = None
            for name, idx in zip(self.device_names, self.device_indices):
                if name == sel:
                    dev_index = idx
                    break
            self.status.set(f"Playing through device: {sel}\n\n{info}")

            def _play():
                try:
                    sd.play(result, sr, device=dev_index)
                    sd.wait()
                except Exception as ex:
                    messagebox.showerror("Playback error", str(ex))

            threading.Thread(target=_play, daemon=True).start()


def main():
    root = tk.Tk()
    EncoderApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
