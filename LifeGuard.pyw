"""Runs LifeGuard in the background with no window (started automatically at Windows log-in)."""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

from lifeguard.serve import main  # noqa: E402

sys.exit(main())
