# ruff: noqa
# Rule test fixture for prm-tarfile-extract-without-filter (semgrep --test). Not shipped code.
import tarfile


def unpack(path, dest, member):
    with tarfile.open(path) as tar:
        # ruleid: prm-tarfile-extract-without-filter
        tar.extractall(dest)
        # ruleid: prm-tarfile-extract-without-filter
        tar.extract(member, dest)
        # ruleid: prm-tarfile-extract-without-filter
        tar.extractall(dest, filter="fully_trusted")
        # ok: prm-tarfile-extract-without-filter
        tar.extractall(dest, filter="data")
        # ok: prm-tarfile-extract-without-filter
        tar.extract(member, dest, filter=tarfile.data_filter)

    archive = tarfile.open(path, "r:gz")
    # ruleid: prm-tarfile-extract-without-filter
    archive.extractall(path=dest)
    # ok: prm-tarfile-extract-without-filter
    archive.extractall(path=dest, filter="data")
    archive.close()
