# Entry point - run this file to start the yPlanter development server.
# Usage:  python run_planter_garden.py
from yplanter import create_app

app = create_app()

if __name__ == "__main__":
    app.run(debug=True, port=5002)
