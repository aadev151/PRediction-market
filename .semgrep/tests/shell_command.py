# ruff: noqa
# Rule test fixture for prm-no-shell-command (semgrep --test). Not shipped code.
import asyncio
import os
import subprocess


async def run(cmd, argv, enabled):
    # ruleid: prm-no-shell-command
    subprocess.run(cmd, shell=True)
    # ruleid: prm-no-shell-command
    subprocess.check_output(cmd, text=True, shell=True)
    # ruleid: prm-no-shell-command
    subprocess.Popen(cmd, shell=enabled)
    # ruleid: prm-no-shell-command
    os.system(cmd)
    # ruleid: prm-no-shell-command
    os.popen(cmd)
    # ruleid: prm-no-shell-command
    await asyncio.create_subprocess_shell(cmd)
    # ok: prm-no-shell-command
    subprocess.run(argv, shell=False, check=True)
    # ok: prm-no-shell-command
    subprocess.run(argv, check=True, timeout=30)
