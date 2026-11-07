"""Install pinned wheels from official PyPI JSON metadata, verifying SHA-256.

Run inside a fresh virtual environment. Avoids slow simple-index resolution and
source builds. Does not install or configure any LLM service.
"""
import hashlib
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from pip._vendor.packaging.tags import sys_tags
from pip._vendor.packaging.utils import parse_wheel_filename


def download(url, limit):
    with urllib.request.urlopen(url, timeout=30) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise RuntimeError("Download exceeds installer limit")
    return data


def main():
    if sys.prefix == sys.base_prefix:
        raise RuntimeError("Create a virtual environment first, then run its python scripts/bootstrap.py")
    repository = Path(__file__).resolve().parents[1]
    destination = Path(sys.prefix) / "wheelhouse"
    destination.mkdir(exist_ok=True)
    supported = set(sys_tags())
    for line in (repository / "requirements-lock.txt").read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+-]+)", line)
        if not match:
            raise RuntimeError("Only exact pinned requirements are supported")
        name, version = match.groups()
        metadata = json.loads(download(f"https://pypi.org/pypi/{name}/{version}/json", 5 * 1024 * 1024))
        for entry in metadata["urls"]:
            filename = entry["filename"]
            if not filename.endswith(".whl") or Path(filename).name != filename:
                continue
            if not parse_wheel_filename(filename)[3] & supported:
                continue
            url = urlsplit(entry["url"])
            if url.scheme != "https" or url.hostname != "files.pythonhosted.org":
                raise RuntimeError("Unexpected PyPI artifact host")
            path = destination / filename
            expected = entry["digests"]["sha256"]
            if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                payload = download(entry["url"], 50 * 1024 * 1024)
                if hashlib.sha256(payload).hexdigest() != expected:
                    raise RuntimeError("Wheel checksum mismatch")
                path.write_bytes(payload)
            print(f"Verified {filename}", flush=True)
            break
        else:
            raise RuntimeError(f"No compatible wheel: {name}=={version}; use a supported interpreter")
    subprocess.run([sys.executable, "-m", "pip", "--disable-pip-version-check", "install", "--no-index",
                    "--find-links", str(destination), "-r", str(repository / "requirements-lock.txt")], check=True)
    subprocess.run([sys.executable, "-m", "pip", "--disable-pip-version-check", "install", "--no-deps",
                    "--no-build-isolation", "-e", str(repository)], check=True)
    subprocess.run([sys.executable, "-m", "pip", "--disable-pip-version-check", "check"], check=True)


if __name__ == "__main__":
    main()
