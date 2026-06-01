"""Drive the real frontend headlessly and capture GraphQL request/response.

Reveals exactly what the working browser request sends (headers, body) that a
raw SigV4 request may be missing (e.g. a reCAPTCHA token).
"""
import json
import sys

from playwright.sync_api import sync_playwright

URL = "https://divulgacione14presidente.registraduria.gov.co/departamento/16"
GQL = "/graphql"


def main() -> int:
    captured = []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-http2", "--disable-quic"],
        )
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="es-CO",
        )
        page = ctx.new_page()

        def on_request(req):
            if GQL in req.url and req.method == "POST":
                captured.append(req)

        page.on("request", on_request)

        responses = {}

        def on_response(resp):
            if GQL in resp.url and resp.request.method == "POST":
                try:
                    body = resp.text()
                except Exception:
                    body = "<unreadable>"
                responses[id(resp.request)] = (resp.status, body)

        page.on("response", on_response)

        print(f"Loading {URL} ...", file=sys.stderr)
        try:
            page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:
            print("goto warning:", exc, file=sys.stderr)
        # poll until graphql data calls land (or timeout)
        for _ in range(40):
            page.wait_for_timeout(1500)
            data_resps = [r for r in responses.values()
                          if '"data"' in r[1] and "__typename" not in r[1]]
            if data_resps:
                break
        page.wait_for_timeout(2000)

        print(f"\n==== captured {len(captured)} graphql POSTs ====")
        for i, req in enumerate(captured):
            print(f"\n--- request #{i} ---")
            print("URL:", req.url)
            hdrs = req.headers
            for k in sorted(hdrs):
                v = hdrs[k]
                if k.lower() == "authorization":
                    v = v[:40] + "...(truncated)"
                print(f"  {k}: {v}")
            pd = req.post_data
            if pd:
                try:
                    j = json.loads(pd)
                    qname = (j.get("query") or "")[:80].replace("\n", " ")
                    print("  BODY.operationName:", j.get("operationName"))
                    print("  BODY.query[:80]:", qname)
                    print("  BODY.variables:", json.dumps(j.get("variables")))
                    # show any non-standard top-level keys (recaptcha token etc.)
                    extra = {k: v for k, v in j.items()
                             if k not in ("query", "variables", "operationName")}
                    if extra:
                        print("  BODY.extra_keys:", json.dumps(extra)[:300])
                except Exception:
                    print("  BODY(raw):", pd[:300])
            st = responses.get(id(req))
            if st:
                print(f"  RESP status={st[0]} body[:200]={st[1][:200]}")
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
