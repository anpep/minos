#!/bin/sh
LOCAL_EFIVARS=".cache/efivars.fd"
ESP_PATH=".cache/esp"

source .env

if [ ! -f "$LOCAL_EFIVARS" ]
then
    cp "$FACTORY_EFIVARS" "$LOCAL_EFIVARS"
fi

qemu-system-aarch64 \
	-bios "$FIRMWARE_IMAGE" \
	-nographic \
	-machine virt,highmem=off \
	-accel "$ACCELERATION_ENGINE" \
	-cpu cortex-a72 \
	-m 2G \
	-drive format=raw,file=fat:rw:"$ESP_PATH",media=disk \
	-drive if=pflash,format=raw,unit=1,file="$LOCAL_EFIVARS"
