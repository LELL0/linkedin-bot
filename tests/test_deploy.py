"""The server image and compose file stay in step with the code.

CI never builds the Docker image, and kumite-dev builds it from an rsync of the repo, so a
mismatch would first show up as a broken deploy. These checks read the files only.
"""

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = (ROOT / "deploy/Dockerfile").read_text()
COMPOSE = (ROOT / "deploy/docker-compose.yml").read_text()
ENTRYPOINT = ROOT / "deploy/entrypoint.sh"
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())
LOCK = tomllib.loads((ROOT / "uv.lock").read_text())

# Import name -> distribution name, where the two differ.
DISTRIBUTION = {"dotenv": "python-dotenv"}
# Python version of each Ubuntu release the Playwright image is built on.
UBUNTU_PYTHON = {"jammy": (3, 10), "noble": (3, 12)}


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def requirement_name(spec: str) -> str:
    return normalize(re.match(r"[A-Za-z0-9_.-]+", spec).group())


def base_image() -> tuple[str, str]:
    """(Playwright version, Ubuntu release) of the FROM line."""
    match = re.search(r"^FROM mcr\.microsoft\.com/playwright/python:v([\d.]+)-(\w+)$",
                      DOCKERFILE, re.MULTILINE)
    assert match, "deploy/Dockerfile no longer starts from the Playwright Python image"
    return match.group(1), match.group(2)


def image_requirements() -> dict[str, str]:
    """Normalized name -> requirement, from the image's pip install."""
    command = re.search(r"pip install((?:[^\n\\]|\\\n)*)", DOCKERFILE)  # Up to the last \ line.
    assert command, "deploy/Dockerfile has no pip install"
    specs = re.findall(r'"([^"]+)"', command.group(1))
    return {requirement_name(spec): spec for spec in specs}


def locked_version(name: str) -> str:
    for package in LOCK["package"]:
        if normalize(package["name"]) == normalize(name):
            return package["version"]
    raise AssertionError(f"{name} is not in uv.lock")


def test_image_runs_the_locked_playwright():
    # The image's Chromium is built for its Playwright version. A pip pin that moves without
    # the FROM tag leaves Playwright looking for a browser that is not there.
    image_version, _ = base_image()
    assert image_requirements()["playwright"] == f"playwright=={image_version}"
    # The tests run against uv.lock, so the server must run the same version.
    assert locked_version("playwright") == image_version, (
        "uv.lock and deploy/Dockerfile disagree on Playwright: change the FROM tag and the "
        "pip pin together with uv.lock"
    )


def test_image_installs_every_dependency():
    declared = {requirement_name(spec) for spec in PYPROJECT["project"]["dependencies"]}
    assert set(image_requirements()) == declared


def test_image_python_meets_requires_python():
    _, release = base_image()
    assert release in UBUNTU_PYTHON, f"unknown Ubuntu release {release}: add its Python"
    minimum = re.fullmatch(r">=(\d+)\.(\d+)", PYPROJECT["project"]["requires-python"])
    assert UBUNTU_PYTHON[release] >= (int(minimum.group(1)), int(minimum.group(2)))


def test_every_third_party_import_is_installed_in_the_image():
    local = {path.stem for path in (ROOT / "bot").glob("*.py")} | {"bot"}
    installed = set(image_requirements())
    missing = set()
    for path in sorted((ROOT / "bot").glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(), str(path))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module]
            else:
                continue
            for name in names:
                top = name.split(".")[0]
                if top in sys.stdlib_module_names or top in local:
                    continue
                if normalize(DISTRIBUTION.get(top, top)) not in installed:
                    missing.add(f"{path.name}: {top}")
    assert not missing, f"imported but not installed in deploy/Dockerfile: {sorted(missing)}"


def test_copied_sources_exist_and_reach_the_build():
    ignored = {line.strip() for line in (ROOT / ".dockerignore").read_text().splitlines()}
    for source in re.findall(r"^COPY (\S+) \S+$", DOCKERFILE, re.MULTILINE):
        assert (ROOT / source).exists(), f"COPY {source}: not in the repository"
        assert source not in ignored, f"COPY {source}: excluded by .dockerignore"


def test_build_context_keeps_secrets_and_data_out():
    ignored = {line.strip() for line in (ROOT / ".dockerignore").read_text().splitlines()}
    assert {".env", ".env.*", "deploy/.env", "data", "deploy/data", ".git"} <= ignored


def test_entrypoint_is_executable_and_valid_shell():
    assert os.access(ENTRYPOINT, os.X_OK), "deploy/entrypoint.sh lost its executable bit"
    assert ENTRYPOINT.read_text().rstrip().endswith('exec "$@"')
    if shutil.which("sh") is None:
        pytest.skip("no sh to check the syntax")
    subprocess.run(["sh", "-n", str(ENTRYPOINT)], check=True)


def test_container_commands_exist():
    known = set(re.findall(r'command == "(\w+)"', (ROOT / "bot/__main__.py").read_text()))
    used = re.findall(r'"python3", "-m", "bot", "(\w+)"', DOCKERFILE)
    assert used, "deploy/Dockerfile no longer runs python3 -m bot"
    assert set(used) <= known


def published_ports() -> list[str]:
    """Entries of the ports: list in docker-compose.yml."""
    ports, inside = [], False
    for line in COMPOSE.splitlines():
        stripped = line.strip()
        if stripped == "ports:":
            inside = True
        elif inside and stripped.startswith("- "):
            ports.append(stripped[2:].strip().strip("\"'"))
        elif inside and stripped and not stripped.startswith("#"):
            inside = False
    return ports


def test_compose_keeps_vnc_on_loopback_and_hardening_on():
    ports = published_ports()
    assert ports, "no published ports found in deploy/docker-compose.yml"
    assert all(port.startswith("127.0.0.1:") for port in ports), ports
    assert "no-new-privileges:true" in COMPOSE
    assert "cap_drop: [ALL]" in COMPOSE


def test_seccomp_profile_is_docker_default_plus_the_sandbox():
    profile = json.loads((ROOT / "deploy" / re.search(r"seccomp=(\S+)", COMPOSE).group(1))
                         .read_text())
    assert profile["defaultAction"] == "SCMP_ACT_ERRNO"
    # Chromium's sandbox needs these allowed without conditions on top of Docker's default.
    unconditional = {
        name
        for rule in profile["syscalls"]
        if rule["action"] == "SCMP_ACT_ALLOW" and not {"args", "includes", "excludes"} & set(rule)
        for name in rule["names"]
    }
    assert {"chroot", "clone", "clone3", "unshare", "setns"} <= unconditional
