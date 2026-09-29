"""atomic_file.py

write_atomic(): replace a file on the SD card so that a power cut at any
moment leaves either the old content or the new, never a truncated or empty
file. A plain open(path, "w") truncates first and writes after; a school
unplugging the device between the two loses the file (settings.json →
defaults, .env → the auth tokens).

How: write a temp file next to the target, fsync it, give it the target's
owner and mode, rename it over the target (atomic on the same filesystem),
then fsync the directory so the rename itself is on disk.

Stdlib-only: imported by the app (venv), the wizard (system python) and the
setup helpers.
"""

import os
import stat
from pathlib import Path


def write_atomic(path, text: str) -> None:
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        try:
            st = path.stat()
        except FileNotFoundError:
            st = None
        if st is not None:
            # Keep who owns it and who may read it (.env holds tokens), e.g.
            # when the wizard (root) rewrites admin's .env.
            try:
                os.chown(tmp, st.st_uid, st.st_gid)
            except PermissionError:
                pass
            os.chmod(tmp, stat.S_IMODE(st.st_mode))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(path.parent)


def _fsync_dir(directory: Path) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
