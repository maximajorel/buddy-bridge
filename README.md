# bridge — puente ESP32 <-> Hermes

Servicio que le cuenta a la placa qué está haciendo Sofía, y le permite pedirle
cosas con un botón. Python puro de stdlib: sin pip, sin venv, sin dependencias.

## Arranque

```bash
cp bridge/.env.example bridge/.env   # y completar los tokens
python3 -m zymo_bridge --check        # valida el .env y sale
python3 -m zymo_bridge                # servicio completo (desde bridge/)
python3 -m zymo_bridge --once         # un paso del poll, para debug
python3 -m zymo_bridge --print-config # config con secretos enmascarados
```

Tests:

```bash
python3 tools/run_tests.py            # 92 tests, stdlib unittest
python3 tools/run_tests.py --filter face -v
```

## Estructura

```
bridge/
  zymo_bridge/
    config.py     .env + entorno, una sola puerta (entorno > .env > default)
    labels.py     actividad -> etiqueta de 40 chars (pura, sin I/O)
    events.py     ring de eventos + long-poll
    face.py       el estado que se dibuja + phase + LED
    hermes.py     cliente HTTP del API server
    state_db.py   lectura RO de state.db
    service.py    el hilo que decide la cara
    http_api.py   el contrato que consume la placa
    __main__.py   arranque
  .env.example
tools/
  run_tests.py
  tests/
systemd/
  hermes-buddy.service
docs/
  ARCHITECTURE.md
```

Las reglas de diseño que no se rompen están en `docs/ARCHITECTURE.md`; las que
más importan: el bridge no decide cuándo dormirse (manda `sleep_hint`, decide el
firmware), no manda `bored`, y todo cambio de cara pasa por `Face.set_state`.
