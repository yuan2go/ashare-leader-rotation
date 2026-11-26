"""Run an allowlisted job in a separate, lock-holding Python process."""
import os
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from threading import Thread
from uuid import UUID, uuid4

from research.pit import forward_session as fs

from ..config import ROOT, Settings
from ..schemas import Action, Job, JobRequest


class Busy(RuntimeError):
    pass


def now():
    return datetime.now(timezone.utc)


@contextmanager
def lock(path, *, wait=False):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open("a+b") as file:
        if not file.seek(0,os.SEEK_END):
            file.write(b"\0")
            file.flush()
        file.seek(0)
        if os.name=="nt":
            import msvcrt
            try:
                msvcrt.locking(file.fileno(),msvcrt.LK_LOCK if wait else msvcrt.LK_NBLCK,1)
            except OSError as exc:
                raise Busy("another shadow job holds the lock") from exc
        else:
            import fcntl
            try:
                fcntl.flock(file,fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise Busy("another shadow job holds the lock") from exc
        yield


def jobs_dir(settings):
    return settings.ops_lock.parent/"jobs"


def queue_lock(settings):
    return settings.ops_lock.with_suffix(".queue.lock")


def job_file(settings, job_id):
    return jobs_dir(settings)/f"{job_id}.json"


def save_job(settings, job):
    fs.write(job_file(settings,job.id),job.model_dump(mode="json"))
    return job


def get_job(settings, job_id):
    return Job.model_validate(fs.load(job_file(settings,job_id)))


def _jobs(settings):
    return sorted((Job.model_validate(fs.load(file)) for file in jobs_dir(settings).glob("*.json")),
                  key=lambda job: job.created_at,reverse=True)


def reconcile(settings):
    """The OS releases the execution lock if the worker is terminated."""
    jobs = _jobs(settings)
    if not any(job.state in ("queued","running") for job in jobs):
        return jobs
    try:
        with lock(settings.ops_lock):
            for job in jobs:
                if job.state=="running" or job.state=="queued" and now()-job.created_at>timedelta(seconds=60):
                    job.state = "interrupted"
                    job.finished_at = now()
                    save_job(settings,job)
    except Busy:
        pass
    return jobs


def collection(settings):
    found = {}
    for profile,root in (("v5a",settings.reports_dir/"phase5a_multi_shadow"),("v7a",settings.shadow_dir)):
        for path in sorted((root/"collection_status").glob("*.json")):
            value = fs.load(path)
            found[value["day"]] = dict(value,source_profile=profile,market_http_requests=value["http_requests"])
    return [found[day] for day in sorted(found,reverse=True)]


def submit(settings, request):
    with lock(queue_lock(settings)):
        if any(job.state in ("queued","running") for job in reconcile(settings)):
            raise Busy("another shadow job is queued or running")
        with lock(settings.ops_lock):
            job = save_job(settings,Job(id=uuid4(),request=request,state="queued",created_at=now()))
        environment = {
            **os.environ,"PHASE7A_DIR":str(settings.shadow_dir),"PHASE7A_OPS_LOCK":str(settings.ops_lock),
            "PYTHONUTF8":"1","PYTHONIOENCODING":"utf-8","PYTHONUNBUFFERED":"1",
        }
        try:
            with job_file(settings,job.id).with_suffix(".log").open("ab") as logfile:
                proc = subprocess.Popen(
                    [sys.executable,"-B","-X","utf8","-m","backend.app.services.ops_service",str(job.id)],
                    cwd=ROOT,env=environment,stdin=subprocess.DEVNULL,stdout=logfile,stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0,
                    start_new_session=os.name!="nt",
                )
            Thread(target=proc.wait,daemon=True).start()
        except OSError:
            job.state,job.finished_at,job.exit_code = "failed",now(),1
            save_job(settings,job)
            raise
    return job


def read_log(settings, job_id, max_bytes):
    get_job(settings,job_id)
    path = job_file(settings,job_id).with_suffix(".log")
    if not path.is_file():
        return dict(id=job_id,text="",truncated=False)
    with path.open("rb") as file:
        size = file.seek(0,os.SEEK_END)
        file.seek(max(0,size-max_bytes))
        return dict(id=job_id,text=file.read(max_bytes).decode("utf-8",errors="replace"),truncated=size>max_bytes)


def execute(request, *, export_v1=None):
    environment = {
        **os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
    }

    def run(script, args):
        return subprocess.run(
            [sys.executable, "-B", "-X", "utf8", str(ROOT/"scripts"/script), *args],
            cwd=ROOT, env=environment, stdin=subprocess.DEVNULL, check=False,
        ).returncode

    if request.action in (Action.DAILY,Action.COLLECT):
        args = ["--profile","v7a"]
        if request.day:
            args += ["--date",request.day.isoformat()]
        if export_v1:
            args += ["--export-phase3d-v1",export_v1]
        code = run("collect_forward_session.py",args)
        if code != 0:
            state = "PENDING_UPSTREAM_PUBLICATION" if code == 10 else "COLLECTION_ERROR"
            print(f"{state} (exit={code})", flush=True)
            return code
    if request.action in (Action.DAILY,Action.INGEST,Action.VERIFY):
        arg = "--verify-offline" if request.action==Action.VERIFY else "--ingest-ready"
        return run("run_phase7a_shadow.py",[arg])
    return 0


def work(settings, job_id):
    from contextlib import ExitStack
    with ExitStack() as execution:
        with lock(queue_lock(settings),wait=True):
            job = get_job(settings,job_id)
            if job.state!="queued":
                return
            execution.enter_context(lock(settings.ops_lock))
            job.state,job.started_at = "running",now()
            save_job(settings,job)
        code = 1
        try:
            print(f"{job.started_at.isoformat()} {job.request.action}",flush=True)
            code = execute(job.request)
        finally:
            job.state = "succeeded" if code==0 else "failed"
            job.exit_code,job.finished_at = code,now()
            save_job(settings,job)
        return code


def main():
    import argparse
    from core.integrity import require_runtime_guard

    require_runtime_guard()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_id",type=UUID,nargs="?")
    parser.add_argument("--action",choices=list(Action))
    parser.add_argument("--date")
    parser.add_argument("--export-phase3d-v1")
    args = parser.parse_args()
    settings = Settings.from_env()
    if args.job_id:
        return work(settings,args.job_id)
    else:
        request = JobRequest(action=args.action,day=args.date,
                             allow_market_http=args.action in (Action.DAILY,Action.COLLECT))
        with lock(queue_lock(settings)):
            if any(job.state in ("queued","running") for job in reconcile(settings)):
                raise Busy("another shadow job is queued or running")
            with lock(settings.ops_lock):
                # Hold both locks through a direct CLI invocation.
                return execute(request,export_v1=args.export_phase3d_v1)


if __name__=="__main__":
    raise SystemExit(main())
