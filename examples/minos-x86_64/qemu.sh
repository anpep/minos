#!/bin/bash
LOCAL_EFIVARS=".cache/efivars.fd"
ESP_PATH=".cache/esp.img"

source .env

if [ ! -f "$LOCAL_EFIVARS" ]
then
    cp "$FACTORY_EFIVARS" "$LOCAL_EFIVARS"
fi

qemu-system-x86_64 \
	-net none \
	-nographic \
	-machine q35 \
	-cpu max \
	-m 1G \
	-drive file="$ESP_PATH",format=raw \
	-drive if=pflash,format=raw,readonly=yes,unit=0,file="$FIRMWARE_IMAGE" \
	-drive if=pflash,format=raw,unit=1,file="$LOCAL_EFIVARS"
