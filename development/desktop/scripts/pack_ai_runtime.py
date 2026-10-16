from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import struct
import tempfile
from pathlib import Path
from typing import Any


MAGIC = b"CUBICAI1"
FOOTER_SIZE = 56
MAX_PACKAGE_BYTES = 1_500_000_000
WORKER_FILES = {
    "worker/ai_radar/__init__.py",
    "worker/ai_radar/__main__.py",
    "worker/ai_radar/core.py",
    "worker/ai_radar/sources.json",
    "worker/ai_radar/watch.py",
}
REQUIRED_FILES = {
    "model/Qwen3-0.6B-Q4_0.gguf",
    "llama/llama-completion.exe",
    "llama/msvcp140.dll",
    "llama/vcruntime140.dll",
    "llama/vcruntime140_1.dll",
    "python/python.exe",
    "python/python313.zip",
    "python/python313._pth",
    "worker/ai_radar/watch.py",
    "worker/ai_radar/sources.json",
    "schema/summary-schema.json",
    "feed/initial-ai-intelligence.json",
    "licenses/Apache-2.0.txt",
    "licenses/MIT-llama.cpp.txt",
    "licenses/LLVM-OpenMP.txt",
    "licenses/Python-3.13.16-EMBED-SBOM.json",
    "licenses/Microsoft-VCRedist-NOTICE.md",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_stream(source, size: int) -> str:
    digest = hashlib.sha256()
    remaining = size
    while remaining:
        chunk = source.read(min(1024 * 1024, remaining))
        if not chunk:
            raise ValueError("unexpected end of package input")
        digest.update(chunk)
        remaining -= len(chunk)
    return digest.hexdigest()


def validate_pe(path: Path) -> None:
    with path.open("rb") as source:
        dos = source.read(64)
        if len(dos) != 64 or dos[:2] != b"MZ":
            raise ValueError("base executable is not a Windows PE file")
        pe_offset = struct.unpack_from("<I", dos, 0x3C)[0]
        source.seek(pe_offset)
        header = source.read(6)
        if len(header) != 6 or header[:4] != b"PE\0\0" or struct.unpack_from("<H", header, 4)[0] != 0x8664:
            raise ValueError("base executable is not x86-64")


def allowed_path(name: str) -> bool:
    parts = name.split("/")
    if not name or "\\" in name or ":" in name or any(part in {"", ".", ".."} for part in parts):
        return False
    if parts[0] == "python":
        suffix = Path(name).suffix.lower()
        return name == "python/python.exe" or suffix in {".dll", ".pyd", ".zip", ".txt", ".cat"} or name.endswith("._pth")
    if parts[0] == "llama":
        return name == "llama/llama-completion.exe" or name.endswith(".dll") or name == "llama/LICENSE-LLVM-OpenMP"
    if parts[0] == "worker":
        return name in WORKER_FILES
    if parts[0] == "model":
        return name == "model/Qwen3-0.6B-Q4_0.gguf"
    if parts[0] == "schema":
        return name == "schema/summary-schema.json"
    if parts[0] == "feed":
        return name == "feed/initial-ai-intelligence.json"
    if parts[0] == "licenses":
        return name in {
            "licenses/Apache-2.0.txt",
            "licenses/MIT-llama.cpp.txt",
            "licenses/LLVM-OpenMP.txt",
            "licenses/Python-3.13.16-EMBED-SBOM.json",
            "licenses/Microsoft-VCRedist-NOTICE.md",
        }
    return False


def collect_payload(root: Path) -> list[Path]:
    if not root.is_dir():
        raise ValueError("payload staging directory is missing")
    files = sorted(root.rglob("*"))
    result: list[Path] = []
    names: set[str] = set()
    for path in files:
        if path.is_symlink():
            raise ValueError("payload must not contain links")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError("payload contains an unsupported filesystem entry")
        name = path.relative_to(root).as_posix()
        if not allowed_path(name):
            raise ValueError(f"payload contains an unapproved file: {name}")
        if name in names:
            raise ValueError(f"payload path is duplicated: {name}")
        names.add(name)
        result.append(path)
    missing = REQUIRED_FILES - names
    if missing:
        raise ValueError("payload is missing required files: " + ", ".join(sorted(missing)))
    if names.intersection({"accounts.json", "model_observer.db", "ai-intelligence.json"}):
        raise ValueError("payload contains a protected file")
    source_list = json.loads((root / "worker/ai_radar/sources.json").read_text(encoding="utf-8"))
    if len(source_list) != 91:
        raise ValueError("public source registry count differs from the reviewed 91-source snapshot")
    return result


def validate_payload(root: Path, files: list[Path], lock: dict[str, Any]) -> None:
    model_path = root / "model/Qwen3-0.6B-Q4_0.gguf"
    model_lock = lock["model"]
    if model_path.stat().st_size != model_lock["size_bytes"] or sha256_file(model_path) != model_lock["sha256"]:
        raise ValueError("Qwen model size or SHA-256 differs from resource-lock.json")
    for runtime in lock["msvc_runtime"]["files"]:
        runtime_path = root / "llama" / runtime["name"]
        if runtime_path.stat().st_size != runtime["size_bytes"] or sha256_file(runtime_path) != runtime["sha256"]:
            raise ValueError("MSVC runtime file differs from resource-lock.json: " + runtime["name"])
    pth = (root / "python/python313._pth").read_text(encoding="ascii")
    if "..\\worker" not in pth.splitlines():
        raise ValueError("embedded Python path does not include the packaged worker")
    initial_feed = json.loads((root / "feed/initial-ai-intelligence.json").read_text(encoding="utf-8"))
    if initial_feed.get("version") != 1 or not isinstance(initial_feed.get("events"), list):
        raise ValueError("initial feed is not a version 1 AI feed")
    if not any(path.name.endswith(".dll") for path in files if path.is_relative_to(root / "llama")):
        raise ValueError("llama.cpp runtime DLLs are missing")


def make_manifest(root: Path, files: list[Path], lock_bytes: bytes, lock: dict[str, Any]) -> tuple[bytes, list[dict[str, Any]]]:
    entries: list[dict[str, Any]] = []
    offset = 0
    total = 0
    for path in files:
        size = path.stat().st_size
        total += size
        if total > MAX_PACKAGE_BYTES:
            raise ValueError("payload exceeds the 1.5 GB safety limit")
        entry = {
            "path": path.relative_to(root).as_posix(),
            "offset": offset,
            "size": size,
            "sha256": sha256_file(path),
        }
        entries.append(entry)
        offset += size
    manifest = {
        "format_version": 1,
        "resource_lock_sha256": hashlib.sha256(lock_bytes).hexdigest(),
        "model_sha256": lock["model"]["sha256"],
        "llama_archive_sha256": lock["inference_runtime"]["sha256"],
        "python_archive_sha256": lock["python_runtime"]["sha256"],
        "entries": entries,
    }
    encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return encoded, entries


def verify_package(path: Path) -> tuple[int, str, dict[str, Any]]:
    with path.open("rb") as package:
        size = path.stat().st_size
        package.seek(-FOOTER_SIZE, os.SEEK_END)
        footer = package.read(FOOTER_SIZE)
        if footer[48:] != MAGIC:
            raise ValueError("package footer is invalid")
        payload_offset, manifest_size = struct.unpack_from("<QQ", footer, 0)
        manifest_offset = size - FOOTER_SIZE - manifest_size
        if manifest_offset < payload_offset or manifest_size > 1_048_576:
            raise ValueError("package manifest range is invalid")
        package.seek(manifest_offset)
        manifest_bytes = package.read(manifest_size)
        manifest_hash = hashlib.sha256(manifest_bytes).digest()
        if manifest_hash != footer[16:48]:
            raise ValueError("package manifest hash mismatch")
        manifest = json.loads(manifest_bytes)
        entries = manifest["entries"]
        for entry in entries:
            package.seek(payload_offset + entry["offset"])
            actual = sha256_stream(package, entry["size"])
            if actual != entry["sha256"]:
                raise ValueError("package entry hash mismatch: " + entry["path"])
    return size, sha256_file(path), manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Append a verified local AI runtime to a Windows x64 Cubic EXE")
    parser.add_argument("--base-exe", type=Path, required=True)
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--resource-lock", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest-output", type=Path)
    args = parser.parse_args()

    if not args.base_exe.is_file() or args.output.exists():
        raise ValueError("base EXE must exist and output must be new; refusing overwrite")
    if args.manifest_output and args.manifest_output.exists():
        raise ValueError("manifest output already exists; refusing overwrite")
    if args.base_exe.resolve() == args.output.resolve():
        raise ValueError("base and output EXE paths must differ")
    validate_pe(args.base_exe)
    lock_bytes = args.resource_lock.read_bytes()
    lock = json.loads(lock_bytes)
    files = collect_payload(args.payload)
    validate_payload(args.payload, files, lock)
    manifest_bytes, entries = make_manifest(args.payload, files, lock_bytes, lock)
    payload_offset = args.base_exe.stat().st_size
    manifest_hash = hashlib.sha256(manifest_bytes).digest()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".cubic-ai-package-", suffix=".tmp", dir=args.output.parent)
    try:
        with os.fdopen(fd, "wb") as output, args.base_exe.open("rb") as base:
            shutil.copyfileobj(base, output, 1024 * 1024)
            for path, entry in zip(files, entries, strict=True):
                digest = hashlib.sha256()
                written = 0
                with path.open("rb") as source:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        output.write(chunk)
                        digest.update(chunk)
                        written += len(chunk)
                if written != entry["size"] or digest.hexdigest() != entry["sha256"]:
                    raise ValueError("payload changed during packaging: " + entry["path"])
            output.write(manifest_bytes)
            output.write(struct.pack("<QQ", payload_offset, len(manifest_bytes)))
            output.write(manifest_hash)
            output.write(MAGIC)
            output.flush()
            os.fsync(output.fileno())
        temp_path = Path(temp_name)
        verify_package(temp_path)
        os.replace(temp_path, args.output)
    except Exception:
        try:
            Path(temp_name).unlink(missing_ok=True)
        except OSError:
            pass
        raise

    size, digest, manifest = verify_package(args.output)
    if args.manifest_output:
        args.manifest_output.parent.mkdir(parents=True, exist_ok=True)
        args.manifest_output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    seed = next(item["sha256"] for item in manifest["entries"] if item["path"] == "feed/initial-ai-intelligence.json")
    print(json.dumps({
        "size_bytes": size,
        "sha256": digest,
        "entries": len(entries),
        "model_sha256": manifest["model_sha256"],
        "initial_feed_sha256": seed,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        raise SystemExit(f"package failed: {error}")
