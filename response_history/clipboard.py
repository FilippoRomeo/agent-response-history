import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class CopyResult:
    status: str


def copy_verified(payload: str, runner=None) -> CopyResult:
    runner = runner or subprocess.run
    data = payload.encode("utf-8")
    try:
        write = runner(["/usr/bin/pbcopy"], input=data, capture_output=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return CopyResult("write_failed")
    if write.returncode:
        return CopyResult("write_failed")
    try:
        read = runner(["/usr/bin/pbpaste"], capture_output=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return CopyResult("verification_unavailable")
    if read.returncode:
        return CopyResult("verification_unavailable")
    return CopyResult("verified" if read.stdout == data else "mismatch")
