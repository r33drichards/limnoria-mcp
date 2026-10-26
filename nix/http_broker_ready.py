"""Wait only for the fixed broker socket; never issue a network request."""
import os
import stat
import time

for _ in range(100):
    try:
        if stat.S_ISSOCK(os.lstat('/run/limnoria-http-broker/http.sock').st_mode):
            break
    except FileNotFoundError:
        pass
    time.sleep(.1)
else:
    raise SystemExit('HTTP broker socket did not become ready')
