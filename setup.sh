#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

command_exists() { command -v "$1" >/dev/null 2>&1; }

update_path() {
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
}

install_uv() {
    if command_exists uv; then
        echo "uv already installed: $(uv --version)"
        return
    fi
    echo "uv not found; installing it..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    update_path
    if ! command_exists uv; then
        echo "ERROR: uv was installed but is not on PATH. Open a new terminal and run setup.sh again." >&2
        exit 1
    fi
}

install_python() {
    if uv python find 3.12 >/dev/null 2>&1; then
        echo "Python 3.12 available: $(uv python find 3.12)"
    else
        echo "Python 3.12 not found; installing it via uv..."
        uv python install 3.12
    fi

    local venv_python="$SCRIPT_DIR/.venv/bin/python"
    if [ ! -x "$venv_python" ]; then
        echo "Creating virtual environment in .venv..."
        uv venv --python 3.12 .venv
    fi
    uv pip install --python "$venv_python" -e .

    update_path
    if ! command_exists python; then
        echo "ERROR: Python was installed but is not on PATH. Open a new terminal and run setup.sh again." >&2
        exit 1
    fi
}

install_clang() {
    if command_exists cc || command_exists clang || command_exists gcc; then
        echo "C compiler already installed."
        return
    fi
    echo "C compiler not found; installing LLVM/Clang..."

    if command_exists apt-get; then
        sudo apt-get update && sudo apt-get install -y clang
    elif command_exists dnf; then
        sudo dnf install -y clang
    elif command_exists pacman; then
        sudo pacman -S --noconfirm clang
    elif command_exists brew; then
        brew install llvm
        update_path
        if ! command_exists clang; then
            echo "WARNING: LLVM was installed but clang is not on PATH. Open a new terminal and run setup.sh again."
            return
        fi
    else
        echo "WARNING: no supported package manager found; install LLVM/Clang or GCC manually."
        return
    fi

    update_path
    if ! command_exists clang; then
        echo "WARNING: LLVM was installed but clang is not on PATH. Open a new terminal and run setup.sh again."
    fi
}

install_uv
install_python
install_clang

echo "Setup complete."
echo "Activate the environment with: source .venv/bin/activate"