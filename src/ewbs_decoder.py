# -*- coding: utf-8 -*-
"""
ewbs_decoder.py
===============

EWBS decoder prototype (receiver side), with a Tkinter GUI.

What this program does
-----------------------
It continuously monitors an audio signal - either a WAV file (e.g. one
produced by ewbs_encoder.py) or a live audio input (line-in/microphone,
e.g. the audio output of a real FM/AM radio connected via cable to the
sound card) - for the EWBS tone signal (640 Hz / 1024 Hz FSK, 64 bit/s,
see ewbs_common.py).

As soon as a valid start or end block is recognized, the program reacts
the way historical EWBS receivers did:

  What real EWBS radios historically did (per the 2006 NHK/ITU
  presentation and other sources referenced in ewbs_common.py):
  -----------------------------------------------------------------------
  - The vast majority of classic EWBS consumer devices ("conventional
    receivers", often with a built-in clock or a simple on/off standby
    switch, priced around $60-130 at the time) had NO display and NO way
    to show the decoded content (area, category, date). On detecting a
    valid start signal, these devices simply:
        1. woke themselves from standby / powered on,
        2. unmuted the speaker,
        3. making the following spoken announcement audible.
    On the end signal, some devices automatically returned to standby.
  - There were also simple "adapter" solutions (e.g. the roughly $1
    MSP430-based single-chip solution shown in the NHK presentation) that
    likewise only produced a switching signal (relay/transistor), with no
    display either.
  - Devices with a real display (that would have shown the decoded
    area/category/time code) are not documented as a standard consumer
    product in the available sources; the field information is mainly used
    by BROADCASTERS to automatically re-distribute the signal (e.g. cable
    headends that only go on-air for specific areas).

  -> This decoder prototype therefore simulates BOTH behaviors at once, so
     a developer sees as much as possible while testing:
        (a) The "classic" behavior: a clearly visible/audible wake-up
            event (system sound + window brought to front), just like a
            simple radio would "wake up".
        (b) Additionally, as a high-end/diagnostic device might: a message
            box with the fully decoded plaintext fields (category, area,
            date/time) - useful for protocol verification during
            development.

Dependencies: numpy, sounddevice (for live input), Tkinter.
"""

import wave
import threading
import queue
import time

import numpy as np

try:
    import sounddevice as sd
    HAVE_SOUNDDEVICE = True
except Exception:
    HAVE_SOUNDDEVICE = False

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import ewbs_common as ewbs


# ---------------------------------------------------------------------------
# WAV loader (mono downmix for analysis; multi-channel files are averaged)
# ---------------------------------------------------------------------------

def load_wav_mono(path: str):
    with wave.open(path, "rb") as wf:
        n_channels = wf.getnchannels()
        sample_rate = wf.getframerate()
        sample_width = wf.getsampwidth()
        n_frames = wf.getnframes()
        raw = wf.readframes(n_frames)

    if sample_width == 2:
        data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif sample_width == 1:
        data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif sample_width == 4:
        data = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"Unsupported sample width: {sample_width} bytes")

    if n_channels > 1:
        data = data.reshape(-1, n_channels).mean(axis=1)

    return data, sample_rate


# ---------------------------------------------------------------------------
# Bit-stream monitor: continuously accepts audio chunks, classifies them
# bit by bit, and keeps looking for valid EWBS blocks.
# ---------------------------------------------------------------------------

class EwbsStreamMonitor:
    """Maintains a rolling bit buffer and reports newly found subframes and
    fully verified blocks.

    How it works:
      - feed(samples) is called with successive audio chunks (any size,
        e.g. straight from a sounddevice callback or from WAV blocks).
      - Internally, a leftover-sample buffer is kept so bit boundaries are
        never "cut" across chunk boundaries.
      - Each full bit period (15.625 ms) is turned into a '0'/'1'/None
        character via Goertzel analysis and appended to a running bit
        string ('X' means "no tone/silence" - this reliably breaks any
        detection across that point, since 'X' is never a valid bit).

    TWO detection layers (see the comment in ewbs_common.py):
      1. scan_subframes() - TOLERANT, 32-bit grid. Reliably recognizes area
         codes even in real recordings where date/time framing might not
         line up perfectly. This is the primary driver of the "radio
         reaction" in the GUI.
      2. find_blocks() - STRICT, 96-bit grid, a fully self-consistent block
         (area+date+time). Additionally provides a "fully verified" flag
         once ALL fields have been decoded.
    """

    def __init__(self, sample_rate: int, min_power: float = 1e-3):
        self.sample_rate = sample_rate
        self.samples_per_bit = round(sample_rate * ewbs.BIT_DURATION_S)
        self.min_power = min_power

        self._residual = np.array([], dtype=np.float32)
        self._bitstring = ""
        self._subframe_reported_end = 0  # bit index up to which scan_subframes() has already run
        self._block_reported_end = 0     # bit index up to which find_blocks() has already run

        # Cap so the buffer doesn't grow unbounded during continuous operation.
        self._max_bits = 96 * 20

    def feed(self, new_samples: np.ndarray):
        """Accepts new audio samples. Returns (new_subframes,
        new_strict_blocks) - two lists of newly recognized entries."""
        buf = np.concatenate([self._residual, new_samples.astype(np.float32)])

        n_full_bits = len(buf) // self.samples_per_bit
        for i in range(n_full_bits):
            chunk = buf[i * self.samples_per_bit:(i + 1) * self.samples_per_bit]
            bit = ewbs.samples_to_bit(chunk, self.sample_rate, self.min_power)
            self._bitstring += bit if bit is not None else "X"

        used_samples = n_full_bits * self.samples_per_bit
        self._residual = buf[used_samples:]

        # Trim the buffer if needed (cut from the front), keeping the
        # reporting offsets consistent.
        if len(self._bitstring) > self._max_bits:
            cut = len(self._bitstring) - self._max_bits
            self._bitstring = self._bitstring[cut:]
            self._subframe_reported_end = max(0, self._subframe_reported_end - cut)
            self._block_reported_end = max(0, self._block_reported_end - cut)

        # --- 1) Tolerant subframe scan (32-bit grid, 31-bit overlap) ---
        search_start = max(0, self._subframe_reported_end - 31)
        found_sub = ewbs.scan_subframes(self._bitstring[search_start:])
        new_subframes = []
        for entry in found_sub:
            global_idx = search_start + entry["bit_index"]
            if global_idx >= self._subframe_reported_end:
                entry = dict(entry)
                entry["bit_index"] = global_idx
                new_subframes.append(entry)
        if found_sub:
            last = found_sub[-1]
            self._subframe_reported_end = max(self._subframe_reported_end,
                                               search_start + last["bit_index"] + 32)

        # --- 2) Strict block scan (96-bit grid, 95-bit overlap) ---
        search_start_b = max(0, self._block_reported_end - 95)
        found_blk = ewbs.find_blocks(self._bitstring[search_start_b:])
        new_blocks = []
        for local_idx, decoded in found_blk:
            global_idx = search_start_b + local_idx
            if global_idx >= self._block_reported_end:
                new_blocks.append(decoded)
        if found_blk:
            last_local_idx, _ = found_blk[-1]
            self._block_reported_end = max(self._block_reported_end,
                                            search_start_b + last_local_idx + 96)

        return new_subframes, new_blocks


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class DecoderApp:
    def __init__(self, root):
        self.root = root
        self.root.title("EWBS Decoder - Prototype (Receiver Simulation)")
        self.root.geometry("700x600")

        self.monitor_thread = None
        self.stop_flag = threading.Event()
        self.gui_queue = queue.Queue()  # thread -> GUI communication

        self.alarm_active = False       # simulated "standby/awake" state
        self.last_alert_key = None      # for duplicate suppression (block repetitions)
        self.last_alert_time = 0.0

        # Rolling short-term memory of the most recently interpreted
        # subframes (kind='date'/'time'), used to opportunistically pair
        # them with a just-detected area code if they're close by (in bit
        # index). Value: list of (bit_index, interpreted-dict).
        self.recent_date_hits = []
        self.recent_time_hits = []

        self._build_ui()
        self.root.after(100, self._poll_queue)

    # -- UI ------------------------------------------------------------

    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        frm_src = ttk.LabelFrame(self.root, text="Audio source")
        frm_src.pack(fill="x", **pad)

        self.var_source = tk.StringVar(value="file")
        ttk.Radiobutton(frm_src, text="Analyze a WAV file", variable=self.var_source,
                         value="file", command=self._on_source_changed).pack(anchor="w")

        row_file = ttk.Frame(frm_src)
        row_file.pack(fill="x")
        self.var_file = tk.StringVar()
        self.entry_file = ttk.Entry(row_file, textvariable=self.var_file, width=50)
        self.entry_file.pack(side="left", padx=6, pady=4)
        self.btn_browse = ttk.Button(row_file, text="Browse...", command=self.choose_wav)
        self.btn_browse.pack(side="left")

        self.rb_live = ttk.Radiobutton(
            frm_src, text="Monitor live input (line-in/microphone):",
            variable=self.var_source, value="live", command=self._on_source_changed,
            state=("normal" if HAVE_SOUNDDEVICE else "disabled"))
        self.rb_live.pack(anchor="w")

        self.input_names, self.input_indices = [], []
        if HAVE_SOUNDDEVICE:
            for idx, d in enumerate(sd.query_devices()):
                if d["max_input_channels"] > 0:
                    self.input_names.append(f"[{idx}] {d['name']}")
                    self.input_indices.append(idx)
        else:
            self.input_names = ["sounddevice/PortAudio not available"]

        self.var_input_device = tk.StringVar(value=self.input_names[0] if self.input_names else "")
        self.cb_input = ttk.Combobox(frm_src, textvariable=self.var_input_device,
                                      values=self.input_names, width=50,
                                      state="disabled")
        self.cb_input.pack(anchor="w", padx=20, pady=4)

        frm_sens = ttk.LabelFrame(self.root, text="Sensitivity")
        frm_sens.pack(fill="x", **pad)
        ttk.Label(frm_sens, text="Minimum signal energy (lower = more sensitive, but more noise-prone):").pack(anchor="w", padx=6)
        self.var_threshold = tk.DoubleVar(value=1e-3)
        ttk.Scale(frm_sens, from_=1e-5, to=1e-1, orient="horizontal",
                  variable=self.var_threshold, length=300).pack(anchor="w", padx=6, pady=2)

        self.var_calibrate_phase = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            frm_sens,
            text="Calibrate bit phase (more robust on noisy/analog recordings, "
                 "but delays the start - see explanation below)",
            variable=self.var_calibrate_phase
        ).pack(anchor="w", padx=6, pady=(4, 0))
        ttk.Label(
            frm_sens,
            text="Off by default: a real warning system must above all react FAST. "
                 "Calibration searches for up to 8s for the optimal bit-grid offset "
                 "before that - that costs time before anything can be detected at all. "
                 "Only enable this for heavily degraded recordings (e.g. old off-air "
                 "tape recordings) where the decoder otherwise finds nothing.",
            foreground="gray40", wraplength=560, justify="left"
        ).pack(anchor="w", padx=6, pady=(0, 4))

        # --- Controls -----------------------------------------------
        frm_ctrl = ttk.Frame(self.root)
        frm_ctrl.pack(fill="x", **pad)
        self.btn_start = ttk.Button(frm_ctrl, text="Start monitoring", command=self.start_monitor)
        self.btn_start.pack(side="left", padx=4)
        self.btn_stop = ttk.Button(frm_ctrl, text="Stop", command=self.stop_monitor, state="disabled")
        self.btn_stop.pack(side="left", padx=4)

        self.status_var = tk.StringVar(value="Ready. Radio is in standby (simulated).")
        ttk.Label(self.root, textvariable=self.status_var, foreground="blue").pack(fill="x", padx=8)

        # --- Log -----------------------------------------------
        frm_log = ttk.LabelFrame(self.root, text="Reception log")
        frm_log.pack(fill="both", expand=True, **pad)
        self.log = tk.Text(frm_log, height=20, state="disabled", wrap="word")
        self.log.pack(fill="both", expand=True, padx=4, pady=4)

    def _on_source_changed(self):
        is_file = self.var_source.get() == "file"
        self.entry_file.configure(state="normal" if is_file else "disabled")
        self.btn_browse.configure(state="normal" if is_file else "disabled")
        self.cb_input.configure(state=("disabled" if is_file else "readonly"))

    def choose_wav(self):
        path = filedialog.askopenfilename(
            title="Select WAV file to monitor",
            filetypes=[("WAV files", "*.wav"), ("All files", "*.*")]
        )
        if path:
            self.var_file.set(path)

    def _log_line(self, text: str):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    # -- Start/Stop -----------------------------------------------

    def start_monitor(self):
        source = self.var_source.get()
        if source == "file" and not self.var_file.get():
            messagebox.showwarning("No file", "Please select a WAV file first.")
            return
        if source == "live" and not HAVE_SOUNDDEVICE:
            messagebox.showerror("Not available", "sounddevice/PortAudio is not installed.")
            return

        self.stop_flag.clear()
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.status_var.set("Monitoring... (radio in standby, waiting for EWBS tone)")
        self._log_line("--- Monitoring started ---")

        if source == "file":
            self.monitor_thread = threading.Thread(target=self._run_file_monitor, daemon=True)
        else:
            self.monitor_thread = threading.Thread(target=self._run_live_monitor, daemon=True)
        self.monitor_thread.start()

    def stop_monitor(self):
        self.stop_flag.set()
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self.status_var.set("Monitoring stopped.")
        self._log_line("--- Monitoring stopped ---")

    # -- Monitor threads -----------------------------------------------

    def _run_file_monitor(self):
        """Reads a WAV file in chunks and feeds it into the monitor, with
        realistic timing (like a live playback), so progress can be
        followed in the log. If you're in a hurry, set REALTIME_PLAYBACK to
        False for maximum speed."""
        REALTIME_PLAYBACK = True
        try:
            samples, sr = load_wav_mono(self.var_file.get())
        except Exception as ex:
            self.gui_queue.put(("error", f"Could not load WAV file:\n{ex}"))
            return

        # --- Optional bit-phase calibration (see checkbox in the GUI) ---
        # OFF BY DEFAULT: a warning system must primarily react FAST. For a
        # cleanly, digitally generated file (our own encoder, most test
        # recordings), the bit grid starting at sample 0 is virtually
        # always already correctly aligned - calibration wouldn't help
        # there, but would cost time. It's only needed for real, noisy
        # off-air recordings with an arbitrary phase (see
        # ewbs_common.find_best_bit_phase()'s docstring for an example).
        if self.var_calibrate_phase.get():
            self.gui_queue.put(("info", "Calibrating bit phase (may take a moment)..."))
            phase_offset = ewbs.find_best_bit_phase(samples, sr, min_power=self.var_threshold.get())
            self.gui_queue.put(("info", f"Bit phase calibrated (offset: {phase_offset} samples "
                                         f"out of {round(sr*ewbs.BIT_DURATION_S)} samples/bit)."))
            samples = samples[phase_offset:]

        monitor = EwbsStreamMonitor(sr, min_power=self.var_threshold.get())
        chunk_size = max(1, round(sr * 0.25))  # 250 ms chunks

        for i in range(0, len(samples), chunk_size):
            if self.stop_flag.is_set():
                break
            chunk = samples[i:i + chunk_size]
            new_subframes, new_blocks = monitor.feed(chunk)
            for sf in new_subframes:
                self.gui_queue.put(("subframe", sf))
            for decoded in new_blocks:
                self.gui_queue.put(("block", decoded))
            if REALTIME_PLAYBACK:
                time.sleep(chunk_size / sr)

        self.gui_queue.put(("info", "File fully processed."))

    def _run_live_monitor(self):
        if not HAVE_SOUNDDEVICE:
            return
        sel = self.var_input_device.get()
        dev_index = None
        for name, idx in zip(self.input_names, self.input_indices):
            if name == sel:
                dev_index = idx
                break

        sr = 8000  # plenty for EWBS (the signal lives at 640/1024 Hz)
        monitor = EwbsStreamMonitor(sr, min_power=self.var_threshold.get())

        if not self.var_calibrate_phase.get():
            # DEFAULT (fast): no calibration phase, immediate start. For a
            # warning system, response speed matters more than optimal bit
            # alignment on noisy sources - see the checkbox explanation in
            # the GUI.
            def callback(indata, frames, time_info, status):
                if status:
                    self.gui_queue.put(("info", f"Audio status: {status}"))
                mono = indata[:, 0] if indata.ndim > 1 else indata
                new_subframes, new_blocks = monitor.feed(mono)
                for sf in new_subframes:
                    self.gui_queue.put(("subframe", sf))
                for decoded in new_blocks:
                    self.gui_queue.put(("block", decoded))

            try:
                with sd.InputStream(device=dev_index, channels=1, samplerate=sr,
                                     blocksize=round(sr * 0.25), callback=callback):
                    while not self.stop_flag.is_set():
                        time.sleep(0.1)
            except Exception as ex:
                self.gui_queue.put(("error", f"Live input failed:\n{ex}"))
            return

        # --- Optional short calibration phase (only if the checkbox is on) ---
        # With live audio we don't know the signal in advance, so we first
        # collect CALIBRATION_SECONDS seconds of raw samples, determine the
        # best bit-grid offset from them ONCE, and only then start feeding
        # continuously into the actual monitor. Trade-off: if a live signal
        # starts exactly DURING the calibration window, the estimate can be
        # inaccurate (in that case: stop and restart monitoring once the
        # alarm tone is already playing).
        CALIBRATION_SECONDS = 3.0
        calib_buffer = []
        calib_samples_needed = round(sr * CALIBRATION_SECONDS)
        calib_done = threading.Event()
        phase_offset_holder = {"value": 0}

        def callback(indata, frames, time_info, status):
            if status:
                self.gui_queue.put(("info", f"Audio status: {status}"))
            mono = indata[:, 0] if indata.ndim > 1 else indata

            if not calib_done.is_set():
                calib_buffer.append(mono.copy())
                total = sum(len(c) for c in calib_buffer)
                if total >= calib_samples_needed:
                    concat = np.concatenate(calib_buffer)
                    offset = ewbs.find_best_bit_phase(concat, sr, min_power=self.var_threshold.get())
                    phase_offset_holder["value"] = offset
                    self.gui_queue.put(("info", f"Bit phase calibrated (offset: {offset} samples)."))
                    calib_done.set()
                    # feed the calibrated buffer (from the offset onward) right away
                    new_subframes, new_blocks = monitor.feed(concat[offset:])
                    for sf in new_subframes:
                        self.gui_queue.put(("subframe", sf))
                    for decoded in new_blocks:
                        self.gui_queue.put(("block", decoded))
                return

            new_subframes, new_blocks = monitor.feed(mono)
            for sf in new_subframes:
                self.gui_queue.put(("subframe", sf))
            for decoded in new_blocks:
                self.gui_queue.put(("block", decoded))

        try:
            with sd.InputStream(device=dev_index, channels=1, samplerate=sr,
                                 blocksize=round(sr * 0.25), callback=callback):
                while not self.stop_flag.is_set():
                    time.sleep(0.1)
        except Exception as ex:
            self.gui_queue.put(("error", f"Live input failed:\n{ex}"))

    # -- GUI queue processing (runs on the main thread!) -----------------

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.gui_queue.get_nowait()
                if kind == "subframe":
                    self._handle_subframe(payload)
                elif kind == "block":
                    self._handle_decoded_block(payload)
                elif kind == "info":
                    self._log_line(f"[Info] {payload}")
                elif kind == "error":
                    self._log_line(f"[Error] {payload}")
                    messagebox.showerror("Error", payload)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _handle_subframe(self, sf: dict):
        """Called for EVERY recognized fixed+payload subframe (32 bits) -
        the tolerant, primary detection layer (see scan_subframes() in
        ewbs_common.py). This is the most robust way to evaluate real EWBS
        recordings (not just our own encoder), since it doesn't require
        date/time to be decodable as well.
        """
        interp = sf["interpreted"]
        raw = sf["payload_raw"]
        fc = "Type I / End" if sf["fixed_code_type"] == "I_or_end" else "Type II Start"

        if interp is None:
            # The payload couldn't be matched to any of the three known
            # field types. We log it raw rather than discarding it - that
            # keeps a record for further reverse-engineering if needed.
            self._log_line(f"[Subframe @{sf['bit_index']}] Fixed={fc}  "
                            f"Payload={raw}  -> not interpretable (logged raw)")
            return

        kind = interp["kind"]
        is_start = interp["is_start"]

        if kind == "date":
            self.recent_date_hits.append((sf["bit_index"], interp))
            self.recent_date_hits = self.recent_date_hits[-5:]
            self._log_line(f"[Subframe @{sf['bit_index']}] Fixed={fc}  Date field recognized: "
                            f"day={interp['day']:02d} month={interp['month']:02d} "
                            f"({'start' if is_start else 'end'})")
            return

        if kind == "time":
            self.recent_time_hits.append((sf["bit_index"], interp))
            self.recent_time_hits = self.recent_time_hits[-5:]
            self._log_line(f"[Subframe @{sf['bit_index']}] Fixed={fc}  Time field recognized: "
                            f"hour={interp['hour']:02d} year-cycle-index={interp['year_index']} "
                            f"({'start' if is_start else 'end'})")
            return

        # kind == "area" -> our reliable alarm trigger
        area = interp["area"]
        self._log_line(f"[Subframe @{sf['bit_index']}] Fixed={fc}  Area code recognized: "
                        f"{area} ({'start' if is_start else 'end'})")

        key = (is_start, area)
        now = time.time()
        if key == self.last_alert_key and (now - self.last_alert_time) < 8.0:
            return  # debounce: the same message just came in (block repetition)
        self.last_alert_key = key
        self.last_alert_time = now

        # Opportunistically look for a nearby date/time field (same
        # start/end type, as close as possible in bit index). This can
        # come up empty if no matching date/time subframe was seen yet.
        date_hit = self._closest_hit(self.recent_date_hits, is_start, sf["bit_index"])
        time_hit = self._closest_hit(self.recent_time_hits, is_start, sf["bit_index"])

        decoded_like = {
            "is_start": is_start,
            "signal_type": 2 if sf["fixed_code_type"] == "II_start" else (1 if is_start else None),
            "area": area,
            "day": date_hit["day"] if date_hit else None,
            "month": date_hit["month"] if date_hit else None,
            "hour": time_hit["hour"] if time_hit else None,
            "year_index": time_hit["year_index"] if time_hit else None,
        }

        if is_start:
            self._simulate_radio_wakeup(decoded_like, area,
                                         decoded_like["day"], decoded_like["month"],
                                         decoded_like["hour"], decoded_like["year_index"])
        else:
            self._simulate_radio_standby(area)

    @staticmethod
    def _closest_hit(hits, is_start, bit_index, max_distance=200):
        candidates = [(idx, d) for idx, d in hits
                      if d["is_start"] == is_start and abs(idx - bit_index) <= max_distance]
        if not candidates:
            return None
        candidates.sort(key=lambda t: abs(t[0] - bit_index))
        return candidates[0][1]

    def _handle_decoded_block(self, decoded: dict):
        """Called for every newly recognized, FULLY VERIFIED (strict
        96-bit parser) EWBS block - area+date+time could ALL be decoded and
        are mutually consistent.

        Since a signal is officially repeated multiple times (start: 4-10x,
        end: 2-4x), without debouncing every single repetition would pop up
        its own message box. We therefore suppress identical messages
        within a short time window - exactly like a real device, which
        reacts once on the first valid block rather than re-triggering on
        every repetition.
        """
        key = (decoded["is_start"], decoded["signal_type"], decoded["area"],
               decoded["day"], decoded["month"], decoded["hour"], decoded["year_index"])
        now = time.time()
        is_duplicate = (key == self.last_alert_key and (now - self.last_alert_time) < 10.0)

        area = decoded["area"]
        day, month, hour = decoded["day"], decoded["month"], decoded["hour"]
        # Translate the year-cycle index into a plausible plaintext year
        # (the closest year to the current system time with a matching index):
        import datetime as _dt
        current_year = _dt.datetime.now().year
        candidate_years = [y for y in range(current_year - 10, current_year + 10)
                            if (y - 1985) % 10 == decoded["year_index"]]
        year_guess = min(candidate_years, key=lambda y: abs(y - current_year)) if candidate_years else "?"

        if decoded["is_start"]:
            cat_text = {1: "Type I - Earthquake/evacuation instruction",
                        2: "Type II - Tsunami warning",
                        None: "unknown"}[decoded["signal_type"]]
            self._log_line(f"[BLOCK] START recognized | {cat_text} | Area: {area} | "
                            f"{day:02d}.{month:02d}.{year_guess} {hour:02d}:xx")
        else:
            self._log_line(f"[BLOCK] END recognized | Area: {area} | "
                            f"{day:02d}.{month:02d}.{year_guess} {hour:02d}:xx")

        if is_duplicate:
            return  # already reported -> no repeat radio reaction/popup

        self.last_alert_key = key
        self.last_alert_time = now

        if decoded["is_start"]:
            self._simulate_radio_wakeup(decoded, area, day, month, hour, year_guess)
        else:
            self._simulate_radio_standby(area)

    # -- Simulated radio behavior -----------------------------------------------

    def _simulate_radio_wakeup(self, decoded, area, day, month, hour, year_info):
        """Simulates a classic EWBS receiver's behavior on a start signal:
        'waking up from standby' + unmuting the speaker. Since most real
        devices had NO display, the actual 'core' reaction is just a clear
        audible/visual wake-up event. In addition, as a high-end/
        diagnostic variant might, we show the fully decoded fields in the
        same message, so the protocol can be checked immediately during
        development.

        day/month/hour/year_info may be None: the area-code field is
        recognized by scan_subframes() independently of date/time (real
        recordings don't always yield a decodable date/time subframe
        nearby). In that case we honestly show "not decodable" instead of
        making up values.
        """
        self.alarm_active = True
        self.status_var.set("ALARM ACTIVE - receiver 'woken up', speaker unmuted")

        # Classic radio: no text, just a clear audible signal. We use
        # Tkinter's system bell for this (works cross-platform without an
        # extra audio library).
        self.root.bell()

        cat_text = {1: "Type I - Earthquake / evacuation instruction",
                    2: "Type II - Tsunami warning",
                    None: "unknown (not encoded in the start signal)"}[decoded["signal_type"]]

        if day is not None and month is not None:
            year_suffix = (f"{year_info}" if isinstance(year_info, int) and year_info > 100
                            else (f" [year-cycle index {year_info}]" if year_info is not None else ""))
            date_text = f"{day:02d}.{month:02d}.{year_suffix}"
        else:
            date_text = "not decodable (experimental)"
        time_text = f"{hour:02d}:00" if hour is not None else "not decodable (experimental)"

        message = (
            "A simple EWBS radio without a display would now have just:\n"
            "  - turned itself on from standby\n"
            "  - unmuted the speaker, to make the following announcement "
            "audible\n\n"
            "For protocol verification, this prototype additionally shows "
            "the decoded raw data:\n\n"
            f"Category:    {cat_text}\n"
            f"Area:        {area}\n"
            f"Date:        {date_text}\n"
            f"Time:        {time_text}\n"
        )
        # Shown non-blocking, so monitoring keeps running in the background
        # while the user reads the message box.
        self.root.after(0, lambda: messagebox.showwarning("EWBS ALARM received", message))

    def _simulate_radio_standby(self, area):
        """Simulates the behavior on an end signal: some classic devices
        automatically muted and returned to standby once the end signal
        was received."""
        was_active = self.alarm_active
        self.alarm_active = False
        self.status_var.set("End signal received - receiver returning to standby (simulated)")
        if was_active:
            self.root.after(0, lambda: messagebox.showinfo(
                "EWBS - Alarm ended",
                f"End signal received for area '{area}'.\n"
                "A classic device would now mute and return to standby."))


def main():
    root = tk.Tk()
    DecoderApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
