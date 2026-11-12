import argparse
import shutil
import subprocess
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path


def prepare(directory):
    directory.mkdir(parents=True, exist_ok=True)
    metadata = directory / "repository.xml"
    urllib.request.urlretrieve("https://dl.google.com/android/repository/repository2-1.xml", metadata)
    document = ET.parse(metadata)
    package = next(item for item in document.getroot() if item.tag.endswith("remotePackage")
                   and item.attrib.get("path") == "cmdline-tools;latest")
    archives = next(item for item in package if item.tag.endswith("archives"))
    archive = next(item for item in archives if item.find("host-os").text == "macosx")
    relative = archive.find("complete/url").text
    bundle = directory / "commandline-tools.zip"
    urllib.request.urlretrieve("https://dl.google.com/android/repository/" + relative, bundle)
    sdk = directory / "android-sdk"
    staging = directory / "commandline-tools"
    with zipfile.ZipFile(bundle) as zipped:
        zipped.extractall(staging)
    target = sdk / "cmdline-tools/latest"
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staging / "cmdline-tools"), target)
    for executable in (target / "bin").iterdir():
        executable.chmod(0o755)
    manager = target / "bin/sdkmanager"
    subprocess.run([str(manager), "--sdk_root=" + str(sdk), "--licenses"], input="y\n" * 100, text=True, check=True)
    subprocess.run([str(manager), "--sdk_root=" + str(sdk), "platforms;android-36", "platforms;android-37.0",
                    "build-tools;36.0.0", "ndk;28.2.13676358", "cmake;3.22.1"], check=True)
    platform = sdk / "platforms/android-37"
    if not platform.exists():
        platform.symlink_to("android-37.0", target_is_directory=True)
    print(sdk)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    prepare(parser.parse_args().directory)
