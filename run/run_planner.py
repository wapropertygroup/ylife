# Entry point - run this file to start the yPlanner development server.
# Usage:  python run_planner.py
from yplanner import create_app

app = create_app()

if __name__ == "__main__":
    app.run(debug=True, port=5001)
