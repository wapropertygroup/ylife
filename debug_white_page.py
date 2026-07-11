
import os
import sys
from pathlib import Path

# Ensure the project root is on sys.path so that ystocker is importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from ystocker import create_app
    app = create_app()
    with app.test_request_context():
        from flask import render_template, url_for
        # Mock what the root route does (redirect to markets)
        from flask import redirect
        print("Creating app SUCCESS")
        
        # Test rendering markets.html directly
        try:
            html = render_template("markets.html", peer_groups=[])
            print("Rendering markets.html SUCCESS")
            print("HTML length:", len(html))
            if len(html) < 100:
                print("HTML content:", html)
        except Exception as e:
            print("Rendering markets.html FAILED:", e)
            import traceback
            traceback.print_exc()

except Exception as e:
    print("App creation FAILED:", e)
    import traceback
    traceback.print_exc()
