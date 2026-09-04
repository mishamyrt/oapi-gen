"""Generate, preflight, and benchmark response validation modes over Uvicorn HTTP sockets."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import platform
import random
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from oapi_gen import generate_package
from spec import CASES, SPEC


def process_stats(pid):
    result = subprocess.check_output(["ps", "-o", "rss=,time=", "-p", str(pid)], text=True)
    rss, cpu = result.split()
    minutes, seconds = cpu.split(":")
    return int(rss), int(minutes) * 60 + float(seconds)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def preflight(base):
    for name, method, path, body, expected in CASES:
        request = Request(
            base + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Content-Type": "application/json",
                "X-Token": "bench",
                "Cookie": "session=bench",
            },
        )
        try:
            response = urlopen(request, timeout=3)
        except HTTPError as error:
            response = error
        with response:
            assert response.status == expected, (name, response.status, response.read())
            data = response.read()
            if expected == 200:
                decoded = json.loads(data)
                if body:
                    assert decoded == body
                else:
                    assert decoded == [
                        {"id": i, "name": f"item {i}", "quantity": i + 1, "active": True}
                        for i in range(100)
                    ]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=3)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--concurrency", type=int, default=32)
    parser.add_argument("--output", type=Path, default=Path("benchmarks/results-starlette.json"))
    args = parser.parse_args()
    here = Path(__file__).resolve().parent
    result = {
        "environment": {
            "platform": platform.platform(),
            "cpu": (
                subprocess.check_output(
                    ["sysctl", "-n", "machdep.cpu.brand_string"], text=True
                ).strip()
                if sys.platform == "darwin"
                else platform.processor()
            ),
            "go": subprocess.check_output(["go", "version"], text=True).strip(),
            "python": sys.version,
            "machine": platform.machine(),
            "versions": {
                name: importlib.metadata.version(name)
                for name in (
                    "starlette",
                    "msgspec",
                    "uvicorn",
                    "uvloop",
                    "httptools",
                )
            },
            "workers": 1,
            "loop": "uvloop",
            "http": "httptools",
            "concurrency": args.concurrency,
            "seconds": args.seconds,
            "repeats": args.repeats,
            "warmup_seconds": 1,
            "load_generator": "Go net/http, GOMAXPROCS=2, HTTP/1.1 keep-alive, localhost",
        },
        "runs": [],
    }
    with tempfile.TemporaryDirectory(prefix="oapi-gen-benchmark-") as directory:
        work = Path(directory)
        spec = work / "openapi.json"
        spec.write_text(json.dumps(SPEC))
        for checked in (True, False):
            generate_package(
                spec,
                work / f"starlette_{'checked' if checked else 'trusted'}",
                validate_responses=checked,
            )
        load = work / "load"
        subprocess.run(
            ["go", "build", "-o", str(load), str(here / "load.go")],
            env={**os.environ, "GOCACHE": str(work / "go-cache")},
            check=True,
        )
        payloads = {}
        for name, _, _, body, _ in CASES:
            if body is not None:
                payloads[name] = work / f"{name}.json"
                payloads[name].write_text(json.dumps(body, separators=(",", ":")))
        order = [
            (repeat, variant)
            for repeat in range(args.repeats)
            for variant in (
                "starlette_checked",
                "starlette_trusted",
            )
        ]
        random.Random(731).shuffle(order)
        for repeat, variant in order:
            port = free_port()
            base = f"http://127.0.0.1:{port}"
            env = {
                **os.environ,
                "OAPI_BENCH_PACKAGE": variant,
                "PYTHONPATH": os.pathsep.join((str(work), str(here))),
            }
            log = work / "server.log"
            with log.open("w") as output:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "uvicorn",
                        "server:app",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(port),
                        "--workers",
                        "1",
                        "--loop",
                        "uvloop",
                        "--http",
                        "httptools",
                        "--no-access-log",
                        "--log-level",
                        "error",
                    ],
                    env=env,
                    stdout=output,
                    stderr=output,
                )
                try:
                    for _ in range(200):
                        if process.poll() is not None:
                            raise RuntimeError(log.read_text())
                        try:
                            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                                break
                        except OSError:
                            time.sleep(0.025)
                    else:
                        raise RuntimeError("Uvicorn did not start")
                    preflight(base)
                    for name, method, path, _, status in CASES:
                        command = [
                            str(load),
                            "-url",
                            base + path,
                            "-method",
                            method,
                            "-concurrency",
                            str(args.concurrency),
                            "-status",
                            str(status),
                        ]
                        if name in payloads:
                            command += ["-body", str(payloads[name])]
                        load_env = {**os.environ, "GOMAXPROCS": "2"}
                        subprocess.run(
                            [*command, "-duration", "1s"],
                            env=load_env,
                            stdout=subprocess.DEVNULL,
                            check=True,
                        )
                        _, before_cpu = process_stats(process.pid)
                        measured = subprocess.check_output(
                            [*command, "-duration", f"{args.seconds}s"], env=load_env, text=True
                        )
                        rss, after_cpu = process_stats(process.pid)
                        row = json.loads(measured)
                        row.update(
                            variant=variant,
                            case=name,
                            repeat=repeat,
                            rss_mib=rss / 1024,
                            cpu_percent=(after_cpu - before_cpu) / row["seconds"] * 100,
                        )
                        result["runs"].append(row)
                        print(
                            f"{variant:18} {name:14} {row['rps']:9.0f} RPS  "
                            f"p99={row['p99_ms']:.2f} ms",
                            flush=True,
                        )
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, indent=2) + "\n")
    summary = []
    for name, _, _, _, _ in CASES:
        for variant in (
            "starlette_checked",
            "starlette_trusted",
        ):
            rows = [r for r in result["runs"] if r["case"] == name and r["variant"] == variant]
            summary.append(
                {
                    "case": name,
                    "variant": variant,
                    **{
                        key: statistics.median(r[key] for r in rows)
                        for key in ("rps", "p50_ms", "p95_ms", "p99_ms", "rss_mib", "cpu_percent")
                    },
                }
            )
    result["summary"] = summary
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(f"Saved {args.output}")


if __name__ == "__main__":
    main()
