"""Recover the latest trusted daily backup if the Actions cache was evicted."""
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import zipfile
from datetime import datetime, timezone


def api(path):
    return subprocess.run(["gh", "api", path], capture_output=True, check=True).stdout


def main():
    repo = os.environ["GITHUB_REPOSITORY"]
    today = datetime.now(timezone.utc).date().isoformat()
    artifacts = json.loads(api(f"repos/{repo}/actions/artifacts?name=robinhood-state&per_page=30"))["artifacts"]
    artifacts = [a for a in artifacts if not a["expired"] and a.get("workflow_run", {}).get("head_branch") == "main"
                 and isinstance(a["workflow_run"].get("repository_id"), int)
                 and a["workflow_run"].get("head_repository_id") == a["workflow_run"].get("repository_id")]
    artifacts.sort(key=lambda a: a["created_at"], reverse=True)
    with open(os.environ["GITHUB_OUTPUT"], "a") as f:
        f.write(f"backup_due={'false' if artifacts and artifacts[0]['created_at'].startswith(today) else 'true'}\n")
    target = Path("data/robinhood.sqlite")
    if target.exists() or not artifacts:
        print("Using cached database" if target.exists() else "No backup yet; first indexed window will be explicit")
        return
    item = artifacts[0]
    if item["size_in_bytes"] > 25_000_000:
        raise ValueError("Backup exceeds restore size limit")
    archive = zipfile.ZipFile(io.BytesIO(api(f"repos/{repo}/actions/artifacts/{item['id']}/zip")))
    member = archive.getinfo("robinhood.sqlite")
    if member.file_size > 100_000_000:
        raise ValueError("Expanded database exceeds size limit")
    target.parent.mkdir(exist_ok=True)
    temporary = target.with_suffix(".restore")
    temporary.write_bytes(archive.read(member))
    db = sqlite3.connect(f"file:{temporary}?mode=ro", uri=True)
    try:
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("Backup integrity check failed")
        snapshot = json.loads(db.execute("SELECT value FROM cache WHERE key='snapshot'").fetchone()[0])
        if snapshot.get("chain_id") != 4663:
            raise ValueError("Backup belongs to another network")
    finally:
        db.close()
    temporary.replace(target)
    print("Recovered Robinhood history from daily backup", item["created_at"])


if __name__ == "__main__":
    main()
