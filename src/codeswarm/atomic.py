from contextlib import suppress
import stat
import tempfile
import os


class AtomicWriteError(Exception):
    """An Atomic write failed."""


def write(path: str, content: str) -> None:
    """Write a file in an atomic manner.

    Args:
        filename: Filename of new file.
        content: Content to write.

    """
    path = os.path.abspath(path)
    dir_name = os.path.dirname(path) or "."
    try:
        existing_mode: int | None = stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        existing_mode = None

    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            delete=False,
            dir=dir_name,
            prefix=f".{os.path.basename(path)}_tmp_",
        ) as temporary_file:
            temp_name = temporary_file.name
            temporary_file.write(content)
            # The rename below is atomic, but the bytes are not durable until
            # they reach the disk. Without this a crash can leave an empty
            # file where the previous contents used to be.
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
    except Exception as error:
        if temp_name is not None:
            with suppress(OSError):
                os.unlink(temp_name)
        raise AtomicWriteError(
            f"Failed to write {path!r}; error creating temporary file: {error}"
        ) from error

    try:
        if existing_mode is not None:
            # A temporary file is created 0600. Replacing a file the user
            # already owns must not silently change its permissions.
            os.chmod(temp_name, existing_mode)
        os.replace(temp_name, path)  # Atomic on POSIX and Windows
    except Exception as error:
        # A failed replace leaves the temporary file behind; repeated
        # failures would otherwise litter the directory.
        with suppress(OSError):
            os.unlink(temp_name)
        raise AtomicWriteError(f"Failed to write {path!r}; {error}") from error
