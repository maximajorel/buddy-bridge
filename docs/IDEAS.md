# IDEAS.md — personalidad y expresividad

Sesgo de este backlog: **que el chiche se sienta vivo**, no que sea una herramienta más.
Todo lo de acá es local al firmware (o casi): no requiere tocar Hermes.

Método: cada idea lleva **impacto** (cuánto se nota), **esfuerzo** (cuánto cuesta) y
**estado** — para que nadie implemente algo creyendo que la señal existe cuando no.

---

## Fase 1 — Lo que hace la diferencia entre "pantalla" y "bicho"

### 1. Microsaccades (el #1 absoluto)

Saltos de mirada de 1–3 px cada 2–5 s mientras está `idle`. Ninguna otra cosa hace tanto
con tan poco: unos ojos perfectamente quietos se leen como imagen, unos ojos que derivan
se leen como alguien. **Impacto: altísimo · Esfuerzo: ~20 líneas.**

### 2. Parpadeo con distribución no uniforme

No un timer fijo. Intervalo aleatorio 2–6 s, con **ráfagas** (a veces dos seguidos, a veces
uno largo). El parpadeo regular delata la animación. **Impacto: alto · Esfuerzo: bajo.**

### 3. Respiración idle

Ciclo lento de 4–5 s que mueve los ojos 1 px arriba/abajo, como pecho que sube y baja.
Invisible si mirás de cerca, obvio si mirás dos minutos. **Impacto: alto · Esfuerzo: bajo.**

### 4. Transición `busy → idle` sintetizada en la placa

**La más importante funcionalmente.** `done`/`ok` solo llega por `/buddy/ask`; cuando el
humano habla por Desktop o Telegram, el paso es `busy → idle` directo. La placa tiene que
**fabricar** el tilde y el titileo verde al detectar esa transición. Es el 95% de los casos.

⚠️ **Guardá el `phase` anterior.** Si titilás cada vez que el poll devuelve `idle`, el LED
titila para siempre y pierde todo el sentido: el titileo es una **transición**, no un estado.

---

## Fase 2 — Cuerpo: el IMU (es lo que más sorprende a quien lo agarra)

### 5. Sacudida → mareo, seguida sacudida → fastidio

|a| > 2.5 g sostenido ~300 ms → ojos en espiral, párpados caídos, bamboleo 2–3 s y se
recupera solo. **Si insistís**, no vuelve al mareo: pasa a **fastidio** (ojos apretados).
Que reaccione distinto a la repetición es lo que lo hace parecer que tiene voluntad.
**Impacto: altísimo (es la demo que hacés cuando viene alguien) · Esfuerzo: medio.**

### 6. Inclinación → los ojos compensan la gravedad

Con el acelerómetro: las pupilas se van hacia "abajo" real. Si lo apoyás torcido en un
soporte, los ojos se acomodan. Es la mejora que más se nota y la que menos se explica sola.
**Impacto: alto · Esfuerzo: bajo (una lectura de IMU + offset).**

### 7. Lo levantás → se despierta

Cambio sostenido en Z (de apoyado a en mano) → apertura de ojos en 100 ms desde dormido.
Inverso de la escalera de aburrimiento. **Impacto: alto · Esfuerzo: bajo.**

### 8. Boca abajo → "no veo nada"

Z negativo → párpados apretados. Barato y gracioso. **Impacto: medio · Esfuerzo: bajo.**

### 9. Movimiento repetido de un lado → te sigue un instante

Alguien pasa caminando y las pupilas lo acompañan. **Impacto: medio-alto · Esfuerzo: medio**
(el ruido del IMU hace difícil separar "alguien pasó" de "lo movieron a él"). Requiere
calibración real; no lo des por hecho hasta probarlo en la mesa.

### 10. Sacudida rítmica suave → le gusta

Amplitud baja, frecuencia pareja → ojos entrecerrados de gusto. Contraste cómico con el
mareo. **Impacto: medio · Esfuerzo: bajo.**

---

## Fase 3 — Personalidad acumulada

### 11. Escalera de aburrimiento (ya especificada, falta implementar)

`bored` a los 180 s. 0–20 s neutral · 20–60 s mirada que se va y vuelve · 60–120 s cabeceo ·
120 s+ ojos cerrados, respiración lenta. **Cualquier actividad lo despierta de golpe.**

### 12. Memoria de corto plazo del humor

Si lo sacudiste 3 veces seguidas, arranca el próximo `idle` ya de mal humor y tarda más en
recuperar la neutralidad. No hay persistencia en flash: se pierde al apagar. Barato y da
muchísima sensación de ser alguien. **Impacto: alto · Esfuerzo: bajo.**

### 13. Gestos que no se repiten en la misma sesión

Rotación de 3–6 s entre gestos mientras el `state` no cambia (`thinking` tiene su ciclo,
`tool` otro distinto — leer no es teclear). Un gesto congelado 40 s se lee como "se colgó".

### 14. `elapsed` como paciencia

`thinking` > 20 s → suma un "uf, está tardando" (mirada al usuario, párpado bajo) al ciclo.
Comunica paciencia, no error. `tool` > 30 s → microsuspiro cada tanto.
**El estado lo define el bridge; la paciencia la define la placa.**

### 15. Sorpresa al arrancar una tool inesperada

Ojos muy abiertos 200 ms y vuelta. Detectable por salto `idle → tool` sin `thinking` previo.

### 16. Soñar dormido

Tras 10 min en `bored` profundo: REM falso. Movimiento ocular rápido bajo párpado cerrado,
cada 20-40 s, con micro-twitch. **Impacto: alto (la gente lo mira esperando que pase algo)
· Esfuerzo: bajo.** Es el tipo de detalle que hace que alguien lo deje prendido.

---

## Fase 4 — Cuando la cara ya está bien

### 17. Reloj de estado sin texto

No mostrar la hora. Mostrar **cuánto hace que terminó**: una rayita que crece. Nada de texto
en una pantalla que ve la casa entera.

### 18. Modo "no molestar" con el botón

Botón largo (3 s) → dormir de verdad: sin poll, LED apagado, pantalla en negro.
Botón corto → despertar. Con un solo botón, esa es la distinción que necesitás.

### 19. Multi-agente

Hoy el bridge refleja **un** perfil. Si algún día hay varios agentes, cada uno con su cara y
una transición entre ellas cuando toma el control. Idea linda pero **requiere tocar el
bridge**: no es solo firmware.

---

## Fase 5 — Exploraciones (una sola, y honesta)

### 20. Voz sin micrófono ni parlante

No hay hardware. La única vía sería un **buzzer** por PWM (tonos, no voz) o un I2S externo.
Con el hardware actual: no. Lo dejo escrito para que nadie lo intente dos veces.

---

## Lo que NO está verificado (no implementar esperando que llegue)

### El `!` rojo / `ask`

**Estado real: no aparece en la práctica.** Las aprobaciones y los `clarify` viven en memoria
del proceso del gateway, no en la state DB. El bridge no las ve. `has_blocking_approval()`
existe pero no es importable desde otro proceso.

Vías posibles, **ninguna implementada ni probada**:
1. Un hook de approvals que escriba un flag en disco y el bridge lo lea (la menos invasiva).
2. Un endpoint nuevo en el gateway (se pierde en cada update).
3. Aceptar la limitación y hacer el `!` cosmético.

Implementá el `!` igual —el código tiene que estar— pero **no dependas de él** para nada
crítico, y no prometas que "ahora sí va a andar" sin verificar el canal de punta a punta.

---

## Prioridad recomendada

Si hay que elegir el orden:

1. **LED por color + titileo verde sintetizado.** Se ve desde lejos, no depende de la cara,
   y con `led` ya resuelto es lo más barato de implementar.
2. **HUD completo** (`phase` → puntitos / tilde / `!`) + tilde sintetizado en `busy → idle`.
3. **Microsaccades + parpadeo irregular + respiración.** El salto de "imagen" a "bicho".
4. **IMU: inclinación + despertar al levantarlo.**
5. **Escalera de aburrimiento + soñar dormido.**
6. Sacudida → mareo → fastidio.

Lo de arriba hace que el chiche **comunique**; lo de abajo, que **tenga alma**.
