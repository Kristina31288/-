import sys
from pathlib import Path

# Модули бота лежат в родительской папке (без пакета) — добавляем её в путь.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
