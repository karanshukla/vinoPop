#!/usr/bin/env python3
"""Regenerate `vino_ai/model_digests.json` from the Hugging Face API.

Run it whenever a revision in vino_ai/models.py changes; the download is
verified against this file, so a pin bump without it reads as unpinned.

    ./scripts/update_digests.py
    ./scripts/update_digests.py --model gemma-4-e4b
    ./scripts/update_digests.py --dry-run

Nothing big is downloaded. LFS files (the weights) take their sha256 and
size from the Hub's tree API, which is what git LFS itself checks. Small
non-LFS files (json, txt, tokenizer xml) are fetched at the pinned commit,
checked against their git blob id, and hashed. Commit the diff; it's a
list of hashes and is meant to be read in review.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vino_ai import integrity, models  # noqa: E402

HUB = "https://huggingface.co"
_TIMEOUT = 60


def _get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "vinopop-update-digests"})
    with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:  # nosec B310 - fixed https host
        return response.read()


def _git_blob_id(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


def digests_for(repo: str, revision: str, say=print) -> dict[str, dict[str, Any]]:
    tree = json.loads(_get(f"{HUB}/api/models/{repo}/tree/{revision}?recursive=1"))
    files: dict[str, dict[str, Any]] = {}
    for entry in sorted(tree, key=lambda e: e["path"]):
        if entry.get("type") != "file":
            continue
        name = entry["path"]
        lfs = entry.get("lfs")
        if lfs:
            files[name] = {"sha256": lfs["oid"], "size": int(lfs["size"])}
            continue
        data = _get(f"{HUB}/{repo}/resolve/{revision}/{name}")
        if _git_blob_id(data) != entry["oid"] or len(data) != entry["size"]:
            raise SystemExit(f"{repo}@{revision}: {name} doesn't match its git blob id")
        files[name] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        say(f"    fetched {name} ({len(data)} bytes)")
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--model",
        choices=sorted(models.LLM_MODELS),
        action="append",
        help="Only this model (repeatable). Default: every model in the registry.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print, write nothing.")
    args = parser.parse_args(argv)

    try:
        raw = json.loads(integrity.DIGESTS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    table: dict[str, Any] = raw.get("models", {})
    keys = args.model or sorted(models.LLM_MODELS)
    for key in keys:
        model = models.LLM_MODELS[key]
        print(f"{model.repo_id}@{model.revision}")
        files = digests_for(model.repo_id, model.revision)
        for name, meta in files.items():
            print(f"  {meta['sha256']}  {meta['size']:>12}  {name}")
        # One revision per repo: the pinned one. An older revision's
        # folder reads as drift and is replaced, so its digests aren't needed.
        table[model.repo_id] = {
            model.revision: {"recorded": date.today().isoformat(), "files": files}
        }
    if not args.model:
        pinned = {m.repo_id for m in models.LLM_MODELS.values()}
        table = {repo: v for repo, v in table.items() if repo in pinned}

    if args.dry_run:
        print("\n--dry-run: nothing written")
        return 0
    out = {
        "schema": integrity.SCHEMA,
        "generated_by": "scripts/update_digests.py, generated data, do not edit by hand",
        "models": dict(sorted(table.items())),
    }
    integrity.DIGESTS_PATH.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {integrity.DIGESTS_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
