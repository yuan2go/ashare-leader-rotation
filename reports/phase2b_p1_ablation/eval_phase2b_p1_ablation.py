"""Compatibility entry point for scripts/eval_phase2b_p1_ablation.py."""
from pathlib import Path
import runpy
if __name__ == '__main__':
    runpy.run_path(str(Path(__file__).resolve().parents[2] / 'scripts/eval_phase2b_p1_ablation.py'), run_name='__main__')
