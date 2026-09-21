# -*- coding: utf-8 -*-
"""
ewbs_common.py
==============

Shared protocol library for the EWBS encoder and decoder prototypes.

EWBS = Emergency Warning Broadcasting System (Japanese: 緊急警報放送), the
Japanese analog-broadcast alarm signaling scheme that has been used since
1985 to wake up radios/TVs from standby over plain AM/FM/TV audio and alert
listeners to earthquakes, tsunamis, and other disasters.

Primary source for all bit patterns and tables below:
    Ministry of Posts and Telecommunications Notification No. 405 (1985),
    "Structure of the Emergency Warning Signal under Article 9-3 Item 5 of
    the Radio Equipment Regulations", published at tele.soumu.go.jp

Cross-checked against:
    - NHK STRL / Kazuyoshi Shogen et al., "Implementation of Emergency
      Warning Broadcasting System in the Asia Pacific Region", ITU/ESCAP
      Disaster Communications Workshop, Bangkok 2006.
    - ingen084/EwsDemodulator (github.com/ingen084/EwsDemodulator), an
      independently developed C# decoder field-tested against real
      broadcasts (including the March 11, 2011 tsunami warnings). This was
      the key reference that resolved the exact date/time field layout
      (see encode_date_field()/encode_time_field() below).
    - radio1ban.com, a Japanese amateur-radio site hosting real off-air
      EWBS recordings, independently confirming the 96-bit block structure.

Verified against real recordings:
    - Three demo sound files from the English Wikipedia article on EWBS
      ("Wakeup", "Type 1", "Completion" sounds)
    - A real NHK Osaka monthly test broadcast (Dec 1, 2005, 11:59 JST,
      Kinki region)
    - The real Type-II (tsunami) start signal broadcast during the
      September 5-6, 2004 off-Kii-peninsula earthquakes
    - A short NHK test signal clip
  All of the above decode correctly with this library, including the
  "odd/even block" date rollover behavior around midnight (see
  encode_date_field()).

One known open point: the 12-bit area code for 岩手県 (Iwate) differs
between our transcription of the government text ("010111011010") and the
value used in the field-tested EwsDemodulator project ("010111010100"). We
use the latter here since it has been validated against live broadcasts;
one of the two is presumably a transcription error somewhere. This could be
settled conclusively with an Iwate-specific real recording.
"""

import numpy as np

# ---------------------------------------------------------------------------
# 1. PHYSICAL LAYER: FSK tone parameters
# ---------------------------------------------------------------------------

BIT_RATE_BPS = 64                       # by the specification
BIT_DURATION_S = 1.0 / BIT_RATE_BPS     # = 0.015625 s = 15.625 ms per bit

FREQ_SPACE_HZ = 640.0    # "0" bit ("space"), exactly 10 cycles per bit period
FREQ_MARK_HZ = 1024.0    # "1" bit ("mark"),  exactly 16 cycles per bit period

# Number of times the 96-bit block is repeated:
START_REPEATS_MIN, START_REPEATS_MAX = 4, 10   # start signal: 6-15 seconds
END_REPEATS_MIN, END_REPEATS_MAX = 2, 4        # end signal:   6-12 seconds
START_REPEATS_DEFAULT = 7
END_REPEATS_DEFAULT = 3

# Silence between repeated blocks. The specification calls for a "no
# signal" gap between repetitions; we use a small, configurable value since
# the exact gap length is not bit-exactly specified in the sources we have.
INTER_BLOCK_SILENCE_S_DEFAULT = 0.5

# ---------------------------------------------------------------------------
# 2. PRECEDENCE CODE (4 bits) - sent once, before the whole signal
# ---------------------------------------------------------------------------

PRECEDENCE_START = "1100"   # start signal (same for type I and II)
PRECEDENCE_END = "0011"     # end signal (same for type I and II)

# ---------------------------------------------------------------------------
# 3. FIXED CODE (16 bits) - sync word, present three times per block
# ---------------------------------------------------------------------------

FIXED_CODE_TYPE1_START_OR_ANY_END = "0000111001101101"
FIXED_CODE_TYPE2_START = "1111000110010010"


def get_fixed_code(is_start: bool, signal_type: int) -> str:
    """Returns the correct 16-bit fixed code.

    End signals (whether type I or II) always use the same fixed code as a
    type-I start. Only a type-II start has its own, different fixed code.
    """
    if not is_start:
        return FIXED_CODE_TYPE1_START_OR_ANY_END
    return FIXED_CODE_TYPE1_START_OR_ANY_END if signal_type == 1 else FIXED_CODE_TYPE2_START


# ---------------------------------------------------------------------------
# 4. AREA CODES (12 bits)
# ---------------------------------------------------------------------------
# Key: display name (as shown in the GUI) -> 12-bit code string

AREA_CODES = {
    # --- nationwide / common ---
    "Nationwide (地域共通符号)": "001101001101",

    # --- wide-area codes ---
    "Kanto (関東広域圏)": "010110100101",
    "Chukyo (中京広域圏)": "011100101010",
    "Kinki (近畿広域圏)": "100011010101",
    "Tottori/Shimane (鳥取・島根圏)": "011010011001",
    "Okayama/Kagawa (岡山・香川圏)": "010101010011",

    # --- all 47 prefectures ---
    "Hokkaido (北海道)": "000101101011",
    "Aomori (青森県)": "010001100111",
    "Iwate (岩手県)": "010111010100",
    "Miyagi (宮城県)": "011101011000",
    "Akita (秋田県)": "101011000110",
    "Yamagata (山形県)": "111001001100",
    "Fukushima (福島県)": "000110101110",
    "Ibaraki (茨城県)": "110001101001",
    "Tochigi (栃木県)": "111000111000",
    "Gunma (群馬県)": "100110001011",
    "Saitama (埼玉県)": "011001001011",
    "Chiba (千葉県)": "000111000111",
    "Tokyo (東京都)": "101010101100",
    "Kanagawa (神奈川県)": "010101101100",
    "Niigata (新潟県)": "010011001110",
    "Toyama (富山県)": "010100111001",
    "Ishikawa (石川県)": "011010100110",
    "Fukui (福井県)": "100100101101",
    "Yamanashi (山梨県)": "110101001010",
    "Nagano (長野県)": "100111010010",
    "Gifu (岐阜県)": "101001100101",
    "Shizuoka (静岡県)": "101001011010",
    "Aichi (愛知県)": "100101100110",
    "Mie (三重県)": "001011011100",
    "Shiga (滋賀県)": "110011100100",
    "Kyoto (京都府)": "010110011010",
    "Osaka (大阪府)": "110010110010",
    "Hyogo (兵庫県)": "011001110100",
    "Nara (奈良県)": "101010010011",
    "Wakayama (和歌山県)": "001110010110",
    "Tottori (鳥取県)": "110100100011",
    "Shimane (島根県)": "001100011011",
    "Okayama (岡山県)": "001010110101",
    "Hiroshima (広島県)": "101100110001",
    "Yamaguchi (山口県)": "101110011000",
    "Tokushima (徳島県)": "111001100010",
    "Kagawa (香川県)": "100110110100",
    "Ehime (愛媛県)": "000110011101",
    "Kochi (高知県)": "001011100011",
    "Fukuoka (福岡県)": "011000101101",
    "Saga (佐賀県)": "100101011001",
    "Nagasaki (長崎県)": "101000101011",
    "Kumamoto (熊本県)": "100010100111",
    "Oita (大分県)": "110010001101",
    "Miyazaki (宮崎県)": "110100011100",
    "Kagoshima (鹿児島県)": "110101000101",
    "Okinawa (沖縄県)": "001101110010",
}

# Reverse lookup for the decoder (12-bit code -> display name)
AREA_CODES_REVERSE = {v: k for k, v in AREA_CODES.items()}

# ---------------------------------------------------------------------------
# 5. DATE/TIME CODES (5 bits each)
# ---------------------------------------------------------------------------

DAY_CODES = {
    1: "10000", 2: "01000", 3: "11000", 4: "00100", 5: "10100",
    6: "01100", 7: "11100", 8: "00010", 9: "10010", 10: "01010",
    11: "11010", 12: "00110", 13: "10110", 14: "01110", 15: "11110",
    16: "00001", 17: "10001", 18: "01001", 19: "11001", 20: "00101",
    21: "10101", 22: "01101", 23: "11101", 24: "00011", 25: "10011",
    26: "01011", 27: "11011", 28: "00111", 29: "10111", 30: "01111",
    31: "11111",
}

MONTH_CODES = {
    1: "10001", 2: "01001", 3: "11001", 4: "00101", 5: "10101",
    6: "01101", 7: "11101", 8: "00011", 9: "10011", 10: "01011",
    11: "11011", 12: "00111",
}

HOUR_CODES = {
    0: "00011", 1: "10011", 2: "01011", 3: "11011", 4: "00111",
    5: "10111", 6: "01111", 7: "11111", 8: "00001", 9: "10001",
    10: "01001", 11: "11001", 12: "00101", 13: "10101", 14: "01101",
    15: "11101", 16: "00010", 17: "10010", 18: "01010", 19: "11010",
    20: "00110", 21: "10110", 22: "01110", 23: "11110",
}

# Year code: cyclic, 10 values, starting at Showa 60 (1985).
# YEAR_CODES_BY_INDEX[n] corresponds to the year (1985 + n), n = 0..9; the
# cycle then repeats (per the notification text: year codes after Showa 69
# reuse the table's codes in sequence).
YEAR_CODES_BY_INDEX = {
    0: "10101",  # Showa 60 = 1985
    1: "01101",  # 1986
    2: "11101",  # 1987
    3: "00011",  # 1988
    4: "10011",  # 1989
    5: "01011",  # 1990
    6: "10001",  # 1991
    7: "01001",  # 1992
    8: "11001",  # 1993
    9: "00101",  # 1994
}
YEAR_CODES_REVERSE = {v: k for k, v in YEAR_CODES_BY_INDEX.items()}


def year_to_code(year: int) -> str:
    """Converts a real calendar year (e.g. 2026) into the 5-bit year code.

    The cycle is 10 years long, starting at 1985 (Showa 60). Years outside
    the original range simply wrap modulo 10, as prescribed by the
    notification text.
    """
    index = (year - 1985) % 10
    return YEAR_CODES_BY_INDEX[index]


def code_to_year_index(code: str):
    """Reverse direction: 5-bit code -> cycle index (0-9), or None if unknown."""
    return YEAR_CODES_REVERSE.get(code)


# ---------------------------------------------------------------------------
# 6. FIELD ENCODING (framing of the 16-bit fields)
# ---------------------------------------------------------------------------

def encode_area_field(area_code_12bit: str, is_start: bool) -> str:
    """Area field (16 bits) = marker(2) + area code(12) + marker(2)."""
    if is_start:
        return "10" + area_code_12bit + "00"
    return "01" + area_code_12bit + "11"


def encode_date_field(day: int, month: int, is_start: bool, is_even_block: bool = False) -> str:
    """Date field (16 bits):

        marker(3) + day(5) + even-block flag(1) + month(5) + marker(2)

    Even-block flag: the specification alternates "odd" (current date/time)
    and "even" blocks across repetitions. Near a day/hour boundary, the
    "even" block may carry a DIFFERENT (adjacent) date/time than the "odd"
    one - this flag marks whether a block is "even" (and may therefore
    differ). Away from midnight, odd and even blocks are identical in
    content, so the encoder defaults to is_even_block=False.
    """
    prefix = "010" if is_start else "100"
    suffix = "00" if is_start else "11"
    day_bits = DAY_CODES[day]
    month_bits = MONTH_CODES[month]
    even_flag = "1" if is_even_block else "0"
    return prefix + day_bits + even_flag + month_bits + suffix


def encode_time_field(hour: int, year: int, is_start: bool, is_even_block: bool = False) -> str:
    """Time field (16 bits), structured analogously to encode_date_field():

        marker(3) + hour(5) + even-block flag(1) + year(5) + marker(2)
    """
    prefix = "011" if is_start else "101"
    suffix = "00" if is_start else "11"
    hour_bits = HOUR_CODES[hour]
    year_bits = year_to_code(year)
    even_flag = "1" if is_even_block else "0"
    return prefix + hour_bits + even_flag + year_bits + suffix


def decode_area_field(field16: str):
    """Attempts to interpret a 16-bit field as an area field.

    Returns (is_start: bool, area_name: str), or None if the frame markers
    don't match or the area code is unknown.
    """
    if len(field16) != 16:
        return None
    prefix, payload, suffix = field16[:2], field16[2:14], field16[14:]
    if prefix == "10" and suffix == "00":
        is_start = True
    elif prefix == "01" and suffix == "11":
        is_start = False
    else:
        return None
    name = AREA_CODES_REVERSE.get(payload)
    if name is None:
        return None
    return is_start, name


def decode_date_field(field16: str):
    """16-bit date field -> (is_start, day, month, is_even_block), or None."""
    if len(field16) != 16:
        return None
    prefix, day_bits, even_flag, month_bits, suffix = (
        field16[0:3], field16[3:8], field16[8], field16[9:14], field16[14:16])
    if prefix == "010" and suffix == "00":
        is_start = True
    elif prefix == "100" and suffix == "11":
        is_start = False
    else:
        return None
    day = _reverse_lookup(DAY_CODES, day_bits)
    month = _reverse_lookup(MONTH_CODES, month_bits)
    if day is None or month is None:
        return None
    return is_start, day, month, (even_flag == "1")


def decode_time_field(field16: str):
    """16-bit time field -> (is_start, hour, year_index, is_even_block), or None."""
    if len(field16) != 16:
        return None
    prefix, hour_bits, even_flag, year_bits, suffix = (
        field16[0:3], field16[3:8], field16[8], field16[9:14], field16[14:16])
    if prefix == "011" and suffix == "00":
        is_start = True
    elif prefix == "101" and suffix == "11":
        is_start = False
    else:
        return None
    hour = _reverse_lookup(HOUR_CODES, hour_bits)
    year_index = code_to_year_index(year_bits)
    if hour is None or year_index is None:
        return None
    return is_start, hour, year_index, (even_flag == "1")


def _reverse_lookup(table: dict, value: str):
    for k, v in table.items():
        if v == value:
            return k
    return None


# ---------------------------------------------------------------------------
# 7. BUILDING A COMPLETE BLOCK (96 bits, without the precedence code)
# ---------------------------------------------------------------------------

def build_block(is_start: bool, signal_type: int, area_name: str,
                 day: int, month: int, hour: int, year: int) -> str:
    """Builds one complete 96-bit signal block as a bit string.

    Layout:  Fixed(16) + Area(16) + Fixed(16) + Date(16) + Fixed(16) + Time(16)
             = 96 bits total (WITHOUT the precedence code - that is sent
             separately, once, before the whole repeated sequence; see
             build_full_signal()).

    signal_type: 1 = type I (earthquake/evacuation), 2 = type II (tsunami).
    For end signals, signal_type only matters for documentation purposes,
    since the fixed code for end signals is identical for both types.
    """
    area_code = AREA_CODES[area_name]
    fixed = get_fixed_code(is_start, signal_type)

    area_field = encode_area_field(area_code, is_start)
    date_field = encode_date_field(day, month, is_start)
    time_field = encode_time_field(hour, year, is_start)

    return fixed + area_field + fixed + date_field + fixed + time_field


# ---------------------------------------------------------------------------
# 8. TONE ENCODER: bit string -> audio samples (float32, range -1..1)
# ---------------------------------------------------------------------------

def bits_to_tone(bits: str, sample_rate: int) -> np.ndarray:
    """Converts a bit string into an FSK audio signal.

    Note: within one bit period (15.625 ms), 640 Hz completes exactly 10
    full cycles and 1024 Hz exactly 16 full cycles. Both tones therefore
    start and end at phase 0 - restarting the phase at 0 for every bit
    means a frequency change never introduces a discontinuity (phase jump
    = 0), which keeps the generated signal click-free.
    """
    samples_per_bit = round(sample_rate * BIT_DURATION_S)
    t = np.arange(samples_per_bit) / sample_rate
    tone_space = np.sin(2 * np.pi * FREQ_SPACE_HZ * t).astype(np.float32)
    tone_mark = np.sin(2 * np.pi * FREQ_MARK_HZ * t).astype(np.float32)

    chunks = [tone_mark if b == "1" else tone_space for b in bits]
    return np.concatenate(chunks) if chunks else np.array([], dtype=np.float32)


def build_full_signal(is_start: bool, signal_type: int, area_name: str,
                       day: int, month: int, hour: int, year: int,
                       repeats: int, sample_rate: int,
                       inter_block_silence_s: float = INTER_BLOCK_SILENCE_S_DEFAULT
                       ) -> np.ndarray:
    """Builds the complete alarm signal: the precedence code ONCE, followed
    by the 96-bit block repeated `repeats` times (with short gaps in
    between) - matching the structure observed in real EWBS recordings.
    """
    precedence = PRECEDENCE_START if is_start else PRECEDENCE_END
    precedence_tone = bits_to_tone(precedence, sample_rate)

    block_bits = build_block(is_start, signal_type, area_name, day, month, hour, year)
    block_tone = bits_to_tone(block_bits, sample_rate)
    silence = np.zeros(round(inter_block_silence_s * sample_rate), dtype=np.float32)

    parts = [precedence_tone]
    for i in range(repeats):
        parts.append(block_tone)
        if i != repeats - 1:
            parts.append(silence)
    return np.concatenate(parts) if parts else np.array([], dtype=np.float32)


# ---------------------------------------------------------------------------
# 9. GOERTZEL ALGORITHM (for the decoder)
# ---------------------------------------------------------------------------

def goertzel_power(samples: np.ndarray, freq: float, sample_rate: int) -> float:
    """Computes the signal energy at a given frequency using the Goertzel
    algorithm - much cheaper than a full FFT when only 1-2 fixed
    frequencies matter (here: 640 Hz and 1024 Hz). This is the same
    approach used by NHK's own reference single-chip receiver design
    (an MSP430-based, roughly $1 solution mentioned in their ITU
    presentation).
    """
    n = len(samples)
    if n == 0:
        return 0.0
    k = int(0.5 + (n * freq) / sample_rate)
    omega = (2.0 * np.pi * k) / n
    coeff = 2.0 * np.cos(omega)

    s_prev, s_prev2 = 0.0, 0.0
    for x in samples:
        s = x + coeff * s_prev - s_prev2
        s_prev2 = s_prev
        s_prev = s

    power = s_prev2 ** 2 + s_prev ** 2 - coeff * s_prev * s_prev2
    return float(power)


def samples_to_bit(samples: np.ndarray, sample_rate: int, min_power: float = 1e-3):
    """Classifies one bit period's worth of samples as '0', '1', or None
    (no EWBS tone detected / too quiet - presumably normal program audio).
    """
    p_space = goertzel_power(samples, FREQ_SPACE_HZ, sample_rate)
    p_mark = goertzel_power(samples, FREQ_MARK_HZ, sample_rate)

    if max(p_space, p_mark) < min_power:
        return None  # too quiet / no tone -> presumably not an EWBS signal

    return "1" if p_mark > p_space else "0"


def find_best_bit_phase(samples: np.ndarray, sample_rate: int, min_power: float = 1e-3,
                         search_seconds: float = 8.0, phase_step_samples: int = 4):
    """Automatic bit-phase calibration (coarse clock recovery).

    Background: in a cleanly, digitally generated file (our own encoder,
    the Wikipedia demo recordings), the EWBS tone usually happens to start
    close enough to the sample grid that a bit window starting right at
    sample 0 works fine. In a real, over-the-air received recording that
    was, say, digitized from tape, the tone's phase relative to the sample
    grid is essentially arbitrary - without correction, almost every
    analysis window straddles a bit boundary and "smears" two adjacent
    bits together, drastically increasing the bit error rate.

    This function tries several phase offsets (in steps of
    phase_step_samples) within the first search_seconds seconds and counts
    how often a valid fixed code shows up in the resulting bit stream for
    each offset. The offset with the most hits wins - a simple but
    effective approach, since the fixed code repeats often enough
    (typically 12-30 times within the first few seconds of a start signal)
    that a correctly aligned grid clearly outperforms a misaligned one.

    Note on trade-offs: this scan is deliberately NOT run by default in the
    decoder, because it adds noticeable latency before any detection can
    happen at all - and for a warning system, reacting quickly matters more
    than perfectly decoding a single degraded archival recording. See the
    "Calibrate bit phase" option in the decoder GUI.

    Returns: the best sample offset (int, 0 <= x < samples_per_bit).
    """
    samples_per_bit = round(sample_rate * BIT_DURATION_S)
    n_search_samples = min(len(samples), round(search_seconds * sample_rate))
    search_buf = samples[:n_search_samples]

    best_offset, best_score = 0, -1
    for offset in range(0, samples_per_bit, phase_step_samples):
        bits = []
        for i in range(offset, len(search_buf) - samples_per_bit, samples_per_bit):
            chunk = search_buf[i:i + samples_per_bit]
            b = samples_to_bit(chunk, sample_rate, min_power)
            bits.append(b if b is not None else "X")
        bitstring = "".join(bits)
        score = (bitstring.count(FIXED_CODE_TYPE1_START_OR_ANY_END)
                 + bitstring.count(FIXED_CODE_TYPE2_START))
        # count() only finds non-overlapping matches, which is entirely
        # sufficient as a relative quality measure between candidate phases.
        if score > best_score:
            best_score, best_offset = score, offset

    return best_offset


# ---------------------------------------------------------------------------
# 10. BLOCK PARSERS (used by the decoder)
# ---------------------------------------------------------------------------
# There are two parsers with different strictness:
#
#   try_parse_block_at() / find_blocks()
#       STRICT: requires one complete, internally consistent 96-bit block
#       (area+date+time together, all with matching start/end markers).
#       Works reliably for cleanly, synthetically generated signals (e.g.
#       from ewbs_encoder.py) and has also been verified against several
#       real recordings (see module docstring).
#
#   scan_subframes()
#       TOLERANT: scans the bit stream in 32-bit steps (fixed[16] +
#       payload[16]) and reports every single occurrence of a valid fixed
#       code, even if the associated payload cannot be interpreted. This is
#       what keeps the decoder useful even on noisy/partially-degraded
#       real-world signals: it reliably recovers the area code (the field
#       most robust against bit errors, being checked first) and logs
#       unrecognized payloads raw rather than silently dropping or
#       misinterpreting them.
# ---------------------------------------------------------------------------

def try_parse_block_at(bitstring: str, start_index: int):
    """Attempts to parse one complete 96-bit block starting at start_index
    (STRICT parser, see explanation above).

    Expected layout starting at start_index:
        [0:16]   fixed code
        [16:32]  area field
        [32:48]  fixed code (repeated)
        [48:64]  date field
        [64:80]  fixed code (repeated)
        [80:96]  time field

    Returns a dict with all decoded fields, or None if invalid.
    """
    block = bitstring[start_index:start_index + 96]
    if len(block) < 96:
        return None

    fixed1 = block[0:16]
    area_field = block[16:32]
    fixed2 = block[32:48]
    date_field = block[48:64]
    fixed3 = block[64:80]
    time_field = block[80:96]

    # All three fixed-code occurrences must be identical and valid
    if not (fixed1 == fixed2 == fixed3):
        return None

    area_res = decode_area_field(area_field)
    date_res = decode_date_field(date_field)
    time_res = decode_time_field(time_field)
    if area_res is None or date_res is None or time_res is None:
        return None

    area_is_start, area_name = area_res
    date_is_start, day, month, date_is_even = date_res
    time_is_start, hour, year_index, time_is_even = time_res

    # Consistency check: the start/end markers from all three fields
    # (there is no separate per-block precedence code any more) must agree
    if len({area_is_start, date_is_start, time_is_start}) != 1:
        return None
    is_start = area_is_start

    if fixed1 == FIXED_CODE_TYPE1_START_OR_ANY_END:
        signal_type = 1 if is_start else None  # undetermined for end signals
    elif fixed1 == FIXED_CODE_TYPE2_START and is_start:
        signal_type = 2
    else:
        return None

    return {
        "is_start": is_start,
        "signal_type": signal_type,  # 1, 2, or None (not encoded in end signals)
        "area": area_name,
        "day": day,
        "month": month,
        "hour": hour,
        "year_index": year_index,  # position in the 10-year cycle (0 = 1985, 1995, 2005, ...)
        "is_even_block": date_is_even or time_is_even,  # see encode_date_field()
        "block_length": 96,
    }


def find_blocks(bitstring: str):
    """Scans a complete bit string for all STRICTLY valid 96-bit blocks.
    Returns a list of (start_index, decoded_dict) tuples.
    """
    results = []
    i = 0
    n = len(bitstring)
    while i <= n - 96:
        parsed = try_parse_block_at(bitstring, i)
        if parsed is not None:
            results.append((i, parsed))
            i += 96  # jump straight past this block after a hit
        else:
            i += 1
    return results


def interpret_payload(payload16: str):
    """TOLERANT single-payload interpretation (16 bits): tries the area,
    date, and time field decoders in turn and returns the first result that
    matches a valid frame.

    Returns a dict {"kind": "area"|"date"|"time", "is_start": bool, ...},
    or None if none of the three decoders recognizes a valid frame (the
    caller then treats the payload as "not interpretable" and logs the raw
    bits for further inspection).
    """
    area_res = decode_area_field(payload16)
    if area_res is not None:
        is_start, area_name = area_res
        return {"kind": "area", "is_start": is_start, "area": area_name}

    date_res = decode_date_field(payload16)
    if date_res is not None:
        is_start, day, month, is_even = date_res
        return {"kind": "date", "is_start": is_start, "day": day, "month": month, "is_even_block": is_even}

    time_res = decode_time_field(payload16)
    if time_res is not None:
        is_start, hour, year_index, is_even = time_res
        return {"kind": "time", "is_start": is_start, "hour": hour, "year_index": year_index, "is_even_block": is_even}

    return None


def scan_subframes(bitstring: str):
    """TOLERANT scan: finds every occurrence of a valid 16-bit fixed code in
    the bit stream and tries to interpret the 16-bit payload right after
    it. Jumps 32 bits ahead after each hit (the length of one
    fixed+payload sub-unit), otherwise advances bit by bit.

    Returns a list of dicts:
        {
          "bit_index": int,
          "fixed_code_type": "I_or_end" | "II_start",
          "payload_raw": "16-bit string",
          "interpreted": <result of interpret_payload(), or None>,
        }
    """
    results = []
    i = 0
    n = len(bitstring)
    while i <= n - 32:
        fixed = bitstring[i:i + 16]
        if fixed == FIXED_CODE_TYPE1_START_OR_ANY_END:
            fc_type = "I_or_end"
        elif fixed == FIXED_CODE_TYPE2_START:
            fc_type = "II_start"
        else:
            i += 1
            continue

        payload = bitstring[i + 16:i + 32]
        results.append({
            "bit_index": i,
            "fixed_code_type": fc_type,
            "payload_raw": payload,
            "interpreted": interpret_payload(payload),
        })
        i += 32  # jump straight past this fixed+payload pair

    return results
