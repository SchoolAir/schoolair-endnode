/*
 * sen6x_read.c — one-shot sensor read for the SchoolAir gateway.
 *
 * Usage:
 *   sen6x_read          Fast path: assumes continuous measurement is already
 *                       running (started by `sen6x_read --init` at boot via
 *                       sen6x.service).  Reads one sample, prints JSON to
 *                       stdout, exits.
 *
 *   sen6x_read --init   Slow path: full initialisation sequence (device reset,
 *                       start_continuous_measurement, wait for first valid
 *                       sample).  Run once at boot by sen6x.service before
 *                       the telemetry service starts.
 *
 *   sen6x_read --temp-offset <C>
 *                       Set the sensor's temperature offset (self-heating in
 *                       the enclosure), e.g. -4.7. Volatile; see
 *                       do_temp_offset(). Prints {"temp_offset_c": ...}.
 *
 * Exit codes:
 *   0   Success — valid JSON on stdout.
 *   1   I2C / sensor communication error.
 *   2   Sensor not ready after all retries.
 *   3   Bad --temp-offset argument.
 *
 * JSON output:
 *   {"sen6x": {"measured_at": "2026-06-24T12:05:01Z", "temp": 22.50, ...}}
 *
 * The "measured_at" field carries the C-side wall-clock time (UTC) at the
 * moment the I2C read returned, which is more accurate than the Python-side
 * "recorded_at" timestamp that is set when the subprocess exits.  Both are
 * preserved in the measurement buffer so the server can see any gap.
 */

#include "sen63c_i2c.h"
#include "sen65_i2c.h"
#include "sensirion_common.h"
#include "sensirion_i2c_hal.h"
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

/* Aliases matching sen6x_d.c so both files share the same naming convention */
#define sensirion_hal_sleep_us             sensirion_i2c_hal_sleep_usec
#define sen6x_init                         sen63c_init
#define sen6x_device_reset                 sen63c_device_reset
#define sen6x_get_product_name             sen63c_get_product_name
#define sen6x_start_continuous_measurement sen63c_start_continuous_measurement


/* ── Types ──────────────────────────────────────────────────────────────── */

typedef enum { SEN63C, SEN65, SENSOR_UNKNOWN } sensor_type_t;

typedef struct {
    float    pm10, pm25, pm40, pm100;
    float    temperature, humidity;
    union {
        struct { uint16_t co2;      } sen63c_data;
        struct { float    voc, nox; } sen65_data;
    } sensor_specific;
} sensor_reading_t;


/* ── Helpers ────────────────────────────────────────────────────────────── */

static sensor_type_t detect_sensor_type(void) {
    int8_t name[32] = {0};
    if (sen63c_get_product_name(name, 32) != NO_ERROR)
        return SENSOR_UNKNOWN;
    if (strstr((char *)name, "SEN63")) return SEN63C;
    if (strstr((char *)name, "SEN65")) return SEN65;
    return SENSOR_UNKNOWN;
}

static int16_t read_values(sensor_reading_t *r, sensor_type_t type) {
    if (type == SEN63C)
        return sen63c_read_measured_values(
            &r->pm10, &r->pm25, &r->pm40, &r->pm100,
            &r->humidity, &r->temperature,
            &r->sensor_specific.sen63c_data.co2);
    return sen65_read_measured_values(
        &r->pm10, &r->pm25, &r->pm40, &r->pm100,
        &r->humidity, &r->temperature,
        &r->sensor_specific.sen65_data.voc,
        &r->sensor_specific.sen65_data.nox);
}

/* SEN63C signals "no data yet" with co2 == 32767.
 * SEN65 has no equivalent placeholder — a successful read is a valid read. */
static int reading_is_ready(const sensor_reading_t *r, sensor_type_t type) {
    if (type == SEN63C)
        return r->sensor_specific.sen63c_data.co2 != 32767;
    return 1;
}

static void print_json(const sensor_reading_t *r, sensor_type_t type) {
    char ts[25];
    time_t now = time(NULL);
    strftime(ts, sizeof(ts), "%Y-%m-%dT%H:%M:%SZ", gmtime(&now));

    printf("{\"sen6x\": {\"measured_at\": \"%s\", "
           "\"temp\": %.2f, \"humidity\": %.2f, ",
           ts, r->temperature, r->humidity);

    if (type == SEN63C)
        printf("\"co2\": %u, ", r->sensor_specific.sen63c_data.co2);
    else
        printf("\"voc\": %.1f, \"nox\": %.1f, ",
               r->sensor_specific.sen65_data.voc,
               r->sensor_specific.sen65_data.nox);

    printf("\"pm10\": %.1f, \"pm25\": %.1f, \"pm40\": %.1f, \"pm100\": %.1f}}\n",
           r->pm10, r->pm25, r->pm40, r->pm100);
}


/* ── --init path ────────────────────────────────────────────────────────── */

/* Initialisation with fast-path fallback.
 *
 * First, try a direct read: if the sensor is already in continuous
 * measurement mode (e.g. a service restart, or the daemon ran previously)
 * there is no need to reset it — that would force an unnecessary ~45 s wait
 * for CO2 to re-stabilise.
 *
 * Only if the fast path fails do we run the full reset sequence.  When we
 * do reset, the SEN63C CO2 (NDIR) can take up to ~45 s to produce its first
 * valid reading, so we allow 25 × 3 s = 75 s before giving up. */
static int do_init(sensor_type_t type) {
    /* Fast path: sensor already in continuous mode. */
    sensor_reading_t r;
    int16_t error = read_values(&r, type);
    if (error == NO_ERROR && reading_is_ready(&r, type)) {
        fprintf(stderr, "[sen6x_read] sensor already running — skipping reset\n");
        print_json(&r, type);
        return 0;
    }

    /* Slow path: full reset and start. */
    fprintf(stderr, "[sen6x_read] starting full init sequence\n");
    error = sen6x_device_reset();
    if (error != NO_ERROR) {
        fprintf(stderr, "[sen6x_read] device_reset failed: %d\n", error);
        return 1;
    }
    sensirion_hal_sleep_us(1200000UL);   /* 1.2 s datasheet settling time */

    error = sen6x_start_continuous_measurement();
    if (error != NO_ERROR) {
        fprintf(stderr, "[sen6x_read] start_continuous_measurement failed: %d\n", error);
        return 1;
    }

    /* SEN63C CO2 needs up to ~45 s after a cold reset; allow 75 s. */
    for (int i = 0; i < 25; i++) {
        sensirion_hal_sleep_us(3000000UL);   /* 3 s */
        error = read_values(&r, type);
        if (error == NO_ERROR && reading_is_ready(&r, type)) {
            print_json(&r, type);
            return 0;
        }
        fprintf(stderr, "[sen6x_read] waiting for first valid reading (%d/25)...\n", i + 1);
    }

    fprintf(stderr, "[sen6x_read] no valid reading after 75 s — sensor may be faulty\n");
    return 2;
}


/* ── Fast read path ─────────────────────────────────────────────────────── */

/* Assumes continuous measurement is already running.  Three retries with a
 * 500 ms gap cover the rare case of calling exactly between two internal
 * measurement cycles (~1 s on SEN63C).  Exits non-zero on failure so
 * sensor.py raises RuntimeError and the calling read is skipped cleanly. */
static int do_read(sensor_type_t type) {
    sensor_reading_t r;
    int16_t error = NO_ERROR;

    for (int i = 0; i < 3; i++) {
        error = read_values(&r, type);
        if (error == NO_ERROR && reading_is_ready(&r, type)) {
            print_json(&r, type);
            return 0;
        }
        if (error != NO_ERROR)
            fprintf(stderr, "[sen6x_read] I2C error %d (attempt %d/3)\n", error, i + 1);
        else
            fprintf(stderr, "[sen6x_read] not ready — co2=32767 (attempt %d/3)\n", i + 1);
        sensirion_hal_sleep_us(500000UL);   /* 0.5 s */
    }

    if (error != NO_ERROR) {
        fprintf(stderr, "[sen6x_read] I2C communication failed\n");
        return 1;
    }
    fprintf(stderr, "[sen6x_read] sensor not ready — is sen6x.service running?\n");
    return 2;
}


/* ── --temp-offset path ─────────────────────────────────────────────────── */

/* Correct the sensor's temperature for self-heating in its enclosure (the dock
 * sits next to the Pi): T_compensated = T + offset. The sensor applies it
 * itself and also uses the compensated temperature for its humidity reading,
 * which subtracting in software could not do. Allowed while measuring. It is
 * volatile (a sensor reset clears it), so services/sensor.py sends it at every
 * telemetry start and after every re-init. Offset slot 0, slope 0, applied at
 * once (time constant 0).
 *
 * Prints {"temp_offset_c": <value>} on stdout when the sensor accepted it, so
 * the caller can tell this binary from an older one that doesn't know the
 * option (that one would just print a reading). */
static int do_temp_offset(sensor_type_t type, const char *arg) {
    char *end = NULL;
    double c = strtod(arg, &end);
    if (end == arg || *end != '\0' || c < -20.0 || c > 20.0) {
        fprintf(stderr, "[sen6x_read] --temp-offset needs a number of degrees C between -20 and 20\n");
        return 3;
    }
    int16_t scaled = (int16_t)(c * 200.0 + (c >= 0 ? 0.5 : -0.5));   /* datasheet: value / 200 = degrees C */
    int16_t error = (type == SEN63C)
        ? sen63c_set_temperature_offset_parameters(scaled, 0, 0, 0)
        : sen65_set_temperature_offset_parameters(scaled, 0, 0, 0);
    if (error != NO_ERROR) {
        fprintf(stderr, "[sen6x_read] set temperature offset failed: I2C error %d\n", error);
        return 1;
    }
    printf("{\"temp_offset_c\": %.3f}\n", scaled / 200.0);
    return 0;
}


/* ── Entry point ────────────────────────────────────────────────────────── */

int main(int argc, char *argv[]) {
    int init_mode = (argc > 1 && strcmp(argv[1], "--init") == 0);

    sensirion_i2c_hal_init();
    sen6x_init(0x6b);

    sensor_type_t type = detect_sensor_type();
    if (type == SENSOR_UNKNOWN) {
        fprintf(stderr, "[sen6x_read] cannot detect sensor (I2C error or sensor absent)\n");
        return 1;
    }
    if (type == SEN65)
        sen65_init(0x6b);

    if (argc > 1 && strcmp(argv[1], "--temp-offset") == 0) {
        if (argc < 3) {
            fprintf(stderr, "usage: sen6x_read --temp-offset <degrees C>\n");
            return 3;
        }
        return do_temp_offset(type, argv[2]);
    }
    return init_mode ? do_init(type) : do_read(type);
}
