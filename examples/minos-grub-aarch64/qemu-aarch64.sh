#!/bin/sh
LOCAL_EFIVARS=".cache/efivars.fd"
ESP_PATH=".cache/esp.img"

source .env

if [ ! -f "$LOCAL_EFIVARS" ]
then
    cp "$FACTORY_EFIVARS" "$LOCAL_EFIVARS"
fi

qemu-system-aarch64 \
	-vga cirrus \
	-bios "$FIRMWARE_IMAGE" \
	-machine virt,highmem=off \
	-accel "$ACCELERATION_ENGINE" \
	-cpu cortex-a72 \
	-m 2G \
	-drive file="$ESP_PATH",format=raw,if=virtio \
	-drive if=pflash,format=raw,unit=1,file="$LOCAL_EFIVARS"
