# Entry point - run this file to start the yPay development server.
# Usage:  python run/run_pay.py
import sys
from pathlib import Path

# Ensure the project root is on sys.path so that ypay is importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ypay import create_app

app = create_app()

if __name__ == "__main__":
    app.run(debug=True, port=5005)
