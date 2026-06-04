"""
Monte Carlo π workload server.

Used by `pat demo` as the CPU-intensive container workload.
Each GET /compute?n=<iterations> estimates π via random sampling
and returns the result + elapsed time in JSON.
"""

import json
import os
import random
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

# Bound the per-request work to prevent an unbounded-`n` CPU/availability DoS.
DEFAULT_ITERATIONS = 200_000
MAX_ITERATIONS = 5_000_000


def monte_carlo_pi(n: int) -> float:
    return (
        4.0
        * sum(1 for _ in range(n) if random.random() ** 2 + random.random() ** 2 <= 1.0)  # noqa: S311
        / n
    )


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        elif parsed.path == "/compute":
            params = parse_qs(parsed.query)
            raw_n = params.get("n", [str(DEFAULT_ITERATIONS)])[0]
            try:
                n = int(raw_n)
            except ValueError:
                self.send_error(400, "query parameter 'n' must be an integer")
                return
            if not (1 <= n <= MAX_ITERATIONS):
                self.send_error(
                    400, f"query parameter 'n' must be between 1 and {MAX_ITERATIONS}"
                )
                return
            t0 = time.perf_counter()
            pi = monte_carlo_pi(n)
            elapsed_ms = (time.perf_counter() - t0) * 1000
            body = json.dumps(
                {"pi": round(pi, 6), "n": n, "elapsed_ms": round(elapsed_ms, 2)}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args: object) -> None:  # suppress access logs
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()  # noqa: S104
