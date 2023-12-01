#!/usr/bin/env python3
# coding: utf8

import filecmp
import tempfile
import textwrap
import yaml
import platform
import contextlib
import os
import sys
import shutil
import tarfile
import math
import gzip
import subprocess
import requests

from apt_repo import *
from debian.arfile import ArError
from debian.debfile import DebFile
from tqdm.auto import tqdm
from urllib.parse import urlparse
from typing import Optional, List, Union
from glob import glob
from io import SEEK_SET, FileIO

CACHE_DIR = os.path.join(os.path.abspath(os.getcwd()), ".cache")
ROOTFS_DIR = os.path.join(CACHE_DIR, "rootfs")
PKG_DIR = os.path.join(CACHE_DIR, "packages")
ESP_DIR = os.path.join(CACHE_DIR, "esp")
INITRD_FILENAME = os.path.join(CACHE_DIR, "initrd.img")
ESP_FILENAME = os.path.join(CACHE_DIR, "esp.img")

EFI_ARCH_SUFFIXES = {
    "amd64": "x64",
    "x86_64": "x64",
    "arm64": "aa64",
    "aarch64": "aa64"
}

def parse_config(filename: str) -> dict:
    with open(filename) as f:
        return yaml.load(f, Loader=yaml.FullLoader)
    

def resolve_base(base: str, arch: str) -> str:
    try:
        url = urlparse(base)
        if url.scheme == "":
            raise Exception()
        return base
    except:
        return f"https://cdimage.ubuntu.com/ubuntu-base/releases/{base}/release/ubuntu-base-{base}-base-{arch}.tar.gz"

def resolve_deb(deb: str, repos: Optional[List[APTRepository]] = None, arch: Optional[str] = None) -> str:
    try:
        url = urlparse(deb)
        if url.scheme == "":
            raise Exception()
        return deb
    except:
        if repos is None:
            print(
                f"error: apt configuration required for resolving package `{deb}'", file=sys.stderr
            )
            sys.exit(1)
        
        print(f"resolving package {deb}:{arch}", end="\033[K\r")
        packages = []
        for repo in repos:
            packages = []
            if len(repo.components) == 0:
                packages.extend(repo.get_binary_packages_by_component(None, arch))
            for component in repo.components:
                packages.extend(repo.get_binary_packages_by_component(component, arch))

            selected = None
            for package in packages:
                if package.package == deb:
                    selected = package
                    
            if selected is None:
                continue
                
            print(f"selected {deb}-{selected.version}", end="\033[K\r")
            return repo.url + "/" + selected.filename
        print(
            f"error: cannot find package `{deb}'", file=sys.stderr
        )
        sys.exit(1)


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

        for member in tqdm(members, desc="extracting base"):
            tgz.extract(member, ROOTFS_DIR)

        os.close(os.open(extract_indicator, os.O_CREAT))

    return True

def run_depmod() -> None:
    with tqdm([], desc="running depmod"):
        ksymtab = glob(ROOTFS_DIR + "/boot/System.map*")[0]
        modules_path = glob(ROOTFS_DIR + "/lib/modules/*")[0]
        force_version = os.path.basename(modules_path)
        subprocess.run(["depmod", "-F", ksymtab, "-b", ROOTFS_DIR, force_version])


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

    overlay = os.path.abspath(overlay)
    dcmp = filecmp.dircmp(ROOTFS_DIR, overlay)

    print(f"copying overlay {overlay}")
    shutil.copytree(overlay, ROOTFS_DIR, dirs_exist_ok=True)
    return True

def pack_initramfs():
    pack_lock = os.path.join(ROOTFS_DIR, ".installed_pkgs", ".initramfs")
    os.close(os.open(pack_lock, os.O_CREAT))
    subprocess.run(
        [
            "/bin/sh",
            "-c",
            f"find . | (cpio -o -H newc --owner 0:0) | pv -N 'packing initramfs' > {INITRD_FILENAME}",
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
    efi_arch_suffix = EFI_ARCH_SUFFIXES[arch]
    osrel_filename = os.path.join(ROOTFS_DIR, "etc/os-release")
    linux_stub_filename = os.path.join(
        ROOTFS_DIR, f"lib/systemd/boot/efi/linux{efi_arch_suffix}.efi.stub"
    )
    kernel_src_filename = os.path.join(ROOTFS_DIR, "./" + kernel_filename)
    kernel_dst_filename = os.path.join(
        ESP_DIR, "efi", "boot", f"boot{efi_arch_suffix}.efi"
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
    efi_arch_suffix = EFI_ARCH_SUFFIXES[arch]
    file_list = {
        f"usr/lib/grub/{arch}-efi/monolithic/grub{efi_arch_suffix}.efi": f"efi/boot/boot{efi_arch_suffix}.efi",
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


def build_esp_efibootguard(arch: str, kernel_filename: str, common_cmdline: str, image_config: dict):
    if arch != "arm64":
        print(f"unimplemented architecture \"{arch}\" with EFI boot guard", file=sys.stderr)
        sys.exit(1)

    if not os.path.isfile("efibootguard/efibootguardaa64.efi"):
        if platform.machine() != "aarch64":
            print("cross-compiling EFI boot guard is not supported", file=sys.stderr)
            print("manually cross-compile EFI boot guard for AArch64 and run this command again", file=sys.stderr)
            sys.exit(1)

        print("configuring EFI boot guard")
        efibootguard_cwd = os.path.join(os.getcwd(), "efibootguard")
        subprocess.run(["autoreconf", "-fi"], check=True, cwd=efibootguard_cwd)
        subprocess.run(["/bin/sh", "-c", "./configure --disable-completion"], check=True, cwd=efibootguard_cwd)
        print("building EFI boot guard")
        subprocess.run(["make"], check=True, cwd=efibootguard_cwd)

    print("creating firmware image")
    with open(ESP_FILENAME, "wb") as f:
        f.truncate(int(image_config["size"]) * 1024 * 1024)
        f.flush()

    print("creating partitions")
    parted_cmdline = ["parted", "--script", ESP_FILENAME, "--"]

    num_slots = len(image_config["slots"])
    esp_size = 5 # 5% of 1GiB is 50MiB for the ESP
    free_space = 100.0 - esp_size # free space for every other partition
    config_partition_size = free_space / num_slots

    # create partition table and bootable ESP
    parted_cmdline += ["mklabel", "gpt", ""]
    parted_cmdline += ["mkpart", "fat32", "0%", f"{esp_size}%", ""]
    parted_cmdline += ["set", "1", "esp", "on", ""]

    # create config partitions
    for i in range(0, num_slots):
        partition_start = math.floor(esp_size + i * config_partition_size)
        partition_end = math.floor(partition_start + config_partition_size)
        parted_cmdline += ["mkpart", "fat16", f"{partition_start}%", f"{partition_end}%", ""]

    subprocess.run(
        parted_cmdline,
        check=True
    )

    print("mounting loop device... ", end="")
    loop_dev = subprocess.check_output(["losetup", "-f"]).decode("utf8").strip()
    print(loop_dev, end="")
    subprocess.call(["sudo", "-S", "losetup", "-Pf", ESP_FILENAME])
    print(" OK")

    try:
        mount_dir = tempfile.mkdtemp()

        print("creating ESP")
        subprocess.call(["sudo", "-S", "mkfs.fat", f"{loop_dev}p1"])
        subprocess.call(["sudo", "-S", "mount", f"{loop_dev}p1", mount_dir])
        try:
            subprocess.call(["sudo", "-S", "mkdir", "-p", os.path.join(mount_dir, "EFI", "BOOT")])
            subprocess.call(["sudo", "-S", "cp", os.path.join(os.getcwd(), "efibootguard/efibootguardaa64.efi"), os.path.join(mount_dir, "EFI", "BOOT", "BOOTAA64.EFI")])
        finally:
            subprocess.call(["sudo", "-S", "umount", mount_dir])

        for slot_num in range(1, 1 + num_slots):
            partition_dev = f"{loop_dev}p{1 + slot_num}"
            slot = image_config["slots"][slot_num - 1]
            slot_label = slot["label"]
            slot_timeout = slot["timeout"]
            slot_cmdline = common_cmdline + " " + slot["cmdline"]
            slot_cmdline += " initrd=\\initrd.gz"

            print(f"creating partition for slot {slot_num}: {partition_dev}")
            subprocess.call(["sudo", "-S", "mkfs.fat", "-F", "16", partition_dev])
            subprocess.call(["sudo", "-S", "mount", partition_dev, mount_dir])
            try:
                with tempfile.NamedTemporaryFile(delete=False) as label_out:
                    label_out.write(slot_label.encode("utf-16-le"))
                    label_out.flush()
                    subprocess.call(["sudo", "-S", "cp", label_out.name, os.path.join(mount_dir, "EFILABEL")])

                subprocess.call(["sudo", "-S", "./efibootguard/bg_setenv", "-f", mount_dir, "-r", str(slot_num), f"--kernel=C:{slot_label}:vmlinuz-linux", f"--args=\"{slot_cmdline}\"", f"--watchdog={slot_timeout}"])
                kernel_src_filename = os.path.join(ROOTFS_DIR, "./" + kernel_filename)
                with open_kernel(kernel_src_filename) as kernel_in:
                    with tempfile.NamedTemporaryFile(delete=False) as kernel_out:
                        # decompress kernel
                        shutil.copyfileobj(kernel_in, kernel_out)
                        kernel_out.flush()
                        print("copying initrd")
                        subprocess.call(["sudo", "-S", "cp", kernel_out.name, os.path.join(mount_dir, "vmlinuz-linux")])
                subprocess.call(["sudo", "-S", "cp", INITRD_FILENAME, os.path.join(mount_dir, "initrd.gz")])
            finally:
                subprocess.call(["sudo", "-S", "umount", mount_dir])
    finally:
        print("detaching loop device... ", end="")
        subprocess.call(["sudo", "-S", "losetup", "-D", ESP_FILENAME])
        print("OK")
    

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
        shutil.rmtree(CACHE_DIR, ignore_errors=True)
        return
    elif cmd == "clean-esp":
        shutil.rmtree(ESP_DIR, ignore_errors=True)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(ESP_FILENAME)
        return
    elif cmd == "clean-initramfs":
        shutil.rmtree(ROOTFS_DIR, ignore_errors=True)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(INITRD_FILENAME)
        return
    elif cmd == "clean-packages":
        shutil.rmtree(PKG_DIR, ignore_errors=True)
        return
    elif cmd is not None:
        print(f"error: unrecognized command `{cmd}'", file=sys.stderr)
        sys.exit(1)

    arch = config["arch"]
    base = config["base"]

    if "apt" in config:
        url = config["apt"]["url"]
        components = config["apt"]["components"]
        repos = [
            APTRepository(url, dist, components)
            for dist in config["apt"]["dists"]
        ]
    else:
        repos = None

    base_filename = fetch_file(resolve_base(base, arch))
    deb_filenames = [fetch_file(resolve_deb(deb, repos,  arch)) for deb in config["debs"]]

    has_changes = False
    has_changes |= extract_rootfs(base_filename)

    for filename in deb_filenames:
        has_changes |= install_package(filename)

    if "overlays" in config:
        for overlay in config["overlays"]:
            has_changes |= copy_overlay(overlay)

    if has_changes or is_initramfs_pack_incomplete():
        run_depmod()
        pack_initramfs()

    if not is_esp_created():
        boot_mechanism = config["boot"]["mechanism"]
        kernel, cmdline = config["boot"]["kernel"], config["boot"]["cmdline"]
        if boot_mechanism == "grub":
            build_esp_grub(arch, kernel, cmdline)
        elif boot_mechanism == "systemd-stub":
            build_esp_systemd_stub(arch, kernel, cmdline)
        elif boot_mechanism == "efibootguard":
            build_esp_efibootguard(arch, kernel, cmdline, config["image"])
        else:
            print(
                f"error: unsupported boot mechanism `{boot_mechanism}'", file=sys.stderr
            )


if __name__ == "__main__":
    main()
