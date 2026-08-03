#!/usr/bin/env python3
"""DFRobot Ozone Sensor — reads concentration and prints JSON.

Usage: python3 read_o3.py [--addr 0x73] [--bus 1] [--samples 20]

Output (stdout):
  {"o3": {"o3_ppb": <float>}}

The sensor internally averages --samples readings before returning.
20–50 samples is recommended; fewer = faster but noisier.

Exits non-zero and writes {"error": "..."} to stderr on failure.

Supported I2C addresses (set by DIP switches on the sensor):
  0x70  OZONE_ADDRESS_0
  0x71  OZONE_ADDRESS_1
  0x72  OZONE_ADDRESS_2
  0x73  OZONE_ADDRESS_3  (default)
"""
import argparse
import json
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
from DFRobot_Ozone import DFRobot_Ozone_IIC, MEASURE_MODE_PASSIVE

parser = argparse.ArgumentParser()
parser.add_argument("--addr",    type=lambda x: int(x, 0), default=0x73)
parser.add_argument("--bus",     type=int,                  default=1)
parser.add_argument("--samples", type=int,                  default=20)
args = parser.parse_args()

try:
    ozone = DFRobot_Ozone_IIC(args.bus, args.addr)
    ozone.set_mode(MEASURE_MODE_PASSIVE)
    reading = ozone.get_ozone_data(args.samples)
    print(json.dumps({"o3": {"o3_ppb": reading}}))
except Exception as e:
    print(json.dumps({"error": str(e)}), file=sys.stderr)
    sys.exit(1)
