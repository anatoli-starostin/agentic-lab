"""Files in folders an agent can write, opened by a service outside its sandbox.

An agent may create, rename and link anything in its working folder. A service that opens
a path there (a run's log, a file to hand a member, an attachment to store) must stay in
that folder whatever the agent did to it, even while it changes. The folder itself is
resolved once (`pin`, at the service's start: an admin may have made it a link, e.g. to a
fast local disk) and must be the same folder (device and inode) on every later call;
every component in it is opened relative to its parent and never through a symbolic link
(O_NOFOLLOW); only plain files with a single name are used (never a device, a pipe, or a
second name of a file kept elsewhere); and a file is written as a new file renamed into
place.
"""
import errno
import os
import secrets
import stat

DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class UnsafePath(OSError):
    pass


_pins = {}          # the folder as configured -> (where it really is, st_dev, st_ino)


def pin(root):
    """Resolve the folder `root` once and remember which folder it is. Returns
    (its real path, device, inode)."""
    given = os.path.abspath(root)
    if given not in _pins:
        real = os.path.realpath(given)
        st = os.stat(real)
        if not stat.S_ISDIR(st.st_mode):
            raise UnsafePath(f"{root} is not a folder")
        _pins[given] = (real, st.st_dev, st.st_ino)
    return _pins[given]


def _parts(root, path):
    """`path` (relative to `root`, or absolute: under `root` as configured or as it really
    is) as its components below it."""
    given, real = os.path.abspath(root), pin(root)[0]
    full = os.path.normpath(path if os.path.isabs(path) else os.path.join(given, path))
    for base in (given, real):
        if full.startswith(base + os.sep):
            parts = os.path.relpath(full, base).split(os.sep)
            break
    else:
        raise UnsafePath(f"{path} isn't inside {root}")
    if any(p in ("", ".", "..") for p in parts):
        raise UnsafePath(f"bad path {path}")
    return parts


def _dir(root, parts, create=False):
    """A descriptor of the folder `root`/`parts`, every step without following a link."""
    real, dev, ino = pin(root)
    try:
        fd = os.open(real, DIR)         # where it really is, and still the same folder
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.ENOTDIR):
            raise UnsafePath(f"{root} is not the folder it was") from None
        raise
    st = os.fstat(fd)
    if (st.st_dev, st.st_ino) != (dev, ino):
        os.close(fd)
        raise UnsafePath(f"{root} is not the folder it was")
    try:
        for p in parts:
            try:
                nfd = os.open(p, DIR, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(p, 0o777, dir_fd=fd)
                except FileExistsError:
                    pass
                nfd = os.open(p, DIR, dir_fd=fd)
            except OSError as e:
                raise UnsafePath(f"{p}: not a plain folder ({e.strerror})") from None
            os.close(fd)
            fd = nfd
    except BaseException:
        os.close(fd)
        raise
    return fd


def makedirs(root, path):
    """mkdir -p of `path` inside `root`; returns its full path."""
    parts = _parts(root, path)
    os.close(_dir(root, parts, create=True))
    return os.path.join(os.path.abspath(root), *parts)


def open_read(root, path):
    """A descriptor of the regular file `path` inside `root`, for reading."""
    parts = _parts(root, path)
    d = _dir(root, parts[:-1])
    try:
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                     dir_fd=d)
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.ENOTDIR):     # a link
            raise UnsafePath(f"{path} is not a plain file") from None
        raise
    finally:
        os.close(d)
    _plain(fd, path)
    return fd


def _plain(fd, path):
    """A regular file with one name, read and written without blocking (else UnsafePath)."""
    import fcntl
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
        os.close(fd)
        raise UnsafePath(f"{path} is not a plain file")
    fcntl.fcntl(fd, fcntl.F_SETFL, fcntl.fcntl(fd, fcntl.F_GETFL) & ~os.O_NONBLOCK)


def read(root, path, limit=None):
    """The bytes of `path` inside `root` (at most `limit`: UnsafePath if it's longer)."""
    fd = open_read(root, path)
    with os.fdopen(fd, "rb") as f:
        size = os.fstat(f.fileno()).st_size
        if limit is not None and size > limit:
            raise UnsafePath(f"{path} is {size} bytes; the limit is {limit}")
        return f.read() if limit is None else f.read(limit)


def size(root, path):
    fd = open_read(root, path)
    try:
        return os.fstat(fd).st_size
    finally:
        os.close(fd)


def tail(root, path, nbytes):
    """The last `nbytes` of `path` inside `root`."""
    with os.fdopen(open_read(root, path), "rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - nbytes))
        return f.read()


def write(root, path, data, mode=0o666):
    """Write `data` to `path` inside `root` (folders made as needed): a new file, renamed
    over whatever was there (a link is replaced, never followed). Returns the full path."""
    parts = _parts(root, path)
    d = _dir(root, parts[:-1], create=True)
    tmp = f".tmp-{secrets.token_hex(6)}"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     mode, dir_fd=d)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.rename(tmp, parts[-1], src_dir_fd=d, dst_dir_fd=d)
    except BaseException:
        try:
            os.unlink(tmp, dir_fd=d)
        except OSError:
            pass
        raise
    finally:
        os.close(d)
    return os.path.join(os.path.abspath(root), *parts)


def open_append(root, path, mode=0o666):
    """A descriptor to append to `path` inside `root` (created if missing, never a link)."""
    parts = _parts(root, path)
    d = _dir(root, parts[:-1], create=True)
    try:
        fd = os.open(parts[-1], os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW
                     | os.O_CLOEXEC | os.O_NONBLOCK, mode, dir_fd=d)
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise UnsafePath(f"{path} is not a plain file") from None
        raise
    finally:
        os.close(d)
    _plain(fd, path)
    return fd
