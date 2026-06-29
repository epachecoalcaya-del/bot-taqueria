"""
main.py — Agendify Pedidos
Bot SaaS multi-tenant de toma de pedidos por WhatsApp.
"""
import os
import re
import json
import html
import smtplib
import requests
import datetime
import time
import threading
from email.mime.text import MIMEText
from typing import Optional
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, PlainTextResponse
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage
from langchain.tools import tool
import database as db
import geo
import pagos

load_dotenv()

app = FastAPI(title="Agendify Pedidos - Bot de Taqueria SaaS")

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "agendify_pedidos")
MAX_HISTORIAL = 10
TZ_MX = datetime.timezone(datetime.timedelta(hours=-6))

# Cache de negocios en memoria (se carga al arrancar)
_negocios_cache: dict = {}   # phone_number_id -> dict negocio
_menu_cache:     dict = {}   # negocio_id -> list de items

# Idempotencia: set de message_ids de WhatsApp ya procesados.
# Meta reenvía el mismo webhook varias veces (hasta 3-4 intentos) cuando
# el servidor tarda en responder, lo que causaba procesamiento doble:
# doble agregado de productos, doble confirmación, salsa duplicada, etc.
# Guardamos los últimos 500 IDs en memoria (circulares) — suficiente para
# cubrir la ventana de reintentos de Meta (~30 segundos) sin consumir RAM.
_WAMID_MAX = 500
_wamid_procesados: set   = set()
_wamid_orden:      list  = []   # para mantener tamaño máximo (FIFO)
_wamid_lock = threading.Lock()

def _ya_procesado(wamid: str) -> bool:
    """Devuelve True si este message_id ya fue procesado (duplicado de Meta)."""
    with _wamid_lock:
        return wamid in _wamid_procesados

def _marcar_procesado(wamid: str):
    """Registra el message_id como procesado. Descarta los más viejos si supera el límite."""
    with _wamid_lock:
        if wamid in _wamid_procesados:
            return
        _wamid_procesados.add(wamid)
        _wamid_orden.append(wamid)
        if len(_wamid_orden) > _WAMID_MAX:
            viejo = _wamid_orden.pop(0)
            _wamid_procesados.discard(viejo)


def _cargar_negocios():
    global _negocios_cache, _menu_cache
    negocios = db.cargar_negocios()
    for n in negocios:
        pid = n["phone_number_id"]
        _negocios_cache[pid] = n
        _menu_cache[n["id"]] = db.cargar_menu(n["id"])
    print(f"[Startup] {len(_negocios_cache)} negocio(s) cargado(s).")


@app.on_event("startup")
async def startup():
    _cargar_negocios()


@app.get("/health")
def health():
    return {"status": "ok"}


# ── HELPERS DE WHATSAPP ──────────────────────────────────────────────────────

def enviar_whatsapp(telefono: str, mensaje: str, token: str, phone_id: str,
                     intentos: int = 3) -> bool:
    """Envia un mensaje de WhatsApp. Reintenta hasta `intentos` veces con
    backoff simple si falla por un problema momentaneo de red o de la API
    (timeouts, 5xx). Errores claramente no-recuperables (401, 400 por
    numero invalido, etc.) no se reintentan, para no perder tiempo."""
    url = f"https://graph.facebook.com/v19.0/{phone_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": telefono,
        "type": "text",
        "text": {"body": mensaje},
    }
    ultimo_error = ""
    for intento in range(1, intentos + 1):
        try:
            r = requests.post(url, headers={"Authorization": f"Bearer {token}"},
                              json=payload, timeout=10)
            if r.ok:
                return True
            ultimo_error = f"{r.status_code} {r.text[:200]}"
            # 401 (token invalido/expirado) o 400 (numero invalido) no se
            # arreglan reintentando — fallamos rapido.
            if r.status_code in (400, 401):
                break
        except Exception as e:
            ultimo_error = str(e)
        if intento < intentos:
            time.sleep(1.5 * intento)  # backoff: 1.5s, 3s...

    print(f"!!! FALLO DEFINITIVO enviando WhatsApp a {telefono} tras {intento} intento(s): {ultimo_error}")
    return False


# ── HELPERS DE CORREO ────────────────────────────────────────────────────────

def _html_correo_pedido(negocio: str, nombre: str, telefono: str, items: list,
                         total: float, tipo: str, direccion: str, metodo_pago: str,
                         notas: str = "", extras: list = None) -> str:
    color = "#2563eb"
    emoji = "🛵" if tipo == "envio" else "🏪"
    tipo_texto = "Envío a domicilio" if tipo == "envio" else "Para recoger en local"
    filas = "".join(
        f"<tr><td style='padding:4px 8px'>{html.escape(i['nombre'])}</td>"
        f"<td style='padding:4px 8px;text-align:center'>{i['cantidad']}</td>"
        f"<td style='padding:4px 8px;text-align:right'>${i['precio']*i['cantidad']:.2f}</td></tr>"
        for i in items
    )
    if extras:
        filas += "".join(
            f"<tr><td style='padding:4px 8px;color:#2563eb'>+ {html.escape(e['ingrediente'])} extra "
            f"(en {html.escape(e['producto'])})</td>"
            f"<td style='padding:4px 8px;text-align:center'>{e.get('cantidad',1)}</td>"
            f"<td style='padding:4px 8px;text-align:right'>${15.00*e.get('cantidad',1):.2f}</td></tr>"
            for e in extras
        )
    dir_html = (
        f"<tr><td style='color:#6b7280;padding:4px 8px'>Dirección</td>"
        f"<td colspan='2' style='padding:4px 8px'><b>{html.escape(direccion)}</b></td></tr>"
        if direccion else ""
    )
    notas_html = (
        f"<tr><td style='color:#6b7280;padding:4px 8px'>Notas</td>"
        f"<td colspan='2' style='padding:4px 8px;color:#dc2626'><b>{html.escape(notas)}</b></td></tr>"
        if notas else ""
    )
    return f"""
    <div style='font-family:sans-serif;max-width:520px;margin:0 auto'>
      <div style='background:{color};color:#fff;padding:16px 20px;border-radius:8px 8px 0 0'>
        <b>{emoji} Nuevo pedido en {html.escape(negocio)}</b>
      </div>
      <div style='border:1px solid #e5e7eb;border-top:none;padding:16px 20px;border-radius:0 0 8px 8px'>
        <table style='width:100%;border-collapse:collapse;margin-bottom:12px'>
          <tr><td style='color:#6b7280;padding:4px 8px'>Cliente</td>
              <td colspan='2' style='padding:4px 8px'><b>{html.escape(nombre)}</b></td></tr>
          <tr><td style='color:#6b7280;padding:4px 8px'>Teléfono</td>
              <td colspan='2' style='padding:4px 8px'><b>{html.escape(telefono)}</b></td></tr>
          <tr><td style='color:#6b7280;padding:4px 8px'>Tipo</td>
              <td colspan='2' style='padding:4px 8px'><b>{tipo_texto}</b></td></tr>
          {dir_html}
          {notas_html}
          <tr><td style='color:#6b7280;padding:4px 8px'>Pago</td>
              <td colspan='2' style='padding:4px 8px'><b>{html.escape(metodo_pago or '—')}</b></td></tr>
        </table>
        <table style='width:100%;border-collapse:collapse;background:#f9fafb;border-radius:6px'>
          <tr style='background:#f3f4f6'>
            <th style='padding:6px 8px;text-align:left'>Producto</th>
            <th style='padding:6px 8px'>Cant.</th>
            <th style='padding:6px 8px;text-align:right'>Subtotal</th>
          </tr>
          {filas}
          <tr style='border-top:2px solid #e5e7eb'>
            <td colspan='2' style='padding:8px;font-weight:bold'>TOTAL</td>
            <td style='padding:8px;text-align:right;font-weight:bold;font-size:1.1em'>${total:.2f}</td>
          </tr>
        </table>
        <p style='font-size:11px;color:#9ca3af;margin-top:16px;text-align:center'>
          Enviado automáticamente por Agendify Pedidos.
        </p>
      </div>
    </div>"""


def _enviar_correo_worker(email: str, asunto: str, html_body: str):
    """Worker que corre en hilo separado — nunca bloquea la respuesta al cliente."""
    smtp_email = os.getenv("SMTP_EMAIL", "")
    smtp_pass  = os.getenv("SMTP_APP_PASSWORD", "")
    if not smtp_email or not smtp_pass:
        return
    try:
        msg = MIMEText(html_body, "html", "utf-8")
        msg["Subject"] = asunto
        msg["From"]    = smtp_email
        msg["To"]      = email
        # Timeout de 10s para no bloquear si el servidor no responde
        with smtplib.SMTP_SSL("smtp.gmail.com", 465,
                               context=None,
                               timeout=10) as s:
            s.login(smtp_email, smtp_pass)
            s.sendmail(smtp_email, [email], msg.as_string())
        print(f"   [Correo] Enviado a {email}: {asunto}")
    except Exception as e:
        print(f"!!! Error enviando correo: {e}")


def notificar_dueno(email: str, asunto: str, html_body: str):
    """Lanza el envio de correo en un hilo separado para no bloquear
    la respuesta al cliente aunque el SMTP falle o tarde."""
    if not email:
        return
    import threading
    threading.Thread(
        target=_enviar_correo_worker,
        args=(email, asunto, html_body),
        daemon=True,
    ).start()


# ── HELPERS DEL CARRITO ──────────────────────────────────────────────────────

# Promociones de tacos — Super Tacos George's (confirmadas con el dueño).
# Cada entrada define como se cobra un PAQUETE de N piezas a precio fijo;
# las piezas que sobran fuera del paquete se cobran a precio individual.
# Ej: Bistec 3x$60 -> si piden 7, son 2 paquetes de 3 ($120) + 1 individual
# ($25) = $145, no 7 x $25 = $175.
_PROMOS_TACOS = {
    "Taco de Pastor":            {"paquete": 2, "precio_paquete": 26.00},  # 2x1: paga 1 ($26), lleva 2
    "Taco de Bistec":            {"paquete": 3, "precio_paquete": 60.00},  # 3x$60
    "Taco de Sirloin":           {"paquete": 2, "precio_paquete": 50.00},  # 2x$50
    "Taco de Chorizo":           {"paquete": 3, "precio_paquete": 60.00},  # 3x$60 (corregido, antes $55)
    "Taco de Chorizo Argentino": {"paquete": 2, "precio_paquete": 50.00},  # 2x$50
    "Taco Campechano":           {"paquete": 3, "precio_paquete": 65.00},  # 3x$65
}

# Ingredientes extra: $15 cada uno, sin importar cual sea — confirmado por
# el dueño. Lista cerrada para validar contra alucinaciones del LLM (no
# debe inventar que algo es "extra" si no esta en esta lista real).
_VALOR_EXTRA = 15.00
_INGREDIENTES_EXTRA_VALIDOS = {
    "pastor", "bistec de res", "bistec", "sirloin", "chorizo",
    "queso oaxaca", "queso amarillo", "queso", "jamón", "jamon", "tocino",
    "cebolla", "champiñones", "champiñon", "pimiento", "piña",
    "orden de tortillas", "tortillas", "orden de cebolla asada",
    "cebolla asada", "aguacate", "jitomate",
}


def _precio_linea(nombre: str, cantidad: int, precio_individual: float) -> float:
    """Calcula el precio de una linea del carrito, aplicando la promo por
    paquete si el producto tiene una (ver _PROMOS_TACOS). Las piezas que
    no completan un paquete se cobran a precio individual normal."""
    promo = _PROMOS_TACOS.get(nombre)
    if not promo:
        return precio_individual * cantidad
    tam_paquete = promo["paquete"]
    precio_paquete = promo["precio_paquete"]
    paquetes_completos = cantidad // tam_paquete
    piezas_sueltas = cantidad % tam_paquete
    return (paquetes_completos * precio_paquete) + (piezas_sueltas * precio_individual)


def _extraer_pago_de_notas(notas_raw: str) -> tuple:
    """Extrae el metodo de pago guardado con el prefijo '[PAGO:X]' del
    campo de notas, si existe. Devuelve (metodo_pago, notas_limpias).
    Esta es la UNICA funcion que debe usarse para esto — antes habia
    lugares que hacian este parseo a mano y otros que se les olvidaba,
    lo que causaba que '[PAGO:Tarjeta]' se mostrara crudo al cliente.
    También limpia la marca interna 'SALSA: X' convirtiéndola en texto
    legible para la cocina ('Salsa: X') sin el prefijo en mayúsculas."""
    notas = notas_raw or ""
    # Limpiar marca SALSA: -> texto legible para cocina
    notas = re.sub(r'\bSALSA:\s*', 'Salsa: ', notas)
    if not notas or not notas.startswith("[PAGO:"):
        return "", notas.strip()
    cierre = notas.find("]")
    if cierre == -1:
        return "", notas.strip()
    metodo = notas[6:cierre]
    notas_limpias = notas[cierre + 1:].strip()
    return metodo, notas_limpias


def _calcular_total_extras(extras: list) -> float:
    """Suma el costo de todos los ingredientes extra ($15 c/u)."""
    return sum(_VALOR_EXTRA * e.get("cantidad", 1) for e in (extras or []))


def _calcular_total(carrito: list, extras: list = None) -> float:
    total_carrito = sum(_precio_linea(i["nombre"], i["cantidad"], i["precio"]) for i in carrito)
    return total_carrito + _calcular_total_extras(extras)


# Salsas REALES del negocio (confirmado con el dueño): solo roja y verde.
# "verde" y "guacamole" son la MISMA salsa (dos nombres para lo mismo).
_SALSAS_RECONOCIDAS = {
    "roja": "salsa roja",
    "verde": "salsa verde", "guacamole": "salsa verde",
}

# La PIÑA NO es salsa, es un complemento incluido (sin costo extra). Se
# reconoce aparte para anotarla como complemento, no como salsa. Si el
# cliente dice "verde y piña" quiere salsa verde + piña de complemento.
_COMPLEMENTOS_RECONOCIDOS = {
    "piña": "con piña", "pina": "con piña",
}


def _parsear_salsa_verdura(texto_low: str) -> tuple:
    """Intenta reconocer la eleccion de salsa, complementos y
    con-todo/aparte en la respuesta del cliente. Devuelve (reconocido:
    bool, nota: str). reconocido=True si se identifico AL MENOS una de las
    partes (no es necesario que conteste todo para considerarlo valido).
    También reconoce respuestas de ingredientes ('sin jitomate', 'con todo')
    para que categorías estrictas no pidan reintento cuando el cliente
    responde solo sobre ingredientes sin mencionar salsa."""
    salsas_elegidas = sorted({
        nombre for clave, nombre in _SALSAS_RECONOCIDAS.items() if clave in texto_low
    })
    complementos = sorted({
        nombre for clave, nombre in _COMPLEMENTOS_RECONOCIDOS.items() if clave in texto_low
    })
    # "ambas", "las dos", "los dos" = quiere roja Y verde
    ambas_salsas = any(p in texto_low for p in ["ambas", "las dos", "los dos", "las 2", "los 2", "ambas salsas", "de las dos"])
    if ambas_salsas:
        salsas_elegidas = ["salsa roja", "salsa verde"]

    sin_salsa = any(p in texto_low for p in ["ninguna salsa", "sin salsa", "ninguna", "sin ninguna"])
    aparte = any(p in texto_low for p in ["aparte", "a parte", "separada", "separado", "por separado"])
    con_todo = any(p in texto_low for p in ["con todo", "todo junto", "normal", "así está bien", "asi esta bien"])
    # Reconocer respuestas de ingredientes ("sin jitomate", "sin cebolla",
    # "con queso extra", "extra queso") — validas para hamburguesas, tortas,
    # especialidades. Cualquier mencion de ingredientes comunes cuenta.
    _INGREDIENTES_COMUNES = [
        "jitomate", "cebolla", "mayonesa", "aguacate", "queso", "jamon",
        "tocino", "pimiento", "champiñones", "champi", "catsup", "mostaza",
        "cilantro", "tortilla", "orden"
    ]
    tiene_ingrediente = bool(
        re.search(r'\b(sin|con|quita|quitar|extra)\s+\w{3,}', texto_low)
        or any(ing in texto_low for ing in _INGREDIENTES_COMUNES)
    )

    partes = []
    if salsas_elegidas:
        partes.append(", ".join(salsas_elegidas))
    elif sin_salsa:
        partes.append("sin salsa")
    if complementos:
        partes.append(", ".join(complementos))
    if aparte:
        partes.append("verdura aparte")
    elif con_todo:
        partes.append("con todo")
    # Si solo dijo ingredientes (sin X / con X) sin salsa ni con-todo, usamos
    # el texto tal cual como nota — el cliente respondio lo que se le pregunto.
    if tiene_ingrediente and not partes:
        partes.append(texto_low.strip())

    reconocido = bool(salsas_elegidas) or bool(complementos) or sin_salsa or aparte or con_todo or tiene_ingrediente
    return reconocido, ", ".join(partes)


# Configuracion de personalizacion por CATEGORIA del menu. Cada categoria
# presente en el carrito que tenga una entrada aqui dispara su propia
# pregunta al cerrar el pedido (una vez por categoria, no por producto
# individual). "estricta": True usa el parser de salsa/verdura y reintenta
# una vez si no reconoce la respuesta; "estricta": False acepta cualquier
# respuesta del cliente tal cual como nota, sin reintentar (para preguntas
# de tipo "que NO quieres", donde cualquier respuesta es informacion util).
# Configuracion de personalizacion por CATEGORIA del menu. Tras la decisión
# del dueño: la SALSA y la piña ya NO se preguntan por categoria — se
# preguntan UNA sola vez al final para todo el pedido (ver fase
# 'personalizacion_salsa'). Aqui solo quedan las categorias que tienen
# INGREDIENTES que el cliente puede querer quitar (hamburguesas, tortas,
# especialidades, quesadillas). Tacos y volcanes NO aparecen aqui porque su
# unica personalizacion (salsa + con todo/aparte) se maneja en la pregunta
# global de salsa. "estricta": False acepta cualquier respuesta tal cual.
_CATEGORIAS_PERSONALIZABLES = {
    "Hamburguesas": {
        "pregunta": "para tu(s) hamburguesa(s) 🍔: ¿con todos los ingredientes, o hay algo que NO quieras (catsup, mostaza, mayonesa, jitomate, aguacate, cebolla, jamón)?",
        "estricta": False,
    },
    "Quesadillas": {
        "pregunta": "para tus quesadillas 🫔: ¿con todo, o hay algo que no quieras como la cebolla?",
        "estricta": False,
    },
    "Especialidades": {
        "pregunta": "para tu(s) especialidad(es) 🍽️: ¿con todos los ingredientes, o hay algo que no quieras (cebolla, pimiento, champiñones, tocino o jamón, según el platillo)?",
        "estricta": False,
    },
    "Tortas": {
        "pregunta": "para tu(s) torta(s) 🥖: ¿con todos los ingredientes, o algo que no quieras (mayonesa, jitomate, aguacate, cebolla)?",
        "estricta": False,
    },
    "Papas Rellenas": {
        "pregunta": "para tu(s) papa(s) rellena(s) 🥔: ¿las quieres con tortillas de maíz o de harina?",
        "estricta": False,
    },
}

# Pregunta GLOBAL de salsa/piña/verdura que se hace UNA vez al final, para
# todo el pedido, justo antes de preguntar el tipo de entrega.
_PREGUNTA_SALSA_GLOBAL = (
    "Para todo tu pedido 🌮: ¿qué salsa prefieres (*roja* o *verde*), "
    "lo quieres *con todo* o con la *verdura aparte*, y les agrego *piña*?"
)


def _categorias_en_carrito(carrito: list, menu: list) -> set:
    """Devuelve el set de categorias presentes en el carrito, cruzando
    por nombre de producto contra el menu completo."""
    menu_por_nombre = {m["nombre"]: m.get("categoria", "") for m in menu}
    return {
        menu_por_nombre[c["nombre"]]
        for c in (carrito or [])
        if c.get("nombre") in menu_por_nombre and menu_por_nombre[c["nombre"]]
    }


def _categorias_pendientes_personalizacion(carrito: list, menu: list, notas_pedido: str) -> list:
    """Devuelve, en el orden definido en _CATEGORIAS_PERSONALIZABLES, las
    categorias presentes en el carrito que aun no se le han preguntado al
    cliente (no hay una nota 'Categoria:' guardada todavia)."""
    presentes = _categorias_en_carrito(carrito, menu)
    notas_pedido = notas_pedido or ""
    return [
        cat for cat in _CATEGORIAS_PERSONALIZABLES
        if cat in presentes and f"{cat}:" not in notas_pedido
    ]


# Categorias de productos que NO llevan salsa (no se les pregunta la salsa
# global). Todo lo demas (tacos, volcanes, tortas, quesadillas, hamburguesas,
# especialidades, kilos) si lleva salsa segun el dueño. Las bebidas no.
_CATEGORIAS_SIN_SALSA = {"Bebidas"}


def _falta_salsa_global(carrito: list, menu: list, notas_pedido: str) -> bool:
    """True si el pedido tiene al menos un producto que lleva salsa y aun no
    se ha hecho la pregunta global de salsa (no existe la marca 'SALSA:' en
    las notas). La salsa se pregunta UNA vez para todo el pedido, al final."""
    notas_pedido = notas_pedido or ""
    if "SALSA:" in notas_pedido:
        return False  # ya se pregunto
    presentes = _categorias_en_carrito(carrito, menu)
    # ¿Hay alguna categoria que SI lleva salsa?
    return any(cat not in _CATEGORIAS_SIN_SALSA for cat in presentes)


def _actualizar_nota_categoria(notas_existentes: str, categoria: str, nota_nueva: str) -> str:
    """Actualiza (reemplaza) la nota de una categoria especifica dentro de
    las notas del pedido. Si ya habia una nota 'Categoria: X', la sustituye
    por 'Categoria: nota_nueva'. Si no existia, la agrega al final.
    Esto evita el bug de notas duplicadas/contradictorias cuando el cliente
    corrige la personalizacion (ej. primero 'roja' luego 'verde'):
    en vez de acumular 'Tacos: salsa roja. Tacos: salsa verde.' queda
    solo 'Tacos: salsa verde.'"""
    notas = notas_existentes or ""
    prefijo = f"{categoria}:"
    nota_formateada = f"{categoria}: {nota_nueva}."
    # Si ya existe la categoria, reemplazamos esa parte
    if prefijo in notas:
        # Busca 'Categoria: ....' (hasta el proximo punto seguido de espacio
        # o fin de cadena) y lo reemplaza. Usa regex para ser preciso.
        patron = rf"{re.escape(prefijo)}[^.]*\."
        resultado = re.sub(patron, nota_formateada, notas, count=1)
        return resultado.strip()
    # No existia: la agrega al final
    return f"{notas} {nota_formateada}".strip() if notas else nota_formateada


# Palabras con las que el cliente se refiere EXPLICITAMENTE a una categoria
# cuando responde una personalizacion. Sirve para detectar respuestas "fuera
# de turno": el bot pregunta por Hamburguesas y el cliente contesta "roja y
# con todo LOS TACOS" — esa nota es de Tacos, no de Hamburguesas. Solo se usa
# para REDIRIGIR cuando la mencion es inequivoca (palabra clara). Usa
# singular y plural; las comparaciones van con word boundaries.
_PALABRAS_POR_CATEGORIA = {
    "Tacos": ["taco", "tacos"],
    "Volcanes": ["volcan", "volcanes", "volcán", "volcánes"],
    "Hamburguesas": ["hamburguesa", "hamburguesas", "hambur", "burger"],
    "Quesadillas": ["quesadilla", "quesadillas", "quesadia", "quesadía"],
    "Especialidades": ["especial", "especialidad", "especialidades", "gringa", "alambre"],
    "Tortas": ["torta", "tortas"],
    "Papas Rellenas": ["papa", "papas", "papa rellena", "papas rellenas"],
}


def _categoria_mencionada_en_texto(texto_low: str, categorias_candidatas) -> str:
    """Si el texto menciona EXPLICITAMENTE una de las categorias candidatas
    (por una de sus palabras clave, con limite de palabra), devuelve esa
    categoria. Si menciona varias o ninguna, devuelve "" (ambiguo: mejor no
    redirigir). categorias_candidatas debe ser un set/lista de categorias
    que tienen sentido en el carrito actual."""
    encontradas = set()
    for cat in categorias_candidatas:
        for palabra in _PALABRAS_POR_CATEGORIA.get(cat, []):
            if re.search(rf"\b{re.escape(palabra)}\b", texto_low):
                encontradas.add(cat)
                break
    # Solo devolvemos si hay UNA sola categoria mencionada sin ambiguedad.
    return next(iter(encontradas)) if len(encontradas) == 1 else ""


# El Taco Campechano lleva 2 carnes a elegir. Estas son las opciones
# (las mismas carnes que los tacos normales). Se pregunta UNA VEZ al
# cerrar, ANTES de la personalizacion de salsa/verdura.
_CARNES_CAMPECHANO = "pastor, bistec, sirloin, chorizo o chorizo argentino"

# Todos los productos del menú que son campechanos (2 carnes a elegir).
# Cuando hay cualquiera de estos en el carrito, se pregunta las 2 carnes.
_NOMBRES_CAMPECHANO = {
    "Taco Campechano",
    "Volcán Campechano",
    "Quesadilla Campechana",
}


def _campechano_pendiente_carnes(carrito: list, notas_pedido: str) -> bool:
    """True si hay algún producto campechano en el carrito (Taco, Volcán o
    Quesadilla Campechana) y aún no se ha guardado la nota de cuáles 2
    carnes lleva (marca 'Campechano carnes:')."""
    notas_pedido = notas_pedido or ""
    hay_campechano = any(
        c.get("nombre") in _NOMBRES_CAMPECHANO for c in (carrito or [])
    )
    return hay_campechano and "Campechano carnes:" not in notas_pedido


# Frases con las que el cliente pide MODIFICAR su carrito en plena fase de
# cierre (eligiendo recoger/envio, dando nombre, etc.). Si detectamos una
# de estas, lo regresamos al modo de armado para agregar/quitar y luego
# retomamos el cierre donde iba. Bug real visto en produccion: cliente en
# fase 'tipo' dijo "me quitaste el agua" y el bot solo repetia la pregunta.
_FRASES_MODIFICAR_CARRITO = [
    "agrega", "agregame", "agrégame", "agregar", "añade", "añademe",
    "anade", "anademe", "ponme", "pon ", "quita", "quitame", "quítame",
    "quitale", "quítale", "quitar", "elimina", "borra", "saca",
    "me falta", "falta", "olvidaste", "se te olvido", "se te olvidó",
    "me quitaste", "no pediste", "tambien quiero", "también quiero",
    "agregale", "agrégale", "mejor agrega", "cambia", "cambiame",
]


def _quiere_modificar_carrito(texto_low: str) -> bool:
    """Heuristica: detecta si el cliente quiere agregar/quitar productos
    en vez de responder la pregunta de la fase de cierre actual. Usa
    limites de palabra (word boundaries) para no dar falsos positivos
    con direcciones que contengan estas letras como subcadena (ej.
    'Sacalum' contiene 'saca', 'Zacatecas' no debe activar 'saca')."""
    t = texto_low.strip()
    for f in _FRASES_MODIFICAR_CARRITO:
        # \b al inicio y fin para que coincida la palabra completa, no
        # como parte de otra palabra mas larga.
        if re.search(rf"\b{re.escape(f)}", t):
            # Para frases de una sola palabra exigimos limite tambien al
            # final; para frases con espacio ya es suficientemente especifico.
            if " " in f or re.search(rf"\b{re.escape(f)}\b", t):
                return True
    return False


def _formato_carrito(carrito: list, costo_envio: float = 0, extras: list = None) -> str:
    if not carrito:
        return "🛒 Tu carrito está vacío."
    lineas = ["🛒 *Tu pedido:*"]
    for i in carrito:
        subtotal = _precio_linea(i["nombre"], i["cantidad"], i["precio"])
        promo = _PROMOS_TACOS.get(i["nombre"])
        tiene_promo = promo and i["cantidad"] >= promo["paquete"]
        etiqueta_promo = " 🎉" if tiene_promo else ""
        lineas.append(f"  • {i['cantidad']}x {i['nombre']} — {_fmt_precio(subtotal)}{etiqueta_promo}")
    for e in (extras or []):
        cant_e = e.get("cantidad", 1)
        prefijo_cant = f"{cant_e}x " if cant_e > 1 else ""
        lineas.append(
            f"  • + {prefijo_cant}{e['ingrediente']} extra (en {e['producto']}) — "
            f"{_fmt_precio(_VALOR_EXTRA * cant_e)}"
        )
    total = _calcular_total(carrito, extras)
    if costo_envio > 0:
        lineas.append(f"  • Envío — {_fmt_precio(costo_envio)}")
        lineas.append(f"\n💰 *Total: {_fmt_precio(total + costo_envio)}*")
    else:
        lineas.append(f"\n💰 *Total: {_fmt_precio(total)}*")
    return "\n".join(lineas)


_EMOJI_CATEGORIA = {
    "tacos":        "🌮",
    "quesadillas":  "🫔",
    "tortas":       "🥙",
    "bebidas":      "🥤",
    "complementos": "🍟",
    "postres":      "🍮",
    "desayunos":    "🍳",
    "sopas":        "🍲",
    "mariscos":     "🦐",
    "carnes":       "🥩",
    "ensaladas":    "🥗",
    "pizzas":       "🍕",
    "hamburguesas": "🍔",
    "hot dogs":     "🌭",
    "sushi":        "🍱",
    "combos":       "🎁",
    "especiales":   "⭐",
    "antojitos":    "🫓",
}

def _emoji_cat(categoria: str) -> str:
    return _EMOJI_CATEGORIA.get(categoria.lower().strip(), "🍽️")

def _fmt_precio(precio: float) -> str:
    """Muestra precio sin decimales si es numero redondo."""
    return f"${int(precio)}" if precio == int(precio) else f"${precio:.2f}"

def _formato_menu(items: list) -> str:
    if not items:
        return "No hay productos disponibles en este momento."

    por_categoria: dict = {}
    for item in items:
        cat = item.get("categoria", "General")
        por_categoria.setdefault(cat, []).append(item)

    lineas = ["✨ *MENÚ* ✨", ""]

    total_cats = len(por_categoria)
    for idx, (cat, productos) in enumerate(por_categoria.items()):
        emoji = _emoji_cat(cat)
        lineas.append(f"{emoji} *{cat.upper()}*")
        for p in productos:
            precio_txt = _fmt_precio(float(p['precio']))
            promo = _PROMOS_TACOS.get(p['nombre'])
            if promo:
                if promo["paquete"] == 2 and promo["precio_paquete"] == p['precio']:
                    promo_txt = f" _(2x1)_"
                else:
                    promo_txt = f" _({promo['paquete']}x{_fmt_precio(promo['precio_paquete'])})_"
                lineas.append(f"  • *{p['nombre']}* — {precio_txt}{promo_txt}")
            else:
                lineas.append(f"  • *{p['nombre']}* — {precio_txt}")
        if idx < total_cats - 1:
            lineas.append("")

    lineas += [
        "",
        "━━━━━━━━━━━━━━",
        "👇 *¿Qué te gustaría ordenar?*",
        "_Puedes pedir varios productos a la vez, por ejemplo:_",
        "_\"2 tacos de pastor y una horchata\"_",
        "_¿Quieres saber qué lleva algún platillo? Solo pregunta._",
    ]
    return "\n".join(lineas)


def _normalizar_txt(s: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFD", s.lower()).encode("ascii", "ignore").decode()
    # Tolerar variantes de formato para litros: "1L", "1 Lt", "1 litro",
    # "1lt", "medio litro", "litro" (sin numero, asumido como 1) todas se
    # normalizan igual, para que coincidan sin importar como el cliente
    # (o el LLM al copiarlo) lo haya escrito. El orden importa: primero
    # "medio litro" -> 1/2, luego litro con digito explicito, luego litro
    # suelto sin digito ni "medio" -> se asume 1 litro.
    s = re.sub(r'\bmedio\s*l(?:t|ts|itro|itros)?\b', '1/2lt', s)
    s = re.sub(r'(\d)\s*l(?:t|ts|itro|itros)?\b', r'\1lt', s)
    s = re.sub(r'(?<!\d)\bl(?:itro|itros)\b', '1lt', s)
    return s


_PALABRAS_RELLENO = {"de", "del", "la", "el", "los", "las", "un", "una", "unos", "unas"}


def _quitar_relleno(s: str) -> str:
    """Quita palabras de relleno (de, un, la, etc.) de un texto ya
    normalizado. Solo se usa como ULTIMO recurso de busqueda — varios
    nombres del menu usan 'de' a propósito (ej. 'Taco de Pastor'), así que
    quitarlo de entrada rompería la precisión de los niveles anteriores.
    Pero frases como 'agua DE litro' insertan un 'de' que el nombre real
    del producto ('Agua 1 Lt') no tiene, y sin este nivel nunca matchean."""
    return " ".join(p for p in s.split() if p not in _PALABRAS_RELLENO)


def _singularizar(palabra: str) -> str:
    """Quita plurales simples en espanol: volcanes->volcan, tacos->taco,
    aguas->agua. Permite que 'dame volcanes' encuentre todas las variantes
    de Volcan en el menu sin que el plural rompa la coincidencia."""
    if palabra.endswith("ces"):
        return palabra[:-3] + "z"
    if palabra.endswith("es") and len(palabra) > 4:
        return palabra[:-2]
    if palabra.endswith("s") and len(palabra) > 3:
        return palabra[:-1]
    return palabra


def _buscar_en_menu(texto: str, menu: list) -> Optional[dict]:
    """Busca un producto en el menu por nombre. Solo devuelve resultado si
    hay una unica coincidencia clara (ver _buscar_coincidencias)."""
    coincidencias = _buscar_coincidencias(texto, menu)
    if len(coincidencias) == 1:
        return coincidencias[0]
    return None


def _buscar_coincidencias(texto: str, menu: list) -> list:
    """Devuelve TODAS las coincidencias posibles de un texto contra el menu.
    Prioriza: 1) coincidencia exacta de nombre completo, 2) el texto contiene
    el nombre completo del producto (busqueda especifica), 3) el nombre
    singularizado del texto esta contenido en el nombre del producto
    (busqueda generica, ej. 'volcanes' encuentra las 4 variantes de Volcan),
    4) como ultimo recurso, la misma comparacion pero quitando palabras de
    relleno (de, un, la...) de ambos lados — para casos como 'agua DE litro'
    que de otro modo nunca matchearia con 'Agua 1 Lt'.
    Esto evita que un termino generico devuelva solo la primera coincidencia
    cuando en realidad hay varias variantes — en ese caso deben devolverse
    todas para que la herramienta pueda detectar la ambiguedad y preguntar."""
    t = _normalizar_txt(texto.strip())
    t_sing = " ".join(_singularizar(p) for p in t.split())

    exactas = [i for i in menu if _normalizar_txt(i["nombre"]) == t]
    if exactas:
        return exactas

    contiene_nombre = [i for i in menu if _normalizar_txt(i["nombre"]) in t]
    if contiene_nombre:
        return contiene_nombre

    texto_en_nombre = [i for i in menu if t_sing in _normalizar_txt(i["nombre"])]
    if texto_en_nombre:
        return texto_en_nombre

    sin_relleno = _quitar_relleno(t_sing)
    if sin_relleno and sin_relleno != t_sing:
        sin_relleno_match = [
            i for i in menu
            if sin_relleno in _quitar_relleno(_normalizar_txt(i["nombre"]))
            or _quitar_relleno(_normalizar_txt(i["nombre"])) in sin_relleno
        ]
        if sin_relleno_match:
            return sin_relleno_match

    # ÚLTIMO RECURSO: búsqueda por palabras clave en CUALQUIER ORDEN.
    # Cubre casos como "agua de litro de horchata" vs "Agua Horchata 1 Lt":
    # mismas palabras clave (agua, horchata, 1lt) pero en distinto orden.
    # Solo se activa si los niveles anteriores no dieron nada. Exige que
    # TODAS las palabras del texto (sin relleno, >= 3 chars) aparezcan en
    # el nombre del producto — así 'horchata' no matchea 'Agua Jamaica 1 Lt'.
    palabras_clave = [p for p in sin_relleno.split() if len(p) >= 3]
    if palabras_clave:
        por_palabras = [
            i for i in menu
            if all(p in _quitar_relleno(_normalizar_txt(i["nombre"])) for p in palabras_clave)
        ]
        if por_palabras:
            return por_palabras

    return [i for i in menu if t in _normalizar_txt(i["nombre"])]


# ── WEBHOOK ──────────────────────────────────────────────────────────────────

@app.get("/webhook")
async def verificar_webhook(request: Request):
    params = dict(request.query_params)
    if params.get("hub.verify_token") == VERIFY_TOKEN and params.get("hub.challenge"):
        return PlainTextResponse(params["hub.challenge"])
    raise HTTPException(status_code=403, detail="Token inválido")


@app.post("/webhook")
async def recibir_webhook(request: Request, background_tasks: BackgroundTasks):
    try:
        data = await request.json()
        entry = data.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        value   = changes.get("value", {})
        msgs    = value.get("messages", [])
        if not msgs:
            return {"status": "ok"}
        msg     = msgs[0]
        tipo    = msg.get("type", "")
        coords_ubicacion = None
        if tipo == "text":
            texto = msg["text"]["body"].strip()
        elif tipo == "location":
            loc = msg.get("location", {})
            lat, lng = loc.get("latitude"), loc.get("longitude")
            if lat is None or lng is None:
                return {"status": "ok"}
            coords_ubicacion = (float(lat), float(lng))
            # Texto placeholder; procesar_mensaje detecta coords_ubicacion
            # y lo trata como una direccion ya geocodificada, sin volver a
            # llamar a la API de geocoding (es mas preciso y mas rapido).
            nombre_lugar = loc.get("name") or loc.get("address") or ""
            texto = f"[ubicación]{(' ' + nombre_lugar) if nombre_lugar else ''}"
        elif tipo in ("image", "audio", "video", "document", "sticker"):
            texto = f"[{tipo}]"
        else:
            return {"status": "ok"}
        telefono       = msg["from"]
        wamid          = msg.get("id", "")   # ID único del mensaje de WhatsApp
        phone_number_id = value.get("metadata", {}).get("phone_number_id", "")
        if not phone_number_id or phone_number_id not in _negocios_cache:
            return {"status": "ok"}
        # Idempotencia: ignorar mensajes duplicados que Meta reenvía.
        if wamid and _ya_procesado(wamid):
            print(f"   [Webhook] Duplicado ignorado: wamid={wamid[:20]}...")
            return {"status": "ok"}
        if wamid:
            _marcar_procesado(wamid)
        background_tasks.add_task(procesar_mensaje, texto, telefono, phone_number_id, coords_ubicacion)
        return {"status": "ok"}
    except Exception as e:
        print(f"!!! Error en webhook: {e}")
        return {"status": "ok"}


# ── WEBHOOK DE PAGOS (Mercado Pago) ──────────────────────────────────────────

@app.post("/webhook_pago")
async def recibir_webhook_pago(request: Request, background_tasks: BackgroundTasks):
    """Mercado Pago notifica aqui cuando el estado de un pago cambia.
    IMPORTANTE: Mercado Pago manda el tipo y el id de dos formas distintas
    segun el caso — a veces en el BODY (JSON), a veces como QUERY PARAMS en
    la URL (ej. '?id=123&topic=merchant_order'). Hay que revisar ambos.
    Tambien manda dos topics distintos: 'payment' (id es un payment_id
    directo) y 'merchant_order' (id es una orden que agrupa uno o mas
    pagos — hay que consultarla para sacar los payment_id reales)."""
    try:
        params = dict(request.query_params)
        try:
            body = await request.json()
        except Exception:
            body = {}

        tipo = params.get("topic") or params.get("type") or body.get("type") or body.get("topic")
        id_notificacion = params.get("id") or (body.get("data") or {}).get("id") or body.get("id")

        if not tipo or not id_notificacion:
            return {"status": "ok"}

        if tipo == "payment":
            background_tasks.add_task(procesar_webhook_pago, str(id_notificacion))
        elif tipo == "merchant_order":
            background_tasks.add_task(procesar_webhook_merchant_order, str(id_notificacion))

        return {"status": "ok"}
    except Exception as e:
        print(f"!!! Error en webhook_pago: {e}")
        return {"status": "ok"}


def procesar_webhook_merchant_order(order_id: str):
    """Una merchant_order agrupa los pagos asociados a una preferencia.
    La consultamos para sacar los payment_id reales y procesarlos igual
    que si hubieran llegado como notificacion tipo 'payment'."""
    try:
        info = pagos.consultar_merchant_order(order_id)
        if not info:
            print(f"!!! No se pudo consultar merchant_order {order_id}")
            return
        for payment_id in info.get("payment_ids", []):
            procesar_webhook_pago(payment_id)
    except Exception as e:
        print(f"!!! Error en procesar_webhook_merchant_order: {e}")


def procesar_webhook_pago(payment_id: str):
    """Consulta el pago en Mercado Pago, encuentra el pedido relacionado
    por su preference_id, y si el pago fue aprobado lo pasa a cocina."""
    try:
        info = pagos.consultar_pago(payment_id)
        if not info:
            print(f"!!! No se pudo consultar el pago {payment_id}")
            return

        referencia = info.get("external_reference", "")
        pedido = db.buscar_pedido_por_referencia(referencia)
        if not pedido:
            # Puede pasar si el pago no corresponde a esta taqueria, o si
            # el webhook llega antes de que guardemos la referencia — no
            # es necesariamente un error.
            print(f"!!! No se encontró pedido para referencia '{referencia}' (pago {payment_id})")
            return

        pedido_id = pedido["id"]
        estado_mp = info.get("status")

        if estado_mp == "approved":
            db.confirmar_pago_pedido(pedido_id, payment_id)
            print(f"   [Pagos] Pedido #{pedido_id} — pago APROBADO (${info.get('monto')}). Pasa a cocina.")
            # Avisamos al cliente que su pedido ya esta confirmado. Buscamos
            # el negocio (telefono + token de WhatsApp) por su negocio_id
            # entre los negocios cargados en cache.
            telefono_neg, token_neg = None, None
            for pid, neg in _negocios_cache.items():
                if neg.get("id") == pedido.get("negocio_id"):
                    token_neg = neg.get("whatsapp_token")
                    telefono_neg = pid
                    break
            if telefono_neg and token_neg:
                resp = (
                    f"✅ *¡Pago confirmado! Pedido #{pedido_id}*\n\n"
                    f"Tu pedido ya pasó a preparación. 🌮\n"
                    f"¡Gracias por tu preferencia!"
                )
                enviar_whatsapp(pedido["telefono"], resp, token_neg, telefono_neg)

        elif estado_mp == "rejected":
            db.rechazar_pago_pedido(pedido_id, payment_id)
            print(f"   [Pagos] Pedido #{pedido_id} — pago RECHAZADO.")
            for pid, neg in _negocios_cache.items():
                if neg.get("id") == pedido.get("negocio_id"):
                    resp = (
                        f"❌ Tu pago para el pedido #{pedido_id} no pudo procesarse.\n"
                        f"¿Quieres intentar de nuevo o prefieres pagar en efectivo?"
                    )
                    enviar_whatsapp(pedido["telefono"], resp, neg.get("whatsapp_token"), pid)
                    break
        else:
            print(f"   [Pagos] Pedido #{pedido_id} — pago en estado '{estado_mp}', sin acción.")

    except Exception as e:
        import traceback
        print(f"!!! Error en procesar_webhook_pago: {e}\n{traceback.format_exc()}")


# ── PROCESAMIENTO PRINCIPAL ──────────────────────────────────────────────────

# Candado por numero de telefono: el webhook dispara cada mensaje como una
# tarea en segundo plano independiente (background_tasks.add_task), SIN
# ninguna sincronizacion entre si. Si un cliente manda dos mensajes muy
# seguidos (algo comun en WhatsApp), ambos podian terminar procesandose EN
# PARALELO — cada uno leyendo la sesion ANTES de que el otro terminara de
# guardar sus cambios, pisandose entre si y perdiendo parte del pedido (bug
# real visto en pruebas: "3 tacos de pastor" + "Es todo" mandados muy
# seguidos hicieron que el carrito se perdiera). Este candado obliga a que
# los mensajes del MISMO numero se procesen uno a la vez, en orden — los de
# numeros DISTINTOS siguen procesandose en paralelo sin estorbarse.
_locks_telefono: dict = {}
_locks_telefono_mutex = threading.Lock()


def _obtener_candado(telefono: str) -> threading.Lock:
    with _locks_telefono_mutex:
        if telefono not in _locks_telefono:
            _locks_telefono[telefono] = threading.Lock()
        return _locks_telefono[telefono]


def procesar_mensaje(texto: str, telefono: str, phone_number_id: str, coords_ubicacion=None):
    candado = _obtener_candado(telefono)
    # Timeout de seguridad: si por algun motivo un procesamiento anterior se
    # queda atorado y nunca libera el candado, no queremos bloquear a ese
    # cliente para siempre — despues de 30s seguimos de todas formas.
    adquirido = candado.acquire(timeout=30)
    try:
        if not adquirido:
            print(f"!!! No se pudo adquirir el candado para {telefono} en 30s, procesando de todas formas.")
        _procesar_mensaje_interno(texto, telefono, phone_number_id, coords_ubicacion)
    finally:
        if adquirido:
            candado.release()


def _procesar_mensaje_interno(texto: str, telefono: str, phone_number_id: str, coords_ubicacion=None):
    try:
        # Leemos la config del negocio SIEMPRE fresca de Supabase para que
        # cualquier cambio desde el panel aplique en el siguiente mensaje,
        # sin necesidad de reiniciar el servicio.
        negocio_cache = _negocios_cache.get(phone_number_id)
        if not negocio_cache:
            return
        negocio_id = negocio_cache["id"]

        # Recarga la fila del negocio directo de DB
        negocios_frescos = db.cargar_negocios()
        negocio = next((n for n in negocios_frescos if n["id"] == negocio_id), negocio_cache)
        # Actualizar cache en memoria tambien
        _negocios_cache[phone_number_id] = negocio

        nombre_neg    = negocio["nombre"]
        token         = negocio["whatsapp_token"]
        menu          = _menu_cache.get(negocio_id, [])
        email_notif   = negocio.get("email_notificaciones", "")
        tipo_servicio = negocio.get("tipo_servicio", "ambos")
        pedido_min    = float(negocio.get("pedido_minimo") or 0)
        tiempo_rec    = negocio.get("tiempo_recoger_min", 20)
        tiempo_env    = negocio.get("tiempo_envio_min", 40)
        metodos_pago  = negocio.get("metodos_pago", "Efectivo")
        bienvenida    = negocio.get("mensaje_bienvenida", "¡Hola! ¿Qué te gustaría ordenar?")
        envio_dinamico = bool(negocio.get("costo_envio_dinamico"))
        modo_lluvia    = bool(negocio.get("modo_lluvia"))
        coords_negocio = None
        if negocio.get("lat") and negocio.get("lng"):
            coords_negocio = (float(negocio["lat"]), float(negocio["lng"]))

        llave   = f"{phone_number_id}_{telefono}"
        sesion  = db.cargar_sesion(llave)
        historial  = sesion["historial"]
        carrito    = sesion["carrito"] or []
        fase       = sesion["fase_pedido"] or ""
        texto_low  = texto.lower().strip()

        print(f"-> [Procesando] {telefono} | fase={fase or 'inicio'} | msg='{texto[:60]}'")

        # ── CANCELAR PEDIDO ───────────────────────────────────────────────
        # El cliente puede cancelar su pedido mas reciente en cualquier
        # momento, SIEMPRE que todavia no haya pasado a 'en_proceso' (la
        # cocina ya empezo a prepararlo). Esto funciona desde cualquier
        # fase y aunque el negocio este cerrado, porque cancelar es una
        # accion que no depende del horario.
        #
        # Tambien cubre el caso de un CARRITO A MEDIO ARMAR que todavia no
        # se ha confirmado (sesion en progreso, sin pedido real en la DB
        # todavia) — antes "cancelar" solo buscaba pedidos YA CONFIRMADOS y
        # no hacia nada si el cliente apenas estaba armando su pedido,
        # dejando el carrito viejo vivo en la sesion sin que el cliente se
        # diera cuenta.
        _PALABRAS_CANCELAR = {"cancelar", "cancela", "cancelar pedido", "anular", "anula"}
        _t_cancel = texto_low.strip(".,!¡¿? ")
        # Detectamos cancelacion si: (a) el mensaje es exactamente una palabra
        # de cancelar, o (b) contiene "cancela(r)"/"anula(r)" como palabra
        # completa junto a "pedido"/"todo"/"orden" o como frase de intencion
        # ("mejor cancela todo", "quiero cancelar", "cancela mi pedido"). Usa
        # word boundaries para no disparar con palabras que contengan estas
        # letras. Bug real: "Mejor cancela todo" en fase nombre se guardaba
        # como nombre del cliente ("Mejor Cancela Todo") en vez de cancelar.
        _es_cancelacion = (
            _t_cancel in _PALABRAS_CANCELAR
            or texto_low.startswith("cancelar")
            or bool(re.search(r"\b(cancela|cancelar|anula|anular)\b.*\b(pedido|todo|orden|mi orden)\b", _t_cancel))
            or bool(re.search(r"\b(mejor|quiero|deseo|favor)\b.*\b(cancela|cancelar|anula|anular)\b", _t_cancel))
        )
        if _es_cancelacion:
            pedido_cancelable = db.buscar_pedido_cancelable(negocio_id, telefono)
            if pedido_cancelable:
                db.actualizar_estado_pedido(pedido_cancelable["id"], "cancelado")
                resp = (
                    f"❌ Tu pedido #{pedido_cancelable['id']} fue cancelado. "
                    f"Si cambias de opinión, aquí estamos para ayudarte. 🌮"
                )
                db.limpiar_sesion(llave)
                print(f"   [{nombre_neg}] Pedido #{pedido_cancelable['id']} cancelado por el cliente.")
            elif carrito:
                resp = (
                    "❌ Listo, cancelé tu pedido en progreso y vacié tu carrito. "
                    "Si quieres empezar de nuevo, aquí estamos para ayudarte. 🌮"
                )
                db.limpiar_sesion(llave)
                print(f"   [{nombre_neg}] Carrito en progreso (sin confirmar) cancelado por el cliente.")
            else:
                resp = (
                    "No encontré ningún pedido tuyo que se pueda cancelar en este momento "
                    "(puede que ya esté en preparación — en ese caso contáctanos directo)."
                )
                print(f"   [{nombre_neg}] Cliente pidió cancelar pero no hay nada que cancelar.")
            enviar_whatsapp(telefono, resp, token, phone_number_id)
            print(f"<- [{nombre_neg}] {resp[:80]}")
            return

        # Caducidad de sesion por inactividad: si una sesion con fase activa
        # (pedido a medias) lleva mas de 2 horas sin actividad, la
        # consideramos abandonada y la limpiamos automaticamente — SIN
        # depender de que el cliente salude primero. Esto evita que un
        # cliente real quede atorado en una fase vieja para siempre si
        # simplemente dejo de contestar a medio pedido y vuelve dias
        # despues con un mensaje cualquiera (no necesariamente un saludo).
        SESION_CADUCIDAD_HORAS = 2
        if fase and sesion.get("updated_at"):
            try:
                ts_str = sesion["updated_at"].replace("Z", "+00:00")
                ts_actualizacion = datetime.datetime.fromisoformat(ts_str)
                horas_inactivo = (datetime.datetime.now(datetime.timezone.utc) - ts_actualizacion).total_seconds() / 3600
                if horas_inactivo >= SESION_CADUCIDAD_HORAS:
                    db.limpiar_sesion(llave)
                    carrito = []
                    fase = ""
                    sesion = db.cargar_sesion(llave)
                    historial = sesion["historial"]
                    print(f"   [{nombre_neg}] Sesión caducada por inactividad ({horas_inactivo:.1f}h), limpiada automáticamente.")
            except Exception as e:
                print(f"!!! Error calculando caducidad de sesión: {e}")

        # Limpieza de carrito huerfano: si hay productos en el carrito pero
        # no hay una fase activa (el pedido quedo a medias por algun error),
        # limpiamos al detectar un saludo nuevo para empezar desde cero.
        # (Esta es una red de seguridad adicional para el caso especifico
        # de carrito sin fase; la caducidad de arriba cubre el caso general
        # de cualquier fase atorada, con o sin saludo.)
        # BUG CRITICO CORREGIDO: antes se usaba "in" (substring), lo que
        # causaba que palabras como "chingaos" (contiene "hi") disparuran
        # un falso positivo y BORRARAN EL CARRITO DEL CLIENTE sin que lo
        # pidiera. Ahora usamos limite de palabra completa (\b) con regex,
        # y ademas solo se activa si el mensaje es ESENCIALMENTE un saludo
        # (no si la palabra aparece en medio de una oracion sobre otra cosa).
        _SALUDOS = {"hola","buenas","buen dia","buenos dias","buenas tardes",
                    "buenas noches","hey","hi","hello","ola","saludos"}
        _es_saludo_palabra_completa = any(
            re.search(rf"\b{re.escape(s)}\b", texto_low) for s in _SALUDOS
        )
        _es_saludo_corto = _es_saludo_palabra_completa and len(texto.split()) <= 4
        # Caso 1: carrito huerfano (productos sin fase activa).
        # Caso 2: sesion atorada en CUALQUIER fase del flujo de cierre
        # (tipo/nombre/direccion/pago/personalizacion). Bug real visto en
        # pruebas: un cliente que dejo un pedido a medias en fase 'tipo' y
        # volvio mas tarde (dentro de las 2h, antes de que caduque la
        # sesion) diciendo "Hola" se quedaba atorado — el bot respondia
        # "texto no reconocido en fase tipo" a TODO (incluido el saludo y
        # cualquier producto nuevo) hasta que por casualidad decia una
        # palabra valida de esa fase. Un saludo corto es señal inequivoca
        # de que quiere empezar de nuevo, asi que reiniciamos la sesion.
        _fase_en_flujo = fase in ("tipo", "nombre", "direccion", "pago", "campechano:carnes", "personalizacion_salsa") or fase.startswith("personalizacion:")
        # Reset si:
        # - Carrito huerfano (productos sin fase activa)
        # - Carrito con productos en cualquier fase + saludo nuevo (cliente vuelve a empezar)
        # - Sesion atorada en flujo de cierre
        _tiene_carrito = bool(carrito)
        if _es_saludo_corto and (_tiene_carrito or _fase_en_flujo):
            db.limpiar_sesion(llave)
            carrito = []
            fase    = ""
            sesion  = db.cargar_sesion(llave)
            historial = sesion["historial"]
            print(f"   [{nombre_neg}] Sesión reiniciada al detectar saludo nuevo (carrito/fase previa limpiada).")

        # ── VALIDACION DE HORARIO ────────────────────────────────────────────
        # Si el negocio esta cerrado (cerrado_hoy o fuera de horario), avisamos
        # y no procesamos el pedido. Solo se permite si ya habia una sesion
        # activa en curso (carrito con productos o en fase de confirmacion),
        # para no cortar a un cliente a la mitad de su pedido.
        def _esta_abierto() -> bool:
            if negocio.get("cerrado_hoy"):
                return False
            horarios = negocio.get("horarios_json") or {}
            if isinstance(horarios, str):
                import json as _j
                try: horarios = _j.loads(horarios)
                except: return True  # si no se puede parsear, no bloqueamos
            ahora_mx = datetime.datetime.now(TZ_MX)
            dia_semana = ["lunes","martes","miercoles","jueves","viernes","sabado","domingo"][ahora_mx.weekday()]
            info = horarios.get(dia_semana, {})
            if not info.get("abierto", True):
                return False
            try:
                ap_h, ap_m = map(int, info.get("apertura","00:00").split(":"))
                ci_h, ci_m = map(int, info.get("cierre","23:59").split(":"))
                apertura_min = ap_h * 60 + ap_m
                cierre_min   = ci_h * 60 + ci_m
                actual_min   = ahora_mx.hour * 60 + ahora_mx.minute
                if cierre_min < apertura_min:
                    # cruza medianoche (ej. 18:00 a 01:00)
                    return actual_min >= apertura_min or actual_min <= cierre_min
                return apertura_min <= actual_min <= cierre_min
            except Exception:
                return True  # si falla el parse, no bloqueamos

        sesion_activa = bool(carrito) or fase in ("confirmando", "nombre", "direccion", "tipo", "campechano:carnes", "personalizacion_salsa") or fase.startswith("personalizacion:")
        if not _esta_abierto() and not sesion_activa:
            msg_cerrado = negocio.get("mensaje_cerrado") or (
                "Lo sentimos, en este momento estamos cerrados. "
                "¡Te esperamos en nuestro horario de atención! 🕐"
            )
            enviar_whatsapp(telefono, msg_cerrado, token, phone_number_id)
            print(f"   [{nombre_neg}] Fuera de horario — mensaje de cerrado enviado.")
            return

        # ── GUARD: PEDIDO PENDIENTE_PAGO + CLIENTE QUIERE CAMBIAR A EFECTIVO ──
        # Si el cliente ya había confirmado y se generó un link de pago
        # (Mercado Pago), pero luego dice que prefiere efectivo — la sesión
        # ya está vacía (fase=inicio, carrito=[]) pero el pedido sigue en DB
        # con estado 'pendiente_pago'. Bug real (pedido #62): el cliente tuvo
        # que repetir TODO el pedido desde cero. Ahora detectamos ese caso,
        # cambiamos el método de pago a efectivo y confirmamos directamente.
        if not carrito and fase == "inicio":
            _FRASES_CAMBIO_EFECTIVO = [
                "mejor efectivo", "mejor en efectivo", "pago en efectivo",
                "prefiero efectivo", "efectivo mejor", "mejor pago en efectivo",
                "no mejor efectivo", "no, mejor efectivo", "cambio a efectivo",
                "mejor lo pago en efectivo", "pagare en efectivo", "pagaré en efectivo",
            ]
            _quiere_efectivo = any(f in texto_low for f in _FRASES_CAMBIO_EFECTIVO)
            if _quiere_efectivo:
                _pedido_pendiente = db.buscar_pedido_cancelable(negocio_id, telefono)
                if _pedido_pendiente and _pedido_pendiente.get("estado") == "pendiente_pago":
                    _pid = _pedido_pendiente["id"]
                    db.cambiar_pago_pendiente_a_efectivo(_pid)
                    _items_str = _pedido_pendiente.get("items_resumen", "")
                    _total = _pedido_pendiente.get("total", 0)
                    _nombre_cl = _pedido_pendiente.get("nombre_cliente", "")
                    resp = (
                        f"✅ ¡Listo! Cambié el método de pago a *efectivo*.\n\n"
                        f"📋 *Pedido #{_pid} confirmado*\n"
                        f"👤 {_nombre_cl}\n"
                        f"💰 Total: ${_total:.0f} (pagas al recibir)\n\n"
                        f"¡Tu pedido ya está en preparación! 🌮"
                    )
                    nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                    db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:])
                    enviar_whatsapp(telefono, resp, token, phone_number_id)
                    print(f"   [{nombre_neg}] Pedido #{_pid} cambió de pago online a efectivo — confirmado a cocina.")
                    return

        # ── CAPTURA DE UBICACIÓN GPS — RED DE SEGURIDAD ─────────────────────
        # Si el cliente comparte su ubicación real de WhatsApp pero el flujo
        # determinístico NO está en fase "direccion" (ej. el LLM, en modo
        # libre, le pidió la dirección sin llamar cerrar_pedido primero —
        # un bug real visto en producción), esto la captura de todas formas
        # en vez de dejar que se pierda en el limbo del LLM libre. Sin esto,
        # un cliente puede compartir su ubicación GPS real y el sistema la
        # ignora por completo, perdiendo el pedido.
        if coords_ubicacion and fase != "direccion":
            if not carrito:
                resp = (
                    "Veo que compartiste tu ubicación, pero todavía no tienes nada en tu "
                    "carrito. 😊 ¿Qué te gustaría ordenar?"
                )
                nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:])
                enviar_whatsapp(telefono, resp, token, phone_number_id)
                print(f"   [{nombre_neg}] Ubicación recibida sin carrito activo — ignorada con aviso.")
                return
            # Hay carrito activo: forzamos tipo_entrega=envio y tratamos esto
            # como si el cliente ya hubiera elegido envío y compartido su
            # ubicación en la fase correcta — replicando el mismo cálculo
            # dinámico que se haría normalmente.
            print(f"   [{nombre_neg}] Ubicación GPS capturada fuera de fase 'direccion' (red de seguridad activada).")
            if envio_dinamico and coords_negocio:
                resultado = geo.calcular_envio_desde_coords(coords_ubicacion, coords_negocio, lluvia=modo_lluvia)
                if resultado["ok"]:
                    costo_calculado = resultado["costo"]
                    km = resultado["km"]
                    lat, lng = coords_ubicacion
                    direccion_capturada = f"📍 Ubicación compartida ({km} km)\nhttps://maps.google.com/?q={lat},{lng}"
                    resp = (
                        f"📍 Recibí tu ubicación (Distancia: {km} km — Envío: {_fmt_precio(costo_calculado)}).\n"
                        f"¿A nombre de quién registramos el pedido?"
                    )
                    nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                    db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="nombre",
                                      tipo_entrega="envio", direccion_entrega=direccion_capturada,
                                      carrito=carrito, costo_envio_calc=costo_calculado)
                    enviar_whatsapp(telefono, resp, token, phone_number_id)
                    return
                elif "Fuera de cobertura" in resultado["razon"]:
                    resp = (
                        f"Tu ubicación está fuera de nuestra zona de cobertura para envío "
                        f"({resultado['km']} km, máximo 20 km). "
                        f"¿Prefieres pasar a recoger tu pedido al local?"
                    )
                    nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                    db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="tipo", carrito=carrito)
                    enviar_whatsapp(telefono, resp, token, phone_number_id)
                    return
            resp = (
                "Recibí tu ubicación, pero no pude calcular el envío automáticamente. 😕\n"
                "¿Podrías llamarnos directo para confirmar tu pedido y el costo de envío?"
            )
            nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
            db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="armando", carrito=carrito)
            enviar_whatsapp(telefono, resp, token, phone_number_id)
            return

        # ── SALUDO INICIAL DETERMINISTICO ───────────────────────────────────
        # Si el mensaje es UNICAMENTE un saludo (sin nada mas, ej. "Hola",
        # "Buenas tardes") y no hay carrito/fase activa, respondemos con el
        # mensaje de bienvenida EXACTO configurado por el dueño, sin pasar
        # por el LLM. Esto evita que el modelo improvise un saludo distinto
        # o, peor, prometa "aqui tienes el menu" sin realmente mandarlo —
        # un bug real visto en produccion. Si el saludo viene acompañado de
        # mas texto (ej. "Hola, quiero 2 tacos"), dejamos que el LLM lo
        # maneje normal, porque ahi si hay intencion de pedido real.
        _es_solo_saludo = (
            not carrito and not fase
            and texto_low.strip(".,!¡¿? ") in _SALUDOS
        )
        if _es_solo_saludo:
            aviso_min_saludo = ""
            if pedido_min > 0 and tipo_servicio in ("envio", "ambos"):
                aviso_min_saludo = f" Recuerda que el pedido mínimo para envío a domicilio es de {_fmt_precio(pedido_min)}."
            resp = f"{bienvenida}{aviso_min_saludo}"
            nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
            db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:])
            enviar_whatsapp(telefono, resp, token, phone_number_id)
            print(f"   [{nombre_neg}] Saludo inicial respondido de forma determinística.")
            return

        # ── FLUJO DETERMINISTICO POR FASE ───────────────────────────────────

        # FASE: confirmando — el cliente responde SI/NO al resumen del pedido
        if fase == "confirmando":
            if any(p in texto_low for p in ["si", "sí", "confirmo", "dale", "va", "ok", "correcto", "listo"]):
                tipo_entrega = sesion["tipo_entrega"]
                nombre_cl    = sesion["nombre_cliente"]
                direccion    = sesion["direccion_entrega"]
                notas_raw    = sesion.get("notas_pedido", "")
                extras_sesion = sesion.get("extras_pedido", [])
                costo_envio_real = sesion.get("costo_envio_calc", 0)
                total        = _calcular_total(carrito, extras_sesion)
                total_con_envio = total + (costo_envio_real if tipo_entrega == "envio" else 0)

                # Extraer el metodo de pago si viene en el prefijo [PAGO:..]
                # (solo aplica a envios, ver fase "pago" mas arriba).
                metodo_pago_usado, notas = _extraer_pago_de_notas(notas_raw)

                # Segunda capa de proteccion: el metodo de pago online SOLO
                # tiene sentido si el pedido es de envio. Si por algun
                # motivo quedo un [PAGO:...] arrastrado en la nota pero el
                # pedido actual es para recoger, lo ignoramos — evita
                # cobrar con un metodo de pago de un pedido anterior.
                if tipo_entrega != "envio":
                    metodo_pago_usado = ""

                requiere_pago_online = metodo_pago_usado in ("Tarjeta", "Transferencia")

                if requiere_pago_online:
                    # No confirmamos el pedido todavia — lo guardamos en
                    # estado 'pendiente_pago' (no aparece en cocina) y le
                    # mandamos al cliente un link de Mercado Pago. Solo
                    # cuando el webhook de pagos confirme el pago aprobado,
                    # el pedido pasa a 'nuevo' y aparece en la cocina.
                    referencia = f"taqueria_{telefono}_{int(datetime.datetime.now().timestamp())}"
                    pedido_id = db.guardar_pedido(
                        negocio_id=negocio_id, telefono=telefono,
                        nombre_cliente=nombre_cl, items=carrito,
                        total=total_con_envio, tipo_entrega=tipo_entrega,
                        direccion=direccion, notas=notas,
                        metodo_pago=metodo_pago_usado,
                        estado_pago="pendiente",
                        estado_inicial="pendiente_pago",
                        extras_pedido=extras_sesion,
                    )
                    if not pedido_id:
                        resp = "Hubo un problema guardando tu pedido. Por favor intenta de nuevo en un momento."
                        enviar_whatsapp(telefono, resp, token, phone_number_id)
                        return

                    url_webhook = "https://bot-taqueria.onrender.com/webhook_pago"
                    resultado_mp = pagos.crear_link_pago(
                        pedido_referencia=referencia,
                        nombre_negocio=nombre_neg,
                        descripcion=f"Pedido #{pedido_id}",
                        monto=total_con_envio,
                        url_notificacion=url_webhook,
                    )

                    if not resultado_mp:
                        # Fallo Mercado Pago — no dejamos al cliente colgado,
                        # ofrecemos efectivo como respaldo.
                        resp = (
                            "Tuvimos un problema generando el link de pago. 😕\n"
                            "¿Prefieres pagar en efectivo al recibir tu pedido?"
                        )
                        nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                        db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="pago", carrito=carrito)
                        enviar_whatsapp(telefono, resp, token, phone_number_id)
                        return

                    # Guardamos la referencia de Mercado Pago en el pedido
                    # para poder encontrarlo cuando llegue el webhook.
                    db.actualizar_pedido_referencia_mp(pedido_id, resultado_mp["preference_id"])

                    resp = (
                        f"📋 Tu pedido #{pedido_id} está listo, solo falta el pago.\n\n"
                        f"💰 Total a pagar: *{_fmt_precio(total_con_envio)}*\n"
                        f"💳 Método: *{metodo_pago_usado}*\n\n"
                        f"Paga aquí de forma segura:\n{resultado_mp['link_pago']}\n\n"
                        f"_En cuanto se confirme tu pago, tu pedido pasará a preparación automáticamente._ ✅"
                    )
                    db.limpiar_sesion(llave)
                    nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                    db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:])
                    enviar_whatsapp(telefono, resp, token, phone_number_id)
                    print(f"   [{nombre_neg}] Pedido #{pedido_id} pendiente de pago ({metodo_pago_usado}) — link generado.")
                    return

                # Efectivo (o sin metodo de pago, ej. para recoger): se
                # confirma directo, igual que antes.
                pedido_id = db.guardar_pedido(
                    negocio_id=negocio_id, telefono=telefono,
                    nombre_cliente=nombre_cl, items=carrito,
                    total=total_con_envio, tipo_entrega=tipo_entrega,
                    direccion=direccion, notas=notas,
                    metodo_pago=metodo_pago_usado,
                    extras_pedido=extras_sesion,
                )
                tiempo = tiempo_env if tipo_entrega == "envio" else tiempo_rec
                notas_linea = f"📝 {notas}\n" if notas else ""
                pago_linea = f"💳 Pago: *{metodo_pago_usado}*\n" if metodo_pago_usado else ""
                extras_linea = ""
                if extras_sesion:
                    extras_txt = ", ".join(
                        f"{e.get('cantidad',1)}x {e['ingrediente']} (en {e['producto']})" for e in extras_sesion
                    )
                    extras_linea = f"➕ Extras: {extras_txt}\n"
                resp = (
                    f"✅ *¡Pedido #{pedido_id} confirmado!*\n\n"
                    f"👤 *{nombre_cl}*\n"
                    f"{'🛵 Envío a: ' + direccion if tipo_entrega == 'envio' else '🏪 Para recoger en el local'}\n"
                    f"{pago_linea}"
                    f"{notas_linea}"
                    f"{extras_linea}"
                    f"⏱ Tiempo estimado: *{tiempo} minutos*\n"
                    f"💰 Total: *{_fmt_precio(total_con_envio)}*\n\n"
                    f"¡Gracias por tu preferencia! 🌮\n"
                    f"_Tu pedido ya está en preparación._"
                )
                if email_notif:
                    notificar_dueno(
                        email_notif,
                        f"Nuevo pedido #{pedido_id} en {nombre_neg}",
                        _html_correo_pedido(
                            nombre_neg, nombre_cl, telefono, carrito,
                            total_con_envio, tipo_entrega, direccion,
                            metodo_pago_usado or metodos_pago, notas,
                            extras=extras_sesion,
                        ),
                    )
                db.limpiar_sesion(llave)
                nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:])
                enviar_whatsapp(telefono, resp, token, phone_number_id)
                print(f"   [{nombre_neg}] Pedido #{pedido_id} confirmado — {nombre_cl} / ${total_con_envio:.2f}")
                return

            elif any(p in texto_low for p in ["no", "cancel", "cambiar", "modificar", "error"]):
                resp = (
                    "Sin problema. ¿Qué quieres hacer?\n\n"
                    "1️⃣ *Agregar* un producto\n"
                    "2️⃣ *Quitar* un producto\n"
                    "3️⃣ *Cancelar* el pedido\n\n"
                    "Solo dime qué necesitas, por ejemplo: "
                    "_\"agrega una horchata\"_ o _\"quita el alambre\"_"
                )
                nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="armando", carrito=carrito)
                enviar_whatsapp(telefono, resp, token, phone_number_id)
                return

            else:
                # El cliente dijo algo que no es SÍ ni NO (saludo, pregunta,
                # etc.) — le recordamos que tiene un pedido pendiente en vez
                # de caer al LLM que puede ignorar la fase y hacer cualquier cosa.
                costo_envio_real = sesion.get("costo_envio_calc", 0)
                resumen = _formato_carrito(carrito, costo_envio_real if sesion.get("tipo_entrega") == "envio" else 0)
                resp = (
                    f"Tienes un pedido pendiente de confirmar 👆\n\n"
                    f"{resumen}\n\n"
                    f"¿Lo confirmas?\n"
                    f"✅ *SÍ* para confirmar  |  ❌ *NO* para modificar"
                )
                nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="confirmando", carrito=carrito)
                enviar_whatsapp(telefono, resp, token, phone_number_id)
                return

        # ── FASE 'armando': cliente esta modificando tras volver del cierre ──
        # Si esta en 'armando' (porque pidio modificar durante el cierre) y
        # dice una frase de cierre ("es todo", "ya", "listo") o directamente
        # el tipo de entrega ("recoger", "a domicilio"), disparamos el cierre
        # deterministico en vez de dejar que el LLM lo interprete (lo trataba
        # raro: usaba "Recoger" para mostrar el carrito en vez de avanzar).
        # Asi el cierre se reanuda limpio tras la modificacion.
        _cerrar_desde_armando = False
        if fase == "armando" and carrito:
            # Frases de cierre — se comparan como PALABRAS COMPLETAS (word
            # boundaries) para evitar falsos positivos de subcadenas: "ya"
            # dentro de "playa"/"desayuno" no debe disparar el cierre.
            _FRASES_CIERRE_ARMANDO = [
                "es todo", "eso es todo", "ya es todo", "seria todo", "sería todo",
                "ya", "listo", "nada mas", "nada más", "asi esta bien",
                "así está bien", "ya esta", "ya está", "con eso", "es to",
            ]
            _t_arm = texto_low.strip(".,!¡¿? ")
            _es_frase_cierre = any(
                re.search(rf"\b{re.escape(f)}\b", _t_arm) for f in _FRASES_CIERRE_ARMANDO
            )
            _menciona_recoger = any(
                re.search(rf"\b{re.escape(p)}\b", _t_arm)
                for p in ["recoger", "paso por", "para llevar", "en el local"]
            )
            _menciona_envio = any(
                re.search(rf"\b{re.escape(p)}\b", _t_arm)
                for p in ["domicilio", "envio", "envío", "enviar a", "mandar a"]
            )
            if (_es_frase_cierre or _menciona_recoger or _menciona_envio) and not _quiere_modificar_carrito(texto_low):
                tipo_prev = sesion.get("tipo_entrega")
                if _menciona_recoger:
                    tipo_prev = "recoger"
                elif _menciona_envio:
                    tipo_prev = "envio"
                if tipo_prev:
                    sesion["tipo_entrega"] = tipo_prev
                _cerrar_desde_armando = True

        # ── MODIFICAR CARRITO DURANTE EL CIERRE ─────────────────────────────
        # Si el cliente, estando en una fase de cierre (tipo/nombre/direccion/
        # pago), pide agregar o quitar algo en vez de responder lo que se le
        # preguntó, cambiamos la fase a "armando" y DEJAMOS QUE EL LLM PROCESE
        # ESTE MISMO MENSAJE (no lo descartamos). Esto es clave: el mensaje
        # "me falta un agua de jamaica de litro" ya trae el producto, así que
        # el LLM debe agregarlo de una vez. Si tragáramos el mensaje con un
        # "dime qué quieres ajustar", se perdería el contexto y el cliente
        # tendría que repetir el producto desde cero (bug real visto en logs:
        # "De jamaica" llegaba suelto y el LLM mandaba "agua de litro" sin
        # sabor). Cuando el cliente termine ("es todo" / "recoger"), el cierre
        # se reanuda reutilizando tipo/nombre/dirección que ya tuviéramos.
        if fase in ("tipo", "nombre", "direccion", "pago") and _quiere_modificar_carrito(texto_low):
            # Persistimos el cambio de fase conservando los datos ya
            # capturados (tipo, nombre, direccion) para retomar el cierre.
            db.guardar_sesion(llave, historial, fase_pedido="armando", carrito=carrito,
                              tipo_entrega=sesion.get("tipo_entrega", ""),
                              extras_pedido=sesion.get("extras_pedido", []))
            print(f"   [{nombre_neg}] Cliente quiere modificar carrito en fase '{fase}' — procesando cambio en modo armado.")
            # Cambiamos la fase local y dejamos que el flujo siga hasta el
            # LLM (NO retornamos), para que procese el producto de este mensaje.
            fase = "armando"
            sesion["fase_pedido"] = "armando"

        # FASE: nombre — esperando el nombre del cliente
        if fase == "nombre":
            # Limpiar prefijos conversacionales que no son parte del nombre
            # Ej: "ok Mario" -> "Mario", "oye soy Ana" -> "Ana"
            _texto_limpio = texto.strip()
            for _prefijo in ["ok ", "okay ", "oye ", "oye, ", "soy ", "me llamo ", "mi nombre es ", "a nombre de ", "el nombre es ", "mi nombre ", "nombre: ", "nombre es "]:
                if _texto_limpio.lower().startswith(_prefijo):
                    _texto_limpio = _texto_limpio[len(_prefijo):]
                    break
            nombre_cl = _texto_limpio.strip().title()
            # Frases y palabras que NO son nombres. "vale" es ambiguo
            # (significa "ok" pero tambien es apodo de Valentina/Valeria),
            # asi que lo tratamos aparte: solo lo rechazamos si el cliente
            # lo escribio en minusculas (señal de "ok"), no si lo escribio
            # capitalizado como nombre propio.
            _NO_ES_NOMBRE_PALABRAS = {
                "no","si","sí","ok","okay","cancel","cancelar",
                "modificar","cambiar","espera","otro","otra","nada","ninguno",
                # Palabras de fase de cierre — un cliente que las escribe
                # esta respondiendo el flujo, no dando su nombre. Bug real:
                # "Recoger" se guardo como nombre del cliente (Pedido Recoger).
                "recoger","envio","envío","domicilio","llevar",
                "efectivo","tarjeta","transferencia","mercadopago",
            }
            _NO_ES_NOMBRE_FRASES = [
                "para llevar","para recoger","para envio","para envío",
                "a domicilio","sin cilantro","sin cebolla","sin chile",
                "con todo","sin todo","para mi","es todo","ya es todo",
                "nada mas","nada más",
            ]
            texto_lower_n = texto.lower().strip(".,!¡¿? ")
            # "vale" solo cuenta como no-nombre si vino en minusculas
            _vale_es_confirmacion = texto_lower_n == "vale" and texto.strip() == texto.strip().lower()
            es_nombre_valido = (
                len(nombre_cl) >= 2
                and not any(c.isdigit() for c in nombre_cl)
                and texto_lower_n not in _NO_ES_NOMBRE_PALABRAS
                and not _vale_es_confirmacion
                and not any(f in texto_lower_n for f in _NO_ES_NOMBRE_FRASES)
                and len(nombre_cl.split()) <= 4
            )
            if es_nombre_valido:
                carrito      = sesion["carrito"]
                tipo_entrega = sesion["tipo_entrega"]
                direccion    = sesion["direccion_entrega"]

                # Si es ENVÍO, antes del resumen final preguntamos el método
                # de pago (el dueño confirmó que pide el pago por adelantado
                # para pedidos a domicilio). Si es para recoger, se salta este
                # paso — el cliente paga al llegar al local.
                if tipo_entrega == "envio":
                    metodos_lista = ", ".join(m.strip() for m in metodos_pago.split(","))
                    resp = (
                        f"Para pedidos con envío a domicilio pedimos el pago por adelantado.\n\n"
                        f"¿Cómo deseas pagar? Opciones: *{metodos_lista}*"
                    )
                    db.guardar_sesion(llave, historial, fase_pedido="pago",
                                      nombre_cliente=nombre_cl, carrito=carrito)
                    nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                    db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="pago",
                                      nombre_cliente=nombre_cl, carrito=carrito)
                    enviar_whatsapp(telefono, resp, token, phone_number_id)
                    print(f"   [{nombre_neg}] Nombre capturado: {nombre_cl}, pidiendo método de pago.")
                    return

                # Recoger: no se pregunta metodo de pago, va directo al resumen.
                # IMPORTANTE: aunque aqui nunca deberiamos tener un [PAGO:..]
                # legitimo (la fase "pago" no aplica a recoger), igual lo
                # limpiamos por seguridad — si quedo arrastrado de un pedido
                # anterior, NUNCA debe mostrarse crudo al cliente.
                _, notas = _extraer_pago_de_notas(sesion.get("notas_pedido", ""))
                extras_sesion = sesion.get("extras_pedido", [])
                total        = _calcular_total(carrito, extras_sesion)
                resumen = _formato_carrito(carrito, extras=extras_sesion)
                notas_linea = f"📝 *Notas:* {notas}\n" if notas else ""
                resp = (
                    f"📋 *Resumen de tu pedido*\n"
                    f"━━━━━━━━━━━━━━\n"
                    f"{resumen}\n\n"
                    f"👤 *Nombre:* {nombre_cl}\n"
                    f"🏪 *Para recoger* en el local\n"
                    f"{notas_linea}\n"
                    f"¿Todo está bien? Responde:\n"
                    f"✅ *SÍ* para confirmar\n"
                    f"❌ *NO* para modificar"
                )
                db.guardar_sesion(llave, historial, fase_pedido="confirmando",
                                  nombre_cliente=nombre_cl, carrito=carrito)
                nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="confirmando",
                                  nombre_cliente=nombre_cl, carrito=carrito)
                enviar_whatsapp(telefono, resp, token, phone_number_id)
                print(f"   [{nombre_neg}] Nombre capturado: {nombre_cl}, mostrando resumen.")
                return
            else:
                # El cliente escribio algo que no parece un nombre — puede
                # ser una pregunta suelta ("¿hasta que hora abren?") en
                # medio del flujo. No lo mandamos al LLM libre (eso podria
                # sacarlo de la fase sin querer); solo reconocemos el
                # mensaje y volvemos a pedir el dato pendiente, para no
                # perder el progreso del pedido.
                resp = (
                    "Veo que me escribiste algo más, ¡con gusto te ayudo después! 😊\n"
                    "Por ahora, ¿me confirmas a nombre de quién registramos tu pedido?"
                )
                nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="nombre", carrito=carrito)
                enviar_whatsapp(telefono, resp, token, phone_number_id)
                print(f"   [{nombre_neg}] Texto no parece nombre ('{texto[:30]}'), reiterando pregunta.")
                return

        # FASE: pago — esperando metodo de pago (solo para pedidos de envio)
        if fase == "pago":
            metodos_validos = [m.strip().lower() for m in metodos_pago.split(",")]
            metodo_elegido = None
            for m in metodos_validos:
                if m in texto_low:
                    metodo_elegido = m.title()
                    break
            if not metodo_elegido:
                # Tambien aceptamos sinonimos comunes
                if any(p in texto_low for p in ["efect", "cash"]):
                    metodo_elegido = "Efectivo"
                elif any(p in texto_low for p in ["transf", "deposito", "spei"]):
                    metodo_elegido = "Transferencia"
                elif any(p in texto_low for p in ["tarjeta", "card", "credito", "débito", "debito"]):
                    metodo_elegido = "Tarjeta"

            if metodo_elegido:
                carrito      = sesion["carrito"]
                nombre_cl    = sesion["nombre_cliente"]
                direccion    = sesion["direccion_entrega"]
                # Limpiamos cualquier [PAGO:..] viejo arrastrado ANTES de
                # mostrar las notas o de envolverlas con el nuevo metodo —
                # si no se hace esto, un [PAGO:Tarjeta] viejo termina
                # mostrandose crudo, o anidado dentro de un nuevo [PAGO:..].
                _, notas = _extraer_pago_de_notas(sesion.get("notas_pedido", ""))
                extras_sesion = sesion.get("extras_pedido", [])
                costo_envio_real = sesion.get("costo_envio_calc", 0)
                total        = _calcular_total(carrito, extras_sesion)
                total_con_envio = total + costo_envio_real
                resumen = _formato_carrito(carrito, costo_envio_real, extras_sesion)
                notas_linea = f"📝 *Notas:* {notas}\n" if notas else ""
                resp = (
                    f"📋 *Resumen de tu pedido*\n"
                    f"━━━━━━━━━━━━━━\n"
                    f"{resumen}\n\n"
                    f"👤 *Nombre:* {nombre_cl}\n"
                    f"🛵 *Envío a:* {direccion}\n"
                    f"💳 *Pago:* {metodo_elegido}\n"
                    f"{notas_linea}\n"
                    f"¿Todo está bien? Responde:\n"
                    f"✅ *SÍ* para confirmar\n"
                    f"❌ *NO* para modificar"
                )
                # Guardamos el metodo de pago dentro de notas_pedido con un
                # prefijo reconocible (no hay columna dedicada para esto en
                # la sesion). Se extrae despues al confirmar el pedido, ya
                # limpio (sin anidar con un [PAGO:..] anterior).
                notas_con_pago = f"[PAGO:{metodo_elegido}] {notas}".strip()
                nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="confirmando",
                                  carrito=carrito, notas_pedido=notas_con_pago)
                enviar_whatsapp(telefono, resp, token, phone_number_id)
                print(f"   [{nombre_neg}] Método de pago: {metodo_elegido}, mostrando resumen.")
                return
            else:
                metodos_lista = ", ".join(m.strip() for m in metodos_pago.split(","))
                resp = f"No reconocí ese método de pago. Por favor elige una opción: *{metodos_lista}*"
                nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="pago", carrito=carrito)
                enviar_whatsapp(telefono, resp, token, phone_number_id)
                return
        if fase == "direccion":
            direccion = texto.strip()

            # Si el cliente PEGA un link de Google Maps como texto (en vez de
            # usar el boton nativo de "Compartir ubicacion" de WhatsApp),
            # extraemos las coordenadas del link con regex y lo tratamos
            # igual que una ubicacion GPS real — evita geocodificar la URL
            # cruda como si fuera una direccion de texto, lo cual no funciona.
            if not coords_ubicacion and ("maps.google" in direccion or "google.com/maps" in direccion or "goo.gl/maps" in direccion):
                match = re.search(r"[?&]q=(-?\d+\.\d+)[,%2C\s]+(-?\d+\.\d+)", direccion)
                if not match:
                    match = re.search(r"@(-?\d+\.\d+),(-?\d+\.\d+)", direccion)
                if match:
                    coords_ubicacion = (float(match.group(1)), float(match.group(2)))
                    print(f"   [{nombre_neg}] Coordenadas extraídas de link de Maps: {coords_ubicacion}")

            # Si el cliente comparte su ubicacion GPS (tipo "location" de
            # WhatsApp, o un link de Maps con coordenadas extraidas arriba),
            # usamos las coordenadas exactas directo — es mas preciso que
            # pedirle que escriba la direccion, y mas rapido (sin geocoding
            # de texto). Guardamos un texto legible para el resumen y correo.
            if coords_ubicacion:
                direccion = "📍 Ubicación compartida por WhatsApp"
                aviso_envio = ""

                if envio_dinamico and coords_negocio:
                    resultado = geo.calcular_envio_desde_coords(coords_ubicacion, coords_negocio, lluvia=modo_lluvia)
                    if resultado["ok"]:
                        costo_calculado = resultado["costo"]
                        km = resultado["km"]
                        aviso_envio = f"\n_Distancia: {km} km — Envío: {_fmt_precio(costo_calculado)}_"
                        # Texto legible con la distancia + link aparte, para
                        # que no se vea como una URL larga y cruda pegada al
                        # texto. El link sigue siendo clicable para el dueño
                        # y los repartidores (correo, botón de cocina, etc.).
                        lat, lng = coords_ubicacion
                        direccion = f"📍 Ubicación compartida ({km} km)\nhttps://maps.google.com/?q={lat},{lng}"
                    elif "Fuera de cobertura" in resultado["razon"]:
                        resp = (
                            f"Tu ubicación está fuera de nuestra zona de cobertura para envío "
                            f"({resultado['km']} km, máximo 20 km). "
                            f"¿Prefieres pasar a recoger tu pedido al local?"
                        )
                        nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                        db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="tipo", carrito=carrito)
                        enviar_whatsapp(telefono, resp, token, phone_number_id)
                        print(f"   [{nombre_neg}] Ubicación fuera de cobertura ({resultado['km']} km).")
                        return
                    else:
                        # Fallo el calculo de distancia con la API (raro, pero
                        # puede pasar). Ya NO hay tarifa fija de respaldo —
                        # bloqueamos el pedido y pedimos que llamen directo,
                        # para no arriesgarnos a cobrar de menos o de más.
                        print(f"   [{nombre_neg}] No se pudo calcular distancia desde ubicación: {resultado['razon']}")
                        resp = (
                            "Por el momento no pude calcular automáticamente el costo de envío a tu ubicación. 😕\n"
                            "¿Podrías llamarnos directo para confirmar tu pedido y el costo de envío?"
                        )
                        nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                        db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="direccion", carrito=carrito)
                        enviar_whatsapp(telefono, resp, token, phone_number_id)
                        print(f"   [{nombre_neg}] Pedido bloqueado — fallo cálculo de envío, se pidió llamar directo.")
                        return
                else:
                    # El negocio no tiene configurado el cálculo dinámico de
                    # envío — sin tarifa fija de respaldo, no podemos
                    # calcular el costo. Bloqueamos igual que arriba.
                    resp = (
                        "Por el momento no puedo calcular automáticamente el costo de envío. 😕\n"
                        "¿Podrías llamarnos directo para confirmar tu pedido y el costo de envío?"
                    )
                    nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                    db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="direccion", carrito=carrito)
                    enviar_whatsapp(telefono, resp, token, phone_number_id)
                    print(f"   [{nombre_neg}] Pedido bloqueado — envío dinámico no configurado, se pidió llamar directo.")
                    return

                resp = f"¿A nombre de quién registramos el pedido?{aviso_envio}"
                db.guardar_sesion(llave, historial, fase_pedido="nombre",
                                  direccion_entrega=direccion, carrito=carrito,
                                  costo_envio_calc=costo_calculado)
                nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="nombre",
                                  direccion_entrega=direccion, carrito=carrito,
                                  costo_envio_calc=costo_calculado)
                enviar_whatsapp(telefono, resp, token, phone_number_id)
                print(f"   [{nombre_neg}] Ubicación GPS capturada — envío: ${costo_calculado}")
                return

            if len(direccion) >= 5:
                aviso_envio = ""

                if envio_dinamico and coords_negocio:
                    resultado = geo.calcular_envio_completo(direccion, coords_negocio, lluvia=modo_lluvia)
                    if resultado["ok"]:
                        costo_calculado = resultado["costo"]
                        km = resultado["km"]
                        aviso_envio = f"\n_Distancia: {km} km — Envío: {_fmt_precio(costo_calculado)}_"
                    else:
                        # No se pudo calcular (direccion no encontrada o fuera
                        # de cobertura): avisamos y pedimos que confirme o
                        # de una direccion mas precisa, sin trabar el flujo.
                        if "Fuera de cobertura" in resultado["razon"]:
                            resp = (
                                f"Tu dirección está fuera de nuestra zona de cobertura para envío "
                                f"({resultado['km']} km, máximo 20 km). "
                                f"¿Prefieres pasar a recoger tu pedido al local?"
                            )
                            nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                            db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="tipo", carrito=carrito)
                            enviar_whatsapp(telefono, resp, token, phone_number_id)
                            print(f"   [{nombre_neg}] Dirección fuera de cobertura ({resultado['km']} km).")
                            return
                        else:
                            resp = (
                                "No logré ubicar bien esa dirección. ¿Puedes darme más detalles "
                                "(calle, número, colonia), o mejor mándame tu ubicación con el "
                                "📎 de WhatsApp para calcular el envío exacto?"
                            )
                            nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                            db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="direccion", carrito=carrito)
                            enviar_whatsapp(telefono, resp, token, phone_number_id)
                            print(f"   [{nombre_neg}] No se pudo geocodificar: '{direccion}' — {resultado['razon']}")
                            return
                else:
                    # El negocio no tiene configurado el cálculo dinámico de
                    # envío — sin tarifa fija de respaldo, no podemos
                    # calcular el costo. Bloqueamos y pedimos llamar directo.
                    resp = (
                        "Por el momento no puedo calcular automáticamente el costo de envío. 😕\n"
                        "¿Podrías llamarnos directo para confirmar tu pedido y el costo de envío?"
                    )
                    nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                    db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="direccion", carrito=carrito)
                    enviar_whatsapp(telefono, resp, token, phone_number_id)
                    print(f"   [{nombre_neg}] Pedido bloqueado — envío dinámico no configurado, se pidió llamar directo.")
                    return

                resp = f"¿A nombre de quién registramos el pedido?{aviso_envio}"
                db.guardar_sesion(llave, historial, fase_pedido="nombre",
                                  direccion_entrega=direccion, carrito=carrito,
                                  costo_envio_calc=costo_calculado)
                nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="nombre",
                                  direccion_entrega=direccion, carrito=carrito,
                                  costo_envio_calc=costo_calculado)
                enviar_whatsapp(telefono, resp, token, phone_number_id)
                print(f"   [{nombre_neg}] Dirección capturada: {direccion[:40]} — envío: ${costo_calculado}")
                return
            else:
                # El texto es muy corto para ser una direccion real (ej.
                # "ok", o una pregunta suelta). Reiteramos la pregunta sin
                # perder la fase, en vez de dejar que caiga al LLM libre.
                resp = (
                    "No alcancé a identificar bien tu dirección. ¿Me la compartes de nuevo "
                    "(calle, número, colonia), o mándame tu ubicación con el 📎 de WhatsApp?"
                )
                nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="direccion", carrito=carrito)
                enviar_whatsapp(telefono, resp, token, phone_number_id)
                print(f"   [{nombre_neg}] Texto muy corto para dirección ('{texto[:30]}'), reiterando pregunta.")
                return

        # FASE: campechano:carnes — el cliente acaba de decir cuales 2 carnes
        # quiere en sus campechanos. Guardamos esa nota y seguimos el flujo
        # normal de cierre: si quedan categorias por personalizar (salsa de
        # los tacos, etc.) preguntamos eso; si no, pasamos a tipo de entrega.
        if fase == "campechano:carnes":
            notas_existentes = sesion.get("notas_pedido", "")
            nota_nueva = f"Campechano carnes: {texto.strip()}."
            notas_combinadas = f"{notas_existentes} {nota_nueva}".strip() if notas_existentes else nota_nueva

            # ¿Quedan categorias de personalizacion por preguntar (incluida
            # la salsa de los propios campechanos, que son categoria Tacos)?
            pendientes = _categorias_pendientes_personalizacion(carrito, menu, notas_combinadas)
            if pendientes:
                siguiente = pendientes[0]
                resp = f"¡Anotado! Ahora, {_CATEGORIAS_PERSONALIZABLES[siguiente]['pregunta']}"
                nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido=f"personalizacion:{siguiente}",
                                  carrito=carrito, notas_pedido=notas_combinadas)
                enviar_whatsapp(telefono, resp, token, phone_number_id)
                print(f"   [{nombre_neg}] Carnes de campechano capturadas, preguntando {siguiente} a continuación.")
                return

            # No quedan personalizaciones de ingredientes -> salsa global o tipo de entrega.
            extras_sesion = sesion.get("extras_pedido", [])
            if _falta_salsa_global(carrito, menu, notas_combinadas):
                resp = f"¡Anotado! Ahora, {_PREGUNTA_SALSA_GLOBAL}"
                nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="personalizacion_salsa",
                                  carrito=carrito, notas_pedido=notas_combinadas,
                                  extras_pedido=extras_sesion)
                enviar_whatsapp(telefono, resp, token, phone_number_id)
                print(f"   [{nombre_neg}] Carnes de campechano capturadas — preguntando salsa global.")
                return
            if tipo_servicio == "recoger":
                resp = f"{_formato_carrito(carrito, extras=extras_sesion)}\n\n¿Es para recoger en el local?"
                nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="nombre",
                                  tipo_entrega="recoger", carrito=carrito, notas_pedido=notas_combinadas)
                enviar_whatsapp(telefono, resp, token, phone_number_id)
            elif tipo_servicio == "envio":
                resp = (
                    f"{_formato_carrito(carrito, extras=extras_sesion)}\n"
                    "_El costo de envío se calculará según tu dirección._\n\n"
                    "¿Cuál es tu dirección de entrega? 📍 También puedes compartir tu ubicación con el clip de WhatsApp para mayor precisión."
                )
                nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="direccion",
                                  carrito=carrito, notas_pedido=notas_combinadas)
                enviar_whatsapp(telefono, resp, token, phone_number_id)
            else:
                resp = (
                    f"{_formato_carrito(carrito, extras=extras_sesion)}\n\n"
                    "¿Es para *recoger en el local* o *envío a domicilio*?"
                )
                nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="tipo",
                                  carrito=carrito, notas_pedido=notas_combinadas)
                enviar_whatsapp(telefono, resp, token, phone_number_id)
            print(f"   [{nombre_neg}] Carnes de campechano capturadas, continuando a tipo de entrega.")
            return

        # FASE: personalizacion_salsa — pregunta GLOBAL de salsa/piña/verdura
        # para TODO el pedido (una sola vez, al final de las personalizaciones
        # de ingredientes por categoria). El cliente responde algo como
        # "roja", "verde y piña", "roja con todo", etc.
        if fase == "personalizacion_salsa":
            # Guard de idempotencia: si la salsa ya fue capturada en esta sesion
            # (marca SALSA: en notas), ignoramos mensajes duplicados de Meta.
            _notas_actuales = sesion.get("notas_pedido", "")
            if "SALSA:" in _notas_actuales and not "SALSA_PARCIAL:" in _notas_actuales:
                # Salsa ya procesada — re-enviamos el carrito a tipo de entrega
                # sin volver a tocar nada.
                _extras_curr = sesion.get("extras_pedido", [])
                resp_dup = (
                    f"{_formato_carrito(carrito, extras=_extras_curr)}\n\n"
                    "¿Es para *recoger en el local* o *envío a domicilio*?"
                )
                enviar_whatsapp(telefono, resp_dup, token, phone_number_id)
                return
            _rec, _nota_salsa = _parsear_salsa_verdura(texto_low)
            notas_existentes = sesion.get("notas_pedido", "")
            extras_sesion = sesion.get("extras_pedido", [])
            # Recuperar lo que el cliente ya dijo (con todo/aparte/piña)
            # antes de que supiéramos la salsa — guardado en campo separado.
            _parcial_previo = sesion.get("salsa_parcial", "")

            _t_salsa = texto_low.strip(".,!¡¿? ")
            _es_fase_salsa = any(
                re.search(rf"\b{re.escape(p)}\b", _t_salsa)
                for p in ["recoger", "a domicilio", "domicilio", "para llevar",
                          "envio", "envío", "cancelar", "cancela"]
            )
            if _es_fase_salsa:
                _nota_salsa = "a gusto del cliente"
            elif not _rec:
                # No se entendió nada — reintento completo
                resp_retry = f"No quite entendí 😊 {_PREGUNTA_SALSA_GLOBAL}"
                nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp_retry)]
                db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="personalizacion_salsa",
                                  carrito=carrito, notas_pedido=notas_existentes,
                                  extras_pedido=extras_sesion)
                enviar_whatsapp(telefono, resp_retry, token, phone_number_id)
                return
            elif not any(k in texto_low for k in list(_SALSAS_RECONOCIDAS.keys()) + ["ninguna", "sin salsa", "ambas", "las dos", "los dos", "las 2", "los 2", "2 salsas", "dos salsas"]):
                # Respondió con todo/aparte/piña pero SIN elegir salsa.
                # Guardamos lo que dijo usando el helper que REEMPLAZA
                # (no acumula) — así cada reintento sobreescribe el anterior.
                resp_retry = "¡Anotado! Solo dime la salsa: ¿*roja* o *verde*? 🌮"
                notas_con_parcial = _actualizar_nota_categoria(notas_existentes, "SALSA_PARCIAL", _nota_salsa)
                nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp_retry)]
                db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="personalizacion_salsa",
                                  carrito=carrito, notas_pedido=notas_con_parcial,
                                  extras_pedido=extras_sesion)
                enviar_whatsapp(telefono, resp_retry, token, phone_number_id)
                return

            # Combinar salsa con lo parcial previo si existe, luego limpiar la marca
            _parcial_previo = ""
            if "SALSA_PARCIAL:" in notas_existentes:
                m = re.search(r'SALSA_PARCIAL:\s*([^.]+)', notas_existentes)
                _parcial_previo = m.group(1).strip() if m else ""
                notas_existentes = re.sub(r'\s*SALSA_PARCIAL:[^.]*\.?\s*', ' ', notas_existentes).strip()
            if _parcial_previo:
                # Combinar sin duplicar: separar en componentes, deduplicar
                # preservando orden. Evita "salsa roja, salsa verde, salsa
                # roja, salsa verde" cuando ambos mensajes traian salsa.
                _componentes = []
                for fragmento in [_nota_salsa, _parcial_previo]:
                    for parte in fragmento.split(","):
                        parte = parte.strip()
                        if parte and parte not in _componentes:
                            _componentes.append(parte)
                _nota_salsa = ", ".join(_componentes)

            nota_salsa_fmt = f"SALSA: {_nota_salsa}"
            notas_combinadas = (
                f"{notas_existentes} {nota_salsa_fmt}".strip()
                if notas_existentes else nota_salsa_fmt
            )
            print(f"   [{nombre_neg}] Salsa global capturada: '{_nota_salsa}' — continuando a tipo de entrega.")

            # Si se adelanto con palabra de fase, procesarla aqui
            if _es_fase_salsa:
                if any(p in _t_salsa for p in ["recoger", "para llevar"]):
                    resp = f"{_formato_carrito(carrito, extras=extras_sesion)}\n\n¿A nombre de quién registramos el pedido? 😊"
                    nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                    db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="nombre",
                                      tipo_entrega="recoger", carrito=carrito,
                                      notas_pedido=notas_combinadas, extras_pedido=extras_sesion)
                    enviar_whatsapp(telefono, resp, token, phone_number_id)
                    return
                elif any(p in _t_salsa for p in ["domicilio", "envio", "envío"]):
                    resp = (
                        "¿Cuál es tu dirección de entrega? 📍 También puedes compartir "
                        "tu ubicación con el clip de WhatsApp para mayor precisión."
                    )
                    nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                    db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="direccion",
                                      carrito=carrito, notas_pedido=notas_combinadas,
                                      extras_pedido=extras_sesion)
                    enviar_whatsapp(telefono, resp, token, phone_number_id)
                    return

            # Flujo normal: ir a tipo de entrega
            resp = (
                f"{_formato_carrito(carrito, extras=extras_sesion)}\n\n"
                "¿Es para *recoger en el local* o *envío a domicilio*?"
            )
            nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
            db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="tipo",
                              carrito=carrito, notas_pedido=notas_combinadas,
                              extras_pedido=extras_sesion)
            enviar_whatsapp(telefono, resp, token, phone_number_id)
            return

        # FASE: personalizacion:<Categoria> — preguntando la personalizacion
        # de UNA categoria especifica del pedido (ingredientes a quitar para
        # hamburguesas, quesadillas, especialidades, tortas; tipo de tortilla
        # para papas rellenas). Se pregunta UNA SOLA VEZ por cada categoria
        # presente en el carrito (nunca por producto individual), encadenando
        # a la siguiente categoria pendiente si hay mas de una en el pedido.
        # Despues de todas las categorias viene la pregunta de salsa global.
        if fase.startswith("personalizacion:"):
            categoria_actual = fase.split(":", 1)[1]
            config_cat = _CATEGORIAS_PERSONALIZABLES.get(categoria_actual, {"estricta": False})

            # GUARDA: si el cliente, en vez de personalizar, responde una
            # palabra de FASE (recoger, a domicilio, cancelar...) o pide
            # modificar el carrito, NO la guardamos como nota absurda (bug
            # real: "Hamburguesas: Recoger"). En su lugar, dejamos la
            # personalizacion con un valor por defecto ("con todo") y pasamos
            # ese mensaje al flujo que corresponde. Esto pasa cuando el
            # cliente se adelanta al flujo.
            _t_pers = texto_low.strip(".,!¡¿? ")
            _es_palabra_fase = any(
                re.search(rf"\b{re.escape(p)}\b", _t_pers)
                for p in ["recoger", "a domicilio", "domicilio", "para llevar",
                          "cancelar", "efectivo", "tarjeta", "transferencia"]
            )
            if _es_palabra_fase and not _parsear_salsa_verdura(texto_low)[0]:
                # Guardamos la categoria actual con valor por defecto y
                # marcamos que el cliente ya dijo algo del cierre, para que
                # el resto del flujo (mas abajo) lo procese. Avanzamos la
                # personalizacion sin trabar al cliente.
                notas_existentes = sesion.get("notas_pedido", "")
                nota_default = f"{categoria_actual}: con todo."
                notas_combinadas = f"{notas_existentes} {nota_default}".strip() if notas_existentes else nota_default
                # Reusamos el resto de categorias pendientes (si las hay).
                pendientes = _categorias_pendientes_personalizacion(carrito, menu, notas_combinadas)
                if pendientes:
                    # Aun quedan categorias: preguntamos la siguiente.
                    siguiente = pendientes[0]
                    resp = _CATEGORIAS_PERSONALIZABLES[siguiente]["pregunta"]
                    nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                    db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido=f"personalizacion:{siguiente}",
                                      carrito=carrito, notas_pedido=notas_combinadas)
                    enviar_whatsapp(telefono, resp, token, phone_number_id)
                    print(f"   [{nombre_neg}] Cliente se adelantó con palabra de fase; {categoria_actual} con valor default, preguntando {siguiente}.")
                    return
                # No quedan mas personalizaciones: procesamos el tipo de
                # entrega que el cliente adelantó, AQUI mismo, sin caer al
                # resto del codigo de personalizacion (que volveria a guardar
                # la nota). Detectamos recoger/envio y avanzamos.
                _quiere_recoger = any(re.search(rf"\b{re.escape(p)}\b", _t_pers) for p in ["recoger", "para llevar"])
                _quiere_envio = any(re.search(rf"\b{re.escape(p)}\b", _t_pers) for p in ["a domicilio", "domicilio"])
                if _quiere_recoger or (tipo_servicio == "recoger"):
                    resp = (
                        f"{_formato_carrito(carrito, extras=sesion.get('extras_pedido', []))}\n\n"
                        "¿A nombre de quién registramos el pedido? 😊"
                    )
                    nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                    db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="nombre",
                                      tipo_entrega="recoger", carrito=carrito,
                                      notas_pedido=notas_combinadas, extras_pedido=sesion.get("extras_pedido", []))
                    enviar_whatsapp(telefono, resp, token, phone_number_id)
                    print(f"   [{nombre_neg}] {categoria_actual} con default; cliente eligió recoger — pidiendo nombre.")
                    return
                elif _quiere_envio or (tipo_servicio == "envio"):
                    resp = (
                        f"{_formato_carrito(carrito, extras=sesion.get('extras_pedido', []))}\n"
                        "_El costo de envío se calculará según tu dirección._\n\n"
                        "¿Cuál es tu dirección de entrega? 📍 También puedes compartir tu ubicación con el clip de WhatsApp para mayor precisión."
                    )
                    nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                    db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="direccion",
                                      tipo_entrega="envio", carrito=carrito,
                                      notas_pedido=notas_combinadas, extras_pedido=sesion.get("extras_pedido", []))
                    enviar_whatsapp(telefono, resp, token, phone_number_id)
                    print(f"   [{nombre_neg}] {categoria_actual} con default; cliente eligió envío — pidiendo dirección.")
                    return
                else:
                    # Dijo "cancelar"/"efectivo" u otra palabra: preguntamos
                    # tipo de entrega normalmente (negocio acepta ambos).
                    resp = (
                        f"{_formato_carrito(carrito, extras=sesion.get('extras_pedido', []))}\n\n"
                        "¿Es para *recoger en el local* o *envío a domicilio*?"
                    )
                    nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                    db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="tipo",
                                      carrito=carrito, notas_pedido=notas_combinadas,
                                      extras_pedido=sesion.get("extras_pedido", []))
                    enviar_whatsapp(telefono, resp, token, phone_number_id)
                    print(f"   [{nombre_neg}] {categoria_actual} con default; preguntando tipo de entrega.")
                    return

            # DETECCION DE RESPUESTA "FUERA DE TURNO": el bot pregunta por la
            # categoria_actual (ej. Hamburguesas) pero el cliente responde
            # mencionando EXPLICITAMENTE otra categoria presente y pendiente
            # (ej. "roja y con todo LOS TACOS"). Bug real visto en produccion:
            # esa nota se guardaba bajo la categoria equivocada. Si la mencion
            # es inequivoca, guardamos la nota bajo la categoria CORRECTA y
            # volvemos a preguntar la actual. Solo redirige cuando:
            #   - el texto menciona UNA sola otra categoria (sin ambiguedad), y
            #   - esa categoria sigue pendiente (no se le ha preguntado), y
            #   - NO menciona la categoria_actual (si menciona ambas, es del
            #     turno actual y lo dejamos pasar normal).
            _pend_para_redirigir = [
                c for c in _categorias_pendientes_personalizacion(carrito, menu, sesion.get("notas_pedido", ""))
                if c != categoria_actual
            ]
            _cat_mencionada = _categoria_mencionada_en_texto(texto_low, _pend_para_redirigir)
            _menciona_actual = bool(_categoria_mencionada_en_texto(texto_low, [categoria_actual]))
            if _cat_mencionada and not _menciona_actual:
                # La respuesta es para OTRA categoria. La parseamos segun el
                # tipo de esa categoria (estricta = salsa/verdura; no estricta
                # = texto tal cual) y la guardamos bajo la categoria correcta.
                config_otra = _CATEGORIAS_PERSONALIZABLES.get(_cat_mencionada, {"estricta": False})
                if config_otra["estricta"]:
                    _reconocido_otra, _nota_otra = _parsear_salsa_verdura(texto_low)
                    _texto_nota_otra = _nota_otra if (_reconocido_otra and _nota_otra) else "con todo (sin preferencia de salsa especificada)"
                else:
                    _texto_nota_otra = texto.strip()
                notas_existentes = sesion.get("notas_pedido", "")
                notas_combinadas = _actualizar_nota_categoria(notas_existentes, _cat_mencionada, _texto_nota_otra)
                # ¿Sigue pendiente la categoria_actual u otra? Re-preguntamos.
                pendientes = _categorias_pendientes_personalizacion(carrito, menu, notas_combinadas)
                if pendientes:
                    siguiente = pendientes[0]
                    resp = f"¡Anotado lo de {_cat_mencionada.lower()}! Ahora, {_CATEGORIAS_PERSONALIZABLES[siguiente]['pregunta']}"
                    nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                    db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido=f"personalizacion:{siguiente}",
                                      carrito=carrito, notas_pedido=notas_combinadas,
                                      extras_pedido=sesion.get("extras_pedido", []))
                    enviar_whatsapp(telefono, resp, token, phone_number_id)
                    print(f"   [{nombre_neg}] Respuesta fuera de turno: nota redirigida a {_cat_mencionada}, ahora preguntando {siguiente}.")
                    return
                # Ya no quedan categorias pendientes tras redirigir.
                # Antes de tipo de entrega: preguntar SALSA GLOBAL si falta.
                if _falta_salsa_global(carrito, menu, notas_combinadas):
                    resp = f"¡Anotado lo de {_cat_mencionada.lower()}! Ahora, {_PREGUNTA_SALSA_GLOBAL}"
                    nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                    db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="personalizacion_salsa",
                                      carrito=carrito, notas_pedido=notas_combinadas,
                                      extras_pedido=sesion.get("extras_pedido", []))
                    enviar_whatsapp(telefono, resp, token, phone_number_id)
                    print(f"   [{nombre_neg}] Respuesta fuera de turno: nota redirigida a {_cat_mencionada} — preguntando salsa global.")
                    return
                db.guardar_sesion(llave, historial, fase_pedido="tipo", carrito=carrito,
                                  notas_pedido=notas_combinadas, extras_pedido=sesion.get("extras_pedido", []))
                resp = (
                    f"{_formato_carrito(carrito, extras=sesion.get('extras_pedido', []))}\n\n"
                    "¿Es para *recoger en el local* o *envío a domicilio*?"
                )
                nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="tipo",
                                  carrito=carrito, notas_pedido=notas_combinadas,
                                  extras_pedido=sesion.get("extras_pedido", []))
                enviar_whatsapp(telefono, resp, token, phone_number_id)
                print(f"   [{nombre_neg}] Respuesta fuera de turno: nota redirigida a {_cat_mencionada}, todas las categorías listas, preguntando tipo.")
                return

            if config_cat["estricta"]:
                reconocido, nota_cat = _parsear_salsa_verdura(texto_low)
                ultimo_ai = ""
                for m in reversed(historial):
                    if isinstance(m, AIMessage):
                        ultimo_ai = m.content
                        break
                es_reintento = "no alcancé a identificar" in ultimo_ai.lower()

                if not reconocido and not es_reintento:
                    resp = (
                        "No alcancé a identificar tu respuesta 🙏 ¿Me confirmas la salsa "
                        "(roja, guacamole, piña o ninguna) y si van con todo o con la "
                        "verdura aparte?"
                    )
                    nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                    db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido=fase, carrito=carrito)
                    enviar_whatsapp(telefono, resp, token, phone_number_id)
                    print(f"   [{nombre_neg}] Respuesta de {categoria_actual} no reconocida, reintentando una vez.")
                    return

                texto_nota = nota_cat if (reconocido and nota_cat) else "con todo (sin preferencia de salsa especificada)"
            else:
                # No estricta: aceptamos la respuesta del cliente tal cual.
                # PERO: si el cliente hizo una PREGUNTA (ej. "¿qué formas de
                # pago tienes?") en vez de personalizar, NO la guardamos como
                # nota de cocina (bug pedido #60: la nota decia "Especialidades:
                # Qué formas de pago tienes"). La detectamos y reiteramos la
                # pregunta de personalizacion sin trabar el flujo.
                _t_strip = texto.strip()
                _es_pregunta = (
                    "?" in _t_strip or "¿" in _t_strip
                    or any(_t_strip.lower().startswith(p) for p in [
                        "que ", "qué ", "cual ", "cuál ", "cuanto ", "cuánto ",
                        "como ", "cómo ", "tienen ", "hay ", "puedo ", "aceptan ",
                        "donde ", "dónde ", "cuando ", "cuándo "])
                )
                if _es_pregunta:
                    # Responder preguntas comunes brevemente y volver a preguntar
                    # la personalizacion. Para "formas de pago" damos la info.
                    _resp_pregunta = ""
                    if any(k in _t_strip.lower() for k in ["forma de pago", "formas de pago", "como pago", "cómo pago", "puedo pagar", "aceptan", "metodo de pago", "método de pago"]):
                        _resp_pregunta = "Aceptamos *efectivo*, *tarjeta* y *transferencia*. 💳\n\n"
                    resp = (
                        _resp_pregunta
                        + _CATEGORIAS_PERSONALIZABLES[categoria_actual]["pregunta"]
                    )
                    nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                    db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido=fase,
                                      carrito=carrito, notas_pedido=sesion.get("notas_pedido", ""),
                                      extras_pedido=sesion.get("extras_pedido", []))
                    enviar_whatsapp(telefono, resp, token, phone_number_id)
                    print(f"   [{nombre_neg}] Cliente preguntó algo en personalizacion:{categoria_actual} — respondido sin guardar como nota.")
                    return
                texto_nota = _t_strip

            notas_existentes = sesion.get("notas_pedido", "")
            notas_combinadas = _actualizar_nota_categoria(notas_existentes, categoria_actual, texto_nota)

            # ¿Quedan mas categorias del carrito por preguntar?
            pendientes = _categorias_pendientes_personalizacion(carrito, menu, notas_combinadas)
            if pendientes:
                siguiente = pendientes[0]
                resp = f"¡Anotado! Ahora, {_CATEGORIAS_PERSONALIZABLES[siguiente]['pregunta']}"
                nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido=f"personalizacion:{siguiente}",
                                  carrito=carrito, notas_pedido=notas_combinadas)
                enviar_whatsapp(telefono, resp, token, phone_number_id)
                print(f"   [{nombre_neg}] {categoria_actual} personalizado, preguntando {siguiente} a continuación.")
                return

            # Ya no quedan categorias de ingredientes pendientes.
            # Antes de tipo de entrega: preguntar SALSA GLOBAL si falta.
            extras_sesion = sesion.get("extras_pedido", [])
            if _falta_salsa_global(carrito, menu, notas_combinadas):
                resp = f"¡Anotado! Ahora, {_PREGUNTA_SALSA_GLOBAL}"
                nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="personalizacion_salsa",
                                  carrito=carrito, notas_pedido=notas_combinadas,
                                  extras_pedido=extras_sesion)
                enviar_whatsapp(telefono, resp, token, phone_number_id)
                print(f"   [{nombre_neg}] {categoria_actual} personalizado — preguntando salsa global.")
                return
            if tipo_servicio == "recoger":
                resp = f"{_formato_carrito(carrito, extras=extras_sesion)}\n\n¿Es para recoger en el local?"
                nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="nombre",
                                  tipo_entrega="recoger", carrito=carrito, notas_pedido=notas_combinadas)
                enviar_whatsapp(telefono, resp, token, phone_number_id)
            elif tipo_servicio == "envio":
                resp = (
                    f"{_formato_carrito(carrito, extras=extras_sesion)}\n"
                    "_El costo de envío se calculará según tu dirección._\n\n"
                    "¿Cuál es tu dirección de entrega? 📍 También puedes compartir tu ubicación con el clip de WhatsApp para mayor precisión."
                )
                nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="direccion",
                                  tipo_entrega="envio", carrito=carrito, notas_pedido=notas_combinadas)
                enviar_whatsapp(telefono, resp, token, phone_number_id)
            else:
                resp = (
                    f"{_formato_carrito(carrito, extras=extras_sesion)}\n\n"
                    "¿Es para *recoger en el local* o *envío a domicilio*?"
                )
                nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="tipo",
                                  carrito=carrito, notas_pedido=notas_combinadas)
                enviar_whatsapp(telefono, resp, token, phone_number_id)
            print(f"   [{nombre_neg}] Todas las personalizaciones capturadas: '{notas_combinadas}' — continuando a tipo de entrega.")
            return

        # FASE: tipo — esperando si es para recoger o envío
        if fase == "tipo":
            if any(p in texto_low for p in ["recoger", "local", "paso", "voy", "ahi voy"]):
                if tipo_servicio == "envio":
                    resp = "Solo manejamos envío a domicilio. ¿Me das tu dirección de entrega?"
                    db.guardar_sesion(llave, historial, fase_pedido="direccion",
                                      tipo_entrega="envio", carrito=carrito)
                    nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                    db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="direccion",
                                      tipo_entrega="envio", carrito=carrito)
                else:
                    resp = "¿A nombre de quién registramos el pedido?"
                    db.guardar_sesion(llave, historial, fase_pedido="nombre",
                                      tipo_entrega="recoger", carrito=carrito)
                    nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                    db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="nombre",
                                      tipo_entrega="recoger", carrito=carrito)
                enviar_whatsapp(telefono, resp, token, phone_number_id)
                return

            elif any(p in texto_low for p in ["envio", "envío", "domicilio", "llevar", "lleva", "mandar", "delivery"]):
                if tipo_servicio == "recoger":
                    resp = "Solo manejamos recolección en el local. ¿A qué nombre registramos tu pedido?"
                    db.guardar_sesion(llave, historial, fase_pedido="nombre",
                                      tipo_entrega="recoger", carrito=carrito)
                    nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                    db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="nombre",
                                      tipo_entrega="recoger", carrito=carrito)
                    enviar_whatsapp(telefono, resp, token, phone_number_id)
                    return

                # Validar pedido minimo ANTES de pasar a pedir direccion —
                # el minimo solo aplica a envio, asi que es aqui donde
                # corresponde checarlo, no antes (cuando aun no sabiamos
                # si era recoger o envio).
                total_actual = _calcular_total(carrito)
                if pedido_min > 0 and total_actual < pedido_min:
                    falta = pedido_min - total_actual
                    resumen = _formato_carrito(carrito)
                    resp = (
                        f"{resumen}\n\n"
                        f"_Para envío a domicilio el pedido mínimo es de {_fmt_precio(pedido_min)}. "
                        f"Te faltan {_fmt_precio(falta)}._\n\n"
                        f"¿Quieres agregar algo más, o prefieres pasar a recoger tu pedido sin mínimo?"
                    )
                    nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                    db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="tipo", carrito=carrito)
                    enviar_whatsapp(telefono, resp, token, phone_number_id)
                    return

                resp = "¿Cuál es tu dirección de entrega? 📍 También puedes compartir tu ubicación con el clip de WhatsApp para mayor precisión."
                db.guardar_sesion(llave, historial, fase_pedido="direccion",
                                  tipo_entrega="envio", carrito=carrito)
                nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="direccion",
                                  tipo_entrega="envio", carrito=carrito)
                enviar_whatsapp(telefono, resp, token, phone_number_id)
                return

            else:
                # El cliente escribio algo que no es "recoger" ni "envio"
                # (ej. una pregunta suelta). No lo mandamos al LLM libre
                # porque eso podria sacarlo de la fase sin querer y perder
                # el carrito. Reiteramos la pregunta pendiente.
                resp = (
                    "Antes de seguir, ¿me confirmas si tu pedido es para *recoger en el local* "
                    "o *envío a domicilio*? 😊"
                )
                nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="tipo", carrito=carrito)
                enviar_whatsapp(telefono, resp, token, phone_number_id)
                print(f"   [{nombre_neg}] Texto no reconocido en fase 'tipo' ('{texto[:30]}'), reiterando pregunta.")
                return

        # ── LLM CON HERRAMIENTAS ─────────────────────────────────────────────
        # El modelo maneja: saludo, mostrar menu, agregar/quitar productos del
        # carrito, y disparar el cierre del pedido cuando el cliente diga "ya es
        # todo" / "eso seria todo" / etc.

        # Herramientas disponibles para el modelo
        carrito_estado = list(carrito)  # mutable dentro del turno
        extras_estado = list(sesion.get("extras_pedido", []))  # mutable dentro del turno
        # Snapshot del carrito ANTES de procesar tools. Sirve para detectar
        # el bug de "productos perdidos": si el LLM agrega bebidas pero por
        # alguna razon el carrito pierde productos que NO se pidieron quitar,
        # restauramos los faltantes. Bug real (pedido #60): cliente tenia 10
        # tacos + especial, agrego bebidas, y los 10 tacos desaparecieron.
        carrito_snapshot = [dict(c) for c in carrito]

        # Mapa de "orden de [carne]" → (nombre_real, cantidad_paquete).
        # Definido aquí para ser accesible tanto en agregar_al_carrito como
        # en el guard de re-agregado (_es_reagregado).
        _ORDENES_CARNES = {
            "pastor":            ("Taco de Pastor",            2),
            "bistec":            ("Taco de Bistec",            3),
            "sirloin":           ("Taco de Sirloin",           2),
            "chorizo argentino": ("Taco de Chorizo Argentino", 2),
            "chorizo":           ("Taco de Chorizo",           3),
            "campechano":        ("Taco Campechano",           3),
        }
        _EXCL_ORDEN = ("tortillas", "cebolla", "cebolla asada")

        @tool
        def ver_menu() -> str:
            """Muestra el menu completo con precios al cliente."""
            return _formato_menu(menu)

        @tool
        def info_producto(nombre_producto: str) -> str:
            """Usa esta herramienta cuando el cliente pregunta que lleva o que
            ingredientes tiene un producto especifico (ej. '¿qué lleva la Juana?',
            '¿qué es el Volcán?', '¿tiene queso?'). Devuelve la descripcion exacta
            del producto segun el menu — NUNCA inventes ingredientes."""
            item = _buscar_en_menu(nombre_producto, menu)
            if not item:
                return f"No encontré '{nombre_producto}' en el menú. ¿Te refieres a otro producto?"
            desc = item.get("descripcion", "").strip()
            if not desc:
                return (
                    f"*{item['nombre']}* — {_fmt_precio(float(item['precio']))}\n"
                    f"_No tenemos descripción adicional de este producto._"
                )
            return (
                f"*{item['nombre']}* lleva: {desc}.\n"
                f"Precio: {_fmt_precio(float(item['precio']))} 🌮"
            )

        @tool
        def agregar_al_carrito(nombre_producto: str, cantidad: int = 1) -> str:
            """Agrega un producto al carrito del cliente.
            nombre_producto: copia TEXTUALMENTE las palabras que el cliente usó
            para nombrar el producto. Por ejemplo, si el cliente dice "quiero 2
            que me ves", pasa nombre_producto="que me ves" — NUNCA lo cambies
            por una categoría como "tacos" ni inventes un nombre distinto.
            cantidad: cuantos quiere (default 1).
            NO LLAMES esta herramienta si el cliente solo está ACLARANDO,
            REPITIENDO o RE-EXPLICANDO algo que ya pidió (frases como "te
            pedí X", "en total son X", "ya te dije X", "serían X"), ni si
            solo PREGUNTA un precio ("¿en cuánto están?"). En esos casos el
            producto YA está en el carrito o el cliente no quiere agregar
            nada — volver a agregar descontrola las cantidades.
            IMPORTANTE: si el cliente menciona un producto generico que tiene
            varias variantes en el menu (ej. 'volcán' cuando hay Volcán de
            Pastor/Bistec/Sirloin/Chorizo, o 'agua' cuando hay Agua 1L y ½L),
            esta herramienta te devolverá la lista de opciones — debes
            preguntarle al cliente cuál quiere en vez de elegir tú.
            El resultado SIEMPRE incluye el carrito completo actualizado con
            precios y subtotal — usa ese texto tal cual en tu respuesta al
            cliente, no lo reescribas ni lo resumas tú mismo."""
            # RED DE SEGURIDAD: a veces el LLM no separa la cantidad del
            # nombre del producto y pasa todo junto en nombre_producto (ej.
            # nombre_producto="3 tacos de sirloin", cantidad=1 sin
            # especificar) — esto hace que se agregue solo 1 pieza en vez
            # de las 3 que el cliente pidio, un bug real visto en
            # produccion. Si detectamos un numero al INICIO del nombre Y la
            # cantidad sigue en su valor default (1, señal de que el LLM no
            # la extrajo aparte), tomamos ese numero como la cantidad real.
            match_cantidad = re.match(r'^(\d+)\s+(.+)', nombre_producto.strip())
            if match_cantidad and cantidad == 1:
                cantidad = int(match_cantidad.group(1))
                nombre_producto = match_cantidad.group(2)

            # NORMALIZACIÓN: "orden de [carne]" = paquete de tacos con promo.
            # Ej: "orden de sirloin" → "taco de sirloin" con cantidad=2 (promo 2x$50)
            #     "orden de bistec" → "taco de bistec" con cantidad=3 (promo 3x$60)
            # Excepción: "orden de tortillas" y "orden de cebolla asada" son
            # extras para kilos — esos NO se tocan aquí.
            _nom_low = nombre_producto.strip().lower()
            if ("orden de " in _nom_low or _nom_low.startswith("orden ")) and not any(e in _nom_low for e in _EXCL_ORDEN):
                _carne_raw = re.sub(r'^(una?\s+)?orden\s+de\s+', '', _nom_low).strip()
                if _carne_raw in _ORDENES_CARNES and cantidad == 1:
                    _nombre_real, _cant_promo = _ORDENES_CARNES[_carne_raw]
                    print(f"   [{nombre_neg}] 'orden de {_carne_raw}' → {_cant_promo}x {_nombre_real} (paquete promo)")
                    nombre_producto = _nombre_real
                    cantidad = _cant_promo

            # Cantidad 0 o negativa: el cliente dijo algo como "0 tacos" o
            # "quita" mal interpretado — no agregamos nada y pedimos que
            # aclare, en vez de convertir el 0 a 1 silenciosamente (bug
            # real visto en pruebas: "0 tacos de pastor" agregaba 1 taco).
            if cantidad <= 0:
                return (
                    f"¿Cuántos '{nombre_producto}' te gustaría? Dime una cantidad "
                    "y con gusto lo agrego a tu pedido. 😊"
                )

            coincidencias = _buscar_coincidencias(nombre_producto, menu)

            if not coincidencias:
                # Detectar el contexto de lo que pidió para dar sugerencias
                # útiles en vez de listar todo el menú (44 productos).
                _texto_bus = nombre_producto.lower()
                _resp_no_encontrado = ""

                # "orden de X" donde X no es una carne reconocida
                if "orden de " in _texto_bus or _texto_bus.startswith("orden "):
                    _ordenes_disp = (
                        "  • Orden de Bistec — 3 tacos por $60\n"
                        "  • Orden de Sirloin — 2 tacos por $50\n"
                        "  • Orden de Chorizo — 3 tacos por $60\n"
                        "  • Orden de Chorizo Argentino — 2 tacos por $50\n"
                        "  • Orden de Campechano — 3 tacos por $65\n"
                        "  • Orden de Pastor — 2 tacos (2x1, $26)"
                    )
                    _resp_no_encontrado = (
                        f"No encontré una orden de *\"{nombre_producto}\"* en el menú 🤔\n\n"
                        f"Las órdenes de tacos disponibles son:\n{_ordenes_disp}\n\n"
                        f"¿Cuál te gustaría? 😊"
                    )
                # Palabras clave de categorías — sugerir esa categoría
                elif any(k in _texto_bus for k in ["taco", "tacos"]):
                    _tacos = [i for i in menu if "taco" in i["nombre"].lower()]
                    _opciones = "\n".join(f"  • {i['nombre']} — {_fmt_precio(float(i['precio']))}" for i in _tacos)
                    _resp_no_encontrado = f"No encontré *\"{nombre_producto}\"* 🤔 Los tacos que tenemos son:\n{_opciones}"
                elif any(k in _texto_bus for k in ["volcan", "volcán"]):
                    _items = [i for i in menu if "volc" in i["nombre"].lower()]
                    _opciones = "\n".join(f"  • {i['nombre']} — {_fmt_precio(float(i['precio']))}" for i in _items)
                    _resp_no_encontrado = f"No encontré *\"{nombre_producto}\"* 🤔 Los volcanes disponibles son:\n{_opciones}"
                elif any(k in _texto_bus for k in ["hamburguesa", "burger"]):
                    _items = [i for i in menu if "hamburguesa" in i["nombre"].lower()]
                    _opciones = "\n".join(f"  • {i['nombre']} — {_fmt_precio(float(i['precio']))}" for i in _items)
                    _resp_no_encontrado = f"No encontré *\"{nombre_producto}\"* 🤔 Las hamburguesas disponibles son:\n{_opciones}"
                elif any(k in _texto_bus for k in ["torta", "tortas"]):
                    _items = [i for i in menu if "torta" in i["nombre"].lower()]
                    _opciones = "\n".join(f"  • {i['nombre']} — {_fmt_precio(float(i['precio']))}" for i in _items)
                    _resp_no_encontrado = f"No encontré *\"{nombre_producto}\"* 🤔 Las tortas disponibles son:\n{_opciones}"
                elif any(k in _texto_bus for k in ["agua", "refresco", "bebida", "soda"]):
                    _items = [i for i in menu if any(k in i["nombre"].lower() for k in ["agua","refresco"])]
                    _opciones = "\n".join(f"  • {i['nombre']} — {_fmt_precio(float(i['precio']))}" for i in _items)
                    _resp_no_encontrado = f"No encontré *\"{nombre_producto}\"* 🤔 Las bebidas disponibles son:\n{_opciones}"
                elif any(k in _texto_bus for k in ["quesadilla", "gringa", "juana", "sincronizada"]):
                    _items = [i for i in menu if any(k in i["nombre"].lower() for k in ["gringa","juana","sincronizada","quesadilla"])]
                    _opciones = "\n".join(f"  • {i['nombre']} — {_fmt_precio(float(i['precio']))}" for i in _items)
                    _resp_no_encontrado = f"No encontré *\"{nombre_producto}\"* 🤔 Las quesadillas disponibles son:\n{_opciones}"
                elif any(k in _texto_bus for k in ["especialidad", "especial", "alambre", "papa rellena", "que me ves", "que chingaos", "no que no"]):
                    _items = [i for i in menu if i.get("categoria","").lower() == "especialidades" or "alambre" in i["nombre"].lower() or "papa rellena" in i["nombre"].lower() or "especial" in i["nombre"].lower()]
                    _opciones = "\n".join(f"  • {i['nombre']} — {_fmt_precio(float(i['precio']))}" for i in _items[:8])
                    _resp_no_encontrado = f"No encontré *\"{nombre_producto}\"* 🤔 Las especialidades disponibles son:\n{_opciones}"
                elif any(k in _texto_bus for k in ["kilo", "kilogramo"]):
                    _items = [i for i in menu if "kilo" in i["nombre"].lower()]
                    _opciones = "\n".join(f"  • {i['nombre']} — {_fmt_precio(float(i['precio']))}" for i in _items)
                    _resp_no_encontrado = f"No encontré *\"{nombre_producto}\"* 🤔 Los kilos disponibles son:\n{_opciones}"
                else:
                    # Sin contexto claro — respuesta genérica pero útil
                    _resp_no_encontrado = (
                        f"No encontré *\"{nombre_producto}\"* en el menú 🤔\n\n"
                        f"¿Te refieres a alguna de estas categorías? "
                        f"tacos, volcanes, hamburguesas, tortas, quesadillas, especialidades, kilos o bebidas. "
                        f"O escribe *menú* para ver todo. 😊"
                    )
                return _resp_no_encontrado

            if len(coincidencias) > 1:
                opciones = "\n".join(
                    f"  • {i['nombre']} — {_fmt_precio(float(i['precio']))}" for i in coincidencias
                )
                # Si la cantidad pedida es >1, la incluimos en el texto de
                # forma parseable (ver RED DE SEGURIDAD mas abajo) para no
                # perderla cuando el cliente resuelva la ambiguedad sin
                # repetir el numero (ej. responde "el normal" en vez de
                # "2 del normal") — bug real visto en produccion donde la
                # cantidad original se perdia y solo se agregaba 1 pieza.
                sufijo_cant = f" (cantidad: {cantidad})" if cantidad > 1 else ""
                return (
                    f"Tenemos varias opciones, ¿cuál te gustaría? 😊\n{opciones}{sufijo_cant}"
                )

            item = coincidencias[0]

            # RED DE SEGURIDAD: a veces el LLM, en vez de pasar la frase
            # ambigua del cliente tal cual (ej. "agua de litro"), la
            # "resuelve" el mismo inventando un detalle especifico que el
            # cliente nunca dijo (ej. pasa nombre_producto="Agua
            # Fresa-Limón 1 Lt" cuando el cliente solo dijo "agua de
            # litro") — esto hace que la busqueda de mas arriba encuentre
            # una sola coincidencia "limpia" y nunca dispare la pregunta de
            # desambiguacion, porque la ambiguedad ya fue resuelta (mal)
            # antes de llegar aqui. Para detectarlo: reconstruimos una
            # version del argumento usando SOLO las palabras que el
            # cliente realmente escribio en este turno: si esa version
            # "fiel" encuentra varias coincidencias (en vez de una sola),
            # es señal de que el LLM invento la palabra que desambiguo.
            texto_normalizado = _normalizar_txt(texto_low)
            palabras_arg = [p for p in _normalizar_txt(nombre_producto).split() if len(p) > 1]
            palabras_fieles = [p for p in palabras_arg if p in texto_normalizado]
            if len(palabras_fieles) < len(palabras_arg) and palabras_fieles:
                coincidencias_fieles = _buscar_coincidencias(" ".join(palabras_fieles), menu)
                if len(coincidencias_fieles) > 1:
                    opciones = "\n".join(
                        f"  • {i['nombre']} — {_fmt_precio(float(i['precio']))}" for i in coincidencias_fieles
                    )
                    sufijo_cant = f" (cantidad: {cantidad})" if cantidad > 1 else ""
                    return f"Tenemos varias opciones, ¿cuál te gustaría? 😊\n{opciones}{sufijo_cant}"

            # RED DE SEGURIDAD: si la pregunta de desambiguacion MAS
            # RECIENTE en el historial menciona este mismo producto y
            # tenia una cantidad >1 pendiente, pero llegamos aqui con
            # cantidad=1 (el default, sin especificar), recuperamos esa
            # cantidad en vez de perderla — pasa cuando el cliente
            # responde a la desambiguacion sin repetir el numero (ej. "el
            # normal" en vez de "2 del normal"), o incluso cambia de tema
            # y el LLM "adivina" una opcion sin la cantidad original.
            if cantidad == 1:
                ultimo_ai = ""
                for m in reversed(historial):
                    if isinstance(m, AIMessage):
                        ultimo_ai = m.content
                        break
                if "Tenemos varias opciones" in ultimo_ai and item["nombre"] in ultimo_ai:
                    match_cant_previa = re.search(r'\(cantidad: (\d+)\)', ultimo_ai)
                    if match_cant_previa:
                        cantidad = int(match_cant_previa.group(1))

            # PROMO PASTOR (2x1) — REDONDEO AL PAR (confirmado con el dueño):
            # el Taco de Pastor es 2x1. La gente lo pide como "2 para 4",
            # "3 para 6", o simplemente "3 tacos". En todos los casos, por
            # cada 2 que paga se lleva 2, así que la cantidad ENTREGADA
            # siempre es par: si pide un número impar, se redondea ARRIBA al
            # siguiente par (pide 3 -> se le dan 4) y el carrito muestra esa
            # cantidad real. El precio sale solo con _precio_linea (4 tacos =
            # 2 paquetes de $26 = $52). No se le avisa al cliente; es un
            # ajuste callado del beneficio de la promo. (Decisión: se redondea
            # de forma simple sobre la cantidad de este agregado, sin intentar
            # reconciliar agregados en varios mensajes, porque casi nadie pide
            # pastor en partes.) Solo aplica a pastor; las demás carnes cobran
            # paquetes completos + piezas sueltas a precio individual.
            if item["nombre"] == "Taco de Pastor" and cantidad % 2 != 0:
                cantidad += 1

            ya_existia = False
            for c in carrito_estado:
                if c["nombre"] == item["nombre"]:
                    c["cantidad"] += max(1, cantidad)
                    ya_existia = True
                    break
            if not ya_existia:
                carrito_estado.append({
                    "nombre":   item["nombre"],
                    "precio":   float(item["precio"]),
                    "cantidad": max(1, cantidad),
                })

            carrito_txt = _formato_carrito(carrito_estado, extras=extras_estado)
            subtotal = _calcular_total(carrito_estado, extras_estado)
            aviso_min = ""
            if pedido_min > 0 and subtotal < pedido_min:
                falta = pedido_min - subtotal
                aviso_min = f"\n\n_Te faltan {_fmt_precio(falta)} para el pedido mínimo de {_fmt_precio(pedido_min)}._"
            return f"{carrito_txt}{aviso_min}\n\n¿Algo más, o ya sería todo? 😊"

        @tool
        def agregar_extra(producto: str, ingrediente: str, cantidad: int = 1) -> str:
            """Agrega un ingrediente EXTRA a un producto que YA está en el
            carrito, con cargo de $15 cada uno. Ejemplos: cliente dice 'con
            extra queso' o 'agrégale aguacate extra a la hamburguesa'.
            producto: nombre del platillo al que se le agrega el extra (debe
            coincidir con algo que ya esté en el carrito).
            ingrediente: el ingrediente extra exacto que pidió el cliente
            (ej. 'queso oaxaca', 'aguacate', 'tocino').
            cantidad: cuántas porciones extra de ese ingrediente (default 1).
            IMPORTANTE: solo existen extras de ingredientes reales del menú
            (carnes, quesos, verduras, tortillas) — NUNCA inventes un extra
            que no tenga sentido como ingrediente de cocina."""
            item_match = _buscar_en_menu(producto, [{"nombre": c["nombre"]} for c in carrito_estado])
            nombre_producto_real = item_match["nombre"] if item_match else producto
            en_carrito = any(c["nombre"].lower() == nombre_producto_real.lower() for c in carrito_estado)
            if not en_carrito:
                return (
                    f"No encontré '{producto}' en el carrito todavía — agrégalo primero "
                    f"y luego puedo añadirle el extra."
                )
            ingrediente_norm = _normalizar_txt(ingrediente)
            es_valido = any(ingrediente_norm == _normalizar_txt(v) or ingrediente_norm in _normalizar_txt(v) or _normalizar_txt(v) in ingrediente_norm
                            for v in _INGREDIENTES_EXTRA_VALIDOS)
            if not es_valido:
                lista = ", ".join(sorted({
                    "pastor", "bistec de res", "sirloin", "chorizo", "queso oaxaca",
                    "queso amarillo", "jamón", "tocino", "cebolla", "champiñones",
                    "pimiento", "piña", "orden de tortillas", "orden de cebolla asada",
                    "aguacate", "jitomate",
                }))
                return f"'{ingrediente}' no está en la lista de extras disponibles. Los extras válidos son: {lista}."
            extras_estado.append({
                "producto": nombre_producto_real,
                "ingrediente": ingrediente.strip(),
                "cantidad": max(1, cantidad),
            })
            carrito_txt = _formato_carrito(carrito_estado, extras=extras_estado)
            return f"{carrito_txt}\n\n¿Algo más, o ya sería todo? 😊"

        @tool
        def quitar_del_carrito(nombre_producto: str, cantidad: int = 0) -> str:
            """Quita un producto del carrito del cliente.
            cantidad: cuantas piezas quitar. Si el cliente dice 'quítame uno'
            o 'quita 2', pasa ese numero — se RESTA esa cantidad de la linea,
            sin eliminar el producto completo a menos que la cantidad llegue
            a 0. Si el cliente NO especifica cuántos (ej. 'quita el alambre',
            'ya no quiero las tortas'), deja cantidad=0 (default) y se
            elimina la línea completa, sin importar cuántas piezas había."""
            item = _buscar_en_menu(nombre_producto, menu)
            nombre_buscado = item["nombre"] if item else nombre_producto
            for i, c in enumerate(carrito_estado):
                if c["nombre"].lower() == nombre_buscado.lower():
                    if cantidad > 0 and c["cantidad"] > cantidad:
                        c["cantidad"] -= cantidad
                        return f"Quité {cantidad}x {nombre_buscado}. Quedan {c['cantidad']}x en tu carrito."
                    carrito_estado.pop(i)
                    # Limpiar extras huerfanos: si el producto se elimino
                    # completamente, sus extras asociados ya no tienen
                    # sentido — quitarlos evita cobrar $15 de más por un
                    # extra de un producto que ya no esta en el carrito.
                    extras_antes = len(extras_estado)
                    extras_estado[:] = [
                        e for e in extras_estado
                        if e.get("producto", "").lower() != nombre_buscado.lower()
                    ]
                    extras_quitados = extras_antes - len(extras_estado)
                    if extras_quitados:
                        return f"Eliminado: {nombre_buscado} del carrito (y {extras_quitados} extra(s) asociado(s))."
                    return f"Eliminado: {nombre_buscado} del carrito."
            return f"No encontré '{nombre_producto}' en tu carrito."

        @tool
        def ver_carrito() -> str:
            """Muestra el resumen actual del carrito."""
            # IMPORTANTE: nunca mostrar costo de envío aquí si todavía no
            # sabemos que el cliente eligió envío como tipo de entrega Y ya
            # se calculó la tarifa dinámica por distancia — antes esto
            # mostraba "Envío — $50" de forma fantasma en cualquier momento
            # de la conversación, antes de que el cliente hubiera elegido
            # recoger o envío siquiera.
            costo_a_mostrar = (
                sesion.get("costo_envio_calc", 0)
                if sesion.get("tipo_entrega") == "envio" else 0
            )
            return _formato_carrito(carrito_estado, costo_a_mostrar, extras_estado)

        @tool
        def guardar_nota(nota: str) -> str:
            """Guarda una nota o instruccion especial del cliente para su pedido.
            Usar cuando el cliente pida algo especial: 'sin cilantro', 'sin cebolla',
            'extra queso', 'bien cocido', etc.
            IMPORTANTE:
            - Incluye SIEMPRE a qué producto aplica la nota. Ej: si el cliente
              pide '2 tacos de pastor sin cebolla y 1 con todo', guarda la nota
              como '2 pastor sin cebolla, 1 pastor con todo'.
            - Esta herramienta SOLO guarda la nota. NO vuelvas a llamar
              agregar_al_carrito — el producto YA está en el carrito."""
            notas_actuales = sesion.get("notas_pedido", "")
            # Evitar duplicar una nota identica: el LLM a veces llama
            # guardar_nota dos veces con la misma frase (en turnos
            # distintos), lo que producia notas corruptas con la misma
            # instruccion repetida — bug real visto en produccion.
            nota_limpia = nota.strip()
            notas_previas_lista = [n.strip() for n in notas_actuales.split("|")] if notas_actuales else []
            if nota_limpia and nota_limpia.lower() in [n.lower() for n in notas_previas_lista]:
                # Ya existe identica, no la agregamos de nuevo.
                return f"Esa nota ya estaba guardada ✅. El producto ya está en el carrito, no lo agregues de nuevo."
            nueva_nota = f"{notas_actuales} | {nota_limpia}".strip(" |") if notas_actuales else nota_limpia
            db.guardar_sesion(llave, historial, notas_pedido=nueva_nota)
            sesion["notas_pedido"] = nueva_nota
            return f"Nota guardada: '{nota_limpia}' ✅. El producto ya está en el carrito, no lo agregues de nuevo."

        @tool
        def cerrar_pedido() -> str:
            """Llama esta herramienta cuando el cliente diga que ya termino de
            pedir (frases como: 'eso es todo', 'ya es todo', 'eso seria todo',
            'nada mas', 'con eso', 'es todo', etc.). Inicia el flujo de cierre.
            NO llames agregar_al_carrito antes de esta herramienta en el mismo turno.
            NOTA: el pedido minimo (si aplica) se valida despues, una vez que
            el cliente indique si es para recoger o envio — aqui no se valida
            porque el minimo solo aplica a pedidos con envio a domicilio."""
            carrito_actual = db.cargar_sesion(llave).get("carrito") or carrito_estado
            if not carrito_actual:
                return "El carrito está vacío. Pide algo del menú primero."
            return "INICIAR_CIERRE"

        tools = [ver_menu, info_producto, agregar_al_carrito, agregar_extra, quitar_del_carrito, ver_carrito, guardar_nota, cerrar_pedido]

        horarios = negocio.get("horarios_texto", "")
        base_conoc = negocio.get("base_conocimiento", "")

        sistema = f"""Eres el asistente de pedidos de {nombre_neg}, una taquería que atiende por WhatsApp.
Tu trabajo es tomar el pedido del cliente de forma amigable y precisa.

LÍMITE DE ROL — MUY IMPORTANTE:
Eres un asistente de PEDIDOS DE COMIDA, nada más. Si el cliente habla de temas que no tienen nada que ver con el pedido o el restaurante (vida personal, salud, relaciones, política, deportes, chismes, consejos emocionales, recomendaciones médicas, etc.), responde brevemente con amabilidad y redirige al menú. Ejemplo: "Jaja, me alegra platicar pero mi especialidad son los tacos 🌮 ¿Te puedo ayudar con algo del menú?" NO des consejos médicos, emocionales, de relaciones, ni opines sobre deportes, política o cualquier tema personal. Si el cliente dice algo sin relación al pedido, una respuesta breve y amable es suficiente — luego redirige. Tampoco te ofrezcas como "amigo" ni para "acompañarles" — eres un bot de taquería.

Horarios: {horarios or 'Consultar con el negocio.'}
Tipos de servicio disponibles: {tipo_servicio}
Costo de envío: se calcula automáticamente según la distancia, una vez que el cliente comparta su dirección o ubicación — tú nunca des un número de envío de memoria.
Pedido mínimo: {'$' + str(pedido_min) if pedido_min > 0 else 'sin mínimo'}
Métodos de pago aceptados: {metodos_pago}

{('Información adicional: ' + base_conoc) if base_conoc else ''}

EJEMPLO DE COMPORTAMIENTO CORRECTO:
Cliente: "Quiero 2 que me ves"
Tú: [llamas agregar_al_carrito con nombre_producto="que me ves", cantidad=2 — INMEDIATAMENTE, sin preguntar nada antes]
(La herramienta te responde con el carrito. Si tiene el producto, perfecto. Si hay varias opciones, recién entonces preguntas cuál.)

EJEMPLO DE PEDIDO CON VARIOS PRODUCTOS (MUY IMPORTANTE):
Cliente: "3 tacos de pastor y una hamburguesa de bistec"
Tú: [llamas agregar_al_carrito DOS VECES en el mismo turno: una con nombre_producto="taco de pastor", cantidad=3, y OTRA con nombre_producto="hamburguesa de bistec", cantidad=1]
NUNCA muestres en tu respuesta un producto que no agregaste con la herramienta — si el cliente pidió 2 productos, DEBES llamar agregar_al_carrito 2 veces (una por cada producto). Mostrar un producto en el texto sin haberlo agregado con la herramienta es un error grave: el cliente cree que está en su pedido pero no se cobra ni se prepara.

EJEMPLO CRÍTICO — DIVIDIR UNA CANTIDAD YA PEDIDA EN PREFERENCIAS (NO es agregar más):
Cliente: "4 tacos de pastor" → [agregas 4 tacos de pastor]
Cliente: "2 sin cebolla y 2 con todo" → esto se refiere a CÓMO preparar los 4 tacos QUE YA ESTÁN en el carrito, NO es un pedido de 2+2 tacos nuevos. NO llames agregar_al_carrito. Usa guardar_nota con algo como "2 tacos sin cebolla, 2 con todo" y deja la cantidad del carrito en 4. Agregar 2 tacos más aquí (dejando 6) es un error grave que le cobra de más al cliente.
Regla general: cuando el cliente describe variaciones o preferencias que SUMAN la cantidad que YA pidió (ej. ya pidió 4 y ahora dice "2 de X y 2 de Y"), es personalización, no producto nuevo.

EJEMPLO CRÍTICO — EL CLIENTE ACLARA, RE-EXPLICA O REPITE UNA CANTIDAD QUE YA PIDIÓ (NO es agregar más):
Cliente: "Quiero 5 tacos de pastor" → [agregas 5]
Cliente: "Y 4 de bistec" → [agregas 4 de bistec]
Cliente: "Pero te pedí 5 de pastor y 4 de bistec" → esto es una ACLARACIÓN de lo que YA pidió, NO un pedido nuevo. El cliente está repitiendo o confirmando lo que ya dijo, posiblemente porque cree que te equivocaste. NO llames agregar_al_carrito otra vez — ya están en el carrito. Solo confirma el estado actual mostrando el carrito (puedes usar ver_carrito si necesitas verlo). Volver a agregar 5+4 aquí (dejando 18) es un error GRAVE que descontrola las cantidades y le cobra de más.
Otras frases que son ACLARACIÓN (no pedido nuevo): "en total son X", "entonces serían X", "te dije X", "no, eran X", "son X en total", "pedí X". Cuando el cliente usa "te pedí", "te dije", "en total", "serían", o repite cantidades que ya mencionó antes, está corrigiendo tu entendimiento, NO agregando más.
Si el cliente ACLARA un número distinto al que tienes (ej. tú tienes 5 y dice "no, eran 3"), AJUSTA con quitar_del_carrito o agregar_al_carrito para llegar al número correcto que pide — NO sumes a ciegas. Piensa: "¿cuántos quiere en TOTAL?" y ajusta hasta ese total, no agregues la cantidad que mencionó encima de lo que ya hay.

EJEMPLO CRÍTICO — EL CLIENTE CAMBIA DE OPINIÓN CON "MEJOR" (REEMPLAZA, no suma):
Cliente: "Quiero 4 para 8" → [agregas 8 tacos de pastor, quedan 8]
Cliente: "Bueno mejor 5 para 10" → el cliente CAMBIÓ DE OPINIÓN: ya no quiere 8, ahora quiere 10 EN TOTAL. La palabra "mejor" significa REEMPLAZAR lo anterior, NO sumar. El total final debe ser 10, no 18 ni 16. Para lograrlo: ajusta el carrito al número nuevo. Lo más simple y seguro es quitar TODO lo de ese producto (quitar_del_carrito de los 8) y luego agregar la cantidad nueva (10) — o calcular la diferencia correcta. Piensa siempre en el TOTAL que el cliente quiere ahora (10), no en sumar la diferencia a ciegas.
Frases que significan REEMPLAZAR (cambio de opinión, dejar el total en el número nuevo): "mejor X", "bueno mejor X", "mejor que sean X", "cámbialo a X", "que sean X mejor", "no, mejor X". En todas, el número nuevo es el TOTAL final de ese producto, NO algo que se suma.
Ejemplo del error a evitar: tienes 8 tacos, cliente dice "mejor 5 para 10" (quiere 10), y tú quitas solo 2 y agregas 10 dejando 16. ESO ESTÁ MAL y le cobra de más. Debes dejar exactamente 10.
Ejemplo con productos mixtos: tienes 1 torta de pastor + 1 gringa, cliente dice "mejor 2 tortas de pastor". El TOTAL de tortas de pastor debe quedar en 2. El cliente quiere cambiar su pedido a 2 tortas. Si ya tenía 1, debes agregar 1 más (no 2), y decidir qué pasa con la gringa según el contexto — si el cliente no la mencionó, pregunta si la sigue queriendo. NO agregues 2 encima de la 1 que ya había dejando 3.

EJEMPLO CRÍTICO — EL CLIENTE PREGUNTA POR EL PRECIO O EL PEDIDO (NO es agregar nada):
Cliente: "¿En cuánto están los tacos?" / "¿Cuánto llevo?" / "¿Cuánto es el total?" → esto es una PREGUNTA, NO un pedido. NUNCA llames agregar_al_carrito. Responde la pregunta usando el carrito o el menú, sin modificar nada. Agregar productos cuando el cliente solo pregunta precio es un error grave.

EJEMPLO CRÍTICO — FORMATO "X PARA Y" (la gente pide tacos así, MUY común):
Cliente: "2 para 4" / "dame 2 para 4 de pastor" → el cliente quiere el SEGUNDO número de tacos: 4 tacos. El primer número es cuántos "paga" (por la promo). Llama agregar_al_carrito con cantidad=4 (el segundo número), NO cantidad=2.
Cliente: "5 para 10" → quiere 10 tacos. cantidad=10.
Cliente: "3 para 6" → quiere 6 tacos. cantidad=6.
Cliente: "2 juanas y 4 para 8" → quiere 2 Juanas Y 8 Tacos de Pastor. El "4 para 8" SIEMPRE es la promo de pastor, aunque en el mismo mensaje haya otros productos. NUNCA interpretes "X para Y" como cantidad de otro producto (gringas, quesadillas, volcanes, etc.) — es exclusivo de la promo de pastor.
Regla: en "X para Y", SIEMPRE usa Y (el segundo número, el mayor) como la cantidad DE TACOS DE PASTOR. El primer número es solo el precio que el cliente espera pagar; el sistema calcula el cobro solo.
Si el cliente solo dice un número normal ("dame 3 tacos de pastor"), usa ese número tal cual (cantidad=3) — el sistema se encarga del resto.

REGLAS IMPORTANTES:
- Usa SIEMPRE la herramienta agregar_al_carrito para añadir productos. NUNCA inventes precios.
- REGLA ABSOLUTA: cuando el cliente quiera pedir CUALQUIER producto, llama agregar_al_carrito INMEDIATAMENTE con las palabras que usó el cliente. NUNCA preguntes "¿qué tipo?" ni asumas que un producto tiene variantes por tu cuenta — tú NO sabes qué productos tienen variantes, solo la herramienta lo sabe. Llama la herramienta primero y deja que ELLA te diga si hay que preguntar algo.
- Solo si agregar_al_carrito te DEVUELVE un mensaje con varias opciones, entonces muéstrale esas opciones al cliente tal cual. Si la herramienta agregó el producto sin problema, NO inventes que faltaba especificar nada.
- Algunos tacos tienen promociones por cantidad (ej. Pastor es 2x1, Bistec es 3x$60, etc. — se ven marcadas en el menú). El precio final con la promo ya aplicada se calcula automáticamente y aparece en el carrito que te devuelve la herramienta — tú NUNCA calcules el precio de tacos a mano, solo usa el texto que te da la herramienta.
- Cuando el cliente pregunte qué lleva o qué ingredientes tiene un producto, usa SIEMPRE la herramienta info_producto. NUNCA inventes ingredientes ni agregues cosas que no estén en la descripción del menú.
- Cuando el cliente pida algo especial (sin cilantro, sin cebolla, extra queso, bien cocido, etc.), usa guardar_nota para registrarlo. NUNCA ignores estas instrucciones.
- NO llames agregar_al_carrito y cerrar_pedido en el mismo mensaje, NUNCA. Si el cliente responde "sí" confirmando que agregues algo (ej. tú preguntaste "¿agrego las aguas?" y dice "sí"), eso SOLO significa agregar ese producto — NO es señal de que terminó su pedido. Después de agregar, SIEMPRE pregunta "¿algo más, o ya sería todo?" y espera la respuesta del cliente antes de considerar cerrar_pedido.
- Solo llama cerrar_pedido cuando el cliente lo diga EXPLÍCITAMENTE con frases como "es todo", "ya es todo", "nada más", "eso sería todo", "con eso es todo" — un simple "sí" respondiendo a otra pregunta tuya NUNCA cuenta como esto.
- NUNCA preguntes tú mismo la dirección, ubicación, o tipo de entrega (recoger/envío) de forma libre — eso SIEMPRE debe pasar por cerrar_pedido. Si el cliente menciona "a domicilio" o "envío" de pasada mientras sigue agregando productos, no le preguntes la dirección todavía — sigue tomando su pedido normal hasta que diga explícitamente que ya terminó, y AHÍ llama cerrar_pedido, que se encargará de pedir la dirección correctamente.
- Las salsas, tipo de cocción, o personalizaciones similares NO son productos independientes del menú — son atributos de un platillo. NUNCA llames agregar_al_carrito para "salsa roja", "salsa verde", etc. Si el cliente elige una salsa para algo que ya está en su carrito, usa guardar_nota para anotarlo (ej. "Que Me Ves con salsa roja"), nunca intentes agregarla como producto nuevo.
- REGLA CRÍTICA — DESPUÉS DE CONFIRMAR UN PEDIDO: cuando el cliente acaba de confirmar un pedido (ves "✅ ¡Pedido confirmado!" en el historial) y luego dice que quiere agregar algo, pedir algo más, o hacer otro pedido, el carrito YA ESTÁ VACÍO y DEBES usar agregar_al_carrito para iniciar un nuevo pedido desde cero. NUNCA uses agregar_extra después de que el pedido fue confirmado — el carrito está limpio y no hay productos a los que agregarle extras. Ejemplo: cliente confirmó volcanes, luego dice "agrega tacos campechanos" → usa agregar_al_carrito('taco campechano'), no agregar_extra.
- REGLA CRÍTICA — "ORDEN DE [CARNE]": cuando el cliente dice "una orden de bistec", "orden de sirloin", "orden de pastor", "una de sirloin", etc., siempre significa el PAQUETE de tacos de esa carne con su promo. Ejemplos: "orden de bistec" = 3 Tacos de Bistec por $60 (promo 3x$60); "orden de sirloin" = 2 Tacos de Sirloin por $50 (promo 2x$50); "orden de pastor" = 2 Tacos de Pastor (2x1); "orden de chorizo" = 3 Tacos de Chorizo por $60. NUNCA interpretes "orden de [carne]" como agregar_extra a otro platillo — SIEMPRE es agregar_al_carrito con la cantidad del paquete correspondiente. La ÚNICA excepción es "orden de tortillas" u "orden de cebolla asada" que sí son extras para kilos de carne.
- Si el cliente ya tiene tacos en el carrito y dice "una orden de bistec" / "una de bistec" / "también de bistec", interpreta que quiere un taco de bistec — no desambigues mostrando todas las opciones de bistec (hamburguesa, torta, volcán, etc.). El contexto de tacos aplica cuando el carrito ya tiene tacos o el cliente está en un flujo de tacos. — NUNCA agregar_al_carrito ni guardar_nota para esto, porque agregar_extra es la única que suma el costo correcto ($15) al total. Si el cliente solo dice cómo quiere el platillo SIN que sea claramente un extra con costo (ej. "sin cebolla", "bien dorado"), eso sigue siendo guardar_nota, no agregar_extra. CASOS TÍPICOS de agregar_extra: "una orden de tortillas de harina" (para un kilo de carne), "extra queso", "doble carne", "más aguacate". Cuando el cliente pide "una orden de tortillas" o "una orden de cebolla" junto a un producto de kilo, SIEMPRE usa agregar_extra, NO agregar_al_carrito — esas órdenes no existen como productos independientes en el menú.
- NUNCA inventes ni elijas tú una variante específica (ej. sabor, tamaño) que el cliente no haya mencionado. Si el cliente dice "una agua de litro" sin decir el sabor, pasa nombre_producto="agua de litro" tal cual a agregar_al_carrito — NO adivines ni elijas "Agua Jamaica" o cualquier otro sabor por tu cuenta. La herramienta se encarga de preguntar si hay varias opciones; tu trabajo es pasar las palabras del cliente sin agregarles nada que él no dijo.
- REGLA CRÍTICA: si el cliente responde solo "sí", "ok", "va", "dale", "claro" u otra confirmación corta SIN mencionar ningún producto nuevo, NUNCA llames agregar_al_carrito repitiendo el último producto que pidió — eso duplicaría su pedido por error y es un fallo grave. Una confirmación corta sin producto nuevo significa que está de acuerdo con algo que dijiste (el carrito, el precio, etc.), no que quiera repetir la compra. Si no tienes claro a qué se refiere, pregúntale qué más desea agregar.
- Cuando agregues uno o varios productos, el resultado de agregar_al_carrito ya trae el carrito completo con precios y subtotal formateado. Usa ESE texto en tu respuesta tal cual (puedes agregar una frase corta antes como "¡Listo! Así va tu pedido:"), NUNCA reescribas la lista de productos tú mismo ni inventes cómo agrupar las cantidades — eso causa errores graves como mostrar productos duplicados o cantidades incorrectas.
- REGLA CRÍTICA: si el cliente PREGUNTA si tienen algo o qué opciones hay de cierta categoría (ej. "¿manejan el kilo de pastor?", "¿qué tienen de hamburguesas?", "¿tienen quesadillas?") SIN pedirlo todavía, NUNCA respondas de tu conocimiento general de qué es típico en una taquería — eso puede estar desactualizado o ser simplemente incorrecto para ESTE negocio. SIEMPRE usa la herramienta ver_menu (o intenta agregar_al_carrito si parece un pedido directo) para verificar el menú real antes de decir que sí o que no tienen algo. Negar incorrectamente un producto que sí existe es un error grave que pierde ventas reales.
- REGLA CRÍTICA — NUNCA digas "no tenemos X" sin antes verificar con ver_menu: este negocio tiene un menú amplio que incluye: alambres (Alambre, Alambre Pastor, Alambre Sirloin — en Especialidades), tortas (Torta de Pastor, Torta de Bistec, Torta de Chorizo, Torta de Sirloin, Torta Combinada — en Tortas), gringas (Gringa, Juana, Sincronizada — en Quesadillas), volcanes (de Pastor, Bistec, Sirloin, Chorizo, Chorizo Argentino, Campechano), kilos de carne (Kilo de Pastor $400, Kilo de Bistec $450, Kilo de Sirloin $500), hamburguesas, quesadillas, especialidades (Papa Rellena, Que Me Ves, Especial George's, No Que No, Que Chingaos), campechanos, tacos de todas las carnes y bebidas. Si el cliente pregunta por cualquiera de estos y no lo recuerdas del menú, usa ver_menu ANTES de responder — no asumas que no existe. Decirle a un cliente "no tenemos tortas" o "no tenemos alambres" cuando sí los hay es perder la venta y quedar mal.
- REGLA IMPORTANTE — PRODUCTOS CAMPECHANOS (2 carnes a elegir): cuando el cliente pide cualquier producto campechano del menú (Taco Campechano, Volcán Campechano o Quesadilla Campechana), estos productos siempre llevan 2 carnes a elegir de: pastor, bistec, sirloin, chorizo o chorizo argentino. Si el cliente no especifica las carnes al pedirlo, agrégalo al carrito normalmente y el sistema le preguntará las carnes automáticamente al cerrar. NO asumas las carnes ni preguntes antes de tiempo — solo agrega el producto al carrito y deja que el flujo de cierre haga la pregunta. si el cliente pide una combinación o platillo que no está en el menú pero usa ingredientes reales del negocio (ej. "sirloin con queso", "taco de sirloin con aguacate extra", "quesadilla de pastor"), NO respondas con la lista larga de todos los productos. En cambio, SUGIERE el platillo más parecido que SÍ existe: "No manejamos 'sirloin con queso' como platillo, pero tenemos la *Quesadilla Sirloin* ($65) que lleva tortilla de harina, queso oaxaca y sirloin. ¿Te gustaría esa o prefieres ver el menú completo? 😊". Identifica qué ingredientes pidió, busca el platillo más cercano del menú y sugiérelo de forma amable.
- Si el cliente pide algo que no está en el menú, indícalo claramente y ofrece alternativas.
- Cuando el cliente diga que ya terminó de pedir (eso es todo / ya es todo / nada más / con eso / etc.), llama cerrar_pedido.
- Sé breve, amigable y usa emojis con moderación.
- Si el cliente saluda sin pedir nada, preséntate brevemente y muestra el menú.
- Responde siempre en español.
"""
        if not historial:
            aviso_min_bienvenida = (
                f" Recuerda que el pedido mínimo para envío a domicilio es de {_fmt_precio(pedido_min)}."
                if pedido_min > 0 and tipo_servicio in ("envio", "ambos") else ""
            )
            sistema += f"\n\nMensaje de bienvenida sugerido: {bienvenida}{aviso_min_bienvenida}"

        llm = ChatOpenAI(
            model="gpt-4o-mini",
            temperature=0,
            api_key=os.getenv("OPENAI_API_KEY"),
        ).bind_tools(tools)

        msgs_llm = [SystemMessage(content=sistema)] + historial[-MAX_HISTORIAL:] + [HumanMessage(content=texto)]

        # Si venimos de 'armando' y el cliente ya cerró (dijo "es todo" o el
        # tipo de entrega), saltamos el LLM por completo y vamos directo al
        # cierre determinístico — el LLM trataba "Recoger" de forma errática.
        if _cerrar_desde_armando:
            texto_respuesta = ""
            tool_results = []
            iniciar_cierre = True
        else:
            resp_llm = llm.invoke(msgs_llm)

            # Procesar tool calls
            texto_respuesta = ""
            tool_results = []
            iniciar_cierre = False

        if not _cerrar_desde_armando and resp_llm.tool_calls:
            # Proteccion anti-duplicado INTELIGENTE: el modelo a veces re-agrega
            # un producto que ya estaba en dos escenarios:
            # 1) El cliente da una instruccion tipo "sin cebolla" junto con
            #    guardar_nota -> el modelo repite el producto sin necesidad.
            # 2) El cliente responde una confirmacion CORTA y generica ("si",
            #    "ok", "va", "dale") SIN mencionar ningun producto, y el
            #    modelo malinterpreta eso como "repite mi ultimo pedido".
            # En ambos casos: si el producto ya esta en el carrito con la
            # misma cantidad Y el cliente no nombro el producto en su mensaje,
            # es casi seguro un re-agregado erroneo -> lo bloqueamos.
            nombres_tools = [tc["name"] for tc in resp_llm.tool_calls]
            hay_nota = "guardar_nota" in nombres_tools
            carrito_previo = {c["nombre"]: c["cantidad"] for c in carrito_estado}

            _CONFIRMACIONES_CORTAS = {
                "si", "sí", "ok", "okay", "va", "dale", "claro", "yes",
                "correcto", "asi es", "así es", "perfecto", "vale",
            }
            es_confirmacion_corta = (
                texto_low.strip(".,!¡¿? ") in _CONFIRMACIONES_CORTAS
                and len(texto.split()) <= 3
            )

            def _es_reagregado(args: dict) -> bool:
                nom_arg = args.get("nombre_producto", "")
                cant_arg = max(1, args.get("cantidad", 1))
                # Normalizar "orden de [carne]" igual que en agregar_al_carrito
                # para que el guard pueda encontrar el producto real en el menú
                # Y use la cantidad correcta del paquete (no el 1 del LLM).
                _nom_low_g = nom_arg.strip().lower()
                _EXCL_G = ("tortillas", "cebolla", "cebolla asada")
                if ("orden de " in _nom_low_g or _nom_low_g.startswith("orden ")) and not any(e in _nom_low_g for e in _EXCL_G):
                    _carne_g = re.sub(r'^(una?\s+)?orden\s+de\s+', '', _nom_low_g).strip()
                    if _carne_g in _ORDENES_CARNES:
                        nom_arg, cant_arg = _ORDENES_CARNES[_carne_g]
                item = _buscar_en_menu(nom_arg, menu)
                if not item:
                    return False
                nombre = item["nombre"]
                cant = cant_arg  # ya normalizado (incluye cantidad del paquete si fue "orden de")
                # Ya estaba en el carrito con la misma cantidad...
                if carrito_previo.get(nombre) != cant:
                    return False
                # ...y se cumple alguna de estas 3 señales de re-agregado
                # erroneo (el LLM repitiendo algo del historial sin que el
                # cliente lo haya pedido de nuevo en ESTE mensaje):
                if hay_nota:
                    return True
                if es_confirmacion_corta:
                    return True
                # 3) NO TODAS las palabras significativas del nombre del
                #    producto aparecen en el mensaje actual del cliente
                #    (se requieren todas, no solo una, porque varios
                #    productos comparten palabras como "pastor"/"bistec" —
                #    ej. Taco de Pastor y Hamburguesa de Pastor comparten
                #    "pastor", así que NO basta con que esa palabra aparezca).
                #    Esto detecta casos como "Y 3 tacos de pastor y 1 orden
                #    de bistec" donde el modelo re-agrega "Hamburguesa de
                #    Pastor" (ya en el carrito) sin que el cliente la haya
                #    mencionado en absoluto en este turno — un bug real
                #    visto en producción que duplicó hamburguesas. También
                #    detecta el caso del turno de corrección: cliente dice
                #    "5 para 10" (solo pastor), el LLM re-agrega sirloin que
                #    ya estaba aunque el cliente no lo mencionó en ESTE mensaje.
                palabras_nombre = [
                    p for p in _normalizar_txt(nombre).split()
                    if p not in _PALABRAS_RELLENO and len(p) > 2
                ]
                # Verificamos contra el texto ACTUAL del cliente (texto_low),
                # no contra el historial completo — así "sirloin" que aparece
                # en un mensaje anterior no "justifica" re-agregarlo ahora.
                texto_actual = texto_low
                if palabras_nombre and not all(p in texto_actual for p in palabras_nombre):
                    return True
                return False

            # Nombres de productos bloqueados por re-agregado en este turno
            # (se usa abajo para tambien filtrar notas-fantasma sobre ellos).
            nombres_bloqueados = set()

            _PALABRAS_INSTRUCCION = {
                "sin", "extra", "con", "bien", "poco", "mucho", "no",
                "agregar", "picante", "dorado", "doradito", "cocido",
                "crudo", "aparte", "doble", "cebolla", "cilantro",
                "salsa", "queso", "cocida", "termino",
            }

            def _nota_es_solo_restatement(texto_nota: str, nombre_producto: str) -> bool:
                """Detecta si una nota es solo 'N <producto>' repitiendo lo
                que ya esta en el carrito (sin instruccion real como 'sin
                cebolla' o 'extra queso') — en ese caso es ruido, no una
                nota util, y se debe descartar para no ensuciar el pedido
                con notas vacias de contenido que confunden a la cocina."""
                t = _normalizar_txt(texto_nota)
                if any(p in t.split() for p in _PALABRAS_INSTRUCCION):
                    return False  # tiene contenido real, SI es una nota valida
                # Quitamos digitos y el nombre del producto; si no queda
                # nada sustancial, es pura restatement.
                t_sin_numeros = re.sub(r'\d+', '', t).strip()
                t_sin_producto = _quitar_relleno(t_sin_numeros)
                for palabra in _normalizar_txt(nombre_producto).split():
                    t_sin_producto = t_sin_producto.replace(palabra, "").strip()
                return len(t_sin_producto.strip()) <= 2  # casi nada sobra

            # Primera pasada: identificar TODOS los productos bloqueados por
            # re-agregado en este turno, sin importar el orden en que el
            # modelo haya llamado las herramientas (agregar_al_carrito podria
            # venir antes o despues de guardar_nota en la lista).
            for tc in resp_llm.tool_calls:
                if tc["name"] == "agregar_al_carrito" and _es_reagregado(tc["args"]):
                    item_bloqueado = _buscar_en_menu(tc["args"].get("nombre_producto", ""), menu)
                    if item_bloqueado:
                        nombres_bloqueados.add(item_bloqueado["nombre"])

            for tc in resp_llm.tool_calls:
                fn_name = tc["name"]
                fn_args = tc["args"]
                print(f"   [{nombre_neg}] Tool: {fn_name} {fn_args}")

                if fn_name == "agregar_al_carrito" and _es_reagregado(fn_args):
                    print(f"   [{nombre_neg}] agregar_al_carrito saltado (re-agregado erroneo: confirmacion corta o nota sin producto nuevo).")
                    tool_results.append(
                        "No agregues ese producto de nuevo — el cliente solo confirmó algo, "
                        "no pidió repetir su pedido. Pregúntale si quiere algo más."
                    )
                    continue

                if fn_name == "guardar_nota" and nombres_bloqueados:
                    nota_txt = fn_args.get("nota", "")
                    if any(_nota_es_solo_restatement(nota_txt, nb) for nb in nombres_bloqueados):
                        print(f"   [{nombre_neg}] guardar_nota saltado (solo repetía cantidad/producto ya en el carrito, sin instrucción real).")
                        tool_results.append(
                            "No guardes esa nota — solo repetía lo que ya está en el carrito, no era una instrucción especial."
                        )
                        continue

                fn_map = {
                    "ver_menu":           ver_menu,
                    "info_producto":      info_producto,
                    "agregar_al_carrito": agregar_al_carrito,
                    "agregar_extra":      agregar_extra,
                    "quitar_del_carrito": quitar_del_carrito,
                    "ver_carrito":        ver_carrito,
                    "guardar_nota":       guardar_nota,
                    "cerrar_pedido":      cerrar_pedido,
                }
                resultado = fn_map[fn_name].invoke(fn_args) if fn_name in fn_map else "Herramienta no encontrada."
                if resultado == "INICIAR_CIERRE":
                    iniciar_cierre = True
                    resultado = "Procesando cierre del pedido..."
                tool_results.append(resultado)

            # Si la UNICA tool llamada en este turno es ver_menu o
            # info_producto, usamos su resultado DIRECTO como respuesta, sin
            # pasar por una segunda llamada al modelo. Esto es necesario
            # porque el modelo tiende a "redactar de nuevo" el menu en sus
            # propias palabras y pierde el formato (emojis, precios, saltos
            # de linea) o incluso lo omite por completo — un bug real visto
            # en produccion. Instrucciones en el prompt no bastan para esto;
            # se necesita forzarlo a nivel de codigo.
            tools_llamadas = [tc["name"] for tc in resp_llm.tool_calls]
            si_respuesta_directa = (
                len(tools_llamadas) == 1
                and tools_llamadas[0] in ("ver_menu", "info_producto", "agregar_al_carrito", "agregar_extra", "ver_carrito")
                and not iniciar_cierre
            )
            # Caso de VARIAS tools donde al menos una agregó producto/extra al
            # carrito (y NO fue bloqueada por anti-duplicado). Aqui NO dejamos
            # que el LLM redacte la respuesta, porque tiende a (a) inventar que
            # "hubo un error" si alguna llamada se bloqueó, y (b) mostrar el
            # carrito incompleto o mal. En su lugar mostramos el carrito REAL
            # deterministico. Bug real visto en produccion: cliente agregó agua
            # y el LLM respondió "hubo un error al agregar el agua" mostrando
            # solo la hamburguesa, confundiendo al cliente y haciendole creer
            # que su agua no quedó registrada.
            # IMPORTANTE: solo aplicamos esto si el carrito REALMENTE creció en
            # este turno (entró algun producto nuevo o subió una cantidad). Si
            # todas las llamadas pidieron desambiguacion ("¿cuál de estas
            # opciones?"), el carrito no cambia y debemos mostrar ESAS opciones,
            # no un carrito que oculta la pregunta.
            unidades_antes = sum(c.get("cantidad", 0) for c in (carrito or []))
            unidades_despues = sum(c.get("cantidad", 0) for c in carrito_estado)
            extras_antes = len(sesion.get("extras_pedido", []) or [])
            extras_despues = len(extras_estado or [])
            carrito_crecio = (unidades_despues > unidades_antes) or (extras_despues > extras_antes)
            hubo_agregado_exitoso = (
                not iniciar_cierre
                and not si_respuesta_directa
                and any(t in ("agregar_al_carrito", "agregar_extra") for t in tools_llamadas)
                and carrito_crecio
            )
            # GUARD ADICIONAL: si el LLM llamó agregar_al_carrito + agregar_extra
            # juntos (ej. "dame un kilo y una orden de tortillas"), puede que
            # agregar_extra devuelva error ("no encontré el producto") porque el
            # kilo se acaba de agregar en el mismo turno y el orden de ejecución
            # hace que la validación falle. En ese caso, si el carrito SÍ creció
            # en unidades (el producto principal sí entró), forzamos el carrito
            # determinístico igualmente — no dejamos que el LLM improvise un
            # mensaje de "parece que hubo un error" que confunde al cliente.
            if (not hubo_agregado_exitoso
                    and not iniciar_cierre
                    and not si_respuesta_directa
                    and any(t in ("agregar_al_carrito", "agregar_extra") for t in tools_llamadas)
                    and unidades_despues > unidades_antes):
                hubo_agregado_exitoso = True
                print(f"   [{nombre_neg}] Guard extendido: carrito crecio en unidades aunque extras fallara — mostrando carrito real.")
            if si_respuesta_directa:
                texto_respuesta = tool_results[0]
            elif hubo_agregado_exitoso:
                # Marcamos que necesitamos mostrar carrito — lo construimos
                # DESPUÉS del guard de integridad (más abajo) para que el
                # carrito restaurado ya esté incluido en la respuesta.
                texto_respuesta = "__MOSTRAR_CARRITO__"
            # Segunda llamada al LLM con los resultados de las herramientas
            elif not iniciar_cierre:
                from langchain_core.messages import ToolMessage
                msgs_2 = msgs_llm + [resp_llm]
                for i, tc in enumerate(resp_llm.tool_calls):
                    msgs_2.append(ToolMessage(content=tool_results[i], tool_call_id=tc["id"]))
                resp_2 = ChatOpenAI(model="gpt-4o-mini", temperature=0,
                                    api_key=os.getenv("OPENAI_API_KEY")).invoke(msgs_2)
                texto_respuesta = resp_2.content.strip()
        elif not _cerrar_desde_armando:
            texto_respuesta = resp_llm.content.strip()

        # ── RED DE SEGURIDAD: cierre de pedido improvisado sin la tool ──────
        # A veces el LLM, en vez de llamar cerrar_pedido, improvisa
        # directamente una pregunta de "¿recoger o envío?" o "¿cuál es tu
        # dirección?" en texto libre — un bug real visto en producción que
        # deja la sesión atorada en fase vacía, donde ningún manejador
        # deterministico la reconoce. Si detectamos este patrón y el
        # carrito tiene productos, forzamos el flujo de cierre real en vez
        # de mandar la pregunta improvisada del LLM tal cual.
        if not iniciar_cierre and carrito_estado and not fase:
            _PATRONES_CIERRE_IMPROVISADO = [
                "recoger o envío", "recoger o envio", "envío o recoger", "envio o recoger",
                "recoger en el local o envío", "recoger en el local o envio",
                "para recoger o para envío", "para recoger o para envio",
                "recoger o a domicilio", "recoger o a domicilio",
            ]
            if any(p in texto_respuesta.lower() for p in _PATRONES_CIERRE_IMPROVISADO):
                print(f"   [{nombre_neg}] LLM improvisó pregunta de cierre sin llamar cerrar_pedido — forzando flujo determinístico (red de seguridad).")
                iniciar_cierre = True
                # Igual que el camino normal (cuando cerrar_pedido SI se
                # llama), no guardamos el texto improvisado en el
                # historial — el mensaje real que vera el cliente lo arma
                # el bloque de cierre determinístico de abajo, no este texto.
                texto_respuesta = ""

        # ── RED DE SEGURIDAD: el cliente SI dijo que ya es todo, pero el
        # LLM no llamo cerrar_pedido NI improviso una pregunta reconocible
        # (la red de arriba solo cubre ese segundo caso) ─────────────────
        # Bug real visto en produccion: justo despues de una pregunta de
        # desambiguacion pendiente, el cliente dijo "Es todo" y el LLM
        # simplemente volvio a mostrar el carrito sin cerrar nada, dejando
        # el pedido atorado para siempre en fase vacia. Aqui detectamos la
        # frase de cierre en el TEXTO DEL CLIENTE (no en la respuesta del
        # bot) y forzamos el cierre de todas formas, sin importar que haya
        # hecho el LLM en su lugar.
        if not iniciar_cierre and carrito_estado and not fase:
            _FRASES_CIERRE_CLIENTE = [
                "es todo", "ya es todo", "ya seria todo", "ya sería todo",
                "seria todo", "sería todo", "nada mas", "nada más",
                "eso es todo", "eso seria todo", "eso sería todo",
                "con eso es todo", "ya estaria", "ya estaría",
            ]
            texto_cliente_norm = texto_low.strip(".,!¡¿? ")
            _NEGACIONES_CIERRE = ["no es todo", "no, no es todo", "todavia no", "todavía no", "aun no", "aún no", "falta", "me falta"]
            tiene_negacion = any(n in texto_cliente_norm for n in _NEGACIONES_CIERRE)
            if not tiene_negacion and any(p in texto_cliente_norm for p in _FRASES_CIERRE_CLIENTE):
                print(f"   [{nombre_neg}] Cliente dijo frase de cierre clara pero el LLM no llamó cerrar_pedido ni improvisó pregunta reconocible — forzando cierre de todas formas (red de seguridad).")
                iniciar_cierre = True
                texto_respuesta = ""

        # GUARD DE INTEGRIDAD DEL CARRITO (bug pedido #60: productos perdidos)
        # Si el carrito perdio productos que estaban en el snapshot inicial, y
        # en este turno NO se llamo quitar_del_carrito ni se cancelo, es un
        # error: el LLM o algun reproceso "olvido" productos. Restauramos los
        # que falten para que el cliente no pierda lo que ya habia pedido.
        # NOTA: tools_llamadas puede no existir si el flujo fue determinístico
        # (saludos, fases de cierre, etc.) — usamos getattr para evitar crash.
        _tools_llamadas_guard = tools_llamadas if 'tools_llamadas' in dir() else []
        if carrito_snapshot and not iniciar_cierre:
            hubo_quitar = any(t in ("quitar_del_carrito", "vaciar_carrito") for t in _tools_llamadas_guard)
            if not hubo_quitar:
                nombres_ahora = {c["nombre"].lower() for c in carrito_estado}
                restaurados = []
                for prod_orig in carrito_snapshot:
                    nom = prod_orig["nombre"].lower()
                    if nom not in nombres_ahora:
                        carrito_estado.append(dict(prod_orig))
                        restaurados.append(prod_orig["nombre"])
                    else:
                        for c in carrito_estado:
                            if c["nombre"].lower() == nom and c["cantidad"] < prod_orig["cantidad"]:
                                c["cantidad"] = prod_orig["cantidad"]
                                restaurados.append(f"{prod_orig['nombre']} (cantidad)")
                                break
                if restaurados:
                    print(f"   [{nombre_neg}] GUARD INTEGRIDAD: restaurados productos perdidos sin quitar explicito: {restaurados}")
                    if texto_respuesta and "Tu pedido" in texto_respuesta:
                        texto_respuesta = (
                            "¡Listo! Así va tu pedido:\n\n"
                            + _formato_carrito(carrito_estado, extras=extras_estado)
                            + "\n\n¿Algo más, o ya sería todo? 😊"
                        )

        # Si el turno agregó productos, ahora construimos la respuesta del
        # carrito AQUÍ, después del guard de integridad, para que cualquier
        # producto restaurado ya esté incluido en lo que ve el cliente.
        if texto_respuesta == "__MOSTRAR_CARRITO__":
            _tool_results_carr = tool_results if 'tool_results' in dir() else []
            pendientes_ambiguos = [
                r for r in _tool_results_carr
                if isinstance(r, str) and "varias opciones" in r
            ]
            texto_respuesta = (
                "¡Listo! Así va tu pedido:\n\n"
                + _formato_carrito(carrito_estado, extras=extras_estado)
            )
            if pendientes_ambiguos:
                if len(pendientes_ambiguos) == 1:
                    texto_respuesta += "\n\n" + pendientes_ambiguos[0]
                else:
                    texto_respuesta += "\n\nPero antes necesito que me aclares un par de cosas 😊"
                    for p in pendientes_ambiguos:
                        opciones_solas = p.replace("Tenemos varias opciones, ¿cuál te gustaría? 😊\n", "")
                        texto_respuesta += "\n\n" + opciones_solas
            else:
                texto_respuesta += "\n\n¿Algo más, o ya sería todo? 😊"

        # Si hubo productos no encontrados en este turno (tool devolvió aviso
        # de "no encontré"), y también hubo productos que sí entraron al carrito,
        # adjuntamos el aviso al final para que el cliente no pierda la info.
        # Bug real: "1 orden de sueldo" se ignoraba silenciosamente mientras
        # el carrito solo mostraba los productos que sí entraron.
        _tool_results_guard = tool_results if 'tool_results' in dir() else []
        avisos_no_encontrado = [
            r for r in _tool_results_guard
            if isinstance(r, str) and "No encontré" in r
        ]
        if avisos_no_encontrado and texto_respuesta and "Tu pedido" in texto_respuesta:
            texto_respuesta += "\n\n" + "\n\n".join(avisos_no_encontrado)

        # Guardar carrito actualizado (puede haber cambiado por las tools)
        nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=texto_respuesta or "")]
        db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], carrito=carrito_estado,
                          fase_pedido=fase, extras_pedido=extras_estado)

        # Si la tool cerrar_pedido devolvio INICIAR_CIERRE, arrancamos el flujo
        # deterministico de cierre (tipo de entrega)
        if iniciar_cierre:
            # GUARDA: si el carrito quedo vacio (ej. el cliente quito todo
            # durante una modificacion), NO intentamos cerrar — pedimos que
            # agregue algo. Bug real: tras quitar el unico producto, el cierre
            # mostraba "Parece que se ha perdido tu pedido" repetidamente.
            if not carrito_estado:
                resp = (
                    "Tu carrito quedó vacío. 🛒 ¿Qué te gustaría pedir? "
                    "Dime los productos y los agrego."
                )
                nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="armando", carrito=[])
                enviar_whatsapp(telefono, resp, token, phone_number_id)
                print(f"   [{nombre_neg}] Cierre abortado: carrito vacío — pidiendo productos.")
                return

            # Si el carrito tiene productos de alguna categoria
            # personalizable (tacos, hamburguesas, etc.) que todavia no se
            # le ha preguntado al cliente, lo preguntamos AQUI antes de
            # seguir con el resto del cierre (recoger/envio, direccion,
            # etc.) — una pregunta por categoria presente, nunca por cada
            # producto individual, y se van encadenando si hay varias.
            notas_previas = sesion.get("notas_pedido", "")
            # PRIMERO: si hay productos campechanos sin carnes elegidas,
            # preguntamos cuales 2 carnes llevan, ANTES de la personalizacion.
            if _campechano_pendiente_carnes(carrito_estado, notas_previas):
                camps_en_carrito = sorted({
                    c.get("nombre") for c in carrito_estado
                    if c.get("nombre") in _NOMBRES_CAMPECHANO
                })
                tipo_camp = " y ".join(camps_en_carrito) if camps_en_carrito else "campechano(s)"
                resp_camp = (
                    f"Antes de cerrar tu pedido, para tu(s) *{tipo_camp}* 🌮: "
                    f"¿qué 2 carnes quieres? Las opciones son: *{_CARNES_CAMPECHANO}*. "
                    "Por ejemplo: pastor y bistec."
                )
                nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp_camp)]
                db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="campechano:carnes",
                                  carrito=carrito_estado, extras_pedido=extras_estado)
                enviar_whatsapp(telefono, resp_camp, token, phone_number_id)
                print(f"   [{nombre_neg}] Carrito con campechanos — preguntando carnes antes del cierre.")
                return

            pendientes_iniciales = _categorias_pendientes_personalizacion(carrito_estado, menu, notas_previas)
            if pendientes_iniciales:
                primera = pendientes_iniciales[0]
                resp_pers = f"Antes de cerrar tu pedido, {_CATEGORIAS_PERSONALIZABLES[primera]['pregunta']}"
                nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp_pers)]
                db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido=f"personalizacion:{primera}",
                                  carrito=carrito_estado, extras_pedido=extras_estado)
                enviar_whatsapp(telefono, resp_pers, token, phone_number_id)
                print(f"   [{nombre_neg}] Carrito con categorías personalizables ({', '.join(pendientes_iniciales)}) — preguntando '{primera}' antes de continuar el cierre.")
                return

            # No quedan categorias de ingredientes — preguntar SALSA GLOBAL
            # (una sola pregunta para todo el pedido) antes de tipo de entrega.
            if _falta_salsa_global(carrito_estado, menu, notas_previas):
                resp_salsa = f"Antes de cerrar, {_PREGUNTA_SALSA_GLOBAL}"
                nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp_salsa)]
                db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="personalizacion_salsa",
                                  carrito=carrito_estado, extras_pedido=extras_estado,
                                  notas_pedido=notas_previas)
                enviar_whatsapp(telefono, resp_salsa, token, phone_number_id)
                print(f"   [{nombre_neg}] Categorías listas — preguntando salsa global.")
                return

            # Si el cliente ya habia llegado a definir tipo_entrega antes
            # (ej. dijo "No" al resumen final y luego "Si" de nuevo tras
            # modificar algo), reutilizamos esos datos en vez de reiniciar
            # el flujo desde cero — evita volver a preguntar "recoger o
            # envio" cuando el cliente ya lo habia contestado.
            tipo_entrega_previo = sesion.get("tipo_entrega", "")
            direccion_previa    = sesion.get("direccion_entrega", "")
            nombre_previo       = sesion.get("nombre_cliente", "")

            # Si venimos de modificar (fase 'armando') y el cliente dijo el
            # tipo de entrega ahi ("recoger"/"a domicilio") pero AUN NO habia
            # dado su nombre, avanzamos directo a la fase que toca segun ese
            # tipo, en vez de volver a preguntar "¿recoger o envío?". Bug real:
            # cliente en fase 'tipo' pidio agregar algo, dijo "Recoger" al
            # terminar, y el cierre le re-preguntaba el tipo -> su nombre caía
            # en "no reconocido en fase tipo".
            if _cerrar_desde_armando and tipo_entrega_previo and not nombre_previo:
                if tipo_entrega_previo == "recoger":
                    resp_cierre = (
                        f"{_formato_carrito(carrito_estado, extras=extras_estado)}\n\n"
                        "¿A nombre de quién registramos el pedido? 😊"
                    )
                    nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp_cierre)]
                    db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="nombre",
                                      tipo_entrega="recoger", carrito=carrito_estado, extras_pedido=extras_estado)
                    enviar_whatsapp(telefono, resp_cierre, token, phone_number_id)
                    print(f"   [{nombre_neg}] Cierre desde armado (recoger) — pidiendo nombre.")
                    return
                elif tipo_entrega_previo == "envio":
                    resp_cierre = (
                        f"{_formato_carrito(carrito_estado, extras=extras_estado)}\n"
                        "_El costo de envío se calculará según tu dirección._\n\n"
                        "¿Cuál es tu dirección de entrega? 📍 También puedes compartir tu ubicación con el clip de WhatsApp para mayor precisión."
                    )
                    nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp_cierre)]
                    db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="direccion",
                                      tipo_entrega="envio", carrito=carrito_estado, extras_pedido=extras_estado)
                    enviar_whatsapp(telefono, resp_cierre, token, phone_number_id)
                    print(f"   [{nombre_neg}] Cierre desde armado (envío) — pidiendo dirección.")
                    return

            if tipo_entrega_previo == "recoger" and nombre_previo:
                # Ya tenemos todo lo necesario para recoger -> resumen directo
                _, notas_limpias = _extraer_pago_de_notas(sesion.get("notas_pedido", ""))
                resumen = _formato_carrito(carrito_estado, extras=extras_estado)
                notas_linea = f"📝 *Notas:* {notas_limpias}\n" if notas_limpias else ""
                resp_cierre = (
                    f"📋 *Resumen de tu pedido*\n"
                    f"━━━━━━━━━━━━━━\n"
                    f"{resumen}\n\n"
                    f"👤 *Nombre:* {nombre_previo}\n"
                    f"🏪 *Para recoger* en el local\n"
                    f"{notas_linea}\n"
                    f"¿Todo está bien? Responde:\n"
                    f"✅ *SÍ* para confirmar\n"
                    f"❌ *NO* para modificar"
                )
                db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="confirmando",
                                  carrito=carrito_estado, extras_pedido=extras_estado)
                enviar_whatsapp(telefono, resp_cierre, token, phone_number_id)
                print(f"   [{nombre_neg}] Cierre reutilizando datos previos (recoger) — directo a confirmación.")
                return

            elif tipo_entrega_previo == "envio" and direccion_previa and nombre_previo:
                # Ya teniamos direccion y nombre de envio -> resumen directo,
                # pero re-preguntamos metodo de pago si no lo teniamos ya
                # (es razonable, pudo cambiar el carrito).
                resp_cierre = "¿Cómo deseas pagar? (Efectivo, Transferencia o Tarjeta)"
                metodos_lista = ", ".join(m.strip() for m in metodos_pago.split(","))
                resp_cierre = (
                    f"Para pedidos con envío a domicilio pedimos el pago por adelantado.\n\n"
                    f"¿Cómo deseas pagar? Opciones: *{metodos_lista}*"
                )
                db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="pago",
                                  carrito=carrito_estado, extras_pedido=extras_estado)
                enviar_whatsapp(telefono, resp_cierre, token, phone_number_id)
                print(f"   [{nombre_neg}] Cierre reutilizando datos previos (envío) — directo a método de pago.")
                return

            # Caso normal: no hay datos previos de tipo de entrega, arrancar
            # el flujo desde el principio.
            if tipo_servicio == "recoger":
                resp_cierre = (
                    f"{_formato_carrito(carrito_estado, extras=extras_estado)}\n\n"
                    "¿Es para recoger en el local?"
                )
                db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="nombre",
                                  tipo_entrega="recoger", carrito=carrito_estado, extras_pedido=extras_estado)
                enviar_whatsapp(telefono, resp_cierre, token, phone_number_id)
            elif tipo_servicio == "envio":
                resp_cierre = (
                    f"{_formato_carrito(carrito_estado, extras=extras_estado)}\n"
                    "_El costo de envío se calculará según tu dirección._\n\n"
                    "¿Cuál es tu dirección de entrega? 📍 También puedes compartir tu ubicación con el clip de WhatsApp para mayor precisión."
                )
                db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="direccion",
                                  tipo_entrega="envio", carrito=carrito_estado, extras_pedido=extras_estado)
                enviar_whatsapp(telefono, resp_cierre, token, phone_number_id)
            else:
                # ambos: preguntar primero, NUNCA mostrar costo de envio aqui
                # porque todavia no sabemos si el cliente eligio envio.
                resp_cierre = (
                    f"{_formato_carrito(carrito_estado, extras=extras_estado)}\n\n"
                    "¿Es para *recoger en el local* o *envío a domicilio*?"
                )
                db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="tipo",
                                  carrito=carrito_estado, extras_pedido=extras_estado)
                enviar_whatsapp(telefono, resp_cierre, token, phone_number_id)
            print(f"   [{nombre_neg}] Cierre iniciado — carrito con {len(carrito_estado)} producto(s).")
            return

        if texto_respuesta:
            print(f"<- [{nombre_neg}] {texto_respuesta[:80]}")
            enviar_whatsapp(telefono, texto_respuesta, token, phone_number_id)

    except Exception as e:
        import traceback
        print(f"!!! Error en procesar_mensaje: {e}\n{traceback.format_exc()}")


# ── PANEL ADMIN ──────────────────────────────────────────────────────────────
# (Se importa como router separado para mantener main.py limpio)

from panel import router as panel_router
app.include_router(panel_router)
