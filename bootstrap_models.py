"""
Download faster-whisper model weights into ./models/ inside the repo so the
server can run fully offline (no HuggingFace cache dependency).

Run once after cloning, OR rerun to refresh weights. Idempotent — re-using
the snapshot_download cache logic.
"""

import os
import sys

try:
    from huggingface_hub import snapshot_download
except ImportError:
    print("ERROR: huggingface_hub not installed. Run: pip install huggingface_hub",
          file=sys.stderr)
    sys.exit(1)


REPO_ROOT_DIRECTORY = os.path.dirname(os.path.abspath(__file__))
LOCAL_MODELS_PARENT_DIRECTORY = os.path.join(REPO_ROOT_DIRECTORY, "models")

FASTER_WHISPER_MODEL_SIZES_TO_BUNDLE = [
    "tiny",
    "tiny.en",
    "base",
    "base.en",
    "small",
    "small.en",
]


def main():
    os.makedirs(LOCAL_MODELS_PARENT_DIRECTORY, exist_ok=True)
    for whisper_model_size_label in FASTER_WHISPER_MODEL_SIZES_TO_BUNDLE:
        target_directory = os.path.join(
            LOCAL_MODELS_PARENT_DIRECTORY, whisper_model_size_label
        )
        huggingface_repo_id = f"Systran/faster-whisper-{whisper_model_size_label}"
        print(
            f"[bootstrap] downloading {huggingface_repo_id} -> {target_directory}",
            flush=True,
        )
        snapshot_download(
            repo_id=huggingface_repo_id,
            local_dir=target_directory,
            allow_patterns=[
                "config.json",
                "model.bin",
                "tokenizer.json",
                "vocabulary.txt",
                "preprocessor_config.json",
            ],
        )
    print(f"[bootstrap] all models bundled under {LOCAL_MODELS_PARENT_DIRECTORY}",
          flush=True)


if __name__ == "__main__":
    main()
