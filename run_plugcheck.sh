#!/bin/bash
cd "$(dirname "$0")"
./venv/bin/python -u kona_plug_check.py "$@"
