# Buddy ESP32-S3 🐣

Un chiche de escritorio con dos ojos procedurales que refleja, en tiempo real, qué está
haciendo un agente LLM (Hermes Agent). Sin micrófono, sin parlante, sin texto en pantalla:
solo una cara, un LED RGB y un HUD de cuatro estados.

Hardware: **Waveshare ESP32-S3 LCD 1.47"** (320×172), un botón, IMU, LED RGB.

```
┌─────────────────────┐      ┌──────────────┐      ┌──────────────┐      ┌─────────┐
│  Hermes (agente)    │─────▶│  state.db    │─────▶│    Bridge    │◀─────│  ESP32  │
│  escribe huellas    │      │   (SQLite)   │      │  :8787       │ GET  │  /state │
└─────────────────────┘      └──────────────┘      └──────────────┘  1s └─────────┘
```

## Por qué hay un bridge en el medio

Tres razones concretas, ninguna cosmética:

1. **El ESP32 no puede leer la state DB.** Permisos `600`, dueños del contenedor del agente.
2. **La API key del agente no debe salir del servidor.** Es un bearer estático con poder
   total sobre el agente: tools, archivos, SSH. Si está en el firmware, cualquiera que
   dumpee el flash la tiene.
3. **La placa no debe saber qué es un "run", un "lease" ni un evento SSE.** El bridge
   traduce todo eso a cuatro estados de cara. La placa solo hace `GET /state` cada segundo.

El ESP32 usa un token corto propio (`X-Buddy-Token`) que **solo abre `/state`**.

## Estructura

```
bridge/
  buddy_bridge.py            # el bridge (stdlib pura, sin pip)
  mock_bridge.py             # servidor de mentira para desarrollar el firmware sin agente
  env.example                # variables de entorno
  systemd/hermes-buddy.service
firmware/                    # firmware de la placa (PlatformIO / ESP-IDF)
docs/
  buddy-esp32-environment.md # cómo es el entorno del agente (rutas, API, eventos)
  buddy-hud-y-emociones.md   # spec del HUD, LED y rotación de gestos
  buddy-tareas-firmware.md   # backlog original de firmware
  IDEAS.md                   # ideas de personalidad, priorizadas por impacto
```

## Contrato de la API

El bridge expone tres endpoints. **Ojo: los valores de `<...>` son placeholders**, hay que
reemplazarlos por los de tu instalación.

### `GET /health` — sin auth

```json
{"status": "ok", "service": "hermes-buddy-bridge"}
```

Liveness. Sirve para saber si el bridge está arriba sin gastar el token.

### `GET /state` — header `X-Buddy-Token` (o `Authorization: Bearer`)

```json
{
  "state":   "thinking",
  "label":   "usando la terminal",
  "phase":   "busy",
  "led":     [255, 190, 0],
  "elapsed": 12.4,
  "active":  1,
  "run_id":  null,
  "ts":      1794872283.43,
  "since":   1794872270.91
}
```

| campo | tipo | para qué |
|---|---|---|
| `state` | enum | `idle \| bored \| thinking \| tool \| waiting \| error \| done`. **Desconocido → tratá como `idle`.** |
| `label` | string ≤40 | Informativo, en español. **No parsear como enum**: cambia sin aviso. |
| `phase` | enum | `idle \| busy \| ok \| ask`. **Este es el que va al HUD.** Nunca lo derives de `state`. |
| `led` | `[r,g,b]` | Color ya resuelto por el servidor. El firmware no traduce nada. |
| `elapsed` | float | Segundos en el estado actual. Para escalar gestos (`thinking` 2s ≠ 40s). |
| `active` | int | `1`/`0`. **`-1` = el bridge no pudo leer → cara neutra, no de error.** |
| `run_id` | string\|null | Solo no-null durante un `/buddy/ask`. |
| `ts` / `since` | epoch float | Ahora / arranque del estado actual. |

**Regla de oro:** `phase` y `led` están pre-digeridos a propósito. Cambiar un color o agregar
un estado se hace **solo del lado del servidor**, y el firmware no toca una línea.

### `POST /buddy/ask` — header `X-Buddy-Token`

```json
{"prompt": "¿cómo está el disco del server?"}
→ 202 {"run_id": "...", "status": "queued"}
```

Crea un run en una sesión aislada (`BUDDY_SESSION`) y sigue su SSE interno. Es el único
camino por el que llega `done`/`ok` de forma fiable. **No lo dispares con un solo toque**:
un botón en un bolsillo puede ejecutar tools reales.

## Los tres estados del LED

| `phase` | color | RGB | modo |
|---|---|---|---|
| `busy` | amarillo | `[255,190,0]` | fijo |
| `ok` | verde | `[0,255,80]` | titila 5 s → celeste |
| `ask` | rojo | `[255,40,40]` | pulso lento |
| `idle` | celeste | `[0,180,255]` | fijo, brillo bajo |

El celeste es **también** el color de los ojos en reposo: una sola identidad cromática,
no una pantalla con una luz aparte.

## Instalación del bridge

```bash
# 1. dependencias: ninguna. Es stdlib pura (el LXC no tiene pip).

# 2. env
sudo mkdir -p /etc/hermes-buddy /opt/buddy
sudo cp bridge/env.example /etc/hermes-buddy/env
sudo chmod 600 /etc/hermes-buddy/env
# completar API_SERVER_KEY y BUDDY_TOKEN (openssl rand -hex 24)

# 3. código y servicio
sudo install -m 755 bridge/buddy_bridge.py /opt/buddy/buddy_bridge.py
sudo cp bridge/systemd/hermes-buddy.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now hermes-buddy

# 4. humo
curl -s http://BRIDGE_HOST:8787/health
curl -s -H "X-Buddy-Token: $BUDDY_TOKEN" http://BRIDGE_HOST:8787/state
```

### Notas de despliegue (lo que hace falta en la práctica)

- Corre como **root** bajo systemd. No es capricho: `state.db` es `600 hermes:hermes` y
  vive dentro del volumen del contenedor del agente, así que un usuario sin privilegios
  no la puede leer sin tocar los permisos del volumen.
- El servicio tiene que vivir **donde está el archivo**, no donde está la placa: el bridge
  lee `state.db` del filesystem local. En este despliegue, el LXC host del contenedor.
- `Restart=always` + `RestartSec=5`: el bridge se banca solo un reinicio del agente.


## Probar el firmware sin el agente

```bash
python3 bridge/mock_bridge.py --cycle          # recorre idle→thinking→tool→done→bored
python3 bridge/mock_bridge.py --state tool     # congela un estado para pulir un gesto
```

Apunta la placa al mock y iterás gestos, HUD y LED sin depender de que el agente esté
trabajando justo en ese momento. Es la diferencia entre desarrollar en 20 minutos y en 3 horas.

## Seguridad

- El firmware **nunca** ve el `API_SERVER_KEY`. Solo el `BUDDY_TOKEN`, que abre `/state`.
- El endpoint va **solo en la LAN**. Jamás publicado al WAN: detrás hay un agente con tools.
- No se muestra contenido de conversación, rutas, IPs ni args de tools en la pantalla.
  La etiqueta es genérica a propósito: la pantalla la ve cualquiera en la casa.
- Para probar con el mock, apuntá la placa a tu propia máquina, no al bridge real.

## Licencia

MIT.
