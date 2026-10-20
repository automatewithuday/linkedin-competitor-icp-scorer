"""Qualification core entrypoint: guided ICP -> calibration -> saved evidence -> company fit -> person fit -> CSV.

See docs/OPERATIONS.md for commands. `uv run run.py --help` lists the flags.
"""
from src.common import cli
from src.core import main

if __name__ == "__main__":
    cli(main)
