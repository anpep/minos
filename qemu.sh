#!/bin/sh
LOCAL_EFIVARS=".cache/efivars.fd"
ESP_PATH=".cache/esp"

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
	-drive file=.cache/esp.img,format=raw,if=virtio
	

#	-drive if=pflash,format=raw,unit=1,file="$LOCAL_EFIVARS" \
	
#-device scsi-hd,drive=esp \
#-drive format=raw,file=.cache/esp.img,media=disk,if=none,id=esp \
#-drive format=raw,file=fat:rw:"$ESP_PATH",media=disk \
#

