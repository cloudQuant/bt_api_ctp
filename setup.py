from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
from typing import Iterable

from setuptools import Extension, setup
from setuptools.command.build_ext import build_ext


ROOT = pathlib.Path(__file__).parent.resolve()
PACKAGE_DIR = ROOT / "src" / "bt_api_ctp"
CTP_DIR = PACKAGE_DIR / "ctp"
API_VERSION = "6.7.7"
API_DIR = CTP_DIR / "api" / API_VERSION
WRAPPER = CTP_DIR / "ctp_wrap.cpp"


def _platform_name() -> str:
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "win32":
        return "windows"
    raise RuntimeError(f"Unsupported platform for bt_api_ctp wheel build: {sys.platform}")


def _platform_api_dir() -> pathlib.Path:
    path = API_DIR / _platform_name()
    if not path.exists():
        raise RuntimeError(f"CTP API runtime files are missing: {path}")
    return path


def _copy_file(src: pathlib.Path, dst: pathlib.Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _copy_tree(src: pathlib.Path, dst: pathlib.Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, symlinks=True)


def _runtime_paths(platform_dir: pathlib.Path) -> list[pathlib.Path]:
    if sys.platform == "darwin":
        return sorted(platform_dir.glob("*.framework"))
    if sys.platform.startswith("linux"):
        return sorted(platform_dir.glob("libthost*_se.so"))
    if sys.platform == "win32":
        return sorted(platform_dir.glob("thost*_se.dll"))
    return []


def _mac_framework_binary(framework: pathlib.Path) -> pathlib.Path:
    binary = framework / "Versions" / "A" / framework.stem
    if not binary.exists():
        raise RuntimeError(f"Framework binary is missing: {binary}")
    return binary


def _mac_sdk_root() -> pathlib.Path:
    sdk_root = os.environ.get("SDKROOT")
    if not sdk_root:
        try:
            sdk_root = subprocess.check_output(
                ["xcrun", "--show-sdk-path"],
                text=True,
            ).strip()
        except (OSError, subprocess.CalledProcessError) as exc:
            raise RuntimeError("Unable to locate the macOS SDK") from exc
    return pathlib.Path(sdk_root)


def _mac_libiconv_stub() -> pathlib.Path:
    sdk_root = _mac_sdk_root()

    for name in ("libiconv.tbd", "libiconv.2.tbd"):
        candidate = sdk_root / "usr" / "lib" / name
        if candidate.exists():
            return candidate
    raise RuntimeError(f"Unable to locate libiconv.tbd in macOS SDK: {sdk_root}")


def _mac_include_dir() -> pathlib.Path:
    include_dir = _mac_sdk_root() / "usr" / "include"
    if not include_dir.exists():
        raise RuntimeError(f"Unable to locate macOS SDK include dir: {include_dir}")
    return include_dir


def _extension_kwargs(platform_dir: pathlib.Path) -> dict[str, object]:
    include_dirs: list[str] = [str(platform_dir)]
    library_dirs: list[str] = []
    libraries: list[str] = []
    extra_compile_args: list[str] = []
    extra_link_args: list[str] = []

    if sys.platform == "darwin":
        frameworks = _runtime_paths(platform_dir)
        include_dirs.insert(0, str(_mac_include_dir()))
        include_dirs.extend(str(framework / "Versions" / "A" / "Headers") for framework in frameworks)
        extra_compile_args.extend(["-std=c++11"])
        extra_link_args.extend(["-Wl,-rpath,@loader_path"])
        extra_link_args.extend(str(_mac_framework_binary(framework)) for framework in frameworks)
        extra_link_args.append(str(_mac_libiconv_stub()))
    elif sys.platform.startswith("linux"):
        library_dirs.append(str(platform_dir))
        libraries.extend(["thostmduserapi_se", "thosttraderapi_se"])
        extra_compile_args.extend(["-std=c++11"])
        extra_link_args.extend(["-Wl,-rpath,$ORIGIN"])
    elif sys.platform == "win32":
        library_dirs.append(str(platform_dir))
        libraries.extend(["thostmduserapi_se", "thosttraderapi_se"])
        extra_compile_args.extend(["/std:c++14", "/utf-8"])

    return {
        "include_dirs": include_dirs,
        "library_dirs": library_dirs,
        "libraries": libraries,
        "extra_compile_args": extra_compile_args,
        "extra_link_args": extra_link_args,
    }


class BuildExt(build_ext):
    def run(self) -> None:
        self._validate_inputs()
        super().run()
        self._copy_runtime_libraries()

    def _validate_inputs(self) -> None:
        missing = [path for path in [WRAPPER, *_runtime_paths(_platform_api_dir())] if not path.exists()]
        if missing:
            formatted = "\n".join(f"  - {path}" for path in missing)
            raise RuntimeError(f"Required CTP build inputs are missing:\n{formatted}")

    def _copy_runtime_libraries(self) -> None:
        output_dir = pathlib.Path(self.get_ext_fullpath("bt_api_ctp.ctp._ctp")).parent
        for runtime_path in _runtime_paths(_platform_api_dir()):
            destination = output_dir / runtime_path.name
            if runtime_path.is_dir():
                _copy_tree(runtime_path, destination)
            else:
                _copy_file(runtime_path, destination)


def _package_data() -> dict[str, list[str]]:
    return {"bt_api_ctp": ["configs/*.yaml"]}


def _extensions() -> Iterable[Extension]:
    platform_dir = _platform_api_dir()
    return [
        Extension(
            "bt_api_ctp.ctp._ctp",
            sources=[str(WRAPPER)],
            language="c++",
            **_extension_kwargs(platform_dir),
        )
    ]


setup(
    cmdclass={"build_ext": BuildExt},
    ext_modules=list(_extensions()),
    include_package_data=False,
    package_data=_package_data(),
)
