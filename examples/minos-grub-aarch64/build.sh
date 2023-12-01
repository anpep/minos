#!/bin/bash
set -e
initramfs_root="$(pwd)/initramfs"
pushd ../../../kernos
GOOS=linux GOARCH=arm64 go build ./cmd/kernos
mkdir -p "$initramfs_root/sbin"
cp kernos "$initramfs_root/sbin"
popd
echo $initramfs_root
python3 ../../mincraft/mincraft.py clean-initramfs
python3 ../../mincraft/mincraft.py


