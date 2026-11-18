"""Bridge entre la placa buddy (ESP32-S3) y el API server de Hermes.

El paquete está partido por responsabilidad, igual que el firmware del gadget:

    config.py    una sola puerta a las variables de entorno (`.env` + entorno)
    labels.py    traducción pura de actividad a etiqueta corta (sin estado)
    events.py    ring de eventos + long-poll (lo que "pasó")
    face.py      la cara: estado actual, phase y LED (lo que "es")
    hermes.py    cliente HTTP del API server
    state_db.py  lectura de solo lectura del state.db de Hermes
    service.py   el hilo que decide la cara a partir de la DB y el agregado
    http_api.py  el contrato HTTP que consume la placa
    __main__.py  arranque (`python3 -m zymo_bridge`)

Reglas de diseño (no romper):
  - El bridge NO decide cuándo dormirse: manda `sleep_hint` y el firmware decide.
  - El bridge NO manda `bored` (ver docs/buddy-08-suenio-y-eventos.md).
  - Todo cambio de cara pasa por `face.set_state`, el único lugar que emite eventos.
"""

__version__ = "1.1.0"
