# Uvicorn benchmark

Historical comparison run on 2026-09-05 before FastAPI support was removed. The raw comparison results are preserved in `results.json`; the current generator supports only Starlette/msgspec. The fastest tested configuration is Starlette/msgspec with trusted responses. Input validation is enabled in every configuration.

## Results

Median requests per second across three runs; higher is better. `Checked` validates response values; `trusted` skips that validation. Both check the declared response variant. These are the same contracts, handlers and valid payloads, although the libraries have different validation/coercion/error semantics.

| Scenario | FastAPI checked | Starlette checked | FastAPI trusted | Starlette trusted | Starlette trusted / former FastAPI |
| --- | ---: | ---: | ---: | ---: | ---: |
| Empty 204 | 23,837 | 46,454 | 23,216 | 48,926 | 2.05× |
| Path/query/header/cookie parameters | 10,212 | 30,957 | 9,955 | 34,743 | 3.40× |
| Small JSON echo | 15,979 | 36,803 | 16,652 | 39,422 | 2.47× |
| Nested JSON, 100 items | 6,702 | 16,535 | 5,938 | 23,182 | 3.46× |
| Construct and return 100 models | 7,175 | 18,178 | 7,005 | 26,173 | 3.65× |
| Invalid JSON value (422) | 10,785 | 35,743 | 10,811 | 37,367 | 3.46× |

Starlette also wins with response validation enabled: throughput is 1.95–3.31× the checked FastAPI baseline in these scenarios. Trusting responses adds approximately 40% throughput for nested JSON and 44% for constructing 100 models relative to checked Starlette. Small differences between checked/trusted empty or parameter-only routes are measurement variation: those routes have no response body to validate.

## Latency

Median per-run p99 in milliseconds; lower is better. These are closed-loop measurements at each configuration’s saturation throughput, not comparisons at an identical fixed arrival rate. See `results.json` for every run, p50 and p95.

| Scenario | FastAPI checked | Starlette checked | FastAPI trusted | Starlette trusted |
| --- | ---: | ---: | ---: | ---: |
| Empty 204 | 1.93 | 1.48 | 1.89 | 1.21 |
| Path/query/header/cookie parameters | 4.03 | 2.11 | 4.40 | 1.25 |
| Small JSON echo | 3.35 | 1.70 | 3.23 | 1.46 |
| Nested JSON, 100 items | 6.85 | 3.32 | 7.87 | 2.05 |
| Construct and return 100 models | 7.12 | 2.94 | 6.35 | 1.69 |
| Invalid JSON value (422) | 6.57 | 1.49 | 6.41 | 1.28 |

## Memory and CPU

RSS is sampled after each measured case; it is not peak memory. The following ranges span the per-case medians. CPU is the Uvicorn worker’s accumulated process CPU time divided by elapsed measurement time; 100% means one core.

| Variant | RSS, MiB | CPU, % of one core |
| --- | ---: | ---: |
| fastapi_checked | 55.7–57.0 | 98–100 |
| starlette_checked | 41.0–41.7 | 97–99 |
| fastapi_trusted | 55.8–56.8 | 99–99 |
| starlette_trusted | 41.0–41.6 | 99–100 |

## Method

- Machine: Apple M3 Pro, macOS-27.0-arm64-arm-64bit-Mach-O, Python 3.14.0.
- Versions: fastapi 0.141.1, starlette 1.6.0, pydantic 2.13.5, pydantic_core 2.46.5, msgspec 0.21.1, uvicorn 0.52.4, uvloop 0.22.1, httptools 0.8.0.
- One Uvicorn worker, uvloop, httptools, HTTP/1.1 keep-alive, access log disabled, 32 concurrent connections.
- A separate Go net/http process generates load with GOMAXPROCS=2. It reads each response fully, verifies its status, and records request latency. It does not pipeline requests.
- Six cases × four configurations × three repeats. Each case gets a one-second warmup, then three measured seconds. Configuration/repeat order is shuffled with seed 731; each configuration run starts a fresh worker.
- Preflight checks status codes and decoded successful response bodies on every server start. The invalid-input case expects 422, which is counted as success.
- 4,867,257 measured HTTP responses, 0 load-generator errors.
- The list scenario creates 100 models on each request; it does not return a pre-encoded or cached response. The echo scenarios return the decoded request model.
- CPU and RSS cover the Uvicorn worker, excluding the generator and load client. The worker was near one full core throughout, which supports interpreting the result as a server bottleneck.
- These are short synthetic tests on one local macOS machine. No database, real authentication, Dishka, application middleware, remote network, or TLS is in the measured handlers. Error documents differ between libraries. Results do not predict the speedup of an I/O-bound production application.
- Pydantic model construction validates values; msgspec Struct construction does not. Checked msgspec additionally validates existing Structs by converting to builtins and validating before encoding. The two checked implementations therefore perform different work even when their valid JSON results match.
- FastAPI trusted was not consistently faster than FastAPI checked in this run. Do not infer a regression or a meaningful small improvement from the ordering of these noisy samples.

## Run the current Starlette benchmark

The current runner compares checked and trusted Starlette responses on the same
workloads. It does not reproduce the removed FastAPI baseline. From the repository
root, with Go on PATH:

```bash
uv sync --all-packages
uv run --all-packages python benchmarks/run.py --seconds 3 --repeats 3 --concurrency 32 --output benchmarks/results-starlette.json
```

The runner creates generated packages and the compiled load client in a temporary directory and removes them on exit. Servers listen only on 127.0.0.1 and are stopped in a finally block. No server packages or generated fixtures need to be committed to rerun the experiment.

Files: [runner](run.py), [contracts and cases](spec.py), [handlers](server.py), [load generator](load.go), [historical comparison results](results.json).
