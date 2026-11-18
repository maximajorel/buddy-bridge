"""Traducción pura de actividad a etiqueta de cara.

Sin estado, sin I/O: entra un texto de la DB, sale un string de <=40 caracteres
que la placa dibuja tal cual. Se puede probar sin servidor ni base, igual que las
políticas puras del firmware.
"""

LABEL_MAX = 40

# Etiquetas por tool. Es lo único que se muestra en pantalla: nunca datos del
# contenido (comandos, rutas, prompts), solo qué herramienta está usando.
TOOL_LABELS = {
    "web_search": "buscando en la web",
    "web_extract": "leyendo una página",
    "terminal": "usando la terminal",
    "read_file": "leyendo un archivo",
    "write_file": "escribiendo un archivo",
    "patch": "editando código",
    "search_files": "buscando archivos",
    "execute_code": "corriendo código",
    "vision_analyze": "mirando una imagen",
    "delegate_task": "delegando",
    "todo_list": "ordenando pendientes",
    "skill_view": "consultando una skill",
    "skill_manage": "actualizando una skill",
    "memory": "recordando algo",
    "cronjob_manage": "agendando una tarea",
    "_thinking": "pensando",
}

# Descripción de actividad de la DB -> etiqueta. El orden importa: se evalúa tal cual.
DESC_LABELS = {
    "starting new turn": "escuchándote",
    "thinking": "pensando",
    "responding": "respondiendo",
    "waiting for input": "te espera",
    "waiting": "te espera",
    "tool": "usando una herramienta",
    "streaming": "respondiendo",
    "compressing": "ordenando ideas",
}

FALLBACK_LABEL = "trabajando"
IDLE_LABEL = "acá estoy"
GENERIC_TOOL_LABEL = "usando una herramienta"


def clip(label: str) -> str:
    """Corta a LABEL_MAX. La placa reserva 40 columnas y no filtra por su cuenta."""
    return (label or "")[:LABEL_MAX]


def tool_label(tool_name: str | None) -> str:
    """Etiqueta para una tool concreta; genérica si no está en la tabla."""
    if not tool_name:
        return GENERIC_TOOL_LABEL
    return TOOL_LABELS.get(tool_name, GENERIC_TOOL_LABEL)


def label_for_tool_event(desc: str) -> str:
    """Etiqueta para una línea `tool started/completed: <tool> (<tiempo>)`."""
    d = desc or ""
    for tool, label in TOOL_LABELS.items():
        if tool in d:
            return label
    return GENERIC_TOOL_LABEL


def label_for_desc(desc: str | None) -> str:
    """Traduce la descripción de la DB a una etiqueta de cara, sin filtrar contenido."""
    d = (desc or "").strip().lower()
    if not d:
        return FALLBACK_LABEL
    if "tool completed" in d:
        # Después de una tool respondida se vuelve a pensar: la tool ya terminó.
        return "pensando"
    if "tool started" in d:
        return label_for_tool_event(d)
    for key, label in DESC_LABELS.items():
        if key in d:
            return label
    # Sin la marca "tool started/completed" pero con una tool nombrada: se muestra
    # la tool, que es información más útil que la genérica.
    for tool, label in TOOL_LABELS.items():
        if tool in d:
            return label
    if "tool" in d:
        return GENERIC_TOOL_LABEL
    return FALLBACK_LABEL
