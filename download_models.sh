#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODELS_DIR="$SCRIPT_DIR/models"

mkdir -p "$MODELS_DIR"

echo "Downloading Vosk English model..."
if [ ! -d "$MODELS_DIR/vosk-model-small-en-us-0.15" ]; then
    wget -q --show-progress -O /tmp/vosk-en.zip \
        https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip
    unzip -qo /tmp/vosk-en.zip -d "$MODELS_DIR"
    rm /tmp/vosk-en.zip
    echo "English model ready."
else
    echo "English model already exists, skipping."
fi

echo "Downloading Vosk Russian model..."
if [ ! -d "$MODELS_DIR/vosk-model-small-ru-0.22" ]; then
    wget -q --show-progress -O /tmp/vosk-ru.zip \
        https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip
    unzip -qo /tmp/vosk-ru.zip -d "$MODELS_DIR"
    rm /tmp/vosk-ru.zip
    echo "Russian model ready."
else
    echo "Russian model already exists, skipping."
fi

echo "All models downloaded to $MODELS_DIR"
