"""Guardar las credenciales de Google desde la web, sin romper el `.env`.

El problema real no es el POST: es que `config.py` lee el `.env` al arrancar y
`Config` es inmutable. Si la escritura queda a medias (corte de luz, disco lleno,
un carácter de más), el próximo restart del bridge no levanta y la placa se queda
con la cara de "sin configurar". Por eso:

1. Se valida TODO antes de tocar el disco.
2. Se escribe un `.env.tmp` en el mismo directorio y se hace `os.replace`, que es
   atómico en el mismo filesystem: o está el `.env` viejo entero, o el nuevo
   entero. Nunca uno partido.
3. Se conservan las líneas que no son de Google, en su orden, con sus
   comentarios. El `.env` es del dueño, no nuestro.
4. El servicio toma la config nueva sin reiniciar: el objeto `Config` se
   reemplaza y el cliente de calendario se rearma.

Las credenciales NUNCA se devuelven por HTTP. `GET` dice si están puestas y
cuál es el calendario; el valor del secreto no sale del proceso.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from .config import DEFAULT_ENV_FILE

# Claves que administra este módulo. Cualquier otra línea del `.env` se respeta.
MANAGED_KEYS = (
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "GOOGLE_REFRESH_TOKEN",
    "GOOGLE_CALENDAR_ID",
    "GOOGLE_CALENDAR_LEAD_MIN",
    "GOOGLE_CALENDAR_GRACE_MIN",
)


class ConfigWriteError(Exception):
    """No se pudo validar o escribir el `.env`. Nada quedó a medias."""


def _clean(value) -> str:
    """Un valor de formulario, sin espacios ni comillas que rompan el parseo."""
    text = str(value if value is not None else "").strip()
    if any(c in text for c in "\r\n\x00"):
        raise ConfigWriteError("un valor tiene saltos de línea")
    return text.strip('"').strip("'")


def clean_value(value) -> str:
    """Sanea un valor suelto (ej. las credenciales del OAuth) sin validar el trio."""
    return _clean(value)


def validate(values: dict, current: dict | None = None) -> dict:
    """Valida y normaliza lo que vino del formulario.

    `current` trae lo ya guardado: los secretos vacíos en el POST significan
    "dejá el que estaba", que es lo que quierés cuando cambiás solo el nombre del
    calendario y el navegador no te repite el `client_secret`.
    """
    current = current or {}
    out = {}
    for key in MANAGED_KEYS:
        raw = _clean(values.get(key, ""))
        if raw:
            out[key] = raw
        elif current.get(key):
            out[key] = current[key]

    # Los tres campos de Google van juntos: dos de tres es la peor combinación
    # posible, porque el bridge arranca, intenta y falla cada minuto.
    trio = [out.get("GOOGLE_CLIENT_ID"), out.get("GOOGLE_CLIENT_SECRET"),
            out.get("GOOGLE_REFRESH_TOKEN")]
    present = sum(1 for v in trio if v)
    if present and present < 3:
        missing = [k for k, v in zip(
            ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN"), trio) if not v]
        raise ConfigWriteError("faltan credenciales: " + ", ".join(missing))

    out["GOOGLE_CALENDAR_ID"] = out.get("GOOGLE_CALENDAR_ID") or "primary"

    for key, default, low, high in (
            ("GOOGLE_CALENDAR_LEAD_MIN", 60, 1, 720),
            ("GOOGLE_CALENDAR_GRACE_MIN", 5, 0, 120)):
        raw = out.get(key) or str(default)
        try:
            value = int(raw)
        except ValueError:
            raise ConfigWriteError(f"{key} tiene que ser un número entero") from None
        if not (low <= value <= high):
            raise ConfigWriteError(f"{key} tiene que estar entre {low} y {high}")
        out[key] = str(value)

    if out.get("GOOGLE_CLIENT_ID", "").startswith("GOOGLE_CLIENT_ID"):
        raise ConfigWriteError("parece que pegaste el nombre de la variable en vez del valor")
    return out


def strip_env_text(existing: str) -> str:
    """Borra las claves administradas del texto del `.env`.

    Es el `clear` del panel: apagar el calendario tiene que dejar el archivo
    limpio de credenciales, no con valores vacíos que parezcan configurados.
    Las líneas de otras variables quedan intactas.
    """
    out = []
    for line in existing.splitlines() if existing else []:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key.startswith("export "):
                key = key[len("export "):].strip()
            if key in MANAGED_KEYS:
                continue
        out.append(line)
    # Se va también el bloque comentado que agrega este módulo, si quedó vacío.
    while out and out[-1].strip() == "":
        out.pop()
    lines = [l for l in out
             if l.strip() != "# Google Calendar (configurado desde la web del buddy)"]
    return "\n".join(lines).rstrip("\n") + ("\n" if lines else "")


def merge_env_text(existing: str, values: dict) -> str:
    """Reescribe el `.env` cambiando solo las claves administradas.

    Pura: entra texto, sale texto. Las claves nuevas se agregan al final en un
    bloque comentado; las viejas se reemplazan en su lugar para que el archivo
    siga siendo legible con el mismo orden que tenía.
    """
    lines = existing.splitlines() if existing else []
    pending = dict(values)
    out = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key.startswith("export "):
                key = key[len("export "):].strip()
            if key in pending:
                out.append(f"{key}={pending.pop(key)}")
                continue
        out.append(line)

    if pending:
        if out and out[-1].strip():
            out.append("")
        out.append("# Google Calendar (configurado desde la web del buddy)")
        for key in MANAGED_KEYS:
            if key in pending:
                out.append(f"{key}={pending.pop(key)}")
    return "\n".join(out).rstrip("\n") + "\n"


def write_env(path: Path, values: dict) -> None:
    """Escribe el `.env` de forma atómica: temp en el mismo dir + `os.replace`.

    El `fsync` antes del rename no es ceremonia: sin él el rename puede quedar en
    el page cache y un corte de luz deja el archivo vacío justo cuando el
    servicio lo va a leer.
    """
    path = Path(path)
    text = merge_env_text(path.read_text(encoding="utf-8") if path.exists() else "", values)
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(directory), prefix=".env.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        # 0600: el archivo tiene el `client_secret` y el `refresh_token`.
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        # Si algo falló, el `.env` viejo sigue intacto y el temp se borra.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# Página mínima y autónoma: sin CDN, sin build, sin JS de terceros. El panel del
# ESP32 linkea acá y el POST lo hace esta página contra el bridge (la placa no
# guarda el `client_secret`). El token de ask se pide en el formulario y se
# guarda en `sessionStorage`, no en la URL.
ADMIN_HTML = """<!doctype html>
<html lang="es">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Calendario del buddy</title>
<style>
:root { color-scheme: light dark; }
body { font: 16px/1.5 system-ui, sans-serif; max-width: 34rem; margin: 2rem auto;
       padding: 0 1rem; background: #111; color: #eee; }
h1 { font-size: 1.3rem; margin: 0 0 .3rem; }
p.hint { color: #9aa; font-size: .85rem; margin: 0 0 1.2rem; }
label { display: block; margin: 1rem 0 .3rem; font-size: .9rem; color: #cdd; }
input { width: 100%; box-sizing: border-box; padding: .55rem; border-radius: 8px;
        border: 1px solid #333; background: #1b1b1b; color: #eee; font-size: .9rem; }
input[type=number] { width: 6rem; }
button { margin-top: 1.2rem; padding: .6rem 1.1rem; border-radius: 8px; border: 0;
         background: #6b4bd6; color: #fff; font-size: .95rem; cursor: pointer; }
button.ghost { background: transparent; border: 1px solid #444; color: #bbb; }
#msg { margin-top: 1rem; font-size: .9rem; min-height: 1.2rem; }
.ok { color: #6e6; } .err { color: #f77; }
.state { padding: .6rem .8rem; border-radius: 8px; background: #1b1b1b;
         font-size: .85rem; margin-bottom: 1rem; }
</style>
<h1>Calendario del buddy</h1>
<p class="hint">Las credenciales quedan en el bridge, no en la placa. La placa solo dibuja.</p>
<div class="state" id="state">leyendo estado…</div>
<form id="f">
  <label for="token">Token del bridge (BUDDY_ASK_TOKEN)</label>
  <input id="token" type="password" autocomplete="off" placeholder="el token de configuración">
  <label for="client_id">GOOGLE_CLIENT_ID</label>
  <input id="client_id" autocomplete="off" placeholder="1234-abc.apps.googleusercontent.com">
  <label for="client_secret">GOOGLE_CLIENT_SECRET</label>
  <input id="client_secret" type="password" autocomplete="off" placeholder="GOCSPX-…">
  <label for="refresh_token">GOOGLE_REFRESH_TOKEN</label>
  <input id="refresh_token" type="password" autocomplete="off" placeholder="1//0g…">
  <label for="calendar_id">Calendario</label>
  <input id="calendar_id" placeholder="primary" value="primary">
  <label for="lead_min">Avisar con (minutos de anticipación)</label>
  <input id="lead_min" type="number" min="1" max="720" value="60">
  <label for="grace_min">Seguir mostrando el evento (minutos después de empezar)</label>
  <input id="grace_min" type="number" min="0" max="120" value="5">
  <button type="submit">Guardar</button>
  <button type="button" class="ghost" id="clear">Borrar credenciales</button>
</form>
<div id="msg"></div>
<script>
const $ = id => document.getElementById(id);
const msg = (t, cls) => { const m = $('msg'); m.textContent = t; m.className = cls || ''; };
const token = () => sessionStorage.getItem('buddy_ask') || $('token').value.trim();

async function load() {
  const t = token();
  const r = await fetch('calendar', { headers: t ? {Authorization: 'Bearer ' + t} : {} });
  if (r.status === 401) { $('state').textContent = 'Poné el token y guardá.'; return; }
  const s = await r.json();
  $('calendar_id').value = s.calendar_id;
  $('lead_min').value = s.lead_min;
  $('grace_min').value = s.grace_min;
  const bits = [s.configured ? '✅ configurado' : '⚠️ sin credenciales',
                'calendario: ' + s.calendar_id,
                'aviso: ' + s.lead_min + ' min'];
  if (s.next) bits.push('próximo: ' + s.next.summary + ' en ' + s.next.in_min + ' min');
  $('state').textContent = bits.join(' · ');
  if (s.has_client_secret) {
    $('client_secret').placeholder = 'ya guardado (dejalo vacío para conservarlo)';
    $('refresh_token').placeholder = 'ya guardado (dejalo vacío para conservarlo)';
  }
}
$('f').addEventListener('submit', async e => {
  e.preventDefault();
  const t = $('token').value.trim();
  if (t) sessionStorage.setItem('buddy_ask', t);
  msg('guardando…');
  const r = await fetch('calendar', {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token()},
    body: JSON.stringify({
      GOOGLE_CLIENT_ID: $('client_id').value,
      GOOGLE_CLIENT_SECRET: $('client_secret').value,
      GOOGLE_REFRESH_TOKEN: $('refresh_token').value,
      GOOGLE_CALENDAR_ID: $('calendar_id').value,
      GOOGLE_CALENDAR_LEAD_MIN: $('lead_min').value,
      GOOGLE_CALENDAR_GRACE_MIN: $('grace_min').value,
    }),
  });
  const body = await r.json().catch(() => ({}));
  if (r.ok) { msg('guardado ✓', 'ok'); $('client_secret').value = ''; $('refresh_token').value = ''; load(); }
  else msg(body.error || ('error ' + r.status), 'err');
});
$('clear').addEventListener('click', async () => {
  if (!confirm('¿Borrar las credenciales de Google del bridge?')) return;
  const r = await fetch('calendar', {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token()},
    body: JSON.stringify({clear: true}),
  });
  const body = await r.json().catch(() => ({}));
  if (r.ok) { msg('credenciales borradas', 'ok'); load(); }
  else msg(body.error || ('error ' + r.status), 'err');
});
load();
</script>
</html>
"""


def env_values() -> dict:
    """Los valores crudos del `.env`, para el estado de `/calendar`."""
    return read_env_map(DEFAULT_ENV_FILE)


def read_env_map(path: Path) -> dict:
    """El `.env` como dict. Reusa el parser del bridge: una sola gramática."""
    path = Path(path)
    if not path.exists():
        return {}
    from .config import parse_env_text
    return parse_env_text(path.read_text(encoding="utf-8"))


def current_values(env: dict) -> dict:
    """Lo ya guardado, para devolver estado (sin secretos) y para no borrarlos."""
    return {k: env.get(k, "") for k in MANAGED_KEYS if env.get(k)}


def help_payload() -> dict:
    """Los pasos para obtener las credenciales, servidos como JSON.

    Vive en el código y no en el HTML para que la página y cualquier cliente
    (la placa, un curl) lean la misma lista.
    """
    return {
        "steps": [
            "Entrá a console.cloud.google.com y creá (o elegí) un proyecto.",
            "Habilitá la 'Google Calendar API' en APIs y servicios.",
            "En Credenciales, creá un ID de cliente OAuth de tipo 'App de escritorio'.",
            "Copiá el client_id y el client_secret acá abajo.",
            "Para el refresh_token, corré una vez en tu compu: "
            "python3 scripts/google_oauth.py (te da la URL, pegás el código).",
        ],
        "managed_keys": list(MANAGED_KEYS),
    }


def public_status(env: dict) -> dict:
    """Lo que el panel puede ver: presencia, no valores.

    `has_client_secret` es un booleano igual que el secreto es no nulo. La placa
    muestra "configurado" sin poder leer la credencial.
    """
    has = {k: bool(env.get(k)) for k in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET",
                                         "GOOGLE_REFRESH_TOKEN")}
    return {
        "configured": all(has.values()),
        "has_client_id": has["GOOGLE_CLIENT_ID"],
        "has_client_secret": has["GOOGLE_CLIENT_SECRET"],
        "has_refresh_token": has["GOOGLE_REFRESH_TOKEN"],
        "calendar_id": env.get("GOOGLE_CALENDAR_ID") or "primary",
        "lead_min": int(env.get("GOOGLE_CALENDAR_LEAD_MIN") or 60),
        "grace_min": int(env.get("GOOGLE_CALENDAR_GRACE_MIN") or 5),
    }


def clear_env(path: Path) -> None:
    """Saca las credenciales del `.env`, atómico como `write_env`."""
    path = Path(path)
    if not path.exists():
        return
    text = strip_env_text(path.read_text(encoding="utf-8"))
    directory = path.parent
    fd, tmp = tempfile.mkstemp(dir=str(directory), prefix=".env.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
