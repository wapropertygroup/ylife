# Entry point - run this file to start the yImage development server.
# Usage:  python run/run_image.py
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from yimage import create_app

app = create_app()

if __name__ == "__main__":
    app.run(debug=True, port=5005)
