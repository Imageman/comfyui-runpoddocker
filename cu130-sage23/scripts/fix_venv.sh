#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 2 ]; then
    echo "Usage: $0 <old-venv-path> <new-venv-path>"
    exit 1
fi

OLD_PATH="$1"
NEW_PATH="$2"
BIN_DIR="${NEW_PATH}/bin"

if [ ! -d "${BIN_DIR}" ]; then
    echo "VENV: bin directory not found: ${BIN_DIR}"
    exit 1
fi

echo "VENV: Rewriting ${OLD_PATH} -> ${NEW_PATH}"
cd "${BIN_DIR}"

if [ -f activate ]; then
    sed -i \
        -e "s|VIRTUAL_ENV=\"${OLD_PATH}\"|VIRTUAL_ENV=\"${NEW_PATH}\"|g" \
        -e "s|VIRTUAL_ENV=${OLD_PATH}|VIRTUAL_ENV=${NEW_PATH}|g" \
        activate
fi

find . -maxdepth 1 -type f -exec grep -Il "^#!${OLD_PATH}/bin/python" {} + 2>/dev/null | \
    while IFS= read -r file; do
        sed -i "1s|^#!${OLD_PATH}/bin/python[^ ]*|#!${NEW_PATH}/bin/python3|" "${file}"
    done
