"""The secret intake's HTTP endpoint (served by the secretary on the tailnet).

    GET  /client/lab-member.py   the member client, so members can fetch it without repo access
    POST /v1/secret              {"code", "name", "value"} from the member client
    GET  /s/{code}               a one-time web form for the secret (paste in the browser)
    POST /s/{code}               the form's submission
"""
import html
import json

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from .intake import FIELDS, Intake, IntakeError

MAX_BODY = 16 * 1024
_HEADERS = {"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
                                       "form-action 'self'",
            "Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Agentic Lab</title>
<style>body{{font:16px system-ui,sans-serif;max-width:34rem;margin:3rem auto;padding:0 1rem;
color:#222}}label{{display:block;margin:1rem 0 .3rem}}input{{width:100%;padding:.5rem;
font:inherit;box-sizing:border-box}}button{{margin-top:1.2rem;padding:.6rem 1.2rem;font:inherit}}
.note{{color:#666;font-size:.9rem}}.err{{color:#b00}}</style></head><body>{body}</body></html>"""


def _page(body, status=200):
    return HTMLResponse(_PAGE.format(body=body), status_code=status, headers=_HEADERS)


def _form(code, name, owner, error=""):
    who = f"the lab's {owner[8:]}" if owner.startswith("service:") else owner
    fields = "".join(
        f'<label for="{k}">{html.escape(label)}</label>'
        f'<input id="{k}" name="{k}" type="password" autocomplete="off" required>'
        for k, label, _ in FIELDS[name])
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    return _page(f"<h2>Agentic Lab: {html.escape(name)}</h2>"
                 f"<p>For {html.escape(who)}. Paste the values below; they go straight to the "
                 f"lab and are never shown again.</p>{err}"
                 f'<form method="post" action="/s/{html.escape(code)}">{fields}'
                 f'<button type="submit">Send to the lab</button></form>'
                 f'<p class="note">This link works once.</p>')


def app(intake: Intake, client_path):
    async def client(request: Request):
        try:
            with open(client_path, "rb") as f:
                return Response(f.read(), media_type="text/x-python")
        except FileNotFoundError:
            return PlainTextResponse("client not installed", status_code=404)

    async def secret(request: Request):
        body = await request.body()
        if len(body) > MAX_BODY:
            return JSONResponse({"error": "too large"}, status_code=413)
        try:
            data = json.loads(body)
            human = intake.receive(data.get("code"), data.get("name"), data.get("value"))
        except (ValueError, AttributeError) as e:
            status = 403 if isinstance(e, IntakeError) else 400
            return JSONResponse({"error": str(e) if status == 403 else "bad request"},
                                status_code=status)
        return JSONResponse({"stored": f"{data['name']} for {human}"})

    async def form_get(request: Request):
        code = request.path_params["code"]
        req = intake.lookup(code)
        if req is None:
            return _page("<h2>This link has been used or has expired.</h2>"
                         "<p>Ask for a new one.</p>", status=404)
        return _form(code, req.name, req.human)

    async def form_post(request: Request):
        code = request.path_params["code"]
        req = intake.lookup(code)
        if req is None:
            return _page("<h2>This link has been used or has expired.</h2>", status=404)
        if int(request.headers.get("content-length") or 0) > MAX_BODY:
            return _page("<h2>Too large.</h2>", status=413)
        form = await request.form()
        fields = {k: str(form.get(k) or "") for k, _, _ in FIELDS[req.name]}
        value = fields["value"] if list(fields) == ["value"] else fields
        try:
            intake.receive(code, req.name, value)
        except IntakeError as e:
            return _form(code, req.name, req.human, error=str(e))
        return _page("<h2>✅ Stored.</h2><p>The lab has it. You can close this tab.</p>")

    return Starlette(routes=[
        Route("/client/lab-member.py", client, methods=["GET"]),
        Route("/v1/secret", secret, methods=["POST"]),
        Route("/s/{code}", form_get, methods=["GET"]),
        Route("/s/{code}", form_post, methods=["POST"]),
    ])
