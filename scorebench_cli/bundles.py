from __future__ import annotations

import hashlib
import gzip
import io
import os
import tarfile
from dataclasses import dataclass
from pathlib import Path


# extract_bundle guards against decompression bombs: MAX_BUNDLE_BYTES caps only the
# *compressed* upload, so without these the gzip stream can expand without bound.
MAX_EXTRACTED_BYTES = 512 * 1024 * 1024  # total uncompressed size across all members
MAX_ARCHIVE_MEMBERS = 20000  # guards inode/RAM exhaustion from millions of tiny members


@dataclass(frozen=True)
class Bundle:
    data: bytes
    sha256: str
    manifest: dict


def _safe_arcname(root: Path, item: Path) -> str:
    if root.is_file():
        return root.name
    return str(item.relative_to(root.parent))


def create_bundle(path: str | Path) -> Bundle:
    source = Path(path)
    if not source.exists():
        raise FileNotFoundError(source)
    entries: list[dict] = []
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as gz:
        _write_tar(gz, source, entries)
    data = buffer.getvalue()
    return Bundle(
        data=data,
        sha256=hashlib.sha256(data).hexdigest(),
        manifest={
            "format": "tar.gz",
            "source_name": source.name,
            "entries": entries,
        },
    )


def _write_tar(fileobj, source: Path, entries: list[dict]) -> None:
    with tarfile.open(fileobj=fileobj, mode="w", format=tarfile.PAX_FORMAT) as tf:
        paths = [source] if source.is_file() else sorted(p for p in source.rglob("*") if p.is_file())
        for file_path in paths:
            arcname = _safe_arcname(source, file_path)
            info = tf.gettarinfo(str(file_path), arcname=arcname)
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = 0
            with file_path.open("rb") as fh:
                tf.addfile(info, fh)
            entries.append(
                {
                    "path": arcname,
                    "size": file_path.stat().st_size,
                    "sha256": hashlib.sha256(file_path.read_bytes()).hexdigest(),
                }
            )


def extract_bundle(bundle_path: str | Path, destination: str | Path) -> None:
    bundle = Path(bundle_path)
    dest = Path(destination)
    dest.mkdir(parents=True, exist_ok=True)
    dest_resolved = dest.resolve()
    with tarfile.open(bundle, mode="r:gz") as tf:
        member_count = 0
        total_bytes = 0
        for member in tf.getmembers():
            member_count += 1
            if member_count > MAX_ARCHIVE_MEMBERS:
                raise ValueError(f"archive has too many members; max is {MAX_ARCHIVE_MEMBERS}")
            if not member.isfile() and not member.isdir():
                raise ValueError(f"unsafe archive member type: {member.name}")
            if member.isfile():
                total_bytes += max(0, member.size)
                if total_bytes > MAX_EXTRACTED_BYTES:
                    raise ValueError(
                        f"archive decompresses too large; max is {MAX_EXTRACTED_BYTES} bytes"
                    )
            member_path = dest / member.name
            resolved = member_path.resolve()
            if os.path.commonpath([str(dest_resolved), str(resolved)]) != str(dest_resolved):
                raise ValueError(f"unsafe archive member path: {member.name}")
        tf.extractall(dest)


def write_immutable(path: str | Path, data: bytes) -> str:
    target = Path(path)
    if target.exists():
        raise FileExistsError(target)
    target.write_bytes(data)
    target.chmod(0o444)
    return hashlib.sha256(data).hexdigest()
