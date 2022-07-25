# minos
> A poor man's Linux AArch64 prototyping environment

## Getting started
Rename `.env.example` to `.env` and fill in the variables inside for QEMU to work. You can edit `mincraft.yaml` for changing the selection of packages and other properties of the target OS.

Running `./mincraft.py` will execute the following steps:
1. Download the `base` image supplied to `mincraft.yaml`
2. Extract the `base` image
3. Download all .deb files specified in `mincraft.yaml`
4. Extract all files from all packages in `mincraft.yaml`
5. Copy all files from the directories specified in the `overlays` entry in `mincraft.yaml` onto the `.cache/rootfs` directory
6. Compress `.cache/rootfs` onto the initramfs located at `.cache/initrd.gz`

Depending on the `boot mechanism` specified in the YAML file (currently only `grub` and `systemd-stub` are supported, and the latter is not functional yet), it will perform the following tasks:

### For `grub`
(The `grub-efi-arm64-signed` package must be installed on the initramfs)
- Copy the signed GRUB EFI binary, the kernel image and the initrd file onto the EFI system partition directory located at `.cache/esp`.
- Generate a `grub.cfg` file and place it onto the ESP

### For `systemd-stub`
(The `systemd` package must be installed on the initramfs)
- Decompress the kernel
- Modify the systemd EFI stub PE binary adding the following sections:
    - `.linux` with the decompressed kernel binary
    - `.cmdline` with the configured command line
    - `.osrel` with the contents of `/etc/os-release`
    - `.dtb` with the virt DTB from QEMU
    - `.initrd` with the compressed initramfs image
- Place the unsigned EFI binary on the target EFI system partition

Currently, booting with the systemd-stub mechanism crashes on EDK II.