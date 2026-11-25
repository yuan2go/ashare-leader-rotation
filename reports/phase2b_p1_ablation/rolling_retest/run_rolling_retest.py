"""Compatibility entry point for scripts/run_rolling_retest.py."""
from pathlib import Path
import runpy
if __name__ == '__main__':
    runpy.run_path(str(Path(__file__).resolve().parents[3] / 'scripts/run_rolling_retest.py'), run_name='__main__')
