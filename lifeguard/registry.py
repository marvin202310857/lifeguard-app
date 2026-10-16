"""The cameras the dashboard talks to, by id ("cam1", "cam2").

At the pool these are CameraWorker threads that run the AI; on the cloud server they are
RemoteCamera objects fed by the pool computer. The web code only sees this dictionary.
"""
workers = {}
