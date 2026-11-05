"""Generate a Silo repository index pinned to an immutable GitHub release tag."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def package(directory, repository, tag):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Expected GitHub owner/repository")
    if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        raise ValueError("Expected a stable vMAJOR.MINOR.PATCH tag")
    binary = directory / "tracearr-bridge-linux-amd64"
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    raw = subprocess.check_output([str(binary.resolve()), "manifest"])
    helper = str((directory / "repository-tool").resolve())
    manifest = json.loads(subprocess.check_output([helper, "manifest"], input=raw))
    if manifest["checksum"] != digest or manifest["version"] != tag[1:]:
        raise ValueError("Binary checksum/version does not match release")
    if manifest["plugin_id"] != "community.tracearr-bridge":
        raise ValueError("Unexpected plugin identity")
    if manifest["supported_platforms"] != [{"os": "linux", "arch": "amd64"}]:
        raise ValueError("Unexpected build platforms")
    source = f"https://github.com/{repository}"
    if manifest["presentation"]["source_url"] != source:
        raise ValueError("Binary source URL does not match repository")
    assets = f"{source}/releases/download/{tag}"
    index = {"plugins": [{"manifest": manifest, "repo_url": source,
                           "binaries": {"linux/amd64": {"url": f"{assets}/{binary.name}",
                                                         "checksum": digest}}}]}
    (directory / "repository.json").write_text(json.dumps(index, indent=2) + "\n")
    subprocess.run([helper, str((directory / "repository.json").resolve())], check=True)
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    names = [binary.name, "manifest.json", "repository.json"]
    (directory / "SHA256SUMS").write_text("".join(
        f"{hashlib.sha256((directory / name).read_bytes()).hexdigest()}  {name}\n" for name in names))
    print(f"Packaged {tag}; repository URL: {source}/releases/latest/download/repository.json")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=ROOT / "plugin/dist")
    parser.add_argument("--repository", default="neilsb/silo-tracearr-bridge")
    parser.add_argument("--tag", required=True)
    args = parser.parse_args()
    package(args.directory, args.repository, args.tag)
