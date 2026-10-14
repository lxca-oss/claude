"""Background backtest jobs.

Each job runs in its own process (so a long backtest never slows down live trading in
the terminal process) and talks back through two files in ``data/backtests``:
``<id>.progress.json`` while running and ``<id>.json`` with the final report.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .. import paths


def _atomic_write(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, path)


def _worker(job_id: str, params: dict, out_dir: str, data_dir: str) -> None:
    os.environ["GSE_DATA_DIR"] = data_dir
    from .report import run_full

    out = Path(out_dir)
    prog_path = out / f"{job_id}.progress.json"
    last = [0.0]

    def progress(frac: float, msg: str) -> None:
        now = time.time()
        if now - last[0] < 0.5 and frac < 1:
            return
        last[0] = now
        _atomic_write(prog_path, {"state": "running", "progress": round(frac, 4), "message": msg})

    try:
        report = run_full(params, progress)
        report.update({
            "id": job_id,
            "name": params.get("name") or "",
            "created": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "params": {k: v for k, v in params.items() if k != "settings"},
        })
        _atomic_write(out / f"{job_id}.json", report)
        _atomic_write(out / f"{job_id}.summary.json", {
            "name": report.get("name"), "kind": report.get("kind"), "created": report.get("created"),
            "dataset": (report.get("dataset") or {}).get("id"), "start": report.get("start"),
            "end": report.get("end"), "summary": _brief(report),
        })
        _atomic_write(prog_path, {"state": "done", "progress": 1.0, "message": "finished"})
    except Exception as exc:  # report the failure to the terminal
        _atomic_write(prog_path, {
            "state": "error", "progress": 1.0, "message": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        })


class JobManager:
    def __init__(self):
        self.procs: dict[str, mp.Process] = {}
        self.ctx = mp.get_context("spawn")

    @property
    def dir(self) -> Path:
        return paths.backtests_dir()

    def start(self, params: dict) -> str:
        job_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        _atomic_write(self.dir / f"{job_id}.progress.json", {"state": "queued", "progress": 0.0, "message": "starting"})
        p = self.ctx.Process(
            target=_worker, args=(job_id, params, str(self.dir), str(paths.data_dir())), daemon=True,
        )
        p.start()
        self.procs[job_id] = p
        return job_id

    def status(self, job_id: str) -> dict:
        prog = self.dir / f"{job_id}.progress.json"
        if not prog.exists():
            if (self.dir / f"{job_id}.json").exists():
                return {"state": "done", "progress": 1.0, "message": "finished"}
            return {"state": "unknown", "progress": 0.0, "message": "no such job"}
        try:
            st = json.loads(prog.read_text())
        except json.JSONDecodeError:
            st = {"state": "running", "progress": 0.0, "message": ""}
        p = self.procs.get(job_id)
        if st.get("state") in ("running", "queued") and p is not None and not p.is_alive():
            st = {"state": "error", "progress": 1.0, "message": f"worker exited with code {p.exitcode}"}
        return st

    def cancel(self, job_id: str) -> None:
        p = self.procs.get(job_id)
        if p is not None and p.is_alive():
            p.terminate()
        _atomic_write(self.dir / f"{job_id}.progress.json", {"state": "cancelled", "progress": 1.0, "message": "cancelled"})

    def result(self, job_id: str) -> dict | None:
        f = self.dir / f"{job_id}.json"
        if not f.exists():
            return None
        return json.loads(f.read_text())

    def delete(self, job_id: str) -> None:
        for suffix in (".json", ".progress.json", ".summary.json"):
            f = self.dir / f"{job_id}{suffix}"
            if f.exists():
                f.unlink()

    def list(self) -> list[dict]:
        out = []
        for f in sorted(self.dir.glob("*.progress.json"), reverse=True):
            job_id = f.name[: -len(".progress.json")]
            st = self.status(job_id)
            row = {"id": job_id, **{k: st.get(k) for k in ("state", "progress", "message")}}
            summ = self.dir / f"{job_id}.summary.json"
            if summ.exists():
                try:
                    row.update(json.loads(summ.read_text()))
                except json.JSONDecodeError:
                    pass
            out.append(row)
        return out


def _brief(rep: dict) -> dict:
    if rep.get("kind") == "variants":
        return {"variants": len(rep.get("variants") or [])}
    s = rep.get("summary") or {}
    return {k: s.get(k) for k in ("trades", "mean_r", "profit_factor", "return_pct", "max_drawdown_pct", "t_stat")}
