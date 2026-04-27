# Entry point - run this file to start the yHome development server.
# Usage:  python run/run_home.py
from yhome import create_app

app = create_app()

if __name__ == "__main__":
    app.run(debug=True, port=5003)
