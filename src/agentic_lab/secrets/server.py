"""The secret intake's HTTP endpoint (served by the secretary on the tailnet).

    GET  /client/lab-member.py   the member client, so members can fetch it without repo access
    POST /v1/secret              {"code", "name", "value"} from the member client
"""
import json

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from .intake import Intake, IntakeError

MAX_BODY = 16 * 1024


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

    return Starlette(routes=[
        Route("/client/lab-member.py", client, methods=["GET"]),
        Route("/v1/secret", secret, methods=["POST"]),
    ])
