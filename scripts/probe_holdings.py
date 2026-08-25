"""
Probe: does mfdata.in exist, and what does it return?

Runs on GitHub Actions (no CORS, open network). Tries a spread of
hosts and paths, prints status codes and a slice of every response
that comes back. Read the Action log to learn the real API shape.

Throwaway script - delete once we know the answer.
"""

import json
import urllib.request
import urllib.error
import socket

TEST_CODE = 118778          # Nippon India Small Cap Fund - Direct Growth

HOSTS = [
    "https://mfdata.in",
    "https://www.mfdata.in",
    "https://api.mfdata.in",
]

PATHS = [
    "/api/v1/changelog",
    "/api/v1/schemes/{c}",
    "/api/v1/schemes/{c}/holdings",
    "/api/v1/schemes/{c}/portfolio",
    "/api/v1/holdings/{c}",
    "/api/v1/schemes",
    "/api/schemes/{c}/holdings",
    "/mf/{c}/holdings",
    "/docs",
]


def get(url, timeout=20):
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json,text/html;q=0.9",
            "User-Agent": "Mozilla/5.0 (fund-tracker-probe)",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.headers.get("Content-Type", "?"), r.read().decode("utf-8", "replace")


def main():
    print("=" * 70)
    print("DNS RESOLUTION")
    print("=" * 70)
    for h in ["mfdata.in", "www.mfdata.in", "api.mfdata.in"]:
        try:
            print("  %-18s -> %s" % (h, socket.gethostbyname(h)))
        except Exception as e:
            print("  %-18s -> FAILED (%s)" % (h, e))

    print()
    print("=" * 70)
    print("ENDPOINT PROBE")
    print("=" * 70)

    winners = []
    for host in HOSTS:
        for path in PATHS:
            url = host + path.replace("{c}", str(TEST_CODE))
            try:
                status, ctype, body = get(url)
            except urllib.error.HTTPError as e:
                print("  %-3s  %s" % (e.code, url))
                continue
            except Exception as e:
                print("  ERR  %s  (%s)" % (url, type(e).__name__))
                continue

            print()
            print("  >>> %s  %s  [%s]  %d bytes" % (status, url, ctype, len(body)))
            if "json" in ctype.lower():
                try:
                    j = json.loads(body)
                    if isinstance(j, dict):
                        print("      top-level keys:", list(j.keys())[:15])
                    elif isinstance(j, list):
                        print("      array of %d; first item keys: %s"
                              % (len(j), list(j[0].keys())[:15] if j and isinstance(j[0], dict) else "n/a"))
                    print("      ---- first 1200 chars of pretty JSON ----")
                    print(json.dumps(j, indent=1)[:1200])
                    winners.append(url)
                except Exception as e:
                    print("      JSON parse failed:", e)
                    print("      raw:", body[:400])
            else:
                print("      (not JSON) first 300 chars:")
                print("     ", body[:300].replace("\n", " ")[:300])
            print()

    print("=" * 70)
    print("JSON ENDPOINTS THAT WORKED:")
    for w in winners:
        print("   ", w)
    if not winners:
        print("    NONE - mfdata.in did not return usable JSON from any path.")
    print("=" * 70)


if __name__ == "__main__":
    main()
