"""One background job in its own process: python -m src.print_helper.worker <job dir> <analyze|fix> [nice].

Started by jobs.py, one at a time, at a lower CPU priority. Progress goes to <job dir>/status.json.
"""
import json
import os
import sys
import time
import traceback


def main(argv) -> int:
    job, task = argv[1], argv[2]
    try:
        os.nice(int(argv[3]) if len(argv) > 3 else 10)
    except (AttributeError, OSError, ValueError):
        pass
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    from src.print_helper import jobs, pipeline

    status_path = os.path.join(job, "status.json")
    started = time.time()
    last = [0.0]

    def progress(frac: float, msg: str = "") -> None:
        now = time.time()
        if now - last[0] < 0.7 and frac < 1.0:
            return
        last[0] = now
        jobs.write_json(status_path, {"state": "running", "task": task, "progress": round(frac, 3),
                                      "message": msg, "started": started, "updated": now})

    try:
        if task == "analyze":
            pipeline.analyze(job, progress)
        elif task == "fix":
            from src.print_helper import slicer
            cfg = jobs.load_config()
            options = jobs.read_json(os.path.join(job, "options.json"), {}) or {}

            def slice_fn(files, out_dir, opts, p):
                return slicer.slice_pieces(files, out_dir, cfg, opts, p)
            pipeline.fix(job, options, progress, slicer=slice_fn)
        else:
            raise ValueError(f"unknown task {task}")
    except Exception as e:
        traceback.print_exc()
        jobs.write_json(status_path, {"state": "failed", "task": task, "error": str(e)[:500] or type(e).__name__,
                                      "message": str(e)[:500], "started": started, "updated": time.time()})
        return 1
    jobs.write_json(status_path, {"state": "done", "task": task, "progress": 1.0, "message": "done",
                                  "started": started, "updated": time.time(),
                                  "seconds": round(time.time() - started, 1)})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
