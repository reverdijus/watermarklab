"""Job executors: ``local`` (subprocess on this Mac) and ``ssh`` (remote GPU servers)."""

from wmlab.spec import HostSpec
from wmlab.server.executors.base import (
    ACTIVE_STATUSES,
    API_FIELDS,
    FINAL_STATUSES,
    JOB_KINDS,
    CommandResult,
    ExecContext,
    Executor,
    JobRecord,
    LaunchResult,
    ProcessState,
    Runner,
    UNLIMITED_KINDS,
    default_runner,
    job_log_name,
)
from wmlab.server.executors.local import LocalExecutor, pid_alive, terminate_group
from wmlab.server.executors.ssh import SshExecutor, rsync_path, shell_path


def make_executor(host: HostSpec, ctx: ExecContext) -> Executor:
    """Return the executor matching ``host.kind``."""
    if host.kind == "ssh":
        return SshExecutor(host, ctx)
    return LocalExecutor(host, ctx)


__all__ = [
    "ACTIVE_STATUSES", "API_FIELDS", "FINAL_STATUSES", "JOB_KINDS", "CommandResult",
    "ExecContext", "Executor", "JobRecord", "LaunchResult", "LocalExecutor", "ProcessState",
    "Runner", "SshExecutor", "UNLIMITED_KINDS", "default_runner", "job_log_name", "make_executor", "pid_alive",
    "rsync_path", "shell_path", "terminate_group",
]
