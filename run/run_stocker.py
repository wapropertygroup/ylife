# Entry point - run this file to start the Flask development server.
# Usage:  python run/run_stocker.py
import sys
from pathlib import Path

# Ensure the project root is on sys.path so that ystocker is importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ystocker import create_app

app = create_app()

if __name__ == "__main__":
    app.run(debug=True)
