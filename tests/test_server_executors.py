"""Executors: SSH command construction and quoting (injected runner), local process helpers."""

from __future__ import annotations

import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

from helpers_server import FakeRunner, wait_for
from wmlab.server.executors import (
    CommandResult,
    ExecContext,
    JobRecord,
    LocalExecutor,
    SshExecutor,
    default_runner,
    make_executor,
    pid_alive,
    rsync_path,
    shell_path,
    terminate_group,
)
from wmlab.spec import HostSpec


def ssh_host(**changes) -> HostSpec:
    data = {"name": "scw", "kind": "ssh", "ssh_target": "root@51.15.0.1",
            "workdir": "/root/wm lab", "data_dir": "/data/wm'data",
            "price_eur_per_hour": 1.4}
    data.update(changes)
    return HostSpec.model_validate(data)


def make(tmp_path: Path, runner: FakeRunner, **changes) -> SshExecutor:
    ctx = ExecContext(workspace=tmp_path / "ws", data=tmp_path / "ws" / ".wmlab", runner=runner)
    (tmp_path / "ws" / ".wmlab").mkdir(parents=True)
    return SshExecutor(ssh_host(**changes), ctx)


def train_job(**changes) -> JobRecord:
    job = JobRecord(job_id="job-1", kind="train", host="scw",
                    target="projects/p/experiments/e 1.yaml", run_id="run-1",
                    args=["train", "projects/p/experiments/e 1.yaml", "--run-id", "run-1",
                          "--set", "loss.terms.img.weight=0.5; rm -rf /"],
                    launch_file="/tmp/launch.json")
    for key, value in changes.items():
        setattr(job, key, value)
    return job


def test_path_quoting():
    assert shell_path("/root/wm lab") == "'/root/wm lab'"
    assert shell_path("~/wmlab") == '"$HOME"/wmlab'
    assert shell_path("~") == '"$HOME"'
    assert rsync_path("~/wmlab") == "wmlab" and rsync_path("/abs") == "/abs"


def test_launch_command_is_quoted(tmp_path: Path):
    executor = make(tmp_path, FakeRunner())
    command = executor.launch_command(train_job(), "/data/wm'data/launch/job-1.json")
    assert command.startswith("cd '/root/wm lab' && { WMLAB_DATA='/data/wm'\"'\"'data' "
                              "WMLAB_HOST=scw WMLAB_PRICE_EUR_PER_HOUR=1.4 nohup setsid ")
    assert "'loss.terms.img.weight=0.5; rm -rf /'" in command
    assert "'projects/p/experiments/e 1.yaml'" in command
    assert command.endswith("2>&1 < /dev/null & echo $!; ps -o lstart= -p $! 2>/dev/null || true; }")
    # the whole command parses back into the expected words
    words = shlex.split(command)
    assert words[words.index("setsid") + 1:words.index("setsid") + 4] == [
        ".venv/bin/python", "-m", "wmlab.cli"]
    assert "loss.terms.img.weight=0.5; rm -rf /" in words
    assert words[words.index("--launch-file") + 1] == "/data/wm'data/launch/job-1.json"


def test_launch_sequence_and_pid(tmp_path: Path):
    runner = FakeRunner()
    executor = make(tmp_path, runner, workdir="~/wmlab", data_dir=None, python="/opt/py")
    launch_file = tmp_path / "launch.json"
    launch_file.write_text("{}", encoding="utf-8")
    result = executor.launch(train_job(launch_file=str(launch_file)))
    assert result.pid == 4242
    assert result.remote_log == "~/wmlab/.wmlab/runs/run-1/stdout.log"
    mkdir, launch = runner.ssh_commands()
    assert mkdir == ('mkdir -p "$HOME"/wmlab "$HOME"/wmlab/.wmlab/runs "$HOME"/wmlab/.wmlab/studies '
                     '"$HOME"/wmlab/.wmlab/launch "$HOME"/wmlab/.wmlab/runs/run-1')
    assert launch.startswith('cd "$HOME"/wmlab && { WMLAB_DATA="$HOME"/wmlab/.wmlab ')
    assert "nohup setsid /opt/py -m wmlab.cli train" in launch
    assert '--launch-file "$HOME"/wmlab/.wmlab/launch/job-1.json' in launch
    push, copy = runner.rsync_calls()
    assert push[-1] == "root@51.15.0.1:wmlab/"
    assert push[push.index("-e") + 1] == "ssh -o BatchMode=yes -o ConnectTimeout=10"
    assert copy[-2:] == [str(launch_file), "root@51.15.0.1:wmlab/.wmlab/launch/job-1.json"]


def test_launch_failure_raises(tmp_path: Path):
    class Failing(FakeRunner):
        def __call__(self, argv, *, timeout=None):
            if argv[0] == "rsync":
                return CommandResult(12, "", "rsync: connection unexpectedly closed")
            return super().__call__(argv, timeout=timeout)

    executor = make(tmp_path, Failing())
    with pytest.raises(RuntimeError, match="synchronisation du code"):
        executor.launch(train_job(launch_file=None))


def test_launch_without_pid_raises(tmp_path: Path):
    runner = FakeRunner()
    runner.pid = "pas-un-pid"  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="pid"):
        make(tmp_path, runner).launch(train_job(launch_file=None))


def test_remote_pid_reuse_guard(tmp_path: Path):
    """The remote start time (``ps -o lstart=``) is recorded at launch and compared on poll
    and cancel: a reused pid (host rebooted) is reported dead and never signalled."""
    class Runner(FakeRunner):
        lstart = "Tue Sep 30 10:00:00 2026"

        def __call__(self, argv, *, timeout=None):
            self.calls.append(list(argv))
            command = argv[-1]
            if argv[0] == "ssh" and "nohup setsid" in command:
                return CommandResult(0, f"{self.pid}\n  {self.lstart}\n")
            if argv[0] == "ssh" and command.startswith("kill -0"):
                return CommandResult(0, f"alive\n {self.lstart}\n" if self.alive else "dead\n")
            return CommandResult(0, "")

    runner = Runner()
    executor = make(tmp_path, runner, workdir="~/wmlab", data_dir=None)
    result = executor.launch(train_job(launch_file=None))
    assert result.pid == 4242 and result.remote_start == "Tue Sep 30 10:00:00 2026"
    job = train_job(pid=result.pid, remote_start=result.remote_start)
    assert executor.poll(job).alive is True
    runner.lstart = "Wed Oct  1 08:00:00 2026"  # same pid, another process
    state = executor.poll(job)
    assert state.alive is False and "réutilisé" in (state.detail or "")
    executor.cancel(job)
    assert not any(c[-1].startswith("kill -TERM") for c in runner.calls if c[0] == "ssh")
    runner.lstart = "Tue Sep 30 10:00:00 2026"
    executor.cancel(job)
    assert runner.ssh_commands()[-1] == "kill -TERM -4242 2>/dev/null || kill -TERM 4242"
    # jobs launched before this guard (no remote_start) keep working
    assert executor.poll(train_job(pid=4242)).alive is True


def test_poll_cancel_pull(tmp_path: Path):
    runner = FakeRunner()
    executor = make(tmp_path, runner)
    job = train_job(pid=77)
    assert executor.poll(job).alive is True
    runner.alive = False
    assert executor.poll(job).alive is False
    runner.fail_ssh = True
    assert executor.poll(job).alive is None
    runner.fail_ssh = False
    executor.cancel(job)
    assert runner.ssh_commands()[-1] == "kill -TERM -77 2>/dev/null || kill -TERM 77"
    executor.sync(job)
    pull = runner.rsync_calls()[-1]
    assert "--delete" not in pull and "checkpoints/*.pt" in pull
    # the remote path is quoted for the remote shell (rsync hands it to that shell)
    assert pull[-2] == "root@51.15.0.1:" + shell_path("/data/wm'data/runs/run-1") + "/"
    assert shlex.split(pull[-2].split(":", 1)[1]) == ["/data/wm'data/runs/run-1/"]
    local = tmp_path / "ws" / ".wmlab" / "runs" / "run-1"
    assert pull[-1] == f"{local}/" and local.is_dir()
    (local / "notes.md").write_text("mes notes", encoding="utf-8")
    executor.sync(job)
    assert "notes.md" in runner.rsync_calls()[-1]  # local notes are never overwritten


def test_pull_errors(tmp_path: Path):
    class Partial(FakeRunner):
        code = 23

        def __call__(self, argv, *, timeout=None):
            if argv[0] == "rsync":
                self.calls.append(list(argv))
                return CommandResult(self.code, "", "rsync error")
            return super().__call__(argv, timeout=timeout)

    runner = Partial()
    executor = make(tmp_path, runner)
    executor.sync(train_job())  # remote folder not created yet: tolerated
    runner.code = 255
    with pytest.raises(RuntimeError, match="synchronisation de runs/run-1"):
        executor.sync(train_job())


def test_hpo_sync_pulls_unfinished_trials(tmp_path: Path):
    runner = FakeRunner()
    executor = make(tmp_path, runner)
    data = tmp_path / "ws" / ".wmlab"
    study = data / "studies" / "st-1"
    study.mkdir(parents=True)
    (study / "trials.jsonl").write_text('{"number": 0, "run_id": "t0"}\n'
                                        '{"number": 1, "run_id": "t1"}\n', encoding="utf-8")
    (data / "runs" / "t0").mkdir(parents=True)
    (data / "runs" / "t0" / "run.json").write_text('{"status": "completed"}', encoding="utf-8")
    executor.sync(JobRecord(job_id="job-2", kind="hpo", host="scw", target="x",
                            study_id="st-1"))
    sources = [c[-2] for c in runner.rsync_calls()]
    assert sources == ["root@51.15.0.1:" + shell_path("/data/wm'data/studies/st-1") + "/",
                       "root@51.15.0.1:" + shell_path("/data/wm'data/runs/t1") + "/"]


def test_setup_and_test(tmp_path: Path):
    runner = FakeRunner()
    executor = make(tmp_path, runner)
    assert executor.setup(JobRecord(job_id="j", kind="setup", host="scw", target="scw")).ok
    assert runner.ssh_commands()[-1] == (
        "cd '/root/wm lab' && bash scripts/remote_bootstrap.sh '/data/wm'\"'\"'data'")
    result = executor.test()
    assert result["ok"] is True and "L40S" in result["output"]
    assert runner.ssh_commands()[-1].endswith("df -h '/root/wm lab' | tail -1")


def test_make_executor_and_default_runner(tmp_path: Path):
    ctx = ExecContext(workspace=tmp_path, data=tmp_path / ".wmlab")
    local = HostSpec.model_validate({"name": "local", "kind": "local"})
    assert isinstance(make_executor(local, ctx), LocalExecutor)
    assert isinstance(make_executor(ssh_host(), ctx), SshExecutor)
    assert ctx.local_cli_prefix()[-2:] == ["-m", "wmlab.cli"]
    ok = default_runner([sys.executable, "-c", "print('bonjour')"])
    assert ok.ok and ok.stdout.strip() == "bonjour"
    missing = default_runner(["commande-qui-n-existe-pas-42"])
    assert missing.returncode == 127 and "introuvable" in missing.stderr
    slow = default_runner([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.2)
    assert slow.returncode == 124


def test_local_process_helpers(tmp_path: Path):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                            start_new_session=True)
    try:
        assert pid_alive(proc.pid)
        assert not pid_alive(proc.pid, proc_start=time.time() - 3600)  # pid reuse guard
        terminate_group(proc.pid)
        assert wait_for(lambda: not pid_alive(proc.pid), timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)


def test_local_executor_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    host = HostSpec.model_validate({"name": "mac", "kind": "local", "price_eur_per_hour": 0.25})
    ctx = ExecContext(workspace=tmp_path, data=tmp_path / "data",
                      cli_prefix=[sys.executable, "-c",
                                  "import os, sys; print(os.environ['WMLAB_HOST'], "
                                  "os.environ['WMLAB_PRICE_EUR_PER_HOUR'], "
                                  "os.environ['WMLAB_DATA'], sys.argv[1:])"])
    executor = LocalExecutor(host, ctx)
    job = JobRecord(job_id="job-9", kind="eval", host="mac", target="x", run_id="r",
                    args=["eval", "--run", "r"])
    result = executor.launch(job)
    assert wait_for(lambda: executor.poll(job, result.handle).alive is False, timeout=10)
    assert executor.poll(job, result.handle).exit_code == 0
    output = Path(result.log_path).read_text(encoding="utf-8")
    assert f"mac 0.25 {tmp_path / 'data'} ['eval', '--run', 'r']" in output
    assert Path(result.log_path) == tmp_path / "data" / "jobs" / "logs" / "job-9.log"
