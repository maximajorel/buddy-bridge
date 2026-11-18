# Buddy ESP32-S3 — HUD de estado + emociones rotativas

Actualizado: 2026-11-16. Escrito por Sofía (lado servidor).

Este documento **reemplaza** la sección 3 del doc anterior y agrega el HUD.
El contrato del bridge y las tareas 1 (verde), 2 (canales) y 4 (IMU) siguen vigentes.

---

## Idea central: dos canales separados

La cara comunica **emoción**. Pero la emoción es ambigua: una cara concentrada puede
significar "pensando" o "aburrida". Y no hay forma de saber si ya terminé.

**Solución: separar los dos mensajes.**

| Canal | Qué comunica | Dónde |
|---|---|---|
| **Cara** (ojos, boca) | emoción, vida, personalidad | centro de la pantalla |
| **HUD** (esquina sup. derecha) | estado funcional, sin ambigüedad | 24×24 px, siempre igual |

La cara puede hacer lo que quiera; el HUD nunca miente.

---

## El HUD (lo más importante)

El bridge ahora manda un campo **`phase`** que ya resuelve la semántica. **No lo derives
vos del `state`**: usá `phase` directo. Si mañana agrego estados, `phase` sigue siendo
uno de estos cuatro.

```
busy  ->  tres puntitos animados      "todavía no te contesté"
ok    ->  tilde ✓ (verde)             "ya te contesté"      (dura 8 s)
ask   ->  ! rojo pulsante             "necesito algo tuyo"
idle  ->  nada, o punto gris tenue    "en standby"
```

### Especificación visual

- **Posición:** esquina superior derecha, 8 px de margen. Tamaño ~20 px.
- **`busy` — tres puntitos.** Tres círculos de 4 px en fila, separados 3 px.
  Rotación de opacidad: cada punto sube a opacidad 1.0 y baja a 0.35, desfasados
  0, 180 y 360 ms, ciclo de 1.2 s. **El movimiento es lo que comunica "trabajando"**,
  no la forma. Si los puntitos están quietos, no sirven.
- **`ok` — tilde.** Trazo simple ✓ en verde (`0x07E0`), aparece con un "pop" de escala
  (0.8 → 1.1 → 1.0 en 250 ms) y se queda quieto. Sin animación después del pop:
  el tilde quieto = terminado.
- **`ask` — admiración roja.** `!` rojo (`0xF800`) que **pulsa** (escala 1.0 ↔ 1.15,
  600 ms, suave). El pulso se nota periféricamente, que es el punto.
- **`idle` — nada.** Opcional: un punto gris al 25% de opacidad. Recomendado: pantalla
  limpia, el HUD desaparece. Que el tilde desaparezca es tan informativo como que aparezca.

### Reglas del HUD

1. **Nunca muestres dos indicadores a la vez.** Es uno solo, se reemplaza.
2. **Respetá `phase` tal cual.** No lo recalcules desde `state`.
3. **`ok` dura 8 s** y después pasa a `idle` por su cuenta (el bridge ya lo hace).
4. **Si `phase` llega desconocido o falta**, mostrá `idle`. Nunca un tilde falso.
5. **El HUD va por encima de la cara**, sin tapar los ojos. Si la cara ocupa toda la
   pantalla, reservá ese rincón.

---

## Rotación de gestos dentro de una misma sesión

Pedido explícito de Maxi: cuando trabaja, la cara **no puede quedar congelada en un gesto**.
Un gesto repetido 40 segundos se lee como "se colgó".

### Regla de rotación

Mientras el `state` sea el mismo, rotá el gesto cada **3–6 s** (aleatorio, no fijo —
la regularidad se nota y se vuelve mecánica).

**`thinking` (ciclo sugerido, en orden, volviendo al principio):**
1. Ojos arriba-izquierda (recordando)
2. Ojos entrecerrados (concentración)
3. Mirada al usuario, parpadeo
4. Ojos arriba-derecha (buscando)
5. Parpadeo largo y vuelta a 1

**`tool` (distinto de `thinking` — esto es clave):**
1. Ojos entrecerrados + micro-movimiento lateral (leyendo)
2. Un ojo apenas más cerrado que el otro (foco)
3. Ceja/tensión arriba
4. Parpadeo rápido doble

Si querés ir más fino, el `label` dice qué tool es. **No lo parsees como enum** (puede
cambiar), pero si querés podés buscar palabras: si contiene "terminal" → gesto de tecleo;
"web"/"busc" → gesto de leer; "archivo" → gesto de escribir. **Si no matchea, usá el ciclo
genérico.** Nunca dejes la cara sin gesto por no entender el label.

### Duración adaptativa
El campo **`elapsed`** dice cuánto lleva en el estado actual (segundos, float).
Usalo para escalar:
- `thinking` con `elapsed` > 20 s → sumá el gesto de "uf, está tardando" (mirada al
  usuario, párpado bajo) al ciclo. Comunica paciencia, no error.
- `tool` con `elapsed` > 30 s → un microsuspiro cada tanto.

**No** uses `elapsed` para cambiar de estado vos: el estado lo define el bridge.

---

## Cuándo Sofía "piensa mucho" (y cómo se ve)

Para que la placa no se sienta trabada en trabajos largos:

| Situación real | `state` | `phase` | Cara |
|---|---|---|---|
| Leyendo tu mensaje | `thinking` | `busy` | ojos arriba, atenta |
| Procesando la respuesta | `thinking` | `busy` | ciclo de concentración |
| Corriendo un comando | `tool` | `busy` | gesto de tecleo/lectura |
| Trabajo largo (>20 s) | `thinking` | `busy` | ciclo + "tardando" |
| Terminó | `done` | `ok` | alivio/satisfacción + ✓ |
| Te contestó y sigue libre | `idle` | `idle` | respirando, HUD vacío |
| Nada hace 3 min | `bored` | `idle` | escalera de aburrimiento |

**El caso importante:** `done` sólo aparece en `/buddy/ask`. Cuando le hablás por el
Desktop o por Telegram, el paso es **`thinking`/`tool` → `idle`**, sin `done`. La señal de
"ya te contesté" en esos casos es **el HUD pasando de `busy` a `idle`** (los puntitos
desaparecen). Por eso el HUD importa tanto: sin él, `idle` y `thinking` se confunden.

Entonces, **si querés que aparezca el tilde también en Desktop/Telegram**, la regla es:
cuando `phase` pase de **`busy` a `idle`**, mostrá el tilde **3 segundos** por tu cuenta
y después limpialo. Eso lo sintetiza la placa, no el bridge. **Sí, hacelo: es la mejora
más grande para "que quede claro cuando ya respondió".**

---

## El LED RGB: estado por color

Pedido de Maxi: el color del LED tiene que cambiar según lo que está pasando, para saberlo
desde lejos sin mirar la pantalla.

**El bridge ya manda el color resuelto en el campo `led`** (`[r,g,b]`), así que no tenés
que traducir nada. Si querés cambiar un color, se cambia en el servidor en un solo lugar.

| `phase` | Color | RGB | Modo | Significado |
|---|---|---|---|---|
| `busy` | **amarillo** | `[255, 190, 0]` | fijo | trabajando |
| `ok` | **verde** | `[0, 255, 80]` | **titila 5 s**, después celeste | ya te contesté |
| `ask` | **rojo** | `[255, 40, 40]` | pulso lento (1.5 s) | necesito algo tuyo |
| `idle` | **celeste** | `[0, 180, 255]` | fijo, brillo bajo | standby |

### El celeste es el color de los ojos

Maxi pidió explícitamente que el **celeste del LED coincida con el color de los ojos** en
reposo. Usá la misma paleta en ambos lados: si los ojos están en celeste, el LED también.
Eso hace que el chiche se lea como una sola cosa, no como una pantalla con una luz aparte.

### Comportamiento del titileo verde (importante)

```
busy (amarillo fijo)
   ↓  terminé
ok (verde) → TITILA 5 segundos → idle (celeste fijo)
```

Reglas:
1. **Duración exacta: 5 s.** No lo hagas depender del `elapsed` del bridge.
2. **Titileo:** on/off a ~2 Hz (250 ms encendido, 250 ms apagado) durante esos 5 s.
   Que sea visible de reojo pero no agresivo: bajá el brillo al 60% en el titileo.
3. **A los 5 s, transición suave a celeste** (fade de ~300 ms, no un corte seco).
4. **Si llega actividad nueva antes de los 5 s** (volvés a hablarle), cortá el titileo y
   pasá a amarillo de inmediato. El trabajo manda sobre la celebración.

### ⚠️ En Desktop y Telegram no llega `ok`

Igual que con el tilde: `done`/`ok` **sólo existe en `/buddy/ask`**. Cuando Maxi le habla
por el Desktop o Telegram, el paso es `busy` → `idle` directo.

**Entonces la placa tiene que sintetizar el verde:** cuando `phase` pase de **`busy` a
`idle`**, hacé el titileo verde de 5 s y después quedate en celeste. Eso es lo que va a
pasar el 95% de las veces, así que **esto es lo que más vas a ver y lo más importante de
implementar bien**.

### Cómo distinguir `ok` de `idle`

- `ok` = el bridge dice que terminé (dura 8 s, `done`).
- Transición `busy → idle` sin `ok` = terminé en Desktop/Telegram.
- `idle` que ya venía de antes = no pasó nada nuevo, **no titiles**. Si titilás cada vez
  que el poll devuelve `idle`, el LED titila para siempre y pierde todo el sentido.

**Guardá el `phase` anterior.** El titileo es una **transición**, no un estado.

---

## Advertencia sobre `ask` (el `!` rojo)

Hoy `waiting` **sólo se detecta en `/buddy/ask`** (cuando te pido aprobación en ese run).
Si me pedís aprobación mientras hablás por **Desktop o Telegram**, el bridge **no se enterá**
y el `!` no aparece. No es un bug del firmware: no hay forma de leerlo desde la DB.

Así que el `!` va a ser **poco frecuente al principio**. Implementalo igual, pero no
dependas de él para nada crítico.

---

## Resumen del contrato actualizado

```json
GET /state  (header X-Buddy-Token)
{
  "state":   "thinking",        // idle|bored|thinking|tool|waiting|error|done
  "label":   "usando la terminal",  // informativo, NO enum
  "phase":   "busy",            // idle|busy|ok|ask  <- USÁ ESTO PARA EL HUD
  "led":     [255,190,0],       // color RGB ya resuelto para el LED
  "elapsed": 12.4,              // segundos en el estado actual
  "active":  1,                 // 1/0, -1 si no pudo leer
  "run_id":  null,              // != null sólo con /buddy/ask
  "ts":      1794872283.43,
  "since":   1794872270.91
}
```

Side-effect: `since` va incluido; `elapsed` es lo mismo ya calculado, por comodidad.

---

## Prioridad sugerida

Si tenés que elegir el orden:

1. **LED por color + titileo verde.** Es lo que se ve desde lejos y no depende de la cara.
   Con `led` ya resuelto es lo más barato de implementar y lo de mayor impacto.
2. **HUD completo** (`phase` + puntitos + tilde + !) y **tilde sintetizado** en la placa
   al pasar de `busy` a `idle`.
3. **Ojos verdes** (tarea 1 del doc original) — el test de los 3 rectángulos.
4. **Rotación de gestos** con `elapsed`.
5. **IMU** y escalera de aburrimiento.

Lo de arriba de todo es lo que hace que el chiche se sienta "vivo y comunicativo";
lo de abajo es lo que lo hace adorable.
