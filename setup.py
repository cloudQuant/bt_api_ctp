from __future__ import annotations

import hashlib
import os
import pathlib
import platform
import shutil
import subprocess
import sys
from collections.abc import Iterable

from setuptools import Extension, setup
from setuptools.command.build_ext import build_ext

ROOT = pathlib.Path(__file__).parent.resolve()
PACKAGE_DIR = ROOT / "src" / "bt_api_ctp"
CTP_DIR = PACKAGE_DIR / "ctp"
API_VERSION = "6.7.7"
API_DIR = CTP_DIR / "api" / API_VERSION
WRAPPER = CTP_DIR / "ctp_wrap.cpp"

# CTP's macOS arm64 6.7.7 framework has a private four-argument
# ``ReqUserLogin`` implementation while its shipped header declares two
# arguments.  The shim is deliberately compiled only for this audited bundle.
_AUDITED_DARWIN_ARM64_TRADER_SHA256 = (
    "e22611e2b844c0eeefe1df85f9c7008d6931c37c13ef10f68db73efb5e0d4be7"
)


def _platform_name() -> str:
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "win32":
        return "windows"
    raise RuntimeError(
        f"Unsupported platform for bt_api_ctp wheel build: {sys.platform}"
    )


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


def _sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _darwin_build_architectures() -> set[str]:
    if sys.platform != "darwin":
        return set()
    flags = os.environ.get("ARCHFLAGS", "").split()
    requested = {
        flags[index + 1].lower()
        for index, flag in enumerate(flags[:-1])
        if flag == "-arch"
    }
    return requested or {platform.machine().lower()}


def _is_darwin_arm64_build() -> bool:
    return _darwin_build_architectures() in ({"arm64"}, {"aarch64"})


def _validate_darwin_arm64_trader_framework(platform_dir: pathlib.Path) -> None:
    architectures = _darwin_build_architectures()
    if {"arm64", "aarch64"} & architectures and len(architectures) != 1:
        raise RuntimeError(
            "Refusing multi-architecture Darwin build for the audited "
            "ReqUserLogin ABI shim."
        )
    if not _is_darwin_arm64_build():
        return
    binary = _mac_framework_binary(platform_dir / "thosttraderapi_se.framework")
    if _sha256_file(binary) != _AUDITED_DARWIN_ARM64_TRADER_SHA256:
        raise RuntimeError(
            "Refusing unverified Darwin arm64 Trader framework for the "
            "ReqUserLogin ABI shim."
        )


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
    define_macros: list[tuple[str, str]] = []

    if sys.platform == "darwin":
        frameworks = _runtime_paths(platform_dir)
        include_dirs.insert(0, str(_mac_include_dir()))
        include_dirs.extend(
            str(framework / "Versions" / "A" / "Headers") for framework in frameworks
        )
        extra_compile_args.extend(["-std=c++11"])
        extra_link_args.extend(["-Wl,-rpath,@loader_path"])
        extra_link_args.extend(
            str(_mac_framework_binary(framework)) for framework in frameworks
        )
        extra_link_args.append(str(_mac_libiconv_stub()))
        if _is_darwin_arm64_build():
            define_macros.append(("BT_API_CTP_DARWIN_ARM64_AUDITED_TRADER_ABI", "1"))
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
        "define_macros": define_macros,
    }


class BuildExt(build_ext):
    def run(self) -> None:
        self._validate_inputs()
        super().run()
        self._copy_runtime_libraries()

    def _validate_inputs(self) -> None:
        missing = [
            path
            for path in [
                WRAPPER,
                *_runtime_paths(_platform_api_dir()),
            ]
            if not path.exists()
        ]
        if missing:
            formatted = "\n".join(f"  - {path}" for path in missing)
            raise RuntimeError(f"Required CTP build inputs are missing:\n{formatted}")
        _validate_darwin_arm64_trader_framework(_platform_api_dir())

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


def _reuse_configured_msvc_environment() -> None:
    """Trust the MSVC environment of a Visual Studio developer prompt.

    distutils discovers the compiler by running
    ``cmd /u /c "vcvarsall.bat" <plat_spec> && set`` and aborts with
    ``Error executing cmd /u /c ...vcvarsall.bat...`` when that nested cmd exits
    non-zero -- which is what happens on machines where cmd.exe starts with a
    hook such as conda's auto-activation.  A developer prompt already exports the
    same variables, so tell distutils to use them instead of re-running vcvarsall.
    """
    if sys.platform != "win32" or os.environ.get("DISTUTILS_USE_SDK"):
        return
    if os.environ.get("VCINSTALLDIR") and os.environ.get("VSCMD_VER"):
        os.environ["DISTUTILS_USE_SDK"] = "1"
        os.environ["MSSdk"] = "1"


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


_reuse_configured_msvc_environment()

setup(
    cmdclass={"build_ext": BuildExt},
    ext_modules=list(_extensions()),
    include_package_data=False,
    package_data=_package_data(),
)
