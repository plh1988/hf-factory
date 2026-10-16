"""Job executor port: runs a sharded data-engine stage (s4, s5) as N tasks.

local          tasks run in this process, one after another, each retried up to max_retries
cloud_run_job  one Cloud Run Job execution with task_count = N; every task runs `factory task` in the same image,
               reads the run spec from the BlobStore, generates its slice (CLOUD_RUN_TASK_INDEX) and writes its
               partial counts and checksums back to the BlobStore, where they are merged.

Re-running a task is safe in both: rows are upserted on deterministic ids, and partial results are per task.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from factory.config import AdapterConfig

log = logging.getLogger("factory.jobs")


@dataclass
class TaskResult:
    index: int
    counts: dict[str, int] = field(default_factory=dict)
    checksums: dict[str, int] = field(default_factory=dict)
    attempts: int = 1


class TaskFailed(RuntimeError):
    pass


def task_key(stage: str, index: int) -> str:
    return f"runs/{stage}/tasks/{index:04d}.json"


class LocalExecutor:
    """In-process tasks. Profile keys: max_retries (default 2)."""

    def __init__(self, cfg: AdapterConfig, namespace: str | None = None) -> None:
        self.max_retries = int(cfg.get("max_retries", 2))

    def run(self, stage: str, task_count: int, run_task: Callable[[int], TaskResult], blobs: Any, args: list[str]) -> list[TaskResult]:
        results = []
        for i in range(task_count):
            for attempt in range(1, self.max_retries + 2):
                try:
                    res = run_task(i)
                    res.attempts = attempt
                    results.append(res)
                    break
                except Exception as exc:  # retry the shard; upserts make a re-run safe
                    if attempt > self.max_retries:
                        raise TaskFailed(f"{stage} task {i} failed after {attempt} attempts: {exc!r}") from exc
                    log.warning("%s task %d failed (%r); retry %d/%d", stage, i, exc, attempt, self.max_retries)
        return results

    def health(self) -> str:
        return f"ok (local tasks, {self.max_retries} retries)"


class CloudRunJobExecutor:
    """One Cloud Run Job execution per stage. Profile keys: job (projects/<p>/locations/<r>/jobs/<name>),
    timeout_s (default 3600), task_timeout_s (default 1800). The job's container is this image; its
    service account needs AlloyDB, Storage, Pub/Sub and Vertex AI access."""

    def __init__(self, cfg: AdapterConfig, namespace: str | None = None) -> None:
        self.job = cfg.get("job")
        self.timeout_s = int(cfg.get("timeout_s", 3600))
        self.task_timeout_s = int(cfg.get("task_timeout_s", 1800))
        self.s2_json = cfg.get("s2_json")

    def run(self, stage: str, task_count: int, run_task: Callable[[int], TaskResult], blobs: Any, args: list[str]) -> list[TaskResult]:
        from google.cloud import run_v2
        from google.protobuf import duration_pb2

        for i in range(task_count):  # clear partial results of an earlier attempt
            try:
                blobs.put(task_key(stage, i), b"", "application/json")
            except Exception:  # noqa: BLE001 - best effort
                pass
        overrides = run_v2.RunJobRequest.Overrides(
            task_count=task_count,
            timeout=duration_pb2.Duration(seconds=self.task_timeout_s),
            container_overrides=[run_v2.RunJobRequest.Overrides.ContainerOverride(
                args=args, env=[run_v2.EnvVar(name="FACTORY_PROFILE", value="gcp")]
                + ([run_v2.EnvVar(name="FACTORY_S2_JSON", value=self.s2_json)] if self.s2_json else []))],
        )
        t0 = time.time()
        op = run_v2.JobsClient().run_job(request=run_v2.RunJobRequest(name=self.job, overrides=overrides))
        execution = op.result(timeout=self.timeout_s)
        log.info("%s: execution %s finished in %.0fs (%d/%d tasks succeeded, %d retried)", stage, execution.name,
                 time.time() - t0, execution.succeeded_count, task_count, execution.retried_count)
        if execution.succeeded_count != task_count:
            raise TaskFailed(f"{stage}: {execution.failed_count} of {task_count} Cloud Run tasks failed (execution {execution.name})")
        results = []
        for i in range(task_count):
            raw = blobs.get(task_key(stage, i))
            if not raw:
                raise TaskFailed(f"{stage} task {i} reported success but wrote no result")
            d = json.loads(raw)
            results.append(TaskResult(index=i, counts=d["counts"], checksums={k: int(v, 16) for k, v in d["checksums"].items()}))
        return results

    def health(self) -> str:
        return f"ok (Cloud Run Job {self.job})"
