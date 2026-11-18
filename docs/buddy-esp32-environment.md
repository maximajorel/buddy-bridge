# Hermes (Sofía) — entorno para el buddy ESP32-S3

Verificado el 2026-11-16 en vivo (curl real contra el servicio). Rutas y nombres son de
`/opt/hermes/gateway/platforms/api_server.py` y `/DATA_DIR/config.yaml`.

## Despliegue

1. **Corre**: Docker (s6-overlay dentro del contenedor, `container_name: hermes`; ver
   `/opt/hermes/docker-compose.yml`). No es systemd. Procesos: `hermes gateway run --replace`
   (PID del gateway, `/DATA_DIR/gateway.pid`) + `hermes dashboard --host 0.0.0.0 --port 9119`.
2. **Escucha**: el gateway API en `0.0.0.0:8642` (`config.yaml:2501` → `platforms.api_server`,
   `extra.host/port`). Ojo: `docker-compose.yml` declara `network_mode: host` pero el contenedor
   corriendo está en bridge docker (`172.18.0.2/16`, gw `172.18.0.1`) — o sea **8642 sólo se
   alcanza por el puerto publicado del host**. El dashboard (9119) es otra cosa: tiene login
   propio (`/login`), no sirve para el ESP32.
3. **LAN**: sí, misma LAN (host Proxmox `<proxmox-host>`, IP fija). Los otros
   dispositivos de la LAN viven en la misma subred (HA en su propia IP fija). No tiene Tailscale: es
   alcanzable sólo por la LAN.
4. **Runtime**: Python 3.14, Hermes Agent **0.22.5**, `aiohttp`. Healthcheck:
   `GET /health` (sin auth) → `{"status":"ok","platform":"hermes-agent","version":"0.22.5"}`.

## API del agente

5. **Sí**: OpenAI-compatible + Responses API + un API de runs propio (aiohttp, ver
   `_http_route_table`, línea ~1650). Rutas exactas:
   - `GET /health`, `GET /health/detailed`, `GET /v1/health`
   - `GET /v1/models`, `GET /v1/capabilities`, `GET /api/model/options`
   - `POST /v1/chat/completions` (streaming ok)
   - `POST /v1/responses`, `GET|DELETE /v1/responses/{id}`
   - `POST /v1/runs` → `{run_id, status, replayed}`
   - `GET /v1/runs/{run_id}`, `GET /v1/runs/{run_id}/events` (SSE)
   - `POST /v1/runs/{run_id}/{approval|steer|stop|resolve-unknown}`
   - `GET|POST /api/sessions`, `GET/PATCH/DELETE /api/sessions/{id}`,
     `POST /api/sessions/{id}/chat`, `POST /api/sessions/{id}/chat/stream`
   - `GET /v1/skills`, `GET /v1/toolsets`, `/api/jobs/*`
   - Multiplex por perfil: prefijo `/p/<profile>/...`
6. **Sí, turno headless**: `POST /v1/runs` con `{"input": "..."}` (+ `profile`, `model`) y
   después `GET /v1/runs/{run_id}/events`. **SSE**, no WebSocket (el único WS es
   `/v1/browser-control/ws`, no aplica acá). Probado: respuesta en <1 s.
7. **Estado/salud**: `/health` (liveness) y `/health/detailed` (readiness con checks de
   `state_db`, `session_store`, `config`, `model`, `disk`, `gateway`, `background_queues` y
   `metrics.active_runs`). `/v1/runs/{run_id}` devuelve `status`:
   `queued|running|waiting_for_approval|cancelled|failed|completed`.
8. **Auth**: Bearer `API_SERVER_KEY` en `Authorization: Bearer <key>` (obligatoria, ≥16 chars;
   está en `/DATA_DIR/.env`, 64 hex). Se rota regenerándola en `.env` y reiniciando el gateway.
   `/health` queda abierto; todo lo demás valida.
9. **charmd**: los adaptadores OpenAI-compatible y Responses API funcionan. ACP: existe
   `acp_adapter/` en la imagen. Para el buddy conviene **no** usar charmd: el API de runs
   (`/v1/runs` + SSE) es exactamente lo que necesita la cara.

## Eventos para la cara

10. Línea de vida (SSE, `GET /v1/runs/{id}/events`), con `id:` = `seq` monotónico y
    `timestamp` epoch float:
    `run.queued`, `run.started`, `message.started`, `assistant.delta`, `assistant.commentary`,
    `assistant.completed`, `tool.progress` (incluye pensamiento, `tool_name: "_thinking"`),
    `tool.started`, `tool.completed`, `tool.failed`, `reasoning.available`,
    `approval.request`, `run.completed`, `run.failed`, `run.cancelled`.
    Es **pull** por SSE (el ESP32 abre el stream y lo lee); no hay webhook push a LAN
    configurado (existe `/api/platforms/{platform}/events` sólo como callback de plataformas).
11. **Sí**: `/v1/runs/{run_id}` da `status` corto y `output` final. Para ≤40 chars conviene
    mapear en el puente el `tool_name`/`status` a una etiqueta propia (nada sensible: los args
    de tools pueden traer rutas y contenido de archivos — no mandarlos a la pantalla).
12. Distinción: `queued` = esperando slot; `running` = trabajando;
    `waiting_for_approval` = **esperando al usuario**; `completed` = listo; sin run activo
    (consulta a `/health/detailed` → `metrics.active_runs == 0`) = **inactivo**.
13. **Sí, concurrencia**: `max_concurrent_runs: 10` (`config.yaml:1427`), subagentes vía
    delegación (`background_queues.active_delegations`). El buddy debería representar **un run
    elegido** (por `X-Hermes-Session-Id` propio del buddy), no el agregado; si querés el
    agregado, leé `active_runs` del health.
14. Timestamps: en los eventos, epoch float UTC. En `/health/detailed`, ISO-8601 con `+00:00`.
    El host está en `America/Argentina/Tucuman` (UTC-3): convertir en el puente si la cara
    muestra hora.
15. Sin rate limit propio en el API server. Costo: consultar `/health/detailed` cada 2–5 s es
    gratis, pero **no** poll-ears `/v1/runs` para un run terminado indefinidamente (retention
    86400 s) y no abras un SSE nuevo por cada poll: un SSE abierto por turno alcanza.

## Red y alcance

16. **Sí, LAN sin internet**: publicá el 8642 sólo en la IP LAN del host
    (`PROXMOX_HOST:8642`) o agregá un vhost en NPM con allow-list de la subred
    `LAN_SUBNET`. **Nunca** publicarlo en el WAN: la key es un bearer estático con poder
    total sobre el agente (tools, archivos, SSH). Todo por Tailscale no hace falta.
17. Sí: hay Docker en el host Proxmox. Un `charmd` o puente propio puede correr como contenedor
    hermano con `network_mode: host` (para ver `0.0.0.0:8642` directo) o en la red bridge
    apuntando a `host.docker.internal:8642` / `172.18.0.1`.
18. **No mostrar**: contenido de mensajes, args/outputs de tools (rutas, SSH, tokens),
    nombres de sesiones/perfiles de trabajo, IPs y puertos internos. Para la casa, sólo estado
    + etiqueta genérica ("pensando", "usando herramienta", "listo", "te espera").

## Pruebas

19. Sí: creá un perfil dedicado (p. ej. `buddy`) o usá una sesión propia vía
    `X-Hermes-Session-Id`; el run no toca las conversaciones del Desktop. Run reciente de
    prueba: `POST /v1/runs {"input":"Responde solo: OK-BUDDY"}` → `OK-BUDDY` en <1 s.
    (No hay un modo "fake" aparte: el perfil separado es el aislamiento.)
20. Documentación/config legible sin secretos: `/opt/hermes/docker-compose.yml`,
    `/opt/hermes/gateway/platforms/api_server.py` (tabla de rutas al inicio y ~1650),
    `/DATA_DIR/config.yaml` (secciones `platforms.api_server` en 2501 y 1426),
    `/opt/hermes/acp_adapter/`. **No** leer `/DATA_DIR/.env` (tiene la key y otras credenciales).

## Recomendación

Puente propio mínimo en el host: un loop Python/Node que (a) `POST /v1/runs` cuando se aprieta
el botón, (b) mantiene un SSE abierto y traduce cada evento a 6 estados de cara
(`idle|thinking|tool|waiting|error|done`), (c) expone `GET /state` en LAN con
`{"state":"thinking","label":"buscando en casa","ts":1794864312}`. El ESP32 sólo hace
long-poll/GET cada 1 s a ese `/state`. Evita meter la `API_SERVER_KEY` en el firmware (el ESP32
sólo habla con el puente, autenticado por un token corto propio).
