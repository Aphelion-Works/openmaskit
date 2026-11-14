"""Vendor a reviewed immutable design-system revision; rendering stays offline."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import zipfile
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "Aphelion-Works/openmaskit-design-system"
FILES = {
    "web/tokens.css": "tokens.css",
    "web/components.css": "components.css",
    "web/tokens.resolved.json": "tokens.resolved.json",
    "LICENSE": "LICENSE",
    "NOTICE": "NOTICE",
    "TRADEMARKS.md": "TRADEMARKS.md",
}
DESTINATION = ROOT / "src/openmaskit/web/static/design"
LOCK = ROOT / "design-system.lock.json"


def validate_lock(lock):
    if lock.get("repository") != REPOSITORY or not re.fullmatch(
        r"[0-9a-f]{40}", lock.get("revision", "")
    ):
        raise ValueError(
            "Expected the design-system repository and an exact Git revision"
        )
    assets = lock.get("files", [])
    if not isinstance(assets, list) or any(
        not isinstance(asset, dict) or set(asset) != {"path", "sha256"}
        for asset in assets
    ):
        raise ValueError("Invalid design-system asset records")
    if (
        not re.fullmatch(r"\d+\.\d+\.\d+", lock.get("version", ""))
        or {asset["path"] for asset in assets} != set(FILES)
        or len(assets) != len(FILES)
    ):
        raise ValueError("Invalid design-system version or file list")
    if any(
        not re.fullmatch(r"[0-9a-f]{64}", digest)
        for digest in (asset["sha256"] for asset in lock["files"])
    ):
        raise ValueError("Invalid design-system checksum")


def download(revision, name):
    # Callers validate revision and use the fixed file list before any request.
    # The design system is private because Companion consumes the native package.
    # raw.githubusercontent.com rejects the workflow token, so a token uses the API.
    token = os.environ.get("OPENMASKIT_DESIGN_SYSTEM_TOKEN", "").strip()
    if token:
        url = f"https://api.github.com/repos/{REPOSITORY}/contents/{name}?ref={revision}"
        headers = {
            "Accept": "application/vnd.github.raw",
            "Authorization": f"Bearer {token}",
            "User-Agent": "openmaskit-design-pin",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        allowed = "https://api.github.com/"
    else:
        url = f"https://raw.githubusercontent.com/{REPOSITORY}/{revision}/{name}"
        headers = {"User-Agent": "openmaskit-design-pin"}
        allowed = "https://raw.githubusercontent.com/"
    try:
        with urlopen(Request(url, headers=headers), timeout=30) as response:
            if not response.url.startswith(allowed):
                raise ValueError("Unexpected design-system download origin")
            data = response.read(1024 * 1024 + 1)
    except HTTPError as exc:
        raise ValueError("The pinned design revision could not be downloaded") from exc
    if len(data) > 1024 * 1024:
        raise ValueError("Design asset exceeds size limit")
    return data


def verify(lock, contents):
    validate_lock(lock)
    if set(contents) != set(FILES):
        raise ValueError("Incomplete design asset set")
    for name, data in contents.items():
        if (
            hashlib.sha256(data).hexdigest()
            != {asset["path"]: asset["sha256"] for asset in lock["files"]}[name]
        ):
            raise ValueError(f"Design asset checksum mismatch: {name}")


def check(root=ROOT, upstream=False):
    lock = json.loads((root / "design-system.lock.json").read_text())
    validate_lock(lock)
    destination = root / "src/openmaskit/web/static/design"
    contents = {
        name: (destination / target).read_bytes() for name, target in FILES.items()
    }
    verify(lock, contents)
    if upstream:
        directory = os.environ.get("OPENMASKIT_DESIGN_SYSTEM_DIR", "").strip()
        upstream_contents = (
            read_design_checkout(Path(directory), lock["revision"])
            if directory
            else {name: download(lock["revision"], name) for name in FILES}
        )
        verify(lock, upstream_contents)
    return lock


def read_design_checkout(directory, revision):
    """Read the fixed asset list from a checkout of the pinned revision."""
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("The design checkout must be a directory")
    head = subprocess.run(
        ["git", "-C", str(directory), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    if head.returncode != 0 or head.stdout.strip() != revision:
        raise ValueError("The design checkout is not the pinned revision")
    contents = {}
    for name in FILES:
        path = directory.joinpath(*name.split("/"))
        if path.is_symlink() or not path.is_file() or directory not in path.parents:
            raise ValueError(f"Design checkout is missing {name}")
        contents[name] = path.read_bytes()
    return contents


def update(revision, root=ROOT):
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError(
            "Update requires an exact Git commit, never a moving branch/tag"
        )
    manifest = json.loads(download(revision, "design-manifest.json"))
    lock = {
        "repository": REPOSITORY,
        "revision": revision,
        "version": manifest["version"],
        "files": [asset for asset in manifest["files"] if asset["path"] in FILES],
    }
    validate_lock(lock)
    contents = {name: download(revision, name) for name in FILES}
    verify(lock, contents)  # Verify everything before changing any consumer file.
    destination = root / "src/openmaskit/web/static/design"
    destination.mkdir(parents=True, exist_ok=True)
    for name, target in FILES.items():
        path = destination / target
        if path.is_symlink():
            raise ValueError("Design assets cannot be symbolic links")
    for name, target in FILES.items():
        (destination / target).write_bytes(contents[name])
    (root / "design-system.lock.json").write_text(
        json.dumps(lock, indent=2, sort_keys=True) + "\n"
    )
    return lock


def check_wheel(path, root=ROOT):
    lock = check(root)
    with zipfile.ZipFile(path) as wheel:
        contents = {
            name: wheel.read("openmaskit/web/static/design/" + target)
            for name, target in FILES.items()
        }
        verify(lock, contents)
        css = wheel.read("openmaskit/web/static/style.css")
        if any(
            f'@import url("design/{name}")'.encode() not in css
            for name in ["tokens.css", "components.css"]
        ):
            raise ValueError("Built wheel is missing shared design imports")
    return lock


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--check", action="store_true", help="Verify vendored files offline (default)"
    )
    group.add_argument(
        "--check-upstream",
        action="store_true",
        help="Also compare with the pinned upstream revision",
    )
    group.add_argument(
        "--update",
        metavar="GIT_COMMIT",
        help="Explicitly update vendored assets and lock",
    )
    group.add_argument(
        "--check-wheel", type=Path, help="Verify assets in a built distribution wheel"
    )
    args = parser.parse_args()
    if args.check_wheel:
        lock = check_wheel(args.check_wheel)
    else:
        lock = (
            update(args.update) if args.update else check(upstream=args.check_upstream)
        )
    print(f"OpenMaskit design system {lock['version']} verified at {lock['revision']}")


if __name__ == "__main__":
    main()
