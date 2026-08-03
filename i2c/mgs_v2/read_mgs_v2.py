#!/usr/bin/env python3
"""Seeed Multichannel Gas Sensor v2 — reads all channels and prints JSON.

Usage: python3 read_mgs_v2.py [--addr 0x08] [--bus 1]

Output (stdout):
  {"mgs_v2": {"no2": <raw>, "c2h5oh": <raw>, "voc": <raw>, "co": <raw>}}

Values are raw ADC counts (uint32). Calibrated PPM conversion requires
sensor-specific calibration curves not supplied by this driver.

Exits non-zero and writes {"error": "..."} to stderr on failure.
"""
import argparse
import json
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
from multichannel_gas_gmxxx import MultichannelGasGMXXX

parser = argparse.ArgumentParser()
parser.add_argument("--addr", type=lambda x: int(x, 0), default=0x08)
parser.add_argument("--bus",  type=int,                  default=1)
args = parser.parse_args()

try:
    sensor = MultichannelGasGMXXX(bus_num=args.bus, address=args.addr)
    data = {
        "no2":    sensor.measure_no2(),
        "c2h5oh": sensor.measure_c2h5oh(),
        "voc":    sensor.measure_voc(),
        "co":     sensor.measure_co(),
    }
    sensor.close()
    print(json.dumps({"mgs_v2": data}))
except Exception as e:
    print(json.dumps({"error": str(e)}), file=sys.stderr)
    sys.exit(1)
