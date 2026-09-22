/*
 * main.c - EWBS decoder proof-of-concept firmware for ATmega328P
 * =================================================================
 *
 * Pure avr-gcc / avr-libc, NO Arduino core, no bloated abstraction layers.
 * Target: any ATmega328P board (tested target: Arduino Nano, since it's a
 * cheap, readily available ATmega328P breakout with an onboard USB-serial
 * adapter and an LED already wired to PB5/D13).
 *
 * Hardware assumptions
 * ---------------------
 *   - Audio input on ADC0 (PC0/A0), AC-coupled and biased to Vcc/2 by a
 *     resistor divider, preceded by an analog low-pass filter (corner
 *     frequency around 1.5-2 kHz is plenty - see the sample-rate
 *     derivation below; the filter's job is just to keep out-of-band
 *     energy - music, speech harmonics above ~2 kHz - from aliasing back
 *     into the passband, it does not need to be sharp).
 *   - Alarm LED on PB5 (Arduino Nano's onboard LED, pin D13) - lets you
 *     get a visual result with zero extra wiring for first bring-up.
 *   - USART0 (PD0/PD1, i.e. the Nano's onboard USB-serial) for console
 *     status/alarm messages, 9600 baud 8N1.
 *
 * Sample rate derivation
 * ------------------------
 *   EWBS uses 640 Hz ("0") / 1024 Hz ("1") FSK at 64 bit/s, i.e. one bit
 *   period is exactly 15.625 ms. We want the LOWEST sample rate that (a)
 *   satisfies Nyquist for the 1024 Hz tone with reasonable margin for a
 *   simple analog anti-alias filter, and (b) makes the Goertzel detector
 *   as cheap and exact as possible.
 *
 *   Choosing fs = 4096 Hz gives EXACTLY 64 samples per bit period
 *   (4096 * 0.015625 = 64, no rounding at all), and both target
 *   frequencies land exactly on a Goertzel bin for a 64-sample window:
 *       k(640 Hz)  = 64 * 640  / 4096 = 10   (matches the spec's
 *       k(1024 Hz) = 64 * 1024 / 4096 = 16    "10/16 cycles per bit")
 *   Integer bins mean zero spectral leakage from the window length itself
 *   - about as clean as a fixed-length Goertzel detector can get. As a
 *   bonus, the Goertzel coefficient for exactly k/N = 1/4 (1024 Hz here)
 *   is 2*cos(pi/2) = 0, so the "mark" tone detector doesn't need a
 *   multiply at all.
 *
 *   Nyquist margin at fs=4096 Hz is 2048 Hz vs. a 1024 Hz tone (2x) -
 *   comfortable for a simple 1-2 pole RC/Sallen-Key low-pass ahead of the
 *   ADC, without needing a steep brick-wall filter.
 *
 * Goertzel implementation
 * -------------------------
 *   This is the STREAMING form: only two accumulator words (s1, s2) are
 *   kept per frequency, updated once per incoming sample - there is no
 *   need to buffer 64 samples at all, which keeps RAM usage tiny
 *   regardless of the window length. Coefficients are Q13 fixed-point
 *   (scale factor 8192), computed offline (see the comment above each
 *   constant) - no floating point or runtime trig on the MCU.
 *
 * Protocol / field decoding
 * ---------------------------
 *   This firmware ports the TOLERANT subframe scanner from the PC
 *   prototype's ewbs_common.py (scan_subframes()/interpret_payload()):
 *   it looks for a 16-bit fixed code, then decodes the following 16-bit
 *   payload as an area/date/time field. The area field is what drives the
 *   LED and the "ALARM"/"CLEAR" messages, since it's the field most
 *   robust against bit errors (see the project's PC-side README/commit
 *   history for the reverse-engineering process and validation against
 *   real recordings). Date/time fields are decoded and printed too, but
 *   don't affect the LED - they're supplementary information.
 *
 *   Bit- and field-level constants (fixed codes, day/month/hour/year/area
 *   tables) are reproduced exactly from ewbs_common.py - see
 *   ../../ewbs_common.py in this repository for the authoritative,
 *   independently-tested source and the full citation trail.
 */

#include <avr/io.h>
#include <avr/interrupt.h>
#include <avr/pgmspace.h>
#include <stdint.h>

/* ------------------------------------------------------------------ */
/* Configuration                                                      */
/* ------------------------------------------------------------------ */

#define F_CPU_HZ        16000000UL
#define UART_BAUD       9600UL
#define LED_DDR         DDRB
#define LED_PORT        PORTB
#define LED_BIT         PB5     /* Arduino Nano onboard LED / D13 */

/* ------------------------------------------------------------------ */
/* UART (bare register access, no <stdio.h> stream / no printf)       */
/* ------------------------------------------------------------------ */

static void uart_init(void)
{
    uint16_t ubrr = (uint16_t)((F_CPU_HZ / (16UL * UART_BAUD)) - 1);
    UBRR0H = (uint8_t)(ubrr >> 8);
    UBRR0L = (uint8_t)ubrr;
    UCSR0B = (1 << TXEN0);                      /* transmitter only */
    UCSR0C = (1 << UCSZ01) | (1 << UCSZ00);      /* 8N1 */
}

static void uart_putc(char c)
{
    while (!(UCSR0A & (1 << UDRE0))) {
        /* wait for the transmit buffer to be empty */
    }
    UDR0 = (uint8_t)c;
}

/* Prints a string that lives in RAM (e.g. a stack buffer built at
 * runtime, like the decimal digits in uart_put_u16() below). */
static void uart_puts(const char *s)
{
    while (*s) {
        uart_putc(*s++);
    }
}

/* Prints a string literal that lives in FLASH, not RAM. Always call this
 * as uart_puts_P(PSTR("...")) - plain string literals passed to
 * uart_puts(const char*) would otherwise be duplicated into RAM by
 * avr-gcc (the classic AVR Harvard-architecture gotcha), which matters a
 * lot on a part with only 2 KB of SRAM. */
static void uart_puts_P(const char *s)
{
    char c;
    while ((c = (char)pgm_read_byte(s++)) != '\0') {
        uart_putc(c);
    }
}

/* Prints an unsigned 16-bit value as decimal, no leading zeros. */
static void uart_put_u16(uint16_t v)
{
    char buf[6];
    int8_t i = 5;
    buf[i] = '\0';
    do {
        buf[--i] = (char)('0' + (v % 10));
        v /= 10;
    } while (v != 0);
    uart_puts(&buf[i]);
}

/* Prints an unsigned 32-bit value as decimal (used only for diagnostic
 * output - see DEBUG_ADC below - so no need to optimize this further). */
static void uart_put_u32(uint32_t v)
{
    char buf[11];
    int8_t i = 10;
    buf[i] = '\0';
    do {
        buf[--i] = (char)('0' + (v % 10));
        v /= 10;
    } while (v != 0);
    uart_puts(&buf[i]);
}

/* Prints an unsigned value as zero-padded 2-digit decimal (for day/hour). */
static void uart_put_u2(uint8_t v)
{
    uart_putc((char)('0' + (v / 10)));
    uart_putc((char)('0' + (v % 10)));
}

/* Prints a 16-bit value as 3 hex digits (area codes are 12 bit). */
static void uart_put_hex12(uint16_t v)
{
    static const char hexdig[] PROGMEM = "0123456789ABCDEF";
    uart_putc('0');
    uart_putc('x');
    uart_putc((char)pgm_read_byte(&hexdig[(v >> 8) & 0xF]));
    uart_putc((char)pgm_read_byte(&hexdig[(v >> 4) & 0xF]));
    uart_putc((char)pgm_read_byte(&hexdig[v & 0xF]));
}

/* Prints a 16-bit value as 4 hex digits (fixed codes are 16 bit). */
static void uart_put_hex16(uint16_t v)
{
    static const char hexdig[] PROGMEM = "0123456789ABCDEF";
    uart_putc('0');
    uart_putc('x');
    uart_putc((char)pgm_read_byte(&hexdig[(v >> 12) & 0xF]));
    uart_putc((char)pgm_read_byte(&hexdig[(v >> 8) & 0xF]));
    uart_putc((char)pgm_read_byte(&hexdig[(v >> 4) & 0xF]));
    uart_putc((char)pgm_read_byte(&hexdig[v & 0xF]));
}

/* ------------------------------------------------------------------ */
/* Protocol tables (verbatim from ewbs_common.py - do not hand-edit;  */
/* regenerate from the Python source if the tables ever change)       */
/* ------------------------------------------------------------------ */

#define FIXED_CODE_TYPE1_START_OR_ANY_END  0x0E6DU  /* 0000111001101101 */
#define FIXED_CODE_TYPE2_START             0xF192U  /* 1111000110010010 */

/* index 0 = day 1 ... index 30 = day 31 */
static const uint8_t day_codes[31] PROGMEM = {
    16, 8, 24, 4, 20, 12, 28, 2, 18, 10, 26, 6, 22, 14, 30, 1, 17, 9, 25, 5,
    21, 13, 29, 3, 19, 11, 27, 7, 23, 15, 31,
};

/* index 0 = month 1 ... index 11 = month 12 */
static const uint8_t month_codes[12] PROGMEM = {
    17, 9, 25, 5, 21, 13, 29, 3, 19, 11, 27, 7,
};

/* index 0 = hour 0 ... index 23 = hour 23 */
static const uint8_t hour_codes[24] PROGMEM = {
    3, 19, 11, 27, 7, 23, 15, 31, 1, 17, 9, 25, 5, 21, 13, 29, 2, 18, 10,
    26, 6, 22, 14, 30,
};

/* index 0..9 = 10-year cycle starting 1985 (Showa 60) */
static const uint8_t year_codes[10] PROGMEM = {
    21, 13, 29, 3, 19, 11, 17, 9, 25, 5,
};

/* 53 area codes: nationwide + wide-area + all 47 prefectures.
 * Index -> name legend is documented in ewbs_common.py / the PC tools;
 * this firmware only reports the numeric index and raw 12-bit code,
 * since we're driving an LED and a serial console, not a display. */
#define AREA_CODE_COUNT 53
static const uint16_t area_codes[AREA_CODE_COUNT] PROGMEM = {
    0x34D, 0x5A5, 0x72A, 0x8D5, 0x699, 0x553,
    0x16B, 0x467, 0x5D4, 0x758, 0xAC6, 0xE4C,
    0x1AE, 0xC69, 0xE38, 0x98B, 0x64B, 0x1C7,
    0xAAC, 0x56C, 0x4CE, 0x539, 0x6A6, 0x92D,
    0xD4A, 0x9D2, 0xA65, 0xA5A, 0x966, 0x2DC,
    0xCE4, 0x59A, 0xCB2, 0x674, 0xA93, 0x396,
    0xD23, 0x31B, 0x2B5, 0xB31, 0xB98, 0xE62,
    0x9B4, 0x19D, 0x2E3, 0x62D, 0x959, 0xA2B,
    0x8A7, 0xC8D, 0xD1C, 0xD45, 0x372,
};

/* Linear search through a small PROGMEM table. All these tables are tiny
 * (<= 53 entries) and decoding only happens once per 32-bit subframe
 * (roughly every 8 ms of audio at best), so this is more than fast enough
 * - no need for anything fancier on a part this small. */
static int8_t lookup_progmem_u8(const uint8_t *table, uint8_t count, uint8_t value)
{
    for (uint8_t i = 0; i < count; i++) {
        if (pgm_read_byte(&table[i]) == value) {
            return (int8_t)i;
        }
    }
    return -1;
}

static int8_t lookup_area_index(uint16_t code12)
{
    for (uint8_t i = 0; i < AREA_CODE_COUNT; i++) {
        if (pgm_read_word(&area_codes[i]) == code12) {
            return (int8_t)i;
        }
    }
    return -1;
}

/* ------------------------------------------------------------------ */
/* Goertzel tone detector (fixed-point, Q13 coefficients)             */
/* ------------------------------------------------------------------ */

#define GOERTZEL_SHIFT   13
/* coeff = 2*cos(2*pi*10/64) * 8192, see the derivation in the module
 * docstring above; k=10 corresponds to the 640 Hz "space" tone. */
#define COEFF_640_Q13    9102
/* coeff = 2*cos(2*pi*16/64) * 8192 = 2*cos(pi/2)*8192 = 0 exactly;
 * k=16 corresponds to the 1024 Hz "mark" tone. */
#define COEFF_1024_Q13   0

typedef struct {
    int32_t s1;
    int32_t s2;
} goertzel_state_t;

static inline void goertzel_reset(goertzel_state_t *g)
{
    g->s1 = 0;
    g->s2 = 0;
}

/* One Goertzel update per incoming sample. `sample` is the ADC reading,
 * already centered around 0 (see adc_isr()). */
static inline void goertzel_step(goertzel_state_t *g, int16_t coeff_q13, int16_t sample)
{
    int32_t s = (int32_t)sample + (((int32_t)coeff_q13 * g->s1) >> GOERTZEL_SHIFT) - g->s2;
    g->s2 = g->s1;
    g->s1 = s;
}

/* Goertzel power (proportional to squared magnitude at the target bin).
 * We only ever compare power_640 against power_1024, so the missing
 * normalization constants (which are the same for both bins here since N
 * is identical) cancel out - no need to compute an absolute magnitude. */
static inline int32_t goertzel_power(const goertzel_state_t *g, int16_t coeff_q13)
{
    int32_t cross = (((int32_t)coeff_q13 * g->s1) >> GOERTZEL_SHIFT) * g->s2;
    return g->s1 * g->s1 + g->s2 * g->s2 - cross;
}

/* ------------------------------------------------------------------ */
/* Bit-level state (updated once per 15.625 ms bit period)            */
/* ------------------------------------------------------------------ */

static goertzel_state_t g_space;   /* 640 Hz  */
static goertzel_state_t g_mark;    /* 1024 Hz */
static uint8_t sample_in_bit = 0;  /* 0..63   */

#define SAMPLES_PER_BIT  64

/* --------------------------------------------------------------------
 * DIAGNOSTIC / BRING-UP INSTRUMENTATION
 * --------------------------------------------------------------------
 * Set DEBUG_ADC to 1 to get one diagnostic line per second over UART,
 * showing the raw ADC swing and the last bit period's Goertzel power
 * values. This is for hardware bring-up ONLY - printing happens in the
 * main loop (never in the ISR, which has no time budget for a ~1ms/char
 * UART transmission at 9600 baud), triggered by a flag the ISR merely
 * sets. Once your analog front end is verified working, set this back
 * to 0 for normal operation.
 *
 * How to read the output:
 *   raw_min/raw_max  - the smallest/largest ADC reading (0..1023) seen
 *                       during the last 64-sample bit window. With no
 *                       tone present these should sit close together,
 *                       near 512 (i.e. your bias point). While a 640 Hz
 *                       or 1024 Hz tone is playing, they should swing
 *                       symmetrically further away from 512 - the wider
 *                       the swing, the stronger your signal. If they
 *                       never move at all, no signal is reaching ADC0 -
 *                       check wiring, common ground, and that something
 *                       is actually being played into the front end.
 *   p_space/p_mark   - Goertzel power for 640 Hz / 1024 Hz from the last
 *                       completed bit period. Compare these against
 *                       MIN_TONE_POWER: during a real tone, the higher
 *                       of the two should clear MIN_TONE_POWER by a
 *                       comfortable margin. If raw_min/raw_max show a
 *                       healthy swing but p_space/p_mark stay tiny,
 *                       double check the AC-coupling capacitor value -
 *                       too small a cap forms an unintended high-pass
 *                       filter that can attenuate 640/1024 Hz.
 */
#define DEBUG_ADC 0

#if DEBUG_ADC
static volatile int16_t dbg_raw_min = 1023;
static volatile int16_t dbg_raw_max = 0;
static volatile int32_t dbg_p_space = 0;
static volatile int32_t dbg_p_mark = 0;
static volatile uint8_t dbg_ready = 0;
static uint16_t dbg_bit_counter = 0;
#endif

/* Minimum power (either bin) to accept a bit as a valid tone rather than
 * silence/noise. This threshold is in raw Goertzel-power units and will
 * need tuning against your actual analog front end - start conservative
 * and lower it if genuine tones are being missed, raise it if noise
 * between transmissions is triggering false bit detections. */
#define MIN_TONE_POWER   20000L

/* Result of classifying one bit period. BIT_NONE means "no tone /
 * inconclusive" (silence, program audio, noise) and - like the 'X'
 * character in the PC prototype's bit string - reliably breaks any
 * in-progress fixed-code/payload match, which is exactly what we want:
 * a real EWBS transmission has no gaps inside a block. */
typedef enum { BIT_NONE = 0, BIT_ZERO, BIT_ONE } bit_result_t;

/* ------------------------------------------------------------------ */
/* Subframe-level state machine (fixed code + 16-bit payload)         */
/* ------------------------------------------------------------------ */

typedef enum { SCAN_SEARCHING, SCAN_CAPTURING } scan_state_t;

static scan_state_t scan_state = SCAN_SEARCHING;
static uint16_t window = 0;          /* rolling 16-bit fixed-code window */
static uint16_t payload = 0;         /* 16-bit payload accumulator */
static uint8_t payload_bits = 0;     /* 0..16 bits captured so far */
static uint16_t matched_fixed_code = 0;

static volatile uint8_t alarm_active = 0;

static void report_area(uint16_t payload16, uint16_t fixed_code)
{
    uint8_t prefix = (payload16 >> 14) & 0x3;
    uint8_t suffix = payload16 & 0x3;
    uint16_t area12 = (payload16 >> 2) & 0x0FFF;
    uint8_t is_start;

    if (prefix == 0x2 && suffix == 0x0) {         /* "10"..."00" */
        is_start = 1;
    } else if (prefix == 0x1 && suffix == 0x3) {  /* "01"..."11" */
        is_start = 0;
    } else {
        return; /* not a valid area field */
    }

    int8_t idx = lookup_area_index(area12);
    if (idx < 0) {
        return; /* 12-bit payload didn't match any known area code */
    }

    if (is_start) {
        alarm_active = 1;
        LED_PORT |= (1 << LED_BIT);
        uart_puts_P(PSTR("ALARM START area_idx="));
        uart_put_u16((uint16_t)idx);
        uart_puts_P(PSTR(" area_code="));
        uart_put_hex12(area12);
        uart_puts_P(PSTR(" fixed="));
        uart_put_hex16(fixed_code);
        uart_puts_P(PSTR("\r\n"));
    } else {
        alarm_active = 0;
        LED_PORT &= (uint8_t)~(1 << LED_BIT);
        uart_puts_P(PSTR("CLEAR END area_idx="));
        uart_put_u16((uint16_t)idx);
        uart_puts_P(PSTR(" area_code="));
        uart_put_hex12(area12);
        uart_puts_P(PSTR("\r\n"));
    }
}

static void report_date(uint16_t payload16)
{
    uint8_t prefix = (payload16 >> 13) & 0x7;
    uint8_t suffix = payload16 & 0x3;
    uint8_t day_code = (payload16 >> 8) & 0x1F;
    uint8_t even_flag = (payload16 >> 7) & 0x1;
    uint8_t month_code = (payload16 >> 2) & 0x1F;
    uint8_t is_start;

    if (prefix == 0x2 && suffix == 0x0) {         /* "010"..."00" */
        is_start = 1;
    } else if (prefix == 0x4 && suffix == 0x3) {  /* "100"..."11" */
        is_start = 0;
    } else {
        return;
    }

    int8_t day = lookup_progmem_u8(day_codes, 31, day_code);
    int8_t month = lookup_progmem_u8(month_codes, 12, month_code);
    if (day < 0 || month < 0) {
        return;
    }

    uart_puts_P(is_start ? PSTR("INFO date(start) ") : PSTR("INFO date(end) "));
    uart_puts_P(PSTR("day="));
    uart_put_u2((uint8_t)(day + 1));
    uart_puts_P(PSTR(" month="));
    uart_put_u2((uint8_t)(month + 1));
    uart_puts_P(even_flag ? PSTR(" even_block=1\r\n") : PSTR(" even_block=0\r\n"));
}

static void report_time(uint16_t payload16)
{
    uint8_t prefix = (payload16 >> 13) & 0x7;
    uint8_t suffix = payload16 & 0x3;
    uint8_t hour_code = (payload16 >> 8) & 0x1F;
    uint8_t even_flag = (payload16 >> 7) & 0x1;
    uint8_t year_code = (payload16 >> 2) & 0x1F;
    uint8_t is_start;

    if (prefix == 0x3 && suffix == 0x0) {         /* "011"..."00" */
        is_start = 1;
    } else if (prefix == 0x5 && suffix == 0x3) {  /* "101"..."11" */
        is_start = 0;
    } else {
        return;
    }

    int8_t hour = lookup_progmem_u8(hour_codes, 24, hour_code);
    int8_t year_idx = lookup_progmem_u8(year_codes, 10, year_code);
    if (hour < 0 || year_idx < 0) {
        return;
    }

    uart_puts_P(is_start ? PSTR("INFO time(start) ") : PSTR("INFO time(end) "));
    uart_puts_P(PSTR("hour="));
    uart_put_u2((uint8_t)hour);
    uart_puts_P(PSTR(" year_cycle_idx="));
    uart_put_u16((uint16_t)year_idx);
    uart_puts_P(even_flag ? PSTR(" even_block=1\r\n") : PSTR(" even_block=0\r\n"));
}

/* Called once a full 16-bit payload has been captured right after a
 * recognized fixed code. Tries area, then date, then time - exactly the
 * priority order used by interpret_payload() in ewbs_common.py. */
static void handle_payload(uint16_t payload16, uint16_t fixed_code)
{
    uint8_t prefix2 = (payload16 >> 14) & 0x3;
    uint8_t suffix2 = payload16 & 0x3;
    if ((prefix2 == 0x2 && suffix2 == 0x0) || (prefix2 == 0x1 && suffix2 == 0x3)) {
        report_area(payload16, fixed_code);
        return;
    }
    report_date(payload16);
    report_time(payload16);
}

/* Feeds one newly classified bit into the subframe state machine. This
 * is the direct C equivalent of scan_subframes() in ewbs_common.py,
 * exploiting the fact that on real hardware we process one bit at a time
 * rather than searching a whole buffered string: a 16-bit shift register
 * is all the state a fixed-code search needs. */
static void feed_bit(bit_result_t bit)
{
    if (bit == BIT_NONE) {
        /* Silence/noise breaks any in-progress match, same as the 'X'
         * character in the PC prototype. */
        scan_state = SCAN_SEARCHING;
        window = 0;
        payload_bits = 0;
        return;
    }

    uint8_t b = (bit == BIT_ONE) ? 1 : 0;

    if (scan_state == SCAN_SEARCHING) {
        window = (uint16_t)((window << 1) | b);
        if (window == FIXED_CODE_TYPE1_START_OR_ANY_END) {
            matched_fixed_code = FIXED_CODE_TYPE1_START_OR_ANY_END;
            scan_state = SCAN_CAPTURING;
            payload = 0;
            payload_bits = 0;
        } else if (window == FIXED_CODE_TYPE2_START) {
            matched_fixed_code = FIXED_CODE_TYPE2_START;
            scan_state = SCAN_CAPTURING;
            payload = 0;
            payload_bits = 0;
        }
    } else { /* SCAN_CAPTURING */
        payload = (uint16_t)((payload << 1) | b);
        payload_bits++;
        if (payload_bits == 16) {
            handle_payload(payload, matched_fixed_code);
            /* Per the protocol, the next 16 bits are guaranteed to be
             * another fixed code (sub-frames are contiguous), so it's
             * safe to just reset and resume searching - no bits are
             * lost, ewbs_common.py's scan_subframes() does the same. */
            scan_state = SCAN_SEARCHING;
            window = 0;
        }
    }
}

/* ------------------------------------------------------------------ */
/* Timer1: fires at 4096 Hz and manually starts each ADC conversion.  */
/* (Deliberately NOT using the ADC's built-in "auto-trigger from      */
/* Timer1 Compare Match B" hardware feature - see timer1_init() for   */
/* why.)                                                              */
/* ------------------------------------------------------------------ */

ISR(TIMER1_COMPA_vect)
{
    ADCSRA |= (1 << ADSC);   /* start the next conversion */
}

/* ------------------------------------------------------------------ */
/* ADC interrupt: fires once each conversion completes, ~4096 Hz      */
/* (started by ISR(TIMER1_COMPA_vect) above).                          */
/* ------------------------------------------------------------------ */

ISR(ADC_vect)
{
    /* ADCL must be read before ADCH; reading the combined 16-bit ADC
     * register does this in the correct order. Center the 10-bit
     * unsigned reading (0..1023) around 0 assuming the analog front end
     * biases the signal to the middle of the ADC's input range. */
    uint16_t raw = ADC;
    int16_t sample = (int16_t)raw - 512;

#if DEBUG_ADC
    if ((int16_t)raw < dbg_raw_min) dbg_raw_min = (int16_t)raw;
    if ((int16_t)raw > dbg_raw_max) dbg_raw_max = (int16_t)raw;
#endif

    goertzel_step(&g_space, COEFF_640_Q13, sample);
    goertzel_step(&g_mark, COEFF_1024_Q13, sample);

    sample_in_bit++;
    if (sample_in_bit >= SAMPLES_PER_BIT) {
        sample_in_bit = 0;

        int32_t p_space = goertzel_power(&g_space, COEFF_640_Q13);
        int32_t p_mark = goertzel_power(&g_mark, COEFF_1024_Q13);

        bit_result_t bit;
        if (p_space < MIN_TONE_POWER && p_mark < MIN_TONE_POWER) {
            bit = BIT_NONE;
        } else {
            bit = (p_mark > p_space) ? BIT_ONE : BIT_ZERO;
        }

        goertzel_reset(&g_space);
        goertzel_reset(&g_mark);

#if DEBUG_ADC
        dbg_p_space = p_space;
        dbg_p_mark = p_mark;
        dbg_bit_counter++;
        if (dbg_bit_counter >= 64) {  /* ~1 second at 64 bit/s */
            dbg_bit_counter = 0;
            dbg_ready = 1;
            /* Heartbeat: toggle the LED once per second, completely
             * independent of any tone/threshold logic. This is the most
             * direct possible hardware proof that Timer1 + the ADC
             * auto-trigger + ISR(ADC_vect) are actually running: if you
             * see the LED blink about once a second (with NO audio
             * playing at all), the whole sampling chain is confirmed
             * healthy and any remaining "no ALARM" issue is downstream
             * (analog front end level, MIN_TONE_POWER threshold, or bit
             * sync) rather than in interrupt/timer setup. If the LED
             * never blinks, the problem is here (or in the hardware
             * feeding this interrupt chain), not in signal detection. */
            LED_PORT ^= (1 << LED_BIT);
        }
#endif

        /* Bit-level decoding happens right here in the ISR. This keeps
         * the design simple (no producer/consumer queue to the main
         * loop) and is cheap enough: feed_bit() is O(1) - a handful of
         * shifts/compares - and the table lookups in handle_payload()
         * only run once every 32 bits (roughly every 250 ms at most),
         * which comfortably fits within the ~244us we have before the
         * next sample interrupt even on a 16 MHz ATmega328P. If you
         * extend this firmware with slower reporting (e.g. a full
         * printf-style summary), move that part out to the main loop via
         * a flag instead of doing it here.
         */
        feed_bit(bit);
    }
}

/* ------------------------------------------------------------------ */
/* Peripheral setup                                                   */
/* ------------------------------------------------------------------ */

static void adc_init(void)
{
    /* AVcc reference, ADC0 (PC0/A0) as input. */
    ADMUX = (1 << REFS0);

    /* ADC clock = F_CPU/128 = 125 kHz, within the 50-200 kHz range
     * required for full 10-bit accuracy. A single conversion takes about
     * 13 ADC clock cycles (~104 us) once auto-triggering is running,
     * comfortably faster than our 244 us sample period.
     *
     * Conversions are started manually from ISR(TIMER1_COMPA_vect)
     * rather than via the ADC's own auto-trigger-from-Timer1-Compare-B
     * hardware feature. Both work equally well on real silicon, but the
     * manual-start approach is simpler to reason about, easier to debug
     * (a plain Timer1 compare interrupt is about as standard as AVR
     * peripherals get), and sidesteps a real-world case where the
     * auto-trigger path produced no observable ADC interrupts under the
     * simavr simulator used during bring-up of this firmware - whether
     * that was a simulator limitation or a genuine hardware subtlety,
     * this path avoids the question entirely. */
    ADCSRA = (1 << ADEN) | (1 << ADIE)
           | (1 << ADPS2) | (1 << ADPS1) | (1 << ADPS0);
}

static void timer1_init(void)
{
    /* CTC mode, TOP = OCR1A, prescaler = 1.
     * OCR1A = 3905 -> f = 16MHz / (1 * 3906) = 4096.26 Hz (0.006% off
     * the ideal 4096 Hz target - utterly negligible for a 15.625 ms bit
     * window; see the sample-rate derivation in the file header).
     * OCIE1A enables the Compare A Match interrupt, which we use to
     * manually kick off each ADC conversion (see adc_init() and
     * ISR(TIMER1_COMPA_vect) above) rather than relying on the ADC's own
     * auto-trigger hardware. */
    TCCR1A = 0;
    TCCR1B = (1 << WGM12) | (1 << CS10);   /* CTC, TOP=OCR1A, prescaler=1 */
    OCR1A = 3905;
    TIMSK1 = (1 << OCIE1A);
}

static void led_init(void)
{
    LED_DDR |= (1 << LED_BIT);
    LED_PORT &= (uint8_t)~(1 << LED_BIT);
}

int main(void)
{
    uart_init();
    led_init();
    timer1_init();
    adc_init();

    sei();

    uart_puts_P(PSTR("EWBS decoder PoC - ATmega328P, fs=4096Hz, N=64 samples/bit\r\n"));
    uart_puts_P(PSTR("Waiting for signal...\r\n"));
#if DEBUG_ADC
    uart_puts_P(PSTR("DEBUG_ADC=1: printing raw_min/raw_max/p_space/p_mark once per second.\r\n"));
    uart_puts_P(PSTR("(set DEBUG_ADC to 0 in main.c once your front end is verified working)\r\n"));
    /* One-shot register dump right after init, to catch any setup bug
     * independent of timing. */
    uart_puts_P(PSTR("REG ADCSRA=0x"));
    { char h[3] = {0}; uint8_t v = ADCSRA;
      h[0] = "0123456789ABCDEF"[v >> 4]; h[1] = "0123456789ABCDEF"[v & 0xF];
      uart_puts(h); }
    uart_puts_P(PSTR(" ADCSRB=0x"));
    { char h[3] = {0}; uint8_t v = ADCSRB;
      h[0] = "0123456789ABCDEF"[v >> 4]; h[1] = "0123456789ABCDEF"[v & 0xF];
      uart_puts(h); }
    uart_puts_P(PSTR(" ADMUX=0x"));
    { char h[3] = {0}; uint8_t v = ADMUX;
      h[0] = "0123456789ABCDEF"[v >> 4]; h[1] = "0123456789ABCDEF"[v & 0xF];
      uart_puts(h); }
    uart_puts_P(PSTR(" TCCR1A=0x"));
    { char h[3] = {0}; uint8_t v = TCCR1A;
      h[0] = "0123456789ABCDEF"[v >> 4]; h[1] = "0123456789ABCDEF"[v & 0xF];
      uart_puts(h); }
    uart_puts_P(PSTR(" TCCR1B=0x"));
    { char h[3] = {0}; uint8_t v = TCCR1B;
      h[0] = "0123456789ABCDEF"[v >> 4]; h[1] = "0123456789ABCDEF"[v & 0xF];
      uart_puts(h); }
    uart_puts_P(PSTR(" OCR1A="));
    uart_put_u16(OCR1A);
    uart_puts_P(PSTR(" TIMSK1=0x"));
    { char h[3] = {0}; uint8_t v = TIMSK1;
      h[0] = "0123456789ABCDEF"[v >> 4]; h[1] = "0123456789ABCDEF"[v & 0xF];
      uart_puts(h); }
    uart_puts_P(PSTR(" SREG_I="));
    uart_putc((char)('0' + ((SREG >> 7) & 1)));
    uart_puts_P(PSTR("\r\n"));
#endif

    for (;;) {
#if DEBUG_ADC
        if (dbg_ready) {
            /* Snapshot the volatiles with interrupts briefly disabled so
             * we print a self-consistent set of values, then reset the
             * min/max tracking for the next window. This tiny critical
             * section is far shorter than one sample period, so it does
             * not disturb the 4096 Hz sampling. */
            int16_t raw_min, raw_max;
            int32_t p_space, p_mark;
            cli();
            raw_min = dbg_raw_min;
            raw_max = dbg_raw_max;
            p_space = dbg_p_space;
            p_mark = dbg_p_mark;
            dbg_raw_min = 1023;
            dbg_raw_max = 0;
            dbg_ready = 0;
            sei();

            uart_puts_P(PSTR("DEBUG raw_min="));
            uart_put_u16((uint16_t)raw_min);
            uart_puts_P(PSTR(" raw_max="));
            uart_put_u16((uint16_t)raw_max);
            uart_puts_P(PSTR(" swing="));
            uart_put_u16((uint16_t)(raw_max - raw_min));
            uart_puts_P(PSTR(" p_space="));
            uart_put_u32((uint32_t)(p_space < 0 ? 0 : p_space));
            uart_puts_P(PSTR(" p_mark="));
            uart_put_u32((uint32_t)(p_mark < 0 ? 0 : p_mark));
            uart_puts_P(PSTR(" thresh="));
            uart_put_u32((uint32_t)MIN_TONE_POWER);
            uart_puts_P(PSTR("\r\n"));
        }
#endif
        /* All decoding happens in ISR(ADC_vect); the main loop is free
         * for other housekeeping (a watchdog reset, power management,
         * driving a display, etc.) in a future extension. */
    }
}