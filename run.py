"""LifeGuard entry point:  python run.py [--host 0.0.0.0] [--port 5000] [--no-cameras]"""
import argparse
import atexit
import logging
import os

from lifeguard.app import create_app
from lifeguard.events import write_event
from lifeguard.portcheck import is_listening


def main():
    parser = argparse.ArgumentParser(description="LifeGuard pool safety server")
    parser.add_argument("--host", default=os.environ.get("LIFEGUARD_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("LIFEGUARD_PORT", 5000)))
    parser.add_argument("--no-cameras", action="store_true",
                        help="serve the web UI only (no AI workers) - handy for UI work")
    args = parser.parse_args()

    if is_listening(args.port):
        raise SystemExit(f"\nPort {args.port} is already in use, probably by an old copy of the system.\n"
                         f"Find it with:  python -m lifeguard.portcheck {args.port}\n"
                         f"Stop it with:  python -m lifeguard.portcheck {args.port} --kill\n")

    app = create_app(start_cameras=not args.no_cameras)
    write_event("System Started" + (" (UI ONLY)" if args.no_cameras else ""))
    atexit.register(write_event, "System Stopped")
    logging.getLogger("lifeguard").info("Server running on http://localhost:%d", args.port)
    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()