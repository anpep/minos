#!/bin/sh
LOCAL_EFIVARS=".cache/efivars.fd"
ESP_PATH=".cache/esp.img"

source .env

if [ ! -f "$LOCAL_EFIVARS" ]
then
    cp "$FACTORY_EFIVARS" "$LOCAL_EFIVARS"
fi

qemu-system-x86_64 \
	-vga cirrus \
	-bios "$FIRMWARE_IMAGE" \
	-machine q35 \
	-accel "$ACCELERATION_ENGINE" \
	-cpu max \
	-m 2G \
	-drive file="$ESP_PATH",format=raw \
	-drive if=pflash,format=raw,unit=1,file="$LOCAL_EFIVARS"
