"""Arranque del bridge: `python3 -m zymo_bridge`.

Modos:
  (sin flags)            servicio completo: poll + HTTP
  --check                valida `.env` y sale (0 si está bien, 1 si no)
  --print-config         muestra la config con los secretos enmascarados y sale
  --once                 un paso del poll y sale (útil para debug y para tests)
"""

from __future__ import annotations

import argparse
import sys
import threading

from .config import Config, ConfigError, DEFAULT_ENV_FILE
from .http_api import serve
from .service import BridgeService


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m zymo_bridge", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env-file", default=None, help="ruta al .env (default: bridge/.env)")
    ap.add_argument("--check", action="store_true", help="validar config y salir")
    ap.add_argument("--print-config", action="store_true", help="config con secretos enmascarados")
    ap.add_argument("--once", action="store_true", help="un solo paso del poll y salir")
    args, rest = ap.parse_known_args(argv)
    if rest:
        print(f"[bridge] argumentos ignorados: {' '.join(rest)}", flush=True)

    try:
        config = Config.load(args.env_file or DEFAULT_ENV_FILE)
    except ConfigError as exc:
        print("[bridge] configuración inválida:", flush=True)
        for err in exc.errors:
            print("  - " + err, flush=True)
        return 1

    if args.print_config:
        for key, value in config.redacted().items():
            print(f"{key} = {value}")
        return 0
    if args.check:
        print(f"[bridge] config OK ({config.env_file})")
        return 0

    service = BridgeService(config)
    if args.once:
        service.poll_once()
        print(service.face.snapshot())
        return 0

    if not config.hermes_key:
        print("[bridge] aviso: API_SERVER_KEY vacío, /buddy/ask va a fallar", flush=True)
    if not config.buddy_token:
        print("[bridge] aviso: BUDDY_TOKEN vacío, /state queda sin auth (solo LAN)", flush=True)

    server = serve(service, config)
    threading.Thread(target=service.run_forever, daemon=True).start()
    print(f"[bridge] escuchando en {config.bind}:{config.port} -> {config.hermes_url} "
          f"(perfil default; ask {'on' if config.ask_enabled else 'off'})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        service.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
