import os
import sys

# Ensure the root directory and lifeguard package are in the Python import path
sys.path.insert(0, os.path.dirname(__file__))

# Import your Flask application instance from the lifeguard package
from lifeguard.app import create_app

# Instantiate Flask without background worker threads/cameras for Vercel serverless environment
app = create_app(start_cameras=False, start_maintenance=False)

# Expose 'app' for Vercel's WSGI serverless handler
if __name__ == "__main__":
    app.run()