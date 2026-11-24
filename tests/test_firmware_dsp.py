"""Builds tests/fw/test_dsp.cpp with the host compiler and runs it (the firmware's signal processing)."""
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.mark.skipif(shutil.which("g++") is None, reason="needs g++")
def test_firmware_dsp(tmp_path):
    exe = tmp_path / "test_dsp"
    subprocess.run(["g++", "-std=c++17", "-Wall", "-Wextra", "-Werror", "-I", os.path.join(ROOT, "firmware", "driver_node"),
                    os.path.join(ROOT, "tests", "fw", "test_dsp.cpp"), "-o", str(exe)], check=True)
    r = subprocess.run([str(exe)], capture_output=True, text=True)
    print(r.stdout)
    assert r.returncode == 0, r.stdout
