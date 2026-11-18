# Qué poner acá

Esta carpeta es el firmware de la placa (PlatformIO / ESP-IDF / Arduino).
El código todavía no está: se arma sobre el esqueleto de `docs/buddy-tareas-firmware.md`.

Lo único que el firmware necesita saber del servidor está en el README, sección
**Contrato de la API**. Resumen:

- `GET /state` cada 1 s con `X-Buddy-Token`
- leer `phase` (`idle|busy|ok|ask`), `led`, `elapsed`, `active`
- `state` desconocido → tratar como `idle`; `active == -1` → cara neutra
- **no** parsear `label` como enum

Nunca metas el `API_SERVER_KEY` de Hermes en `platformio.ini`, en `secrets.h` ni en
ningún lado del firmware. El token del buddy es otro y solo abre `/state`.

## Archivos de secrets

Si vas a usar `secrets.h` con `BUDDY_TOKEN` y `WIFI_PASS`, agregalo a `.gitignore`:

```
firmware/include/secrets.h
firmware/.env
.pio/
build/
```
