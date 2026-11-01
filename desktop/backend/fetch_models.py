"""Downloads every speech model the desktop app uses into desktop/models (inside the project), once.
Run by scripts/run-desktop.bat on first start and by scripts/download-models.bat; safe to run again."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "shared" / "core"))
import config  # noqa: E402


def main() -> int:
    config.MODELS_DIR.mkdir(parents=True, exist_ok=True)
    config.WHISPER_DIR.mkdir(parents=True, exist_ok=True)
    from faster_whisper.utils import download_model
    for name in dict.fromkeys([config.WHISPER_MODEL, config.DRAFT_WHISPER_MODEL, config.FINAL_WHISPER_MODEL]):
        print(f"Speech model {name} ...", flush=True)
        download_model(name, cache_dir=str(config.WHISPER_DIR))
    import speakers
    print("Speaker model ...", flush=True)
    speakers.ensure_model(config.SPEAKER_MODEL, True)
    (config.MODELS_DIR / ".ready").write_text("ok")
    print("All speech models are in", config.MODELS_DIR)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print("Could not download the models:", exc)
        sys.exit(1)
