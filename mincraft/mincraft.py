#!/usr/bin/env python3
# coding: utf8

import filecmp
from glob import glob
from io import SEEK_SET, FileIO
import tempfile
import textwrap
import yaml
import os
import sys
import shutil
import tarfile
import gzip
import subprocess
import requests
from debian.arfile import ArError
from debian.debfile import DebFile
from tqdm.auto import tqdm
from urllib.parse import urlparse
from typing import Optional, List, Union

CACHE_DIR = os.path.join(os.path.abspath(os.getcwd()), ".cache")
ROOTFS_DIR = os.path.join(CACHE_DIR, "rootfs")
PKG_DIR = os.path.join(CACHE_DIR, "packages")
ESP_DIR = os.path.join(CACHE_DIR, "esp")
INITRD_FILENAME = os.path.join(CACHE_DIR, "initrd.gz")
ESP_FILENAME = os.path.join(CACHE_DIR, "esp.img")

EFI_ARCH_SUFFIXES = {"amd64": "x64", "arm64": "aa64", "aarch64": "aa64"}


def parse_config(filename: str) -> dict:
    with open(filename) as f:
        return yaml.load(f, Loader=yaml.FullLoader)


def fetch_file(url: str, filename: Optional[str] = None) -> str:
    os.makedirs(PKG_DIR, exist_ok=True)

    if filename is None:
        parsed_url = urlparse(url)
        filename = os.path.basename(parsed_url.path)

    dest_path = os.path.join(PKG_DIR, filename)
    if os.path.isfile(dest_path):
        return dest_path

    with requests.get(url, stream=True) as r:
        total_length = int(r.headers.get("Content-Length"))

        desc = f"fetching {filename}"
        if len(desc) > 40:
            desc = desc[:40] + "..."
        with tqdm.wrapattr(r.raw, "read", total=total_length, desc=desc) as raw:
            with open(dest_path, "wb") as output:
                shutil.copyfileobj(raw, output)

    return dest_path


def extract_rootfs(filename: str) -> bool:
    os.makedirs(ROOTFS_DIR, exist_ok=True)
    os.makedirs(os.path.join(ROOTFS_DIR, ".installed_pkgs"), exist_ok=True)
    extract_indicator = os.path.join(ROOTFS_DIR, ".installed_pkgs", ".base")

    if os.path.isfile(extract_indicator):
        return False

    with tarfile.open(filename, "r:gz") as tgz:
        members = tgz.getmembers()

        for member in tqdm(members, desc=f"extracting base"):
            tgz.extract(member, ROOTFS_DIR)

        os.close(os.open(extract_indicator, os.O_CREAT))

    return True


def install_package(filename: str) -> bool:
    os.makedirs(ROOTFS_DIR, exist_ok=True)
    os.makedirs(os.path.join(ROOTFS_DIR, ".installed_pkgs"), exist_ok=True)

    try:
        deb_file = DebFile(filename)
    except ArError:
        print(f"error: {filename} is not a valid Debian package", file=sys.stderr)
        sys.exit(1)

    package = deb_file.debcontrol()["Package"]
    install_indicator = os.path.join(ROOTFS_DIR, ".installed_pkgs", package)

    if os.path.isfile(install_indicator):
        return False

    tgz = deb_file.data.tgz()
    members = tgz.getmembers()

    for member in tqdm(members, desc=f"installing {package}"):
        tgz.extract(member, ROOTFS_DIR, numeric_owner=True)

    os.close(os.open(install_indicator, os.O_CREAT))
    return True


def copy_overlay(overlay: str) -> bool:
    os.makedirs(ROOTFS_DIR, exist_ok=True)

    dcmp = filecmp.dircmp(overlay, ROOTFS_DIR)
    print(dcmp.diff_files)
    if dcmp.diff_files:
        print(f"copying overlay {overlay}")
        shutil.copytree(overlay, ROOTFS_DIR, dirs_exist_ok=True)
        return True

    return False


def pack_initramfs():
    pack_lock = os.path.join(ROOTFS_DIR, ".installed_pkgs", ".initramfs")
    os.close(os.open(pack_lock, os.O_CREAT))
    subprocess.run(
        [
            "/bin/sh",
            "-c",
            f"find . | (cpio -o --format newc --owner 0:0 2>/dev/null) | gzip | pv -N 'packing initramfs' > {INITRD_FILENAME}",
        ],
        check=True,
        cwd=ROOTFS_DIR,
    )
    os.unlink(pack_lock)


def is_initramfs_pack_incomplete() -> bool:
    pack_lock = os.path.join(ROOTFS_DIR, ".installed_pkgs", ".initramfs")
    return os.path.isfile(pack_lock) or not os.path.isfile(INITRD_FILENAME)


def is_esp_created() -> bool:
    return os.path.isdir(ESP_DIR) and os.path.isfile(ESP_FILENAME)


def require_packages(*packages: List[str]):
    missing_packages = set()
    for package in set(packages):
        package_indicator_filename = os.path.join(
            ROOTFS_DIR, ".installed_pkgs", package
        )
        if not os.path.isfile(package_indicator_filename):
            missing_packages.add(package)

    if missing_packages:
        print(
            f"error: package(s) {', '.join(missing_packages)} required but not installed on target OS"
        )
        sys.exit(1)


def open_kernel(kernel_src_filename) -> Union[gzip.GzipFile, FileIO]:
    try:
        print(f"decompressing kernel {os.path.basename(kernel_src_filename)}")
        f = gzip.open(kernel_src_filename, "rb")
        f.read(1)
        f.seek(0, SEEK_SET)
        return f
    except gzip.BadGzipFile:
        print(f"copying uncompressed kernel {os.path.basename(kernel_src_filename)}")
        return open(kernel_src_filename, "rb")


def open_initrd(initrd_src_filename) -> Union[gzip.GzipFile, FileIO]:
    try:
        print(f"decompressing initramfs")
        f = gzip.open(initrd_src_filename, "rb")
        f.read(1)
        f.seek(0, SEEK_SET)
        return f
    except gzip.BadGzipFile:
        print(f"copying uncompressed initramfs")
        return open(initrd_src_filename, "rb")


def build_esp_systemd_stub(arch: str, kernel_filename: str, cmdline: str = ""):
    require_packages("systemd")

    if arch not in EFI_ARCH_SUFFIXES:
        print(f"unsupported architecture: {arch}", file=sys.stderr)
        sys.exit(1)

    efi_arch_suffix = EFI_ARCH_SUFFIXES[arch]

    osrel_filename = os.path.join(ROOTFS_DIR, "etc/os-release")
    linux_stub_filename = os.path.join(
        ROOTFS_DIR, f"lib/systemd/boot/efi/linux{efi_arch_suffix}.efi.stub"
    )
    kernel_src_filename = os.path.join(ROOTFS_DIR, "./" + kernel_filename)
    kernel_dst_filename = os.path.join(
        ESP_DIR, "efi", "boot", f"_noboot{efi_arch_suffix}.efi"
    )

    os.makedirs(os.path.dirname(kernel_dst_filename), exist_ok=True)

    with open_kernel(kernel_src_filename) as kernel_in:
        with tempfile.NamedTemporaryFile(delete=False) as kernel_out:
            # decompress kernel
            shutil.copyfileobj(kernel_in, kernel_out)
            kernel_out.flush()
            shutil.copyfile(
                kernel_out.name, os.path.join(ESP_DIR, "efi", "boot", "vmlinux")
            )

            with open_initrd(INITRD_FILENAME) as initrd_in:
                with tempfile.NamedTemporaryFile(
                    delete=False, suffix=".cpio"
                ) as initrd_out:
                    shutil.copyfileobj(initrd_in, initrd_out)
                    initrd_out.flush()
                    shutil.copyfile(
                        INITRD_FILENAME,
                        os.path.join(ESP_DIR, "efi", "boot", "initrd.gz"),
                    )
                    shutil.copyfile(
                        initrd_out.name, os.path.join(ESP_DIR, "efi", "boot", "initrd")
                    )

                    with tempfile.NamedTemporaryFile(delete=False) as cmdline_file:
                        cmdline_file.write(cmdline.encode("utf-8"))
                        cmdline_file.flush()
                        print("embedding boot configuration into EFI image")
                        subprocess.run(
                            [
                                "objcopy",
                                "--add-section",
                                f".osrel={osrel_filename}",
                                "--change-section-vma",
                                ".osrel=0x20000",
                                "--add-section",
                                f".cmdline={cmdline_file.name}",
                                "--change-section-vma",
                                ".cmdline=0x30000",
                                "--add-section",
                                f".linux={kernel_out.name}",
                                "--change-section-vma",
                                ".linux=0x2000000",
                                "--add-section",
                                f".initrd={initrd_out.name}",
                                "--change-section-vma",
                                ".initrd=0x3000000",
                                linux_stub_filename,
                                kernel_dst_filename,
                            ],
                            check=True,
                            stderr=sys.stderr.fileno(),
                        )

    pack_esp()


def build_esp_grub(arch: str, kernel_filename: str, cmdline: str = ""):
    require_packages("grub-efi-arm64-signed")

    file_list = {
        "usr/lib/grub/arm64-efi-signed/grubaa64.efi.signed": "efi/boot/bootaa64.efi",
        "./" + kernel_filename: "efi/ubuntu/vmlinuz",
        INITRD_FILENAME: "efi/ubuntu/initrd.gz",
    }

    for src_filename in tqdm(file_list.keys(), desc="building ESP"):
        dst_filename = os.path.join(ESP_DIR, file_list[src_filename])
        dst_directory = os.path.dirname(dst_filename)

        src_filename = os.path.join(ROOTFS_DIR, src_filename)

        if not os.path.isfile(dst_filename) or not filecmp.cmp(
            src_filename, dst_filename
        ):
            os.makedirs(dst_directory, exist_ok=True)
            shutil.copyfile(src_filename, dst_filename)

    with open(os.path.join(ESP_DIR, "efi/ubuntu/grub.cfg"), "w") as f:
        f.write(
            textwrap.dedent(
                f"""
        # autogenerated by mincraft -- please do not modify directly
        menuentry "MinOS" {{
            linux /efi/ubuntu/vmlinuz {cmdline}
            initrd /efi/ubuntu/initrd.gz
        }}
        """
            )
        )

    pack_esp()


def pack_esp():
    print("packing ESP")
    with open(ESP_FILENAME, "wb") as f:
        f.truncate(4096 * 1024 * 1024)
        f.flush()

    subprocess.run(["mkfs.vfat", ESP_FILENAME], check=True, stdout=subprocess.DEVNULL)

    for abs_path in glob(os.path.join(ESP_DIR, "**/*"), recursive=True):
        rel_path = os.path.relpath(abs_path, ESP_DIR)

        if os.path.isdir(abs_path):
            subprocess.run(
                ["mmd", "-i", ESP_FILENAME, "::" + rel_path],
                stdout=subprocess.DEVNULL,
                check=True,
            )
        else:
            subprocess.run(
                ["mcopy", "-i", ESP_FILENAME, abs_path, "::" + rel_path],
                stdout=subprocess.DEVNULL,
                check=True,
            )


def main():
    config = parse_config("mincraft.yaml")
    cmd = sys.argv[1] if len(sys.argv) > 1 else None

    if cmd == "clean":
        shutil.rmtree(CACHE_DIR)
        return
    elif cmd == "clean-esp":
        shutil.rmtree(ESP_DIR)
        os.unlink(ESP_FILENAME)
        return
    elif cmd == "clean-initramfs":
        shutil.rmtree(ROOTFS_DIR)
        os.unlink(INITRD_FILENAME)
        return
    elif cmd == "clean-packages":
        shutil.rmtree(PKG_DIR)
        return
    elif cmd is not None:
        print(f"error: unrecognized command `{cmd}'", file=sys.stderr)
        sys.exit(1)

    base_filename = fetch_file(config["base"])
    deb_filenames = [fetch_file(deb_path) for deb_path in config["debs"]]

    has_changes = False
    has_changes |= extract_rootfs(base_filename)

    for filename in deb_filenames:
        has_changes |= install_package(filename)

    if "overlays" in config:
        for overlay in config["overlays"]:
            has_changes |= copy_overlay(overlay)

    if has_changes or is_initramfs_pack_incomplete():
        pack_initramfs()

    if not is_esp_created():
        arch = config["arch"]
        boot_mechanism = config["boot"]["mechanism"]
        kernel, cmdline = config["boot"]["kernel"], config["boot"]["cmdline"]
        if boot_mechanism == "grub":
            build_esp_grub(arch, kernel, cmdline)
        elif boot_mechanism == "systemd-stub":
            build_esp_systemd_stub(arch, kernel, cmdline)
        else:
            print(
                f"error: unsupported boot mechanism `{boot_mechanism}'", file=sys.stderr
            )


if __name__ == "__main__":
    main()