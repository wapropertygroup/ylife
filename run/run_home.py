# Entry point - run this file to start the yHome development server.
# Usage:  python run/run_home.py
import sys
from pathlib import Path

# Ensure the project root is on sys.path so that yhome is importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from yhome import create_app

app = create_app()

if __name__ == "__main__":
    app.run(debug=True, port=5003)
