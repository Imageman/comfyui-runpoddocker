#!/usr/bin/env bash
set -euo pipefail

PYTHON_VERSION="${PYTHON_VERSION:-3.13}"

apt-get update
apt-get install -y --no-install-recommends \
    software-properties-common \
    ca-certificates \
    curl \
    wget \
    gnupg

add-apt-repository -y ppa:deadsnakes/ppa
apt-get update
apt-get install -y --no-install-recommends \
    "python${PYTHON_VERSION}" \
    "python${PYTHON_VERSION}-dev" \
    "python${PYTHON_VERSION}-venv" \
    "python${PYTHON_VERSION}-tk" \
    bash \
    build-essential \
    pkg-config \
    git \
    git-lfs \
    ffmpeg \
    libglib2.0-0 \
    libsm6 \
    libgl1 \
    libxrender1 \
    libxext6 \
    libgoogle-perftools4 \
    libtcmalloc-minimal4 \
    nginx \
    nodejs \
    npm \
    jq \
    rsync \
    zstd \
    pv \
    aria2 \
    psmisc \
    zip \
    unzip \
    p7zip-full \
    tmux

curl -fsSL https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py
"python${PYTHON_VERSION}" /tmp/get-pip.py
rm -f /tmp/get-pip.py

ln -sfn "/usr/bin/python${PYTHON_VERSION}" /usr/local/bin/python
ln -sfn "/usr/bin/python${PYTHON_VERSION}" /usr/local/bin/python3
ln -sfn /usr/local/bin/pip3 /usr/local/bin/pip

python3 -m pip install --no-cache-dir --upgrade \
    pip \
    wheel \
    "setuptools<82"

python3 -m pip install --no-cache-dir \
    jupyterlab==4.5.10 \
    notebook==7.5.6 \
    ipykernel \
    ipywidgets \
    jupyterlab_widgets

git lfs install --system
update-ca-certificates
apt-get clean
rm -rf /var/lib/apt/lists/*
