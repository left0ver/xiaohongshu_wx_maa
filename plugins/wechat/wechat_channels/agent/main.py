"""Standard PI entry directory required by the host's asset packer."""

import runpy
import sys
from pathlib import Path

collector = Path(__file__).resolve().parents[1] / "collector"
sys.path.insert(0, str(collector))
runpy.run_path(str(collector / "main.py"), run_name="__main__")
