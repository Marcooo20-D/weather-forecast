#!/usr/bin/env python3
"""
LANGIT v61.1 repair injector.

Pakai di ROOT repo:
  python langit_v61_1_repair_injector.py

Fungsi:
1. Backup weather_ensemble_multi_location.py
2. Hapus hotfix v61.1 lama kalau ada
3. Sisipkan sanitizer v61.1 sebelum if __name__ == "__main__"
4. Compile check supaya syntax error ketahuan sebelum commit

Catatan:
- Ini tidak menurunkan kualitas v61 sebelumnya.
- Ini menambal sumber error terbaru: Verify public output gagal karena HTML lama
  masih mengandung "ANEMOS sedang..." / branding lama dari accuracy renderer.
"""

from __future__ import annotations

from pathlib import Path
import py_compile
import re
import shutil
import sys
from datetime import datetime

TARGET = Path("weather_ensemble_multi_location.py")
START = "# ---------- LANGIT v61.1 PUBLIC OUTPUT SANITIZER: START ----------"
END = "# ---------- LANGIT v61.1 PUBLIC OUTPUT SANITIZER: END ----------"

HOTFIX = r