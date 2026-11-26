"""Every module here that exposes `router` (a fastapi.APIRouter) is included by
create_app, sorted by module name. Add a feature by adding a file; do not edit app.py.

Module attribute `AUTH` picks the default guard for all of the router's routes:
  "device" (default)  device token, or a request from localhost (the mirror)
  "localhost"         only requests from the Pi itself
  "none"              no guard; the module adds its own per-route dependencies
Routes must use the /api prefix (or /ws for sockets).
"""
