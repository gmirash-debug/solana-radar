"""Private, checksum-verified R2 recovery copy through the read-only budgeted API.

The inventory is not a transactional R2 snapshot: changed/missing inventoried
objects abort the copy rather than silently publishing an incomplete backup.
New objects created after inventory belong to the next recovery point.
Verify a downloaded release offline with: archive_backup.py verify DIRECTORY.
"""
import argparse
import hashlib
import json
import os
import re
import struct
import sys
import tarfile
import tempfile
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from urllib.parse import urlparse

import requests
if __package__:
    from .storage_backup import GitHub, Deadline, file_sha, BackupError
else:
    from storage_backup import GitHub, Deadline, file_sha, BackupError

MAX_OBJECTS = 25000
MAX_OBJECT_BYTES = 64 * 1024 * 1024
MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024 * 1024
MAX_INVENTORY_PAGES = 1000
MAX_BATCH_OBJECTS = 16
MAX_BATCH_BYTES = 8 * 1024 * 1024
MAX_FRAME_HEADER_BYTES = 8192
SHARD_BYTES = 256 * 1024 * 1024
PREFIX = "r2-backup-"
MARKER = "Verified private R2 backup; format=r2-backup-v1"
SHA = re.compile(r"[a-f0-9]{64}")
TAG = re.compile(r"r2-backup-\d{8}T\d{6}Z(?:-[a-f0-9]{8})?")


def check(deadline, cancelled=None):
    deadline.check()
    if cancelled is not None and cancelled.is_set():
        raise BackupError("archive downloads cancelled")


def normalized_row(row):
    if not isinstance(row, dict):
        raise BackupError("archive inventory is invalid")
    key, size, etag = row.get("key"), row.get("bytes"), row.get("etag")
    if (not isinstance(key, str)
            or not re.fullmatch(r"(?:history|runtime|outbox)/[a-zA-Z0-9/_:.-]{1,1000}", key)
            or ".." in key or type(size) is not int or not 0 <= size <= MAX_OBJECT_BYTES
            or not isinstance(etag, str) or not 1 <= len(etag) <= 256
            or any(ord(char) <= 32 or ord(char) == 127 for char in etag)):
        raise BackupError("archive inventory is invalid")
    return {"key": key, "bytes": size, "etag": etag}


def response_ok(response):
    response.raise_for_status()
    if response.status_code != 200:
        raise BackupError("archive response is not a complete body")


def inventory(url, secret, deadline, session=None):
    session = session or requests
    rows, cursor, seen = {}, None, set()
    for _ in range(MAX_INVENTORY_PAGES):
        deadline.check()
        with session.get(url, params={"cursor": cursor} if cursor else {},
                headers={"x-radar-archive-backup-secret": secret},
                timeout=deadline.timeout(), allow_redirects=False, stream=True) as response:
            response_ok(response)
            body = bytearray()
            for block in response.iter_content(65536):
                deadline.check()
                if len(body) + len(block) > MAX_PAGE_BYTES:
                    raise BackupError("archive inventory page exceeds bounded capacity")
                body.extend(block)
            page = json.loads(body)
        deadline.check()
        if (not isinstance(page, dict) or page.get("ok") is not True
                or not isinstance(page.get("objects"), list) or "cursor" not in page):
            raise BackupError("archive inventory unavailable")
        for value in page["objects"]:
            row = normalized_row(value)
            if row["key"] in rows:
                raise BackupError("archive inventory contains duplicate objects")
            rows[row["key"]] = row
            if len(rows) > MAX_OBJECTS:
                raise BackupError("archive inventory exceeds bounded recovery capacity")
        cursor = page["cursor"]
        if cursor is None:
            return [rows[key] for key in sorted(rows)]
        if (not isinstance(cursor, str) or not 1 <= len(cursor) <= 2048
                or any(ord(char) <= 32 or ord(char) == 127 for char in cursor)
                or cursor in seen or not page["objects"]):
            raise BackupError("archive inventory cursor stalled or invalid")
        seen.add(cursor)
    raise BackupError("archive inventory pagination exceeds bounded capacity")


def download(url, secret, row, directory, deadline, session=None, cancelled=None):
    session = session or requests
    row = normalized_row(row)
    destination = Path(directory) / hashlib.sha256(row["key"].encode()).hexdigest()
    temporary = destination.with_suffix(".partial")
    digest, size, created = hashlib.sha256(), 0, False
    check(deadline, cancelled)
    if destination.exists() or destination.is_symlink():
        raise BackupError("archive recovery destination already exists")
    try:
        with session.get(url, params={"key": row["key"]},
                headers={"x-radar-archive-backup-secret": secret, "Accept-Encoding": "identity"},
                timeout=deadline.timeout(), allow_redirects=False, stream=True) as response:
            response_ok(response)
            if response.headers.get("x-radar-source-etag") != row["etag"]:
                raise BackupError("archive object changed since inventory")
            if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                raise BackupError("archive object transfer encoding is unsupported")
            length = response.headers.get("Content-Length")
            if length is not None and length != str(row["bytes"]):
                raise BackupError("archive object size changed since inventory")
            check(deadline, cancelled)
            with temporary.open("xb") as handle:
                created = True
                os.chmod(temporary, 0o600)
                for block in response.iter_content(65536):
                    check(deadline, cancelled)
                    size += len(block)
                    if size > row["bytes"]:
                        raise BackupError("archive object exceeded its recorded size")
                    digest.update(block)
                    handle.write(block)
        check(deadline, cancelled)
        if size != row["bytes"]:
            raise BackupError("archive object is incomplete")
        temporary.rename(destination)
        return {**row, "sha256": digest.hexdigest(), "file": destination.name}
    finally:
        if created:
            temporary.unlink(missing_ok=True)


def download_groups(rows):
    if not isinstance(rows, list) or len(rows) > MAX_OBJECTS:
        raise BackupError("archive inventory exceeds bounded recovery capacity")
    groups, current, total, seen = [], [], 0, set()
    for value in rows:
        row = normalized_row(value)
        if row["key"] in seen:
            raise BackupError("archive inventory contains duplicate objects")
        seen.add(row["key"])
        if current and (len(current) == MAX_BATCH_OBJECTS or total + row["bytes"] > MAX_BATCH_BYTES):
            groups.append(current)
            current, total = [], 0
        if row["bytes"] > MAX_BATCH_BYTES:
            groups.append([row])
        else:
            current.append(row)
            total += row["bytes"]
    if current:
        groups.append(current)
    return groups


class FrameReader:
    """Consume framed responses without buffering a whole batch or body."""
    def __init__(self, response, deadline, cancelled):
        self.blocks = iter(response.iter_content(65536))
        self.deadline, self.cancelled = deadline, cancelled
        self.buffer = memoryview(b"")
        self.wire_bytes = 0

    def next_block(self):
        check(self.deadline, self.cancelled)
        for block in self.blocks:
            check(self.deadline, self.cancelled)
            if not isinstance(block, bytes):
                raise BackupError("archive batch body is invalid")
            self.wire_bytes += len(block)
            if block:
                return memoryview(block)
        return None

    def pieces(self, size):
        while size:
            check(self.deadline, self.cancelled)
            if not self.buffer:
                self.buffer = self.next_block()
                if self.buffer is None:
                    raise BackupError("archive batch body is incomplete")
            take = min(size, len(self.buffer))
            block, self.buffer = self.buffer[:take], self.buffer[take:]
            size -= take
            yield block

    def exact(self, size):
        return b"".join(self.pieces(size))

    def finish(self):
        check(self.deadline, self.cancelled)
        if self.buffer or self.next_block() is not None:
            raise BackupError("archive batch contains trailing bytes")


def unique_header(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BackupError("archive batch header contains duplicate fields")
        result[key] = value
    return result


def download_batch(url, secret, rows, directory, deadline, session=None, cancelled=None):
    session = session or requests
    groups = download_groups(rows)
    if (len(groups) != 1 or not rows or len(rows) > MAX_BATCH_OBJECTS
            or sum(row["bytes"] for row in groups[0]) > MAX_BATCH_BYTES):
        raise BackupError("archive batch exceeds bounded capacity")
    rows = groups[0]
    directory = Path(directory)
    paths, partials, completed, results = [], [], [], []
    check(deadline, cancelled)
    for row in rows:
        destination = directory / hashlib.sha256(row["key"].encode()).hexdigest()
        temporary = destination.with_suffix(".partial")
        if (destination.exists() or destination.is_symlink()
                or temporary.exists() or temporary.is_symlink()):
            raise BackupError("archive recovery destination already exists")
        paths.append((destination, temporary))
    try:
        with session.post(url.rstrip("/") + "/batch", json={"keys": [row["key"] for row in rows]},
                headers={"x-radar-archive-backup-secret": secret, "Accept-Encoding": "identity",
                         "Accept": "application/octet-stream"},
                timeout=deadline.timeout(), allow_redirects=False, stream=True) as response:
            response_ok(response)
            if response.headers.get("x-radar-batch-count") != str(len(rows)):
                raise BackupError("archive batch count differs from requested objects")
            if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                raise BackupError("archive batch transfer encoding is unsupported")
            length = response.headers.get("Content-Length")
            if length is not None and (not re.fullmatch(r"[0-9]{1,9}", length)
                    or int(length) > MAX_BATCH_BYTES + MAX_BATCH_OBJECTS * (4 + MAX_FRAME_HEADER_BYTES)):
                raise BackupError("archive batch response exceeds bounded capacity")
            stream = FrameReader(response, deadline, cancelled)
            for row, (destination, temporary) in zip(rows, paths):
                header_size = struct.unpack(">I", stream.exact(4))[0]
                if not 1 <= header_size <= MAX_FRAME_HEADER_BYTES:
                    raise BackupError("archive batch header exceeds bounded capacity")
                header = json.loads(stream.exact(header_size).decode("utf-8"), object_pairs_hook=unique_header)
                if (not isinstance(header, dict) or set(header) != {"key", "bytes", "etag"}
                        or normalized_row(header) != row):
                    raise BackupError("archive batch object changed or order differs from inventory")
                digest = hashlib.sha256()
                with temporary.open("xb") as handle:
                    partials.append(temporary)
                    os.chmod(temporary, 0o600)
                    for block in stream.pieces(row["bytes"]):
                        digest.update(block)
                        handle.write(block)
                results.append({**row, "sha256": digest.hexdigest(), "file": destination.name})
            stream.finish()
            if length is not None and stream.wire_bytes != int(length):
                raise BackupError("archive batch response size differs from its header")
        check(deadline, cancelled)
        # Commit bodies only after the complete response has passed framing and
        # inventory checks. Roll back owned files if promotion itself fails.
        for destination, temporary in paths:
            check(deadline, cancelled)
            if destination.exists() or destination.is_symlink():
                raise BackupError("archive recovery destination already exists")
            temporary.rename(destination)
            completed.append(destination)
        return results
    except Exception:
        for destination in completed:
            destination.unlink(missing_ok=True)
        raise
    finally:
        for temporary in partials:
            temporary.unlink(missing_ok=True)


def download_group(url, secret, rows, directory, deadline, session=None, cancelled=None):
    if len(rows) == 1 and rows[0]["bytes"] > MAX_BATCH_BYTES:
        return [download(url, secret, rows[0], directory, deadline, session, cancelled)]
    return download_batch(url, secret, rows, directory, deadline, session, cancelled)


def download_all(url, secret, rows, directory, deadline, session=None, progress=None):
    """At most four pending requests; failure cancels queued and running reads."""
    groups = download_groups(rows)
    cancelled = Event()
    executor = ThreadPoolExecutor(max_workers=4)
    pending, results, index = {}, [None] * len(groups), 0
    completed_objects, completed_bytes = 0, 0
    total_bytes, last_progress = sum(row["bytes"] for row in rows), deadline.clock()
    try:
        while pending or index < len(groups):
            check(deadline, cancelled)
            while index < len(groups) and len(pending) < 4:
                future = executor.submit(download_group, url, secret, groups[index], directory,
                                         deadline, session, cancelled)
                pending[future] = index
                index += 1
            complete, _ = wait(pending, timeout=deadline.timeout(), return_when=FIRST_COMPLETED)
            # Resolve all completed futures before scheduling more: a successful
            # peer must not hide an already failed download in the same batch.
            for future in complete:
                group_index = pending.pop(future)
                results[group_index] = future.result()
                completed_objects += len(groups[group_index])
                completed_bytes += sum(row["bytes"] for row in groups[group_index])
            now = deadline.clock()
            if progress and (now - last_progress >= 30 or completed_objects == len(rows)):
                progress(completed_objects, len(rows), completed_bytes, total_bytes)
                last_progress = now
        deadline.check()
        return [row for group in results for row in group]
    finally:
        cancelled.set()
        for future in pending:
            future.cancel()
        # In-flight network reads retain the <=20s request timeout, observe
        # cancellation between chunks, and finish before the temp dir is removed.
        executor.shutdown(wait=True, cancel_futures=True)


def checked_objects(objects):
    if not isinstance(objects, list) or len(objects) > MAX_OBJECTS:
        raise BackupError("archive recovery object listing is invalid")
    seen = set()
    for row in objects:
        normalized_row(row)
        name = hashlib.sha256(row["key"].encode()).hexdigest()
        if (row.get("file") != name or name in seen or not isinstance(row.get("sha256"), str)
                or not SHA.fullmatch(row["sha256"])):
            raise BackupError("archive recovery object identity is invalid")
        seen.add(name)
    return objects


def shard_size(payload):
    # Include USTAR headers, end markers and whole-record padding.
    return ((payload + 1024 + 10239) // 10240) * 10240


class CheckedReader:
    def __init__(self, handle, deadline):
        self.handle, self.deadline = handle, deadline

    def read(self, size):
        self.deadline.check()
        return self.handle.read(size)


def verify_shard(path, rows, deadline):
    expected, seen = {row["file"]: row for row in rows}, set()
    with tarfile.open(path, "r:") as archive:
        for member in archive:
            deadline.check()
            if (member.name not in expected or member.name in seen or not member.isfile()
                    or member.size != expected[member.name]["bytes"]):
                raise BackupError("archive recovery shard has unexpected or duplicate members")
            digest, size = hashlib.sha256(), 0
            with archive.extractfile(member) as handle:
                for block in iter(lambda: handle.read(65536), b""):
                    deadline.check()
                    size += len(block)
                    digest.update(block)
            if size != member.size or digest.hexdigest() != expected[member.name]["sha256"]:
                raise BackupError("archive recovery shard failed verification")
            seen.add(member.name)
    if seen != set(expected):
        raise BackupError("archive recovery shard is incomplete")


def bundle(objects, directory, deadline):
    checked_objects(objects)
    directory = Path(directory)
    shards, current, total = [], [], 0
    def write(rows):
        path = directory / f"r2-objects-{len(shards):04d}.tar"
        with path.open("xb") as output:
            os.chmod(path, 0o600)
            with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as archive:
                for row in rows:
                    deadline.check()
                    source = directory / row["file"]
                    if source.is_symlink() or not source.is_file() or source.stat().st_size != row["bytes"]:
                        raise BackupError("archive recovery body is missing or changed")
                    info = tarfile.TarInfo(row["file"])
                    info.size, info.mode = row["bytes"], 0o600
                    with source.open("rb") as handle:
                        archive.addfile(info, CheckedReader(handle, deadline))
        if path.stat().st_size > SHARD_BYTES:
            raise BackupError("archive recovery shard exceeds bounded capacity")
        verify_shard(path, rows, deadline)
        for row in rows:
            row["shard"] = path.name
        shards.append({"file": path.name, "bytes": path.stat().st_size,
                       "sha256": file_sha(path, deadline)})
    for row in objects:
        size = 512 + ((row["bytes"] + 511) // 512) * 512
        if current and shard_size(total + size) > SHARD_BYTES:
            write(current)
            current, total = [], 0
        if shard_size(size) > SHARD_BYTES:
            raise BackupError("archive object exceeds shard capacity")
        current.append(row)
        total += size
    if current:
        write(current)
    return shards


def inventory_sha(objects):
    rows = [normalized_row(row) for row in objects]
    return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def write_manifest(objects, shards, directory, started, deadline):
    manifest = {"format": "r2-backup-v1", "inventory_started_at": started,
        "verified_at": datetime.now(timezone.utc).isoformat(), "inventory_count": len(objects),
        "inventory_sha256": inventory_sha(objects), "objects": objects, "shards": shards}
    path = Path(directory) / "r2-manifest.json"
    data = json.dumps(manifest, separators=(",", ":"), allow_nan=False).encode()
    deadline.check()
    if len(data) > MAX_MANIFEST_BYTES:
        raise BackupError("archive recovery manifest exceeds bounded capacity")
    with path.open("xb") as handle:
        os.chmod(path, 0o600)
        handle.write(data)
    verify_backup(directory, manifest, deadline)
    return path, manifest


def load_manifest(directory, deadline):
    path = Path(directory) / "r2-manifest.json"
    if path.is_symlink():
        raise BackupError("archive recovery manifest is invalid")
    with path.open("rb") as handle:
        data = handle.read(MAX_MANIFEST_BYTES + 1)
    deadline.check()
    if len(data) > MAX_MANIFEST_BYTES:
        raise BackupError("archive recovery manifest exceeds bounded capacity")
    return json.loads(data)


def verify_backup(directory, manifest=None, deadline=None):
    """Verify every restore body without extracting untrusted tar paths."""
    directory, deadline = Path(directory), deadline or Deadline()
    if manifest is None:
        manifest = load_manifest(directory, deadline)
    if not isinstance(manifest, dict) or manifest.get("format") != "r2-backup-v1":
        raise BackupError("archive recovery manifest is invalid")
    objects = checked_objects(manifest.get("objects"))
    shards = manifest.get("shards")
    if (type(manifest.get("inventory_count")) is not int or manifest["inventory_count"] != len(objects)
            or manifest.get("inventory_sha256") != inventory_sha(objects)
            or not isinstance(shards, list) or len(shards) > len(objects)):
        raise BackupError("archive recovery inventory is incomplete")
    seen, assigned = set(), set()
    for shard in shards:
        deadline.check()
        if not isinstance(shard, dict):
            raise BackupError("archive recovery shard listing is invalid")
        name = shard.get("file")
        if (not isinstance(name, str) or not re.fullmatch(r"r2-objects-\d{4}.tar", name)
                or name in seen or type(shard.get("bytes")) is not int
                or not 10240 <= shard["bytes"] <= SHARD_BYTES
                or not isinstance(shard.get("sha256"), str) or not SHA.fullmatch(shard["sha256"])):
            raise BackupError("archive recovery shard listing is invalid")
        path = directory / name
        if path.is_symlink() or path.stat().st_size != shard["bytes"] or file_sha(path, deadline) != shard["sha256"]:
            raise BackupError("archive recovery shard SHA-256 differs")
        members = [row for row in objects if row.get("shard") == name]
        if not members:
            raise BackupError("archive recovery shard has no assigned objects")
        verify_shard(path, members, deadline)
        assigned.update(row["file"] for row in members)
        seen.add(name)
    if assigned != {row["file"] for row in objects}:
        raise BackupError("archive recovery objects are missing shards")
    deadline.check()
    return True


def owned_releases(github):
    result, seen = [], set()
    for page in range(1, 21):
        rows = github.request(github.prefix + f"/releases?per_page=100&page={page}")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise BackupError("archive recovery release listing is invalid")
        for row in rows:
            tag, body = row.get("tag_name"), row.get("body")
            if (isinstance(tag, str) and TAG.fullmatch(tag) and isinstance(body, str)
                    and (body == MARKER or re.fullmatch(re.escape(MARKER) + "\nmanifest_sha256=[a-f0-9]{64}", body))
                    and row.get("draft") is False):
                if type(row.get("id")) is not int or row["id"] <= 0:
                    raise BackupError("archive recovery release identifier is invalid")
                if row["id"] in seen:
                    raise BackupError("archive recovery release pagination contains duplicates")
                seen.add(row["id"])
                result.append(row)
        if len(rows) < 100:
            return result
    raise BackupError("archive recovery release pagination exceeds bounded capacity")


def prune(github, protected):
    github.assert_private()
    releases = sorted(owned_releases(github), key=lambda row: row["tag_name"], reverse=True)
    if not any(row["id"] == protected for row in releases):
        raise BackupError("new archive recovery point absent from retention listing")
    preserved = {protected}
    for row in releases:
        if len(preserved) < 2:
            preserved.add(row["id"])
    for row in releases:
        if row["id"] not in preserved:
            github.assert_private()
            github.request(github.prefix + f"/releases/{row['id']}", "DELETE")
            github.request(github.prefix + "/git/refs/tags/" + row["tag_name"], "DELETE")


def publish(github, directory, manifest_path, manifest, deadline):
    if Path(manifest_path) != Path(directory) / "r2-manifest.json" or load_manifest(directory, deadline) != manifest:
        raise BackupError("archive recovery manifest changed before publication")
    verify_backup(directory, manifest, deadline)
    github.assert_private()
    tag = PREFIX + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    release = github.request(github.prefix + "/releases", "POST", {
        "tag_name": tag, "name": "Private R2 recovery backup", "draft": True,
        "make_latest": "false", "body": "Unverified private archive upload; not a recovery point"})
    if (not isinstance(release, dict) or type(release.get("id")) is not int or release["id"] <= 0
            or release.get("draft") is not True):
        raise BackupError("private archive draft was not created")
    verified = False
    try:
        for shard in manifest["shards"]:
            github.upload(release["id"], Path(directory) / shard["file"], shard["sha256"])
        manifest_sha = file_sha(manifest_path, deadline)
        github.upload(release["id"], manifest_path, manifest_sha)
        verified = True
        github.assert_private()
        body = MARKER + "\nmanifest_sha256=" + manifest_sha
        github.request(github.prefix + f"/releases/{release['id']}", "PATCH", {"body": body})
        github.assert_private()
        published = github.request(github.prefix + f"/releases/{release['id']}", "PATCH",
            {"draft": False, "tag_name": tag, "body": body, "make_latest": "false"})
        if (not isinstance(published, dict) or published.get("draft") is not False
                or published.get("tag_name") != tag or published.get("id") != release["id"]
                or published.get("body") != body):
            raise BackupError("verified archive publication unavailable")
        prune(github, protected=release["id"])
    except Exception:
        # Preserve verified drafts on publication/retention failure. Only our
        # unverified replacement is removable, never any older recovery point.
        if not verified:
            try:
                github.assert_private()
                github.request(github.prefix + f"/releases/{release['id']}", "DELETE")
                github.request(github.prefix + "/git/refs/tags/" + tag, "DELETE")
            except BackupError:
                pass
        raise
    return release["id"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("backup", "verify"), nargs="?", default="backup")
    parser.add_argument("directory", nargs="?")
    args = parser.parse_args(argv)
    try:
        deadline = Deadline(1000)
        if args.command == "verify":
            if not args.directory:
                raise BackupError("archive verification requires a directory")
            verify_backup(args.directory, deadline=deadline)
            print("R2 backup verified: complete inventory, shard SHA-256 and every object body")
            return 0
        if args.directory:
            raise BackupError("archive backup uses an isolated temporary directory")
        base = os.environ.get("RADAR_DATA_API_URL","").rstrip("/")
        parsed = urlparse(base)
        secret = os.environ.get("RADAR_ARCHIVE_BACKUP_SECRET")
        if (parsed.scheme != "https" or not parsed.hostname or parsed.port not in (None, 443)
                or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment
                or any(ord(char) <= 32 or ord(char) == 127 for char in base)
                or not secret or any(ord(char) <= 32 or ord(char) == 127 for char in secret)):
            raise BackupError("archive backup configuration unavailable")
        github = GitHub(os.environ.get("GITHUB_REPOSITORY"),os.environ.get("GH_TOKEN"),deadline)
        github.assert_private()
        url = base+"/api/storage/archive-backup"
        started = datetime.now(timezone.utc).isoformat()
        rows = inventory(url,secret,deadline)
        print(f"R2 backup inventory: objects={len(rows)} bytes={sum(row['bytes'] for row in rows)}", flush=True)
        def progress(completed, total, downloaded, total_bytes):
            print(f"R2 backup download progress: objects={completed}/{total} bytes={downloaded}/{total_bytes}", flush=True)
        with tempfile.TemporaryDirectory(prefix="radar-r2-private-") as temporary:
            root=Path(temporary);os.chmod(root,0o700)
            objects = download_all(url, secret, rows, root, deadline, progress=progress)
            shards=bundle(objects,root,deadline)
            path, manifest = write_manifest(objects, shards, root, started, deadline)
            publish(github, root, path, manifest, deadline)
        print(json.dumps({"ok":True,"objects":len(rows),"bytes":sum(row["bytes"] for row in rows),
                          "shards":len(shards),"verified":True}))
        return 0
    except (ValueError, BackupError, OSError, requests.RequestException, tarfile.TarError,
            KeyError, TypeError, AttributeError):
        print("R2 backup failed safely; previous verified copies and source objects remain unchanged",file=sys.stderr)
        return 1


if __name__=="__main__":
    raise SystemExit(main())
