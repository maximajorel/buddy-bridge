# Arquitectura del bridge

El firmware vive en otro repo (`esp-firmware`) y no comparte código con este.
Lo que sí comparten es la **forma**: módulos chicos con una responsabilidad, la
lógica pura separada del I/O, tests que corren con la stdlib y un documento como
este que explica por qué cada pieza está donde está.

Este repo sólo tiene el puente: no hay firmware acá, ni copias del firmware.

## Qué hace el bridge

La placa no habla con Hermes directamente. Habla con este servicio, que traduce
"qué está haciendo el agente" a una cara con etiqueta, y acepta pedidos de la
placa ("preguntale algo") sin exponer el API server ni su bearer.

```
   ESP32-S3  --- HTTP --->  bridge (:8787)  --- HTTP --->  Hermes api_server (:8642)
      |                        |                                |
   long-poll /events      state.db (RO)                    runs / SSE
```

## Las tres fuentes de verdad

En orden de prioridad:

1. **Run propio** (`POST /buddy/ask`): mientras el bridge siga el SSE de un run
   que él creó, la cara la manda ese stream. Nadie más la pisa.
2. **`state.db`** de Hermes, en solo lectura. Es la fuente más honesta para todo
   lo demás: cubre Desktop, Telegram, cron y api_server del perfil default sin
   acoplarse a ningún evento.
3. **Health del API server** (`/health/detailed`): respaldo cuando la DB no se
   puede leer (archivo ausente, lock, esquema nuevo). Ahí se sabe si hay runs,
   pero no cuáles ni con qué tool.

El poll corre cada `BUDDY_POLL_SECONDS` (3 s por default: leer una SQLite cada
0.4 s no agrega nada y la placa no lo necesita). El bridge nunca hace polling contra la
placa: la placa es la que hace long-poll contra `/events`. Un solo lado empuja,
siempre.

## Reglas que no se rompen

- **El bridge no decide cuándo dormirse.** Manda `sleep_hint` (segundos de
  inactividad de Hermes) y el firmware lo suma a su propio timer de IMU. Si el
  bridge decidiera, la placa se dormiría con el agente trabajando.
- **El bridge no manda `bored`.** Ese estado es del firmware (ver
  `docs/buddy-08-suenio-y-eventos.md` del firmware). `phase_for("bored")` existe
  sólo por compatibilidad y devuelve `idle`.
- **Todo cambio de cara pasa por `Face.set_state`.** Es el único lugar que emite
  eventos. Tocar el dict de estado a mano cambia `/state` pero no despierta a la
  placa en long-poll: ese fue el bug que motivó esta separación.
- **La etiqueta es nombre de herramienta, nunca contenido.** Máximo 40
  caracteres. Un comando, una ruta o un pedazo de prompt en pantalla es una fuga:
  en una placa que está sobre un escritorio, cualquiera lee.

## Módulos

| Módulo | Responsabilidad | Qué NO hace |
| --- | --- | --- |
| `config.py` | Leer `.env` + entorno, validar, enmascarar | No conoce HTTP ni DB |
| `labels.py` | Texto de actividad → etiqueta ≤40 chars | Sin estado, sin I/O |
| `events.py` | Ring acotado + `seq` + long-poll | No sabe de caras |
| `face.py` | Estado actual, `phase`, `led`, `sleep_hint` | No lee la DB |
| `hermes.py` | Cliente del API server (bearer en un solo lugar) | No decide estados |
| `state_db.py` | Lee `state.db` con `mode=ro` | Nunca escribe |
| `service.py` | Hilo del poll + seguimiento de runs | No habla HTTP de entrada |
| `http_api.py` | Contrato con la placa + auth | No tiene lógica de cara |
| `__main__.py` | Flags de arranque y wiring | Nada más |

La división sigue la del firmware: allá las políticas puras (`face_model`,
`sleep_policy`) se testean sin hardware; acá `labels` y `face` se testean sin
servidor, y `events` sin reloj real.

## Configuración

`.env` en `bridge/` (o el que diga `HERMES_BUDDY_ENV_FILE`).

**Precedencia: entorno del sistema > `.env` > default del código.** El archivo
nunca pisa lo que systemd ya cargó: si alguien exporta `BUDDY_PORT=9000` en la
unit, un `.env` viejo no lo revierte en silencio. Un `.env` con una variable
vacía (`BUDDY_TOKEN=`) no cuenta como definida: en la shell eso suele ser
"exporté el nombre y me olvidé el valor", no "quiero borrar el del archivo".

No se usa `python-dotenv`: el LXC no lo tiene y no se quiere pip en el arranque
de un servicio. El parser de `config.py` soporta `CLAVE=valor`,
`CLAVE="con espacios"`, `export`, comentarios y comentario al final de línea.

`Config.load()` junta **todos** los errores y los reporta juntos. Arrancar con
media configuración es peor que no arrancar: la placa queda mostrando una cara
que no corresponde y no hay nada en el log que lo explique.

Tampoco se usa `EnvironmentFile=` en la unit por el mismo motivo: systemd corta
en el primer error y no dice cuáles eran los otros.

## Auth

Dos tokens, dos permisos:

- `BUDDY_TOKEN` — lectura (`/state`, `/events`). Vacío = sin auth, aceptable sólo
  en LAN.
- `BUDDY_ASK_TOKEN` — escritura (`/buddy/ask`). Vacío = ask deshabilitado (403).
  Nunca se abre por no tener token de lectura: poder mirar la cara no es poder
  mandar prompts al agente.

Se aceptan `Authorization: Bearer <t>` o `X-Buddy-Token: <t>`. Tras 5 fallos
desde la misma IP, esa IP recibe 429 durante `AUTH_COOLDOWN_SECONDS` (60 s). Un
token de 24 bytes no se rompe por fuerza bruta, pero sí por insistencia con
diccionario; el cooldown hace que la insistencia sea inútil.

`/health` no pide token a propósito: es lo que consulta un monitor de liveness,
y no tiene sentido gastarle credenciales.

## Contrato HTTP

```
GET  /health                      200 {"status":"ok","service":"hermes-buddy-bridge","version":"1.1.0"}
GET  /state                       200 {state,label,phase,led,elapsed,active,run_id,ts,since,sleep_hint,ev_seq}
GET  /events?since=N&wait=T       200 [{seq,event,...}]   (o un único {event:"resync"})
POST /buddy/ask                   202 {run_id,status} | 400 | 401 | 403 | 413 | 502
```

`phase` y `led` vienen pre-digeridos: agregar una emoción o cambiar un color se
hace en `face.py` y el firmware no toca una línea.

`/events` con un `since` más nuevo que el último `seq` (placa reiniciada con
cursor viejo) arranca desde el presente. Con un `since` que quedó fuera del ring,
devuelve un único evento `resync` para que la placa relea `/state`: es mejor una
relectura que creer que no pasó nada.

## Tests

`python3 tools/run_tests.py`. `unittest` de la stdlib, sin dependencias.

El runner falla si descubre **cero** tests: un test que dejó de colectarse se ve
igual que un test que pasa, y esa es la forma más silenciosa de perder cobertura.

`test_http_api.py` levanta un servidor real en un puerto efímero y verifica
códigos y forma del JSON. Es el test que evita que un refactor cambie el contrato
sin que el firmware se entere.
