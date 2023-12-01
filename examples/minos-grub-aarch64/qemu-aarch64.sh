#!/bin/bash
LOCAL_EFIVARS=".cache/efivars.fd"
ESP_PATH=".cache/esp.img"

source .env

if [ ! -f "$LOCAL_EFIVARS" ]
then
    cp "$FACTORY_EFIVARS" "$LOCAL_EFIVARS"
fi

qemu-system-aarch64 \
	-cpu cortex-a72 \
	-nographic \
	-M virt \
	-m 1G \
	-kernel .cache/esp/efi/ubuntu/vmlinuz \
	-initrd .cache/initrd.img \
	-append "apparmor=0" \
	-usb \
	-device usb-ehci,id=ehci \
	-netdev user,id=net0,hostfwd=tcp::2323-:23 \
	-device virtio-net-pci,netdev=net0
	#-bios "$FIRMWARE_IMAGE" \
	#-drive file="$ESP_PATH",format=raw,if=virtio \
	#-drive if=pflash,format=raw,unit=1,file="$LOCAL_EFIVARS"
