import sys
from pathlib import Path

# scripts/ on sys.path so tests can import update_readme directly
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
