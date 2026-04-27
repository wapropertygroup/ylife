# Entry point - run this file to start the Flask development server.
# Usage:  python run.py
from ystocker import create_app

app = create_app()

if __name__ == "__main__":
    app.run(debug=True)
