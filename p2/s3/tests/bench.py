#!/usr/bin/env python3
"""Benchmarks for p2 new features.

Run:  uv run python p2/s3/tests/bench.py
No database or server required — benchmarks pure-Python/Rust logic only.
"""
import hashlib
import json
import os
import struct
import time
from base64 import b64encode

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bench(name, fn, iterations=10_000):
    """Run fn() `iterations` times, print stats."""
    # Warmup
    for _ in range(min(100, iterations)):
        fn()
    start = time.perf_counter()
    for _ in range(iterations):
        fn()
    elapsed = time.perf_counter() - start
    per_op = elapsed / iterations * 1_000_000  # µs
    ops_sec = iterations / elapsed
    print(f"  {name:.<50s} {per_op:8.2f} µs/op  ({ops_sec:,.0f} ops/s)  [{iterations} iters]")


# ---------------------------------------------------------------------------
# Policy evaluation benchmarks
# ---------------------------------------------------------------------------

def bench_policy():
    print("\n=== Policy Evaluation ===")
    from p2.s3.policy import check_access, parse_policy

    simple = parse_policy(json.dumps({
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Action": "s3:GetObject",
            "Resource": "arn:aws:s3:::bucket/*",
        }],
    }))

    complex_policy = parse_policy(json.dumps({
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": "s3:*", "Resource": "arn:aws:s3:::*"},
            {"Effect": "Deny", "Action": "s3:DeleteObject", "Resource": "arn:aws:s3:::prod/*"},
            {"Effect": "Allow", "Action": ["s3:GetObject", "s3:ListBucket"],
             "Resource": ["arn:aws:s3:::staging/*", "arn:aws:s3:::staging"]},
            {"Effect": "Deny", "Action": "s3:PutObject",
             "Resource": "arn:aws:s3:::archive/*"},
        ] + [
            {"Sid": f"rule{i}", "Effect": "Allow", "Action": "s3:GetObject",
             "Resource": f"arn:aws:s3:::bucket{i}/*"}
            for i in range(20)
        ],
    }))

    _bench("parse_policy (simple, 1 stmt)",
           lambda: parse_policy(json.dumps({
               "Version": "2012-10-17",
               "Statement": [{"Effect": "Allow", "Action": "s3:GetObject", "Resource": "*"}],
           })))

    _bench("check_access (1 stmt, match)",
           lambda: check_access(simple, "s3:GetObject", "arn:aws:s3:::bucket/file.txt"))

    _bench("check_access (1 stmt, no match)",
           lambda: check_access(simple, "s3:PutObject", "arn:aws:s3:::bucket/file.txt"))

    _bench("check_access (24 stmts, deny hit)",
           lambda: check_access(complex_policy, "s3:DeleteObject", "arn:aws:s3:::prod/x"))

    _bench("check_access (24 stmts, allow last)",
           lambda: check_access(complex_policy, "s3:GetObject", "arn:aws:s3:::bucket19/x"))


# ---------------------------------------------------------------------------
# Checksum benchmarks
# ---------------------------------------------------------------------------

def bench_checksum():
    print("\n=== Checksum Verification ===")
    from p2.s3.checksum import _ALGORITHMS, _HAS_RUST

    backend = "Rust" if _HAS_RUST else "Python"
    print(f"  Backend: {backend}")

    sizes = [
        ("1 KB", os.urandom(1024)),
        ("64 KB", os.urandom(64 * 1024)),
        ("1 MB", os.urandom(1024 * 1024)),
    ]

    for label, data in sizes:
        for algo, (header, verify_fn, compute_fn) in _ALGORITHMS.items():
            expected = compute_fn(data)
            if verify_fn:
                _bench(f"verify_{algo.lower()} ({label})",
                       lambda d=data, e=expected, v=verify_fn: v(d, e),
                       iterations=1000 if len(data) > 100_000 else 5000)
            else:
                _bench(f"compute_{algo.lower()} ({label})",
                       lambda d=data, c=compute_fn: c(d),
                       iterations=1000 if len(data) > 100_000 else 5000)


# ---------------------------------------------------------------------------
# Conditional header benchmarks
# ---------------------------------------------------------------------------

def bench_conditional():
    print("\n=== Conditional Header Checking ===")
    from unittest.mock import MagicMock
    from p2.s3.views.objects import _check_conditional_headers

    blob = MagicMock()
    blob.attributes = {
        "blob.p2.io/hash/md5": '"abc123def456"',
        "blob.p2.io/stat/mtime": "2025-06-15T12:00:00+00:00",
    }

    req_none = MagicMock()
    req_none.META = {}

    req_match = MagicMock()
    req_match.META = {"HTTP_IF_MATCH": '"abc123def456"'}

    req_miss = MagicMock()
    req_miss.META = {"HTTP_IF_MATCH": '"wrong"'}

    _bench("no conditional headers",
           lambda: _check_conditional_headers(req_none, blob), 50_000)
    _bench("If-Match (hit)",
           lambda: _check_conditional_headers(req_match, blob), 50_000)
    _bench("If-Match (miss → 412)",
           lambda: _check_conditional_headers(req_miss, blob), 50_000)
    _bench("blob=None, no headers",
           lambda: _check_conditional_headers(req_none, None), 50_000)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("p2 Feature Benchmarks")
    print("=" * 60)
    bench_policy()
    bench_checksum()

    # Conditional headers need Django ORM loaded
    import os
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "p2.core.settings")
    try:
        import django
        django.setup()
        bench_conditional()
    except Exception as exc:
        print(f"\n=== Conditional Header Checking ===\n  Skipped: {exc}")

    print("\nDone.")
