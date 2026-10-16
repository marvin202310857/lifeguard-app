# LifeGuard

AI-assisted pool safety monitoring. ESP32-CAM streams are analysed with YOLOv8 +
ByteTrack; a swimmer who stays in the pool area too long escalates from
SAFE to WARNING to DANGER, saving an incident with a snapshot and pre/post-roll video.

## Run it

```bash
pip install -r requirements.txt
python run.py                 # http://localhost:5000
python run.py --no-cameras    # web UI only (no AI workers)
```

Put your existing `lifeguard.db`, `config.json` and `yolov8n.pt` in this folder (next to `run.py`)
and copy your `logo.png` / `logo.ico` into `lifeguard/static/` (a bundled `logo.svg` is used otherwise).

On a **fresh** database the first administrator is created for you: username `admin`, password from
`LIFEGUARD_ADMIN_PASSWORD`, or a random one printed once in the console.

| Task | Command |
|---|---|
| Unlock a locked account | `python -m lifeguard.cli unlock admin` |
| Create / reset an admin | `python -m lifeguard.cli create-admin <user> <password>` |
| Draw the pool area on a live frame | `python -m lifeguard.calibrate --camera-id cam1` |
| Run the tests | `pytest` |

## Layout

```
run.py                    entry point
config.json               camera URLs + pool polygons (cam1_roi, cam2_roi)
lifeguard/
  app.py                  app factory, security headers
  settings.py             paths, constants, config.json access
  db.py                   SQLite layer (users, throttle, incidents, settings)
  auth.py  pages.py       login/register/logout, decorators, HTML pages
  admin.py  live.py       admin API, live stats/feeds/ROI API
  notify.py  cli.py  calibrate.py
  vision/                 stream.py (MJPEG reader), worker.py (detection), video.py
  templates/  static/     base + components, login, dashboard; css/ and js/
tests/                    pytest suite (auth, legacy hashes, alert logic, stream parser)
```

## Settings

Escalation thresholds live in the `system_settings` table (`submersion_warn_sec`,
`submersion_danger_sec`, `occupancy_log_interval_sec`; set it to 0 to stop occupancy logging):

```bash
sqlite3 lifeguard.db "UPDATE system_settings SET value='30' WHERE key='submersion_warn_sec'"
```

Optional e-mail on account lockouts: add an `alert_email` block to `config.json`
(`smtp_host`, `smtp_port`, `username`, `from`, `to`) and set `LIFEGUARD_SMTP_PASSWORD`.
