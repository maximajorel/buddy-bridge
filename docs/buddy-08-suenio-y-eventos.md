# Buddy ESP32-S3 — Sueño, eventos y catálogo de expresiones

Fecha: 2026-11-17. Escrito por Sofía (agente del lado servidor).
Complementa `buddy-tareas-firmware.md`. **Si hay conflicto, manda este documento**
para todo lo que sea sueño, transporte de eventos y expresiones.

Estado del lado servidor: **ya implementado y verificado en producción**
(bridge `<bridge-host>:8787`, servicio `hermes-buddy.service`).

---

## 0. El problema que arreglamos

Síntoma reportado por Maxi: *"lo muevo y se despierta, lo dejo y se vuelve a dormir al toque."*

La causa es un conflicto de autoridad, no un bug de animación:

- El bridge mandaba `bored` a los 180 s de inactividad de Hermes.
- El firmware despertaba con el IMU, pero el poll siguiente (1 s) volvía a traer `bored`.
- Resultado: el body decía "me movieron, estoy despierto" y el servidor lo pisaba
  un segundo después. El sueño duraba exactamente lo que tardaba el próximo GET.

**Regla nueva, no negociable:**

> El **estado** lo define el bridge. La **paciencia y el cuerpo** los define la placa.

Dormirse es paciencia. Por eso `bored` **salió del contrato** y el sueño pasó a ser
100 % local en el firmware.

---

## 1. Contrato nuevo de `GET /state`

```json
{
  "state": "idle",
  "label": "acá estoy",
  "ts": 1795016998.41,
  "run_id": null,
  "active": 0,
  "since": 1795016992.38,
  "elapsed": 6.9,
  "phase": "idle",
  "led": [0, 180, 255],
  "sleep_hint": 42.7,
  "ev_seq": 7
}
```

### Campos

| campo | tipo | qué es |
|---|---|---|
| `state` | string | `idle\|thinking\|tool\|waiting\|error\|done` — **`bored` ya no existe** |
| `label` | string | texto ≤40 chars. Informativo. **No parsear como enum.** |
| `active` | int | `1`/`0`; `-1` = el bridge no pudo leer → cara neutra, nunca de error |
| `since` | float | epoch de inicio del estado actual |
| `elapsed` | float | segundos en el estado actual (ya calculado, no lo restes vos) |
| `phase` | string | indicador de esquina: `busy\|ok\|ask\|idle` |
| `led` | [r,g,b] | color ya resuelto. Usalo tal cual, no traduzcas |
| **`sleep_hint`** | float | **segundos de inactividad de Hermes. Es un INSUMO, no una orden.** |
| **`ev_seq`** | int | cursor del evento más reciente; usalo para `/events` |

### Cómo usar `sleep_hint` (importante)

`sleep_hint` **no** dice "dormite". Dice "el servidor está quieto hace N segundos".
Vos combinás eso con tu propio sensor:

```
silencio_servidor = sleep_hint
silencio_local    = now - ultimo_estimulo_imu
inactividad       = MIN(silencio_servidor, silencio_local)
```

Si `inactividad > (tu umbral)` → cara de dormido. Cualquier estimulo del IMU
**resetea** `silencio_local` a 0 y despierta **inmediatamente**, sin esperar al poll.

> **El bug del "al toque" se arregla con esta línea:** un `sleep_hint` alto NO
> puede volver a dormir a la placa si el IMU registró movimiento hace <umbral.
> El `MIN` es lo que impide que el servidor pise al cuerpo.

### Umbral sugerido

| parámetro | valor | por qué |
|---|---|---|
| Sueño leve (cabeceo) | 60 s | todavía parece "presente" |
| Sueño profundo (ojos cerrados) | 180 s | coincide con lo que antes hacía `bored` |
| Despertar por IMU | inmediato (≤100 ms) | es la gracia de tener cuerpo |

Son valores de partida. Ajustables con `#define`, no hardcodeados en la lógica.

---

## 2. `GET /events` — long-poll (nuevo)

**Problema que resuelve:** con poll de 1 s perdés cualquier evento que dure menos de
1 segundo. Con reacciones más ricas (tool que arranca y termina rápido, error
instantáneo) eso pasa seguido y la cara parpadea de menos.

**Por qué no webhook:** el ESP32 está detrás de NAT. Nadie puede abrir una conexión
hacia él. Todo transporte tiene que iniciarlo la placa.

**Por qué long-poll y no WebSocket:** es HTTP común, lo podés hacer con
`HTTPClient` sin librerías extra, y el bridge ya identifica el `seq` de cada evento,
así que si se te cae la conexión pedís "desde N" y recuperás lo perdido.

### Uso

```
GET /events?since=<ev_seq>&wait=25
Header: X-Buddy-Token: <token>
```

- `since` = último `seq` que procesaste (arrancá con el `ev_seq` de `/state`).
- `wait` = cuántos segundos aguantar colgado antes de contestar vacío (máx 25).
- Si no pasa nada, contesta `[]` a los `wait` segundos → **volvé a pedir**. Un `[]`
  no es un error, es un timeout normal.
- Ahorra ~25 requests por cada request tuyo, y te da latencia de milisegundos.

### Eventos que vas a recibir

```json
[{"seq": 1, "event": "ask.accepted", "ts": 1795017008.08, "run_id": "run_...", "button": "center"}]
[{"seq": 2, "event": "state", "ts": 1795017011.04, "state": "done", "label": "listo", "prev": "thinking"}]
[{"seq": 3, "event": "label", "ts": 1795017012.10, "state": "tool", "label": "usando la terminal"}]
```

| evento | cuándo | campos |
|---|---|---|
| `state` | el estado **cambió** | `state`, `label`, `prev` |
| `label` | mismo estado, cambió el texto | `state`, `label` |
| `ask.accepted` | alguien apretó el botón | `run_id`, `button` |

### Reglas de robustez

1. **Si `/events` falla o tarda**, caé a poll de `/state` cada 1 s. El poll no se
   elimina: es el fallback. La placa tiene que andar con cualquiera de los dos.
2. **Si el `seq` que pedís es más nuevo que el último** (reinició el bridge), el
   servidor te devuelve `[]`. Reponete con un `/state` y seguí.
3. **Buffer de 64 eventos.** Si te quedás colgado más tiempo que eso, perdés viejos.
   Aceptable: el `/state` siempre tiene la verdad actual.
4. **Un solo `/events` abierto a la vez.** No abras hilos haciendo long-poll.
5. Timeout del socket > `wait` (ponele `wait + 10 s`).

---

## 3. `POST /buddy/ask` — ahora con botón

```json
{"prompt": "texto", "button": "left|center|right"}
```

- `button` es **opcional** y se reenvía como metadata al run. Sirve para que después
  podamos darle semántica a cada botón sin tocar el contrato.
- El `ask.accepted` sale como evento, así que si estás en `/events` te enterás sin poll.
- **Sigue valiendo:** un botón en un bolsillo ejecuta tools reales. Doble toque o
  mantener apretado. No lo cambies.

### Códigos de respuesta reales (verificados)

| caso | respuesta |
|---|---|
| sin token o token mal | `401 {"error":"unauthorized"}` |
| `since` no numérico | `400 {"error":"since must be int"}` |
| `wait` no numérico | `400 {"error":"wait must be number"}` |
| ruta inexistente | `404 {"error":"not found"}` |
| `since` > último seq | `200 []` (no es error: reinició el bridge) |
| nada nuevo en `wait` s | `200 []` (timeout normal) |

---

## 3.5. Pseudocódigo del loop principal

Esto es lo que necesitás para que `/events` y el poll convivan **sin bloquear la
animación**. Un solo task de red, un solo socket, el render en otro.

```cpp
// ---- constantes (ajustables) ----
#define EV_WAIT_SEC        25     // lo que se queda colgado el long-poll
#define EV_SOCKET_TIMEOUT  35     // > EV_WAIT_SEC + margen. NUNCA menor.
#define EV_FAIL_POLL_AFTER 3      // 3 fallos seguidos -> pasar a poll-only
#define POLL_PERIOD_MS     1000   // fallback
#define SLEEP_LIGHT_SEC    60
#define SLEEP_DEEP_SEC     180

enum Mode { MODE_EVENTS, MODE_POLL };

// Estado compartido con el task de render: protegido por mutex.
struct Shared {
  String  state = "idle";
  String  prev  = "";
  float   sleepHint = 0;
  int     evSeq = 0;
  bool    connected = false;
  unsigned long lastImuStimulus = 0;   // millis del último movimiento real
  unsigned long lastServerBusy  = 0;   // millis del último state != idle
} g;
SemaphoreHandle_t gMux;

// ---------- un GET, devuelve body o "" si falló ----------
String httpGet(const String &path, int timeoutSec) {
  HTTPClient http;
  http.setTimeout(timeoutSec * 1000);
  http.begin(cfg::BRIDGE_URL + path);
  http.addHeader("X-Buddy-Token", cfg::BUDDY_TOKEN);
  int code = http.GET();
  String body = (code == 200) ? http.getString() : "";
  http.end();
  if (code == 404) return "404";      // bridge viejo sin /events
  return body;
}

// ---------- task de red ----------
void netTask(void *) {
  Mode mode = MODE_EVENTS;
  int fails = 0;
  unsigned long lastPoll = 0;

  while (true) {
    if (mode == MODE_EVENTS) {
      // Cursor: el seq del último evento procesado (arranca del ev_seq de /state).
      String path = "/events?since=" + String(g.evSeq) + "&wait=" + String(EV_WAIT_SEC);
      String body = httpGet(path, EV_SOCKET_TIMEOUT);

      if (body == "404") {            // el bridge no tiene /events: no reintentes
        mode = MODE_POLL;
        continue;
      }
      if (body.length() == 0) {       // fallo de red
        if (++fails >= EV_FAIL_POLL_AFTER) mode = MODE_POLL;
        vTaskDelay(pdMS_TO_TICKS(1000));
        continue;
      }
      fails = 0;
      // body es un array: "[]" -> timeout normal, no es error.
      parseAndApplyEvents(body);      // actualiza g.state/g.prev/g.evSeq bajo mutex
      vTaskDelay(pdMS_TO_TICKS(20));  // no martillar si el server responde al instante
      continue;
    }

    // ---- MODE_POLL ----
    if (millis() - lastPoll >= POLL_PERIOD_MS) {
      lastPoll = millis();
      String body = httpGet("/state", 5);
      if (body.length() > 0) {
        // Si el bridge ya tiene /events, volvé: se puede recuperar solo.
        // Probamos cada 60 s, no en cada poll.
        static unsigned long lastProbe = 0;
        if (millis() - lastProbe > 60000) { lastProbe = millis(); mode = MODE_EVENTS; }
        applyState(body);             // incluye sleep_hint y ev_seq
        fails = 0;
      } else if (++fails >= EV_FAIL_POLL_AFTER) {
        // sin red: la cara sigue viva igual (el sueño es local)
      }
    }
    vTaskDelay(pdMS_TO_TICKS(50));    // cede el core: CRÍTICO para temperatura
  }
}

// ---------- decisión de dormir: TODO local ----------
bool shouldBeAsleep(unsigned long now) {
  float serverIdle = (now - g.lastServerBusy) / 1000.0f;
  float localIdle  = (now - g.lastImuStimulus) / 1000.0f;
  float inactive   = min(serverIdle, localIdle);   // <<< LA LÍNEA CLAVE
  // Nunca dormirse si el servidor está trabajando, aunque el cuerpo esté quieto.
  if (g.sleepHint < 5.0f) return false;
  return inactive > SLEEP_DEEP_SEC;
}

// ---------- IMU: resetea el silencio local ----------
void onImuStimulus() {
  unsigned long now = millis();
  g.lastImuStimulus = now;         // el servidor NO puede pisar esto
  if (wasAsleep()) wakeUp();       // despertar inmediato, no esperes al poll
}
```

### Reglas que este código ya respeta

1. **Un solo task de red.** Nada de long-poll en paralelo con el poll.
2. `EV_SOCKET_TIMEOUT` **siempre** mayor que `EV_WAIT_SEC`, o el cliente corta antes
   de que el servidor conteste y entrás en un loop de reintentos.
3. **`404` → poll-only y no reintentar** hasta dentro de 60 s. Cubre bridges viejos.
4. **`[]` no es error.** Es timeout normal o seq viejo.
5. `vTaskDelay` real en ambos modos. Un `while` sin delay es lo que calienta el chip.
6. `onImuStimulus()` toca `g.lastImuStimulus` **sin esperar al servidor**. Ese es el
   fix del "se duerme al toque".


---

## 4. Catálogo de expresiones — qué implementar

Las que ya estaban en `buddy-tareas-firmware.md` siguen valiendo. Estas son
**nuevas** y aprovechan lo que el bridge ya manda.

### 4.1 Despertar con jerarquía (lo más importante)

No todas las transiciones merecen la misma reacción. Escalá:

| transición | reacción | duración |
|---|---|---|
| dormido → `thinking`/`tool` | despertar de golpe, ojos grandes y enfoque | 100–150 ms |
| dormido → `idle` (por IMU) | bostezo corto, párpado pesado | 600 ms |
| dormido → `error` | despertar + ceño | 150 ms |
| dormido → `waiting` | despertar + mirada directa al usuario | 200 ms |

### 4.2 Reacciones que el bridge ya habilita (sin cambios de servidor)

- **`prev` en el evento `state`** te deja reaccionar a la *dirección* del cambio.
  `tool → done` merece satisfacción; `thinking → done` merece alivio; `tool → tool`
  (tool encadenada) merece un "sigo" sin celebrar. Sin `prev` todo eso se ve igual.
- **`elapsed`** te deja escalar sin contar vos: `thinking` 2 s vs 40 s. Ya lo tenías,
  pero ahora viene en `/state` y no hay que restar `since`.
- **`sleep_hint` bajo** = el servidor está trabajando. Aunque tu IMU diga quieto, no
  te duermas: eso es el "no dormirse mientras trabaja" que ya estaba en la tarea 3.
- **`ask.accepted`** te deja una micro-reacción de confirmación ("te escuché") antes
  de que llegue el primer `thinking`. Sin eso, apretás el botón y no pasa nada por
  hasta 1 segundo, que se siente roto.

### 4.3 Expresiones nuevas sugeridas

- **Micro-reacción de escucha** al `ask.accepted`: párpado que baja 80 ms + mirada
  al frente. Es el "te escuché" de un perro. Barato y de altísimo impacto percibido.
- **Satisfacción graduada** en `done`: si `prev == "tool"` y `elapsed` de la tool fue
  largo, sonrisa más marcada. Si fue corto, solo el tilde.
- **Alivio** en `error → idle` (el usuario lo resolvió): exhalar, hombros/cejas bajan.
- **Curiosidad por sacudida suave** — ya estaba, pero ahora el IMU **no compite** con
  el `bored` del servidor. Antes se pisaban; ahora el sueño es tuyo y respeta tu sensor.
- **Mirada al usuario al despertar**: al salir de dormido por IMU, 300 ms mirando al
  centro antes de volver a `idle`. Da la sensación de "¿qué pasó?".
- **Parpadeo de reconciliación**: si perdió conexión y vuelve (fallback a poll → de
  nuevo `/events`), un parpadeo doble. Comunica "me reconecté" sin mostrar texto.

### 4.4 Lo que NO hay que hacer

- **No dormirse mientras `sleep_hint` esté bajo** aunque el IMU no detecte nada.
  El cuerpo quieto no significa que esté solo.
- **No usar `bored`.** Ya no existe; si lo ves en un doc viejo, está desactualizado.
- **No mostrar estado de error por un `[]` de `/events`.** Es timeout normal.
- **No hacer un `/events` por cada animación.** Uno solo, sostenido.

---

## 5. Cómo probar todo esto sin la placa

```bash
TOK=<token>

# estado, con los campos nuevos
curl -s -H "X-Buddy-Token: $TOK" http://<bridge-host>:8787/state | python3 -m json.tool

# long-poll: queda 25 s colgado y devuelve [] si no pasa nada
curl -s -m 30 -H "X-Buddy-Token: $TOK" \
  "http://<bridge-host>:8787/events?since=0&wait=25"

# en otra terminal: provocá un evento
curl -s -X POST -H "X-Buddy-Token: $TOK" -H 'Content-Type: application/json' \
  -d '{"prompt":"Contestá solo: ok","button":"center"}' \
  http://<bridge-host>:8787/buddy/ask
```

El long-poll tiene que devolver el `ask.accepted` **al instante**, no a los 25 s.

Verificado en producción el 2026-11-17: `ask.accepted` con `button`, y
`state: done` con `prev: thinking`.

---

## 6. Resumen para el agente de firmware

| Necesito | Por qué |
|---|---|
| Eliminar cualquier manejo de `bored` | ya no existe en el contrato |
| Timer de sueño **local** con reseteo por IMU | el servidor no manda dormirse |
| Combinar `MIN(sleep_hint, silencio_local)` | que el servidor no pise al cuerpo |
| `/events` long-poll con fallback a poll 1 s | no perder eventos <1 s |
| Reaccionar a `prev` y `elapsed` | la dirección del cambio cambia la emoción |
| Micro-reacción en `ask.accepted` | sin ella el botón se siente muerto 1 s |
| `vTaskDelay` real + cap de FPS 30 | temperatura: el chip calienta por WiFi y FPS, no por el poll |
| Log cada 30 s: `temperatureRead()` + heap + fps | para medir antes/después de cada cambio |

---

## 7. Temperatura y medición (pedido explícito de Maxi)

El calentamiento **no** viene del poll de 1 s. Viene de, en orden de impacto:

1. **WiFi permanentemente despierto.** `WIFI_PS_NONE` o no llamar a `setSleep(true)`.
   Es la causa #1 y la más fácil de arreglar: transmitir en pulsos en vez de sostener
   el radio puede bajar 10-15 °C solo.
2. **Loop sin ceder el core.** El idle task nunca corre y el chip no baja de
   frecuencia. `vTaskDelay` real, no `delay(0)` ni un `while` ocupado.
3. **FPS alto escribiendo la LCD por SPI.** 320×172 píxeles por frame a 40-80 MHz es
   uso sostenido. **Cap de 30 fps**: para dos ojos alcanza y sobra.
4. **Backlight al 100 % en gabinete cerrado.** Suma 10-15 °C de ambiente interno.

**Es normal:** el S3 tiene throttling térmico y límite de junction ~105 °C. Si la
carcasa se toca pero no quema (<~60 °C), está dentro de especificación. No se "rompe".

### Cómo medirlo (esto es lo que hay que implementar)

El ESP32-S3 tiene un **sensor de temperatura en el die**, gratis, vía
`temperatureRead()` del ESP-IDF.

**Advertencia:** mide el die, no el ambiente. Da 10-20 °C más que el aire y está
sesgado por el propio calor del chip, así que **no sirve como termómetro de la pieza**.
Pero sirve justo para lo que necesitamos: **comparar antes/después de cada cambio**.
Para deltas es confiable (la calibración absoluta de fábrica es mala, los deltas no).

```cpp
void statsTask(void *) {
  while (true) {
    float t = temperatureRead();               // °C del die
    Serial.printf("T=%.1fC heap=%u fps=%.1f wifi_ps=%d\n",
                  t, ESP.getFreeHeap(), g_fps, g_wifiPsMode);
    vTaskDelay(pdMS_TO_TICKS(30000));          // cada 30 s, no más seguido
  }
}
```

### Protocolo de medición (una variable por vez)

| paso | qué hacer | duración |
|---|---|---|
| 1 | Baseline con el firmware actual, sin tocar nada | 30 min |
| 2 | Aplicar **solo** `setSleep(true)` / `WIFI_PS_MIN_MODEM` | 30 min |
| 3 | Agregar **solo** cap de FPS 30 + `vTaskDelay` | 30 min |
| 4 | Agregar **solo** bajar backlight | 30 min |

Comparar el promedio ignorando los primeros 5 minutos de cada tramo (el chip tarda
en estabilizar). Si un cambio no bajó nada, se revierte: no se acumula configuración
"por las dudas".

**No usar el IMU para medir calor.** No tiene relación, y sumar una task de sensores
consume más y ensucia la medición.

---

## 8. Sensores: qué hay y para qué sirve cada uno

| sensor | sirve para | no sirve para |
|---|---|---|
| Temp del die (`temperatureRead()`) | comparar antes/después | temperatura ambiente |
| IMU (acelerómetro) | despertar por movimiento, sacudida, inclinación | medir calor |
| Heap libre (`getFreeHeap()`) | detectar fugas tras horas | estimar CPU |
| Botones | input, `buddy/ask` | — |

**Con la placa por USB** se mide todo esto en tiempo real con `pio device monitor`
y se comparan corridas. Ese es el método: log estructurado, 30 min, una variable.

---

## 9. Checklist de entrega

Antes de decir "listo", verificar:

- [ ] No queda ninguna referencia a `bored` en el código
- [ ] El sueño lo decide la placa con `MIN(sleep_hint, silencio_local)`
- [ ] Mover la placa la despierta en ≤100 ms y **no** se vuelve a dormir al soltarla
- [ ] `/events` funciona y el poll de 1 s sigue como fallback
- [ ] `/events` con `since` viejo no rompe ni duplica la cara
- [ ] `ask.accepted` produce micro-reacción antes del primer `thinking`
- [ ] `prev` distingue `tool→done` de `thinking→done`
- [ ] Cap de FPS 30 y `vTaskDelay` real en todos los loops
- [ ] Log de temperatura cada 30 s funcionando
- [ ] 30 min de baseline medido y anotado

---

## 10. Lo que **no** está probado del lado servidor

Para que no se pierda tiempo buscando un bug de firmware que no existe:

- **Nadie hizo un run largo (>2 min) desde la placa.** El bridge sigue el SSE del run
  y traduce eventos, pero el camino `thinking` sostenido 40+ s no se ejercitó todavía.
- **No hay reconexión probada.** Si se cae el WiFi de la placa a mitad de long-poll, el
  comportamiento del `HTTPClient` de Arduino no está verificado con este servidor.
  El fallback a poll está diseñado para cubrirlo, pero no se probó en hardware.
- **El buffer de 64 eventos no se llenó nunca.** Si la placa se cuelga >64 eventos,
  pierde los viejos y se recompone con el `/state` siguiente. Diseñado, no observado.
- **`active: -1`** (bridge sin acceso al health de Hermes) no se forzó nunca. La placa
  debe mostrar cara neutra, nunca de error, si lo ve.

