"""Static contract checks for the release-delivered Ubuntu bootstrap.

The real acceptance gate is an untouched Ubuntu host. These tests keep dangerous
regressions (opaque piping, missing checksums, unstamped provenance, or a hidden
third install command) from reaching that host or a release.
"""

from pathlib import Path
import os
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "rc-repro-install"
WORKFLOW = ROOT / ".github/workflows/release-installer.yml"
README = ROOT / "README.md"


def test_installer_is_executable_valid_bash_with_a_read_only_help_path():
    assert INSTALLER.stat().st_mode & 0o111
    subprocess.run(["bash", "-n", str(INSTALLER)], check=True)
    result = subprocess.run(
        ["bash", str(INSTALLER), "--help"], capture_output=True, text=True, check=True)
    assert "Ubuntu 24.04 amd64" in result.stdout
    assert "does not create or delete a cluster" in result.stdout


@pytest.mark.parametrize(("kernel", "release", "message"), [
    ("Darwin", "23.6.0", "guided macOS bootstrap is not available"),
    ("MSYS_NT-10.0", "3.5.3", "guided Windows bootstrap is not available"),
    ("Linux", "5.15.153.1-microsoft-standard-WSL2", "Docker Desktop-backed WSL2"),
])
def test_installer_refuses_unsupported_desktop_paths_before_mutation(
        tmp_path, kernel, release, message):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    uname = fake_bin / "uname"
    uname.write_text(
        "#!/bin/sh\n"
        f"case \"$1\" in -s) echo {kernel} ;; -r) echo {release} ;; "
        "-m) echo x86_64 ;; *) echo x86_64 ;; esac\n",
        encoding="utf-8")
    uname.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"

    result = subprocess.run(
        ["bash", str(INSTALLER)], capture_output=True, text=True, env=env)

    assert result.returncode == 1
    assert message in result.stderr
    assert "Installing signed Ubuntu prerequisites" not in result.stdout


def test_installer_uses_auditable_sources_and_published_checksums():
    source = INSTALLER.read_text(encoding="utf-8")
    assert "curl | bash" not in source and "curl | sh" not in source
    assert "download.docker.com/linux/ubuntu" in source
    assert "Signed-By: /etc/apt/keyrings/docker.asc" in source
    for tool in ("kind-linux-amd64.sha256sum", "kubectl.sha256", ".sha256sum"):
        assert tool in source
    assert "sha256sum --check" in source
    assert "pipx install --force" in source
    assert "Next action:\\n  rc-repro onboard" in source
    assert "Docker Desktop-backed WSL2" in source


def test_installer_checks_existing_tools_and_conflicts_before_package_changes():
    source = INSTALLER.read_text(encoding="utf-8")
    inventory = source.index('for tool in docker pipx kind kubectl helm rc-repro')
    conflicts = source.index('for package in docker.io docker-compose')
    noninteractive_sudo = source.index('if ! sudo -n true 2>/dev/null; then')
    sudo_authorisation = source.index('    sudo -v || fail "sudo authorisation failed"')
    first_apt_change = source.index('sudo apt-get update')

    assert inventory < noninteractive_sudo
    assert conflicts < noninteractive_sudo < sudo_authorisation < first_apt_change
    assert 'install_user="$(id -un)"' in source
    assert "SUDO_USER" not in source


def test_unstamped_source_requires_explicit_contributor_provenance():
    source = INSTALLER.read_text(encoding="utf-8")
    assert '${RC_REPRO_INSTALL_REPOSITORY:-klovekesh37/rc-repro}' not in source
    assert '${RC_REPRO_INSTALL_TAG:-main}' not in source
    assert "set both RC_REPRO_INSTALL_REPOSITORY and RC_REPRO_INSTALL_TAG" in source


def test_readme_documents_the_two_command_human_entry_path_and_platform_boundary():
    readme = README.read_text(encoding="utf-8")
    installer_commands = (
        "curl -fsSLO https://github.com/klovekesh37/rc-repro/releases/latest/download/"
        "rc-repro-install\n"
        "bash rc-repro-install"
    )
    assert installer_commands in readme
    assert "Ubuntu 24.04 amd64" in readme
    assert all(platform in readme for platform in ("macOS", "Windows", "WSL"))
    assert "rc-repro onboard" in readme
    microservices = readme.split("## Kubernetes microservices preset", 1)[1]
    microservices = microservices.split("## Agent & JSON interface", 1)[0]
    assert "rc-repro onboard --accept-defaults" not in microservices
    assert "rc-repro up --preset microservices --version 8.6.1 " in microservices
    assert "--name first-repro --wait" in microservices


def test_release_workflow_stamps_immutable_provenance_and_uploads_both_assets():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    # PyYAML 1.1 reads the key `on` as boolean True; accept either representation.
    trigger = workflow.get("on", workflow.get(True))
    assert trigger == {"release": {"types": ["published"]}}
    assert workflow["permissions"] == {"contents": "write"}
    body = WORKFLOW.read_text(encoding="utf-8")
    assert "github.repository" in body and "github.event.release.tag_name" in body
    assert "bash -n dist/rc-repro-install" in body
    assert "sha256sum dist/rc-repro-install" in body
    assert "gh release upload" in body
    assert "--clobber" in body
    assert "dist/rc-repro-install.sha256" in body
