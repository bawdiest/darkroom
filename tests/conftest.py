"""Der Hauptordner muss vor den Testmodulen im Suchpfad stehen (die Tests importieren darkroom.cvx schon beim Laden)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
