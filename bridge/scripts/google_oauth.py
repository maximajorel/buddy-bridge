#!/usr/bin/env python3
"""Saca el `refresh_token` de Google en tu compu, una sola vez.

Por qué existe: el `refresh_token` es lo único que el bridge necesita para leer
el calendario sin que vuelvas a loguearte. Conseguirlo es un flujo OAuth con
navegador, y el bridge corre headless en un LXC sin pantalla. Así que el baile
con el navegador se hace acá, en tu máquina, y el token viaja a mano.

Es OAuth "de escritorio" con PKCE y un servidor local efímero: no hace falta
`google-auth`, ni pip, ni copiar códigos a mano (aunque si el navegador está en
otra máquina, se puede y el script te lo explica).

Uso:
    python3 scripts/google_oauth.py --client-id TU_ID --client-secret TU_SECRET

Después pegá los tres valores en la web del buddy. El `client_secret` de una app
de escritorio NO es un secreto real (Google lo documenta así), pero igual queda
guardado solo en el bridge.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import os
import secrets
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/calendar.readonly"


def _pkce() -> tuple[str, str]:
    """Verifier + challenge S256.

    PKCE es lo que hace que un `client_secret` filtrado no alcance para cambiar
    el código por un token: quien intercepte el código necesita el verifier, que
    nunca sale de este proceso.
    """
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


class _Catcher(http.server.BaseHTTPRequestHandler):
    """Servidor local que recibe el `?code=` del redirect y se apaga."""

    code: str | None = None
    state: str = ""

    def do_GET(self):  # noqa: N802 - nombre que impone BaseHTTPRequestHandler
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        error = query.get("error", [None])[0]
        got_state = query.get("state", [""])[0]
        if error:
            _Catcher.code = f"ERROR:{error}"
        elif got_state != _Catcher.state:
            # El `state` es anti-CSRF: si no coincide, el código no es nuestro.
            _Catcher.code = "ERROR:state no coincide"
        else:
            _Catcher.code = query.get("code", [""])[0]
        body = b"Listo. Pod\xc3\xa9s cerrar esta pesta\xc3\xb1a y volver a la terminal."
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _listen(port: int) -> tuple[str, int]:
    """Un puerto libre: 0 deja que el SO elija."""
    with http.server.HTTPServer(("127.0.0.1", port), _Catcher) as probe:
        return "127.0.0.1", probe.server_address[1]


def exchange(client_id: str, client_secret: str, code: str, verifier: str,
             redirect: str) -> dict:
    data = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "code_verifier": verifier,
        "grant_type": "authorization_code",
        "redirect_uri": redirect,
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--client-id", default=os.environ.get("GOOGLE_CLIENT_ID", ""))
    ap.add_argument("--client-secret", default=os.environ.get("GOOGLE_CLIENT_SECRET", ""))
    ap.add_argument("--port", type=int, default=0, help="0 = el que elija el SO")
    ap.add_argument("--manual", action="store_true",
                    help="todo a mano: sirve si el navegador está en otra máquina")
    args = ap.parse_args(argv)

    if not args.client_id or not args.client_secret:
        ap.error("faltan --client-id / --client-secret (o las variables de entorno)")

    verifier, challenge = _pkce()
    _Catcher.state = secrets.token_urlsafe(16)

    if args.manual:
        redirect = "urn:ietf:wg:oauth:2.0:oob"
        port = None
    else:
        server = http.server.HTTPServer(("127.0.0.1", args.port), _Catcher)
        port = server.server_address[1]
        redirect = f"http://127.0.0.1:{port}/"

    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": args.client_id,
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        # `prompt=consent` es lo que fuerza a Google a devolver el refresh_token:
        # sin esto, una segunda corrida devuelve solo el access_token.
        "prompt": "consent",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": _Catcher.state,
    })

    print("\n1. Abrí esta URL en el navegador y autorizá el acceso:\n")
    print(url)
    print()
    if not webbrowser.open(url):
        print("(no pude abrir el navegador solo: copiá la URL a mano)\n")

    if args.manual:
        code = input("2. Pegá el código que te muestra Google: ").strip()
    else:
        print(f"2. Esperando el redirect en {redirect} …")
        thread = threading.Thread(target=server.handle_request, daemon=True)
        thread.start()
        thread.join(timeout=300)
        server.server_close()
        if not _Catcher.code:
            print("\nNo llegó ningún código en 5 minutos. Probá con --manual.",
                  file=sys.stderr)
            return 1
        code = _Catcher.code

    if code.startswith("ERROR:"):
        print(f"\nGoogle devolvió un error: {code[6:]}", file=sys.stderr)
        return 1

    try:
        tokens = exchange(args.client_id, args.client_secret, code, verifier, redirect)
    except Exception as exc:  # noqa: BLE001 - el mensaje importa más que el tipo
        print(f"\nNo pude cambiar el código por un token: {exc}", file=sys.stderr)
        return 1

    refresh = tokens.get("refresh_token", "")
    if not refresh:
        print("\nGoogle no devolvió refresh_token. Volvé a correr con --manual "
              "y asegurate de aceptar TODOS los permisos.", file=sys.stderr)
        return 1

    print("\n✅ Listo. Pegá estos tres valores en la web del buddy:\n")
    print(f"GOOGLE_CLIENT_ID={args.client_id}")
    print(f"GOOGLE_CLIENT_SECRET={args.client_secret}")
    print(f"GOOGLE_REFRESH_TOKEN={refresh}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
