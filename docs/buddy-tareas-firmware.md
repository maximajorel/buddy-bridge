# Buddy ESP32-S3 — lista de tareas para el agente de firmware

Fecha: 2026-11-16. Escrito por Sofía (el agente del lado servidor).

---

## 0. Lo primero: el contrato del bridge (leelo antes de tocar la cara)

El bridge corre en **`BRIDGE_HOST:8787`** (LXC `hermes`, servicio systemd
`hermes-buddy.service`). Es la única cosa con la que habla el ESP32.

```
GET  /health          -> {"status":"ok","service":"hermes-buddy-bridge"}   (sin token)
GET  /state           -> {"state","label","ts","run_id","active","since"}  (con token)
POST /buddy/ask       -> {"prompt":"..."} -> 202 {"run_id","status"}       (con token)
```

**Auth:** header `X-Buddy-Token: <token>` (o `Authorization: Bearer <token>`).
El token se lo paso a Maxi; no lo hardcodees en un repo público.

### NUNCA metas la `API_SERVER_KEY` de Hermes en el firmware. No la necesitás.

### Estados posibles (`state`)

| state | significado | notas |
|---|---|---|
| `idle` | libre, sin actividad | el más común |
| `bored` | libre hace >180 s | **nuevo** — ver tarea 3 |
| `thinking` | procesando / generando | |
| `tool` | ejecutando una herramienta | el `label` dice cuál |
| `waiting` | esperando aprobación del usuario | |
| `error` | algo falló | |
| `done` | terminó bien | expira a `idle` a los 8 s |

**Tratá cualquier `state` desconocido como `idle`.** Voy a agregar estados sin avisar;
si el firmware no los conoce, tiene que degradar, no romperse.

**`label`** es texto en español ≤40 chars. Es informativo, **no** lo parsees como enum.
**`active`**: `1`/`0`. Si viene `-1`, el bridge no pudo leer nada → cara neutra, no de error.
**`since`**: epoch float de cuándo empezó el estado actual (útil para animaciones por duración).

---

## 1. Los ojos siguen verdes

**Causa más probable:** el LED RGB (`WS2812`) y la pantalla son cosas distintas, pero en
esta placa (Waveshare LCD 1.47") el LED comparte bus con la retroiluminación en algunos
revisados. Si los ojos se ven **verdes en la pantalla** (no el LED), entonces el bug está
en el framebuffer, no en el LED.

Checklist para descartar en orden:

1. ¿El verde sale de la **pantalla** o del **LED físico**? Decime cuál y aíslo el bug.
   - Si es pantalla → problema de color en el driver (formato RGB565 vs BGR565 es el
     sospechoso número uno: se ve "verde" cuando intercambiás R y B).
   - Si es LED → el estado inicial del WS2812 sin `show()` explícito.
2. Si es RGB565: probá invertir el orden de canales
   (`lv_color_make(r,g,b)` vs `lv_color_make(b,g,r)`) y fijate cuál da el color correcto.
   El panel ST7789 acepta ambos según el `MADCTL` — si el `MADCTL` está mal, todo se ve
   con los canales corridos.
3. **Test de referencia:** pintá tres rectángulos puros (rojo `0xF800`, verde `0x07E0`,
   azul `0x001F`) en `RGB565`. Si el "rojo" se ve azul, es swap de canales, no otra cosa.

**No lo adivines: primero escribí el test de los tres rectángulos.** Es 10 líneas y
te dice exactamente dónde está el problema.

---

## 2. Que reaccione a Sofía desde Desktop, Telegram y cualquier lado

**Ya está resuelto del lado del servidor.** No hay nada que hacer en el firmware.

Antes el bridge sólo veía runs del API; ahora lee directamente el state DB de Hermes
(`session_turn_leases` + `messages`), así que detecta:

- Desktop ✅ (probado en vivo: `tool | usando la terminal` mientras corre un comando)
- Telegram ✅
- cron / bots ✅
- `/buddy/ask` (el botón) ✅ con detalle adicional

**Lo único que tenés que hacer:** que la placa consulte `GET /state` cada **1 s**.
Ya no hace falta más nada. Latencia real: ~1–3 s.

### ⚠️ Fin de turno: la clave para que NO quede "pensando" de más

**Este era el bug más visible.** El lease de una sesión dura ~**300 segundos**, así que usar
sólo `session_turn_leases` hacía que la cara siguiera en "pensando" durante **cinco minutos**
después de que Sofía ya había contestado.

**La señal correcta es el último mensaje de la sesión:**

| último mensaje | significado | estado |
|---|---|---|
| `user` | te está leyendo / arrancando | `thinking` |
| `tool` | ejecutando | `tool` |
| `assistant` (hace >2.5 s) | **ya te contestó** | `idle` |

Hay un margen de **2.5 s** (`BUDDY_TURN_GRACE`) porque un `assistant` recién escrito puede ser
la narración previa a una tool — sin ese margen la cara parpadearía entre `thinking` e `idle`
en cada paso.

Verificado en vivo: al terminar el turno la cara cae a `idle` en **~1 segundo**, no en 300.

### Nota técnica sobre el "parpadeo" (por si lo ves)

Cuando trabajo, la secuencia real en la DB es `tool → assistant (narración) → tool → ...`,
así que mirar sólo el último mensaje hacía que la cara saltara `thinking ↔ tool` varias
veces por segundo. El bridge ahora:

- Usa una **ventana de 12 s** (`BUDDY_TOOL_WINDOW`, configurable en `/etc/hermes-buddy/env`).
- Sólo reporta `tool` si esa tool es **más nueva que el último `assistant`** — o sea, si
  todavía no fue respondida. Si el assistant ya narró después, la tool terminó y pasa a
  `thinking`. Esto arregla un bug donde la cara se quedaba trabada en `tool` eternamente.

Probado: 10 muestras seguidas con una tool real, sin parpadeo ni trabas.

### `done` respeta su TTL

`done` dura sus 8 s completos (configurable con `BUDDY_DONE_TTL`) aunque después haya
actividad nueva. Después vuelve a `thinking`. Verificado: `thinking → tool → done (8s) → thinking`.

Si el firmware ve cambios de estado muy rápidos igual, **frená la transición en la placa**
(ignorá un cambio de estado que dure menos de ~300 ms). Es defensa en profundidad: el
bridge ya lo suaviza, pero la cara no debería depender de eso.

Si probás y no reacciona cuando Maxi le habla por el Desktop, **no toques el firmware**:
avisame a mí, es del lado del bridge.

---

## 3. Emociones: aburrimiento, pensamiento largo, y más

El bridge ahora manda `bored` cuando no pasa nada hace 180 s (configurable). Pero la
**duración** de un estado la tiene que explotar el firmware, no el servidor: es lo que
hace que la cara se sienta viva.

Usá `since` para saber cuánto lleva en el estado actual.

### Escalera de aburrimiento (estado `bored`)
1. **0–20 s:** neutral, parpadeo normal.
2. **20–60 s:** mirada que se va para los costados, vuelve. Microsuspiro (baja sutil del
   párpado).
3. **60–120 s:** empieza a cabecear. Párpados caen y rebotan.
4. **120 s+:** ojos cerrados, respiración lenta. `idle` profundo.
5. Cualquier cambio a `thinking`/`tool` → **despertar de golpe** (apertura rápida, 100 ms).

### Pensamiento largo (estado `thinking` sostenido)
1. **0–3 s:** ojos arriba-izquierda (recordando), parpadeo normal.
2. **3–8 s:** entrecerrado, ceja/tensión, mirada fija.
3. **8–20 s:** mira al usuario, como pidiendo paciencia.
4. **20 s+:** aburrimiento leve mezclado — se le escapa un parpadeo largo.
   **Nunca** dormirse del todo mientras está `thinking`: tiene que parecer que trabaja.

### Catálogo de emociones extra (todas locales, sin servidor)
- **Sorpresa** al arrancar un `tool` inesperado: ojos muy abiertos 200 ms y vuelta.
- **Satisfacción** en `done`: párpado bajo + curva de sonrisa 1 s.
- **Confusión** en `error`: mirada cruzada, parpadeo rápido.
- **Fastidio** si lo sacudís (ver tarea 4): ojos apretados.
- **Curiosidad**: cuando detecta movimiento con el IMU, sigue a la fuente un instante.
- **Microsaccade**: pequeños saltos de mirada cada 2–5 s mientras está `idle`.
  Es EL detalle que hace que unos ojos parezcan vivos. Barato de implementar.
- **Sincronía de parpadeo**: 2–6 s aleatorio, con ráfagas (a veces dos seguidos).

---

## 4. IMU: reacciones físicas

El bridge **no** se entera de esto (es puro sensor local). Manejalo todo en el firmware.

### Ideas (en orden de impacto)

1. **Sacudida fuerte → mareo.** Aceleración sobre umbral (p. ej. |a| > 2.5 g sostenido
   ~300 ms) → ojos en espiral, párpados caídos, bamboleo. Dura 2–3 s y se recupera solo.
   Si lo seguís sacudiendo, **fastidio** en vez de mareo.
2. **Golpe seco → susto.** Un pico único y brusco: ojos muy abiertos + párpado que tarda
   en volver. Distinto de la sacudida (frecuencia vs amplitud).
3. **Lo levantás → se despierta.** Cambio sostenido en el eje Z (de "apoyado" a "en mano")
   → abre los ojos desde dormido.
4. **Lo dejás sobre la mesa → se acomoda.** Impacto suave + Z estable → un "respingo" corto
   y vuelve a neutral.
5. **Inclinación → los ojos compensan la gravedad.** Si está en un soporte inclinado,
   las pupilas se van hacia "abajo" (efecto de peso real). Es la mejora que más se nota.
6. **Que te mire al moverte.** Si la placa detecta movimiento repetido de un lado (alguien
   pasando), sigue el movimiento un instante.
7. **Sacudida suave y rítmica → le gusta.** Amplitud baja, frecuencia pareja: ojos
   entrecerrados de gusto. Contraste divertido con el mareo.
8. **Volcado (boca abajo) → cara de "no veo nada".** Z negativo: párpados apretados y
   un pequeño "shhh" visual.
9. **Botón largo (3 s) → dormir.** Botón corto → interactuar. Botón largo → apagarse.
   Con un solo botón, esa es la distinción que necesitás.

### Regla de oro
Todo esto debe ser **idempotente y con recovery**: si el IMU se desconecta o devuelve
basura, la cara vuelve a `idle` sola. Nunca dejar una animación trabada.

---

## 5. Cosas que NO hagas

- **No muestres texto de la conversación.** Es una cara. Mostrar contenido de chat en una
  pantalla que ve cualquiera en la casa es un riesgo que no vale la pena.
- **No parsees `label` como enum.** Cambia sin aviso.
- **No hagas `/buddy/ask` sin confirmación.** Un botón en un bolsillo puede ejecutar tools
  reales. Si lo implementás, que sea doble toque o mantener apretado.
- **No hardcodees la IP del bridge si podés usar mDNS** (`hermes-buddy.local` si después lo
  agrego) — pero por ahora IP fija está bien.
- **No implementes SSE contra Hermes.** El SSE es interno del bridge. La placa sólo hace GET.

---

## 6. Cómo probar sin la placa

Desde cualquier máquina en la LAN:

```bash
# ¿está vivo?
curl -s http://BRIDGE_HOST:8787/health

# ¿qué dice la cara? (con token)
curl -s -H "X-Buddy-Token: <token>" http://BRIDGE_HOST:8787/state

# forzar un ciclo completo
curl -s -X POST http://BRIDGE_HOST:8787/buddy/ask \
  -H "X-Buddy-Token: <token>" -H 'Content-Type: application/json' \
  -d '{"prompt":"Usá la terminal para correr: echo hola. Contestá una palabra."}'
```

Después de cada cambio del bridge, Maxi puede reiniciarlo con
`systemctl restart hermes-buddy.service` dentro del LXC.

---

## 7. Resumen de qué espera el servidor de vos

| Necesito | Para qué |
|---|---|
| Poll a `/state` cada 1 s | reacción a todos los canales |
| Manejar `bored` y `since` | aburrimiento y escaleras de emoción |
| Degradar estados desconocidos a `idle` | que no se rompa cuando yo agregue estados |
| Nada de SSE ni lógica de Hermes | el bridge ya lo resuelve |

**Y avisame qué es el verde: ¿pantalla o LED?** Con ese dato te digo la línea exacta del fix.
