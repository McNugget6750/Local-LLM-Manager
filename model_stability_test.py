#!/usr/bin/env python3
"""
model_stability_test.py — Context-window stress test for any OpenAI-compatible server.

Sends requests at increasing context sizes, measures prefill throughput, decode
throughput, and time-to-first-token.  Works with llama.cpp, vLLM, LM Studio,
Ollama — anything that speaks /v1/chat/completions with SSE streaming.

Usage:
    python model_stability_test.py [options]

Examples:
    python model_stability_test.py
    python model_stability_test.py --url http://localhost:1234 --sizes 1k,4k,16k,64k
    python model_stability_test.py --runs 5 --max-tokens 200 --output results.json
"""

import argparse
import json
import sys
import threading
import time
from datetime import datetime

try:
    import httpx
except ImportError:
    sys.exit("httpx is required: pip install httpx")


# ---------------------------------------------------------------------------
# Context padding content
# Varied enough to avoid tokeniser short-circuits (BPE merges long repetitions
# into very few tokens, which skews the prompt_tokens count).
# ---------------------------------------------------------------------------
_FILLER_BLOCKS = [
    "The study of large language model inference reveals two distinct performance "
    "regimes: prefill and decode. During prefill the entire prompt is processed in "
    "a single parallel pass through the transformer, saturating compute-bound "
    "operations such as attention and feed-forward matrix multiplications. Decode, "
    "by contrast, generates one token at a time and is constrained by VRAM "
    "bandwidth because each step reads the full set of model weights.",

    "Modern GPU architectures provide several knobs to optimise these phases "
    "independently. Flash Attention reduces the memory footprint of the attention "
    "kernel by fusing operations and avoiding large intermediate activations. "
    "Continuous batching allows the server to interleave new prefill requests with "
    "ongoing decode steps, keeping the GPU busier across multiple users. CUDA "
    "graph capture eliminates per-token Python overhead during decode.",

    "Quantisation affects both phases differently. Weight-only quantisation such as "
    "GPTQ or AWQ shrinks model size and increases decode speed because less data "
    "must be streamed from VRAM per token. Activation quantisation further reduces "
    "compute requirements during prefill. FP4 formats like NVIDIA modelopt leverage "
    "Blackwell tensor cores for orders-of-magnitude higher TFLOPS at the cost of "
    "slightly higher quantisation error.",

    "Context window size introduces trade-offs beyond raw throughput. KV-cache "
    "memory grows linearly with sequence length, eventually limiting the number of "
    "concurrent requests a server can handle. Techniques such as sliding-window "
    "attention, sparse attention, and KV-cache quantisation extend the practical "
    "context length without proportional memory growth. Context checkpointing "
    "offloads old KV entries to CPU RAM at the expense of extra memory transfers.",

    "Stress testing a model across context sizes exposes instabilities that only "
    "manifest at scale: numerical overflow in attention softmax, OOM crashes in "
    "the KV-cache allocator, excessively slow prefill for very long prompts, and "
    "degraded output quality when the context exceeds the model's training "
    "distribution. Measuring TTFT and per-phase throughput at each size provides "
    "a clear picture of the usable operating range.",
]

_FILLER = "\n\n".join(_FILLER_BLOCKS) + "\n\n"
# One copy ≈ 400-450 tokens.


def _build_padding(target_tokens: int) -> str:
    """Return a string of approximately *target_tokens* tokens."""
    chars_per_token = 4  # conservative estimate for English prose
    chars_needed = max(0, target_tokens * chars_per_token)
    reps = chars_needed // len(_FILLER) + 1
    return (_FILLER * reps)[:chars_needed]


def _parse_sizes(raw: str) -> list[int]:
    """Parse a comma-separated list like '1k,4k,16000,65k' into integers."""
    result = []
    for part in raw.split(","):
        part = part.strip().lower()
        if part.endswith("k"):
            result.append(int(float(part[:-1]) * 1000))
        else:
            result.append(int(part))
    return result


def _detect_model(base_url: str, client: httpx.Client) -> str:
    r = client.get(f"{base_url}/v1/models", timeout=10)
    r.raise_for_status()
    data = r.json()
    if data.get("data"):
        return data["data"][0]["id"]
    raise RuntimeError("No models returned by /v1/models")


def _run_one(
    base_url: str,
    model: str,
    messages: list[dict],
    max_tokens: int,
    timeout_secs: float = 120.0,
) -> dict:
    """
    Stream one completion and return timing + token stats.
    Runs the HTTP call in a daemon thread so Ctrl+C always works.

    Returns a dict with keys:
        prompt_tokens, completion_tokens,
        ttft, decode_time, total_time,
        prefill_tps, decode_tps,
        preview, error
    """
    result: dict = {}
    stop_event = threading.Event()

    def _stream_worker():
        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "temperature": 0.6,
            "max_tokens": max_tokens,
        }
        t_start = time.perf_counter()
        t_first: float | None = None
        t_end:   float | None = None
        preview = ""
        usage:   dict | None  = None

        try:
            hx_timeout = httpx.Timeout(connect=10.0, read=timeout_secs,
                                        write=10.0, pool=5.0)
            with httpx.Client(timeout=hx_timeout) as client:
                with client.stream(
                    "POST",
                    f"{base_url}/v1/chat/completions",
                    json=payload,
                    headers={"Accept": "text/event-stream"},
                ) as resp:
                    if resp.status_code >= 400:
                        body = resp.read().decode("utf-8", errors="replace")
                        result["error"] = f"HTTP {resp.status_code}: {body[:300]}"
                        return

                    for raw_line in resp.iter_lines():
                        if stop_event.is_set():
                            result["error"] = "cancelled"
                            return

                        if not raw_line.startswith("data: "):
                            continue
                        raw = raw_line[6:].strip()
                        if raw == "[DONE]":
                            break
                        try:
                            chunk = json.loads(raw)
                        except json.JSONDecodeError:
                            continue

                        if chunk.get("usage") and not chunk.get("choices"):
                            usage = chunk["usage"]
                            continue

                        choices = chunk.get("choices") or []
                        if not choices:
                            continue

                        delta   = choices[0].get("delta", {})
                        content = delta.get("content") or ""

                        if content:
                            if t_first is None:
                                t_first = time.perf_counter()
                            if len(preview) < 120:
                                preview += content

                        finish = choices[0].get("finish_reason")
                        if finish:
                            t_end = time.perf_counter()
                            if usage is None and chunk.get("usage"):
                                usage = chunk["usage"]

        except httpx.ReadTimeout:
            result["error"] = f"timeout after {timeout_secs:.0f}s"
            return
        except Exception as exc:
            result["error"] = str(exc)
            return

        if t_end is None:
            t_end = time.perf_counter()

        prompt_tokens     = (usage or {}).get("prompt_tokens", 0)
        completion_tokens = (usage or {}).get("completion_tokens", 0)
        ttft        = (t_first - t_start)      if t_first else 0.0
        decode_time = (t_end   - t_first)      if t_first else 0.0
        total_time  = t_end - t_start
        prefill_tps = prompt_tokens     / ttft        if ttft        > 0 else 0.0
        decode_tps  = completion_tokens / decode_time if decode_time > 0 else 0.0

        result.update({
            "prompt_tokens":     prompt_tokens,
            "completion_tokens": completion_tokens,
            "ttft":              ttft,
            "decode_time":       decode_time,
            "total_time":        total_time,
            "prefill_tps":       prefill_tps,
            "decode_tps":        decode_tps,
            "preview":           preview.strip()[:80],
            "error":             None,
        })

    t = threading.Thread(target=_stream_worker, daemon=True)
    t.start()
    deadline = time.perf_counter() + timeout_secs + 15
    try:
        while t.is_alive():
            t.join(timeout=0.25)   # short slice — Ctrl+C lands between polls
            if not t.is_alive():
                break
            if time.perf_counter() > deadline:
                stop_event.set()
                result["error"] = f"timeout after {timeout_secs:.0f}s"
                break
    except KeyboardInterrupt:
        stop_event.set()
        raise

    return result or {"error": "no result (worker exited silently)"}


def _avg(results: list[dict], key: str) -> float:
    vals = [r[key] for r in results if not r.get("error") and r.get(key) is not None]
    return sum(vals) / len(vals) if vals else 0.0


def run(args: argparse.Namespace) -> None:
    base_url = args.url.rstrip("/")
    sizes    = _parse_sizes(args.sizes)

    sep = "=" * 72
    print(f"\n{sep}")
    print(f"  Model Stability Test")
    print(f"  Server : {base_url}")
    print(f"  Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(sep)

    with httpx.Client(timeout=30) as probe:
        model = args.model or _detect_model(base_url, probe)

    print(f"\n  Model   : {model}")
    print(f"  Sizes   : {', '.join(f'{s:,}' for s in sizes)} tokens (target)")
    print(f"  Runs    : {args.runs} per size")
    print(f"  Output  : {args.max_tokens} tokens max per run")
    print(f"  Timeout : {args.timeout:.0f}s per request")
    if args.warmup:
        print(f"  Warmup  : 1 throwaway run per size")
    print()

    all_records: list[dict] = []
    aborted = False

    try:
        for size in sizes:
            if aborted:
                break

            print(f"  ── ~{size:,} token context {'─' * (50 - len(str(size)))}")

            padding  = _build_padding(max(0, size - 100))
            messages = [
                {
                    "role": "system",
                    "content": "You are a helpful assistant. Keep answers very short.",
                },
                {
                    "role": "user",
                    "content": (
                        f"{padding}\n\n"
                        "Based on the text above, what are the two main LLM inference "
                        "performance phases? Answer in one sentence."
                    ),
                },
            ]

            size_ok: list[dict] = []

            if args.warmup:
                sys.stdout.write("    warmup... ")
                sys.stdout.flush()
                _run_one(base_url, model, messages, args.max_tokens, args.timeout)
                print("done")

            for run_idx in range(1, args.runs + 1):
                sys.stdout.write(f"    run {run_idx}/{args.runs}  ")
                sys.stdout.flush()

                r = _run_one(base_url, model, messages, args.max_tokens, args.timeout)
                record = {"target_tokens": size, "run": run_idx, **r}
                all_records.append(record)

                if r.get("error"):
                    print(f"FAIL  {r['error'][:60]}")
                    if args.stop_on_error:
                        print("\n  Stopping on first error (--stop-on-error).")
                        aborted = True
                        break
                else:
                    size_ok.append(r)
                    print(
                        f"OK  "
                        f"prompt={r['prompt_tokens']:>6,} tok  "
                        f"out={r['completion_tokens']:>3} tok  "
                        f"TTFT={r['ttft']:>5.2f}s  "
                        f"prefill={r['prefill_tps']:>8,.0f} t/s  "
                        f"decode={r['decode_tps']:>5.1f} t/s"
                    )
                    if args.show_preview and r["preview"]:
                        print(f"           → \"{r['preview']}\"")

                if run_idx < args.runs:
                    time.sleep(args.pause)

            if len(size_ok) > 1:
                print(
                    f"\n    avg  "
                    f"{'':>19}"
                    f"TTFT={_avg(size_ok,'ttft'):>5.2f}s  "
                    f"prefill={_avg(size_ok,'prefill_tps'):>8,.0f} t/s  "
                    f"decode={_avg(size_ok,'decode_tps'):>5.1f} t/s"
                )
            print()

    except KeyboardInterrupt:
        print("\n\n  Interrupted by user — printing partial results.\n")

    # ── Summary table ──────────────────────────────────────────────────────
    print(sep)
    print("  RESULTS SUMMARY")
    print(sep)
    hdr = f"  {'Target':>8}  {'Prompt':>7}  {'TTFT':>7}  {'Prefill t/s':>12}  {'Decode t/s':>11}  Status"
    print(hdr)
    print("  " + "─" * (len(hdr) - 2))

    for rec in all_records:
        tgt = f"{rec['target_tokens']:,}"
        if rec.get("error"):
            print(f"  {tgt:>8}  {'—':>7}  {'—':>7}  {'—':>12}  {'—':>11}  ERROR: {rec['error'][:35]}")
        else:
            print(
                f"  {tgt:>8}  "
                f"{rec['prompt_tokens']:>7,}  "
                f"{rec['ttft']:>7.2f}s  "
                f"{rec['prefill_tps']:>12,.0f}  "
                f"{rec['decode_tps']:>11.1f}  "
                f"OK"
            )

    ok_all = [r for r in all_records if not r.get("error")]
    if ok_all:
        print(f"\n  {len(ok_all)}/{len(all_records)} runs succeeded")
        print(
            f"  Overall avg — "
            f"prefill: {_avg(ok_all, 'prefill_tps'):,.0f} t/s  |  "
            f"decode: {_avg(ok_all, 'decode_tps'):.1f} t/s"
        )

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "timestamp": datetime.now().isoformat(),
                    "model":     model,
                    "server":    base_url,
                    "results":   all_records,
                },
                f,
                indent=2,
            )
        print(f"\n  Raw results saved → {args.output}")

    print(f"\n  Finished: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"{sep}\n")


def main() -> None:
    p = argparse.ArgumentParser(
        description="Stress-test any OpenAI-compatible LLM across context sizes.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--url",     default="http://localhost:1234",
                   help="Server base URL (default: http://localhost:1234)")
    p.add_argument("--model",   default=None,
                   help="Model ID — auto-detected from /v1/models if omitted")
    p.add_argument("--sizes",   default="1k,4k,8k,16k,32k,64k",
                   help="Comma-separated context sizes, e.g. '1k,8k,64k' (default: 1k–64k)")
    p.add_argument("--runs",    type=int, default=3,
                   help="Runs per context size (default: 3)")
    p.add_argument("--max-tokens", type=int, default=150,
                   help="Max output tokens per run (default: 150)")
    p.add_argument("--timeout", type=float, default=120.0,
                   help="Per-request timeout in seconds (default: 120). "
                        "Ctrl+C always works regardless.")
    p.add_argument("--warmup",  action="store_true",
                   help="Run one throwaway request per size before timing")
    p.add_argument("--pause",   type=float, default=0.5,
                   help="Seconds to pause between runs (default: 0.5)")
    p.add_argument("--stop-on-error", action="store_true",
                   help="Abort the test suite on the first error")
    p.add_argument("--show-preview", action="store_true",
                   help="Print the first 80 chars of each response")
    p.add_argument("--output",  default=None,
                   help="Save full results to a JSON file")
    run(p.parse_args())


if __name__ == "__main__":
    main()
