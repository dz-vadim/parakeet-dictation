#!/usr/bin/env python3
"""Model profiles: models.json access, downloads, legacy migration.

Command line (what download_models.py used to do):
    python -m parakeet_dictation.models              # download the default (desktop) profile
    python -m parakeet_dictation.models desktop      # download desktop profile
    python -m parakeet_dictation.models laptop       # download laptop profile
    python -m parakeet_dictation.models streaming    # download streaming profile
    python -m parakeet_dictation.models all          # download all profiles

Only the recognizer models are downloaded.  The voice activity detector is
TEN VAD, which ships inside the ten-vad pip package (see audio.py); nothing
is fetched for it.
"""

import json
import sys
import threading
from pathlib import Path

from gi.repository import GLib

from .config import APP_DIR, MODELS_DIR

MODELS_JSON = Path(__file__).resolve().parent / "data" / "models.json"


def _migrate_legacy_models():
    """Move models from APP_DIR/models to the XDG data directory if needed."""
    import shutil

    legacy = APP_DIR / "models"
    if not legacy.is_dir() or legacy == MODELS_DIR:
        return
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    for item in legacy.iterdir():
        dest = MODELS_DIR / item.name
        if dest.exists():
            continue
        try:
            shutil.move(str(item), str(dest))
        except OSError:
            # Installed read-only — copy instead
            if item.is_dir():
                shutil.copytree(str(item), str(dest))
            else:
                shutil.copy2(str(item), str(dest))


_migrate_legacy_models()


def load_model_profiles() -> dict:
    with open(MODELS_JSON) as f:
        return json.load(f)


def _any_model_downloaded(profiles: dict) -> bool:
    """Check if at least one model is downloaded."""
    for mid in profiles:
        if _is_model_downloaded(mid, profiles):
            return True
    return False


def _is_model_downloaded(model_id: str, profiles: dict) -> bool:
    """Check if all files for a model are present on disk."""
    profile = profiles.get(model_id)
    if not profile:
        return False
    model_dir = MODELS_DIR / model_id
    for key, info in profile.get("files", {}).items():
        if not (model_dir / info["filename"]).exists():
            return False
    return True


def _download_file(url: str, dest: Path, on_progress_bytes=None):
    """Download a single file with progress reporting. Raises on failure."""
    import requests

    resp = requests.get(url, stream=True, timeout=(15, 60))
    resp.raise_for_status()
    total = int(resp.headers.get("content-length", 0))
    downloaded = 0
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with open(tmp, "wb") as f:
            for chunk in resp.iter_content(1024 * 1024):
                f.write(chunk)
                downloaded += len(chunk)
                if on_progress_bytes:
                    on_progress_bytes(downloaded, total)
        tmp.rename(dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _download_model(model_id: str, profiles_data: dict, on_progress, on_done):
    """Download a model in a background thread."""

    def _worker():
        try:
            profile = profiles_data["profiles"][model_id]
            model_dir = MODELS_DIR / model_id
            model_dir.mkdir(parents=True, exist_ok=True)

            # Download model files
            files = profile["files"]
            total_files = len(files)
            for i, (key, info) in enumerate(files.items(), 1):
                dest = model_dir / info["filename"]
                if dest.exists() and dest.stat().st_size > 0:
                    continue

                def _file_progress(done, total, fname=info["filename"], idx=i):
                    if total:
                        frac = done / total
                        mb = done / 1024 / 1024
                        total_mb = total / 1024 / 1024
                        GLib.idle_add(
                            on_progress,
                            f"{fname} ({idx}/{total_files}): {mb:.0f}/{total_mb:.0f} MB",
                            frac,
                        )
                    else:
                        mb = done / 1024 / 1024
                        GLib.idle_add(on_progress, f"{fname}: {mb:.0f} MB", -1.0)

                _download_file(info["url"], dest, _file_progress)

            GLib.idle_add(on_done, True, "")
        except Exception as e:
            GLib.idle_add(on_done, False, str(e))

    threading.Thread(target=_worker, daemon=True).start()


def _download_all_models(profiles_data: dict, on_progress, on_done):
    """Download all models sequentially in a background thread."""

    def _worker():
        try:
            profiles = profiles_data["profiles"]
            for mid, profile in profiles.items():
                model_dir = MODELS_DIR / mid
                model_dir.mkdir(parents=True, exist_ok=True)
                files = profile["files"]
                total_files = len(files)
                for i, (key, info) in enumerate(files.items(), 1):
                    dest = model_dir / info["filename"]
                    if dest.exists() and dest.stat().st_size > 0:
                        continue
                    short_name = profile["name"][:18]

                    def _file_progress(done, total, sn=short_name, fname=info["filename"], idx=i, tf=total_files):
                        if total:
                            frac = done / total
                            mb = done / 1024 / 1024
                            total_mb = total / 1024 / 1024
                            GLib.idle_add(on_progress, f"{sn}: {fname} {mb:.0f}/{total_mb:.0f} MB", frac)
                        else:
                            mb = done / 1024 / 1024
                            GLib.idle_add(on_progress, f"{sn}: {fname} {mb:.0f} MB", -1.0)

                    _download_file(info["url"], dest, _file_progress)
            GLib.idle_add(on_done, True, "")
        except Exception as e:
            GLib.idle_add(on_done, False, str(e))

    threading.Thread(target=_worker, daemon=True).start()


# ---------------------------------------------------------------------------
# Command-line downloader (formerly download_models.py)
# ---------------------------------------------------------------------------

def download_file(url: str, dest: Path):
    """CLI download with a progress bar, through the same atomic path the
    GUI uses: the bytes land in `<dest>.part` and are renamed only once the
    transfer is complete.  Writing `dest` directly, as this did, let a
    truncated download pass the exists()/size checks and fail later inside
    sherpa-onnx."""
    from tqdm import tqdm

    with tqdm(total=0, unit="B", unit_scale=True, desc=dest.name) as bar:
        def progress(done, total):
            if total and bar.total != total:
                bar.total = total
                bar.refresh()
            bar.update(done - bar.n)

        _download_file(url, dest, progress)


def download_profile(config: dict, profile_id: str):
    profiles = config["profiles"]
    if profile_id not in profiles:
        print(f"Unknown profile: {profile_id}")
        print(f"Available: {', '.join(profiles.keys())}")
        sys.exit(1)

    profile = profiles[profile_id]
    profile_dir = MODELS_DIR / profile_id
    profile_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n=== {profile['name']} ({profile['size_mb']} MB) ===")

    for key, info in profile["files"].items():
        dest = profile_dir / info["filename"]
        if dest.exists() and dest.stat().st_size > 0:
            print(f"  {info['filename']} already exists.")
            continue
        print(f"Downloading {info['filename']}...")
        download_file(info["url"], dest)


def main():
    with open(MODELS_JSON) as f:
        config = json.load(f)

    MODELS_DIR.mkdir(parents=True, exist_ok=True)

    targets = sys.argv[1:] if len(sys.argv) > 1 else ["desktop"]
    if "all" in targets:
        targets = list(config["profiles"].keys())

    for t in targets:
        download_profile(config, t)

    print("\nDone. Models are in:", MODELS_DIR)


if __name__ == "__main__":
    main()
