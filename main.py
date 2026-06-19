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
from email.mime.text import MIMEText
from typing import Optional
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, PlainTextResponse
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage
from langchain.tools import tool
import database as db

load_dotenv()

app = FastAPI(title="Agendify Pedidos - Bot de Taqueria SaaS")

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "agendify_pedidos")
MAX_HISTORIAL = 10
TZ_MX = datetime.timezone(datetime.timedelta(hours=-6))

# Cache de negocios en memoria (se carga al arrancar)
_negocios_cache: dict = {}   # phone_number_id -> dict negocio
_menu_cache:     dict = {}   # negocio_id -> list de items


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

def enviar_whatsapp(telefono: str, mensaje: str, token: str, phone_id: str) -> bool:
    url = f"https://graph.facebook.com/v19.0/{phone_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": telefono,
        "type": "text",
        "text": {"body": mensaje},
    }
    r = requests.post(url, headers={"Authorization": f"Bearer {token}"}, json=payload, timeout=10)
    if not r.ok:
        print(f"!!! Error enviando WhatsApp: {r.status_code} {r.text[:200]}")
    return r.ok


# ── HELPERS DE CORREO ────────────────────────────────────────────────────────

def _html_correo_pedido(negocio: str, nombre: str, telefono: str, items: list,
                         total: float, tipo: str, direccion: str, metodo_pago: str) -> str:
    color = "#2563eb"
    emoji = "🛵" if tipo == "envio" else "🏪"
    tipo_texto = "Envío a domicilio" if tipo == "envio" else "Para recoger en local"
    filas = "".join(
        f"<tr><td style='padding:4px 8px'>{html.escape(i['nombre'])}</td>"
        f"<td style='padding:4px 8px;text-align:center'>{i['cantidad']}</td>"
        f"<td style='padding:4px 8px;text-align:right'>${i['precio']*i['cantidad']:.2f}</td></tr>"
        for i in items
    )
    dir_html = (
        f"<tr><td style='color:#6b7280;padding:4px 8px'>Dirección</td>"
        f"<td colspan='2' style='padding:4px 8px'><b>{html.escape(direccion)}</b></td></tr>"
        if direccion else ""
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

def _calcular_total(carrito: list) -> float:
    return sum(i["precio"] * i["cantidad"] for i in carrito)


def _formato_carrito(carrito: list, costo_envio: float = 0) -> str:
    if not carrito:
        return "🛒 Tu carrito está vacío."
    lineas = ["🛒 *Tu pedido:*"]
    for i in carrito:
        subtotal = i["precio"] * i["cantidad"]
        lineas.append(f"  • {i['cantidad']}x {i['nombre']} — {_fmt_precio(subtotal)}")
    total = _calcular_total(carrito)
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
            desc = f" _{p['descripcion']}_" if p.get("descripcion") else ""
            lineas.append(f"  • *{p['nombre']}*{desc}")
            lineas.append(f"    {_fmt_precio(float(p['precio']))}")
        if idx < total_cats - 1:
            lineas.append("")

    lineas += [
        "",
        "━━━━━━━━━━━━━━",
        "👇 *¿Qué te gustaría ordenar?*",
        "_Puedes pedir varios productos a la vez,_",
        "_por ejemplo: \"2 tacos de pastor y una horchata\"_",
    ]
    return "\n".join(lineas)


def _buscar_en_menu(texto: str, menu: list) -> Optional[dict]:
    """Busca un producto en el menu por nombre (coincidencia parcial,
    insensible a mayusculas y acentos)."""
    def normalizar(s):
        import unicodedata
        return unicodedata.normalize("NFD", s.lower()).encode("ascii", "ignore").decode()
    t = normalizar(texto)
    for item in menu:
        if normalizar(item["nombre"]) in t or t in normalizar(item["nombre"]):
            return item
    return None


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
        if tipo == "text":
            texto = msg["text"]["body"].strip()
        elif tipo in ("image", "audio", "video", "document", "sticker"):
            texto = f"[{tipo}]"
        else:
            return {"status": "ok"}
        telefono       = msg["from"]
        phone_number_id = value.get("metadata", {}).get("phone_number_id", "")
        if not phone_number_id or phone_number_id not in _negocios_cache:
            return {"status": "ok"}
        background_tasks.add_task(procesar_mensaje, texto, telefono, phone_number_id)
        return {"status": "ok"}
    except Exception as e:
        print(f"!!! Error en webhook: {e}")
        return {"status": "ok"}


# ── PROCESAMIENTO PRINCIPAL ──────────────────────────────────────────────────

def procesar_mensaje(texto: str, telefono: str, phone_number_id: str):
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
        costo_envio   = float(negocio.get("costo_envio") or 0)
        pedido_min    = float(negocio.get("pedido_minimo") or 0)
        tiempo_rec    = negocio.get("tiempo_recoger_min", 20)
        tiempo_env    = negocio.get("tiempo_envio_min", 40)
        metodos_pago  = negocio.get("metodos_pago", "Efectivo")

        llave   = f"{phone_number_id}_{telefono}"
        sesion  = db.cargar_sesion(llave)
        historial  = sesion["historial"]
        carrito    = sesion["carrito"] or []
        fase       = sesion["fase_pedido"] or ""
        texto_low  = texto.lower().strip()

        print(f"-> [Procesando] {telefono} | fase={fase or 'inicio'} | msg='{texto[:60]}'")

        # ── FLUJO DETERMINISTICO POR FASE ───────────────────────────────────

        # FASE: confirmando — el cliente responde SI/NO al resumen del pedido
        if fase == "confirmando":
            if any(p in texto_low for p in ["si", "sí", "confirmo", "dale", "va", "ok", "correcto", "listo"]):
                # Pedido confirmado: guardar en DB y notificar al dueño
                tipo_entrega = sesion["tipo_entrega"]
                nombre_cl    = sesion["nombre_cliente"]
                direccion    = sesion["direccion_entrega"]
                total        = _calcular_total(carrito)
                total_con_envio = total + (costo_envio if tipo_entrega == "envio" else 0)

                pedido_id = db.guardar_pedido(
                    negocio_id=negocio_id, telefono=telefono,
                    nombre_cliente=nombre_cl, items=carrito,
                    total=total_con_envio, tipo_entrega=tipo_entrega,
                    direccion=direccion,
                )
                tiempo = tiempo_env if tipo_entrega == "envio" else tiempo_rec
                resp = (
                    f"✅ *¡Pedido #{pedido_id} confirmado!*\n\n"
                    f"👤 *{nombre_cl}*\n"
                    f"{'🛵 Envío a: ' + direccion if tipo_entrega == 'envio' else '🏪 Para recoger en el local'}\n"
                    f"⏱ Tiempo estimado: *{tiempo} minutos*\n"
                    f"💰 Total: *{_fmt_precio(total_con_envio)}*\n\n"
                    f"¡Gracias por tu preferencia! 🌮\n"
                    f"_Tu pedido ya está en preparación._"
                )
                # Notificar al dueño por correo
                if email_notif:
                    notificar_dueno(
                        email_notif,
                        f"Nuevo pedido #{pedido_id} en {nombre_neg}",
                        _html_correo_pedido(
                            nombre_neg, nombre_cl, telefono, carrito,
                            total_con_envio, tipo_entrega, direccion, metodos_pago,
                        ),
                    )
                db.limpiar_sesion(llave)
                nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:])
                enviar_whatsapp(telefono, resp, token, phone_number_id)
                print(f"   [{nombre_neg}] Pedido #{pedido_id} confirmado — {nombre_cl} / ${total_con_envio:.2f}")
                return

            elif any(p in texto_low for p in ["no", "cancel", "cambiar", "modificar", "error"]):
                resp = "Sin problema, ¿qué te gustaría cambiar? Puedes decirme qué agregar, quitar o modificar."
                db.guardar_sesion(llave, historial, fase_pedido="armando", carrito=carrito)
                nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="armando", carrito=carrito)
                enviar_whatsapp(telefono, resp, token, phone_number_id)
                return

        # FASE: nombre — esperando el nombre del cliente
        if fase == "nombre":
            nombre_cl = texto.strip().title()
            if len(nombre_cl) >= 2 and not any(c.isdigit() for c in nombre_cl):
                carrito = sesion["carrito"]
                tipo_entrega = sesion["tipo_entrega"]
                direccion    = sesion["direccion_entrega"]
                total        = _calcular_total(carrito)
                total_con_envio = total + (costo_envio if tipo_entrega == "envio" else 0)
                resumen = _formato_carrito(carrito, costo_envio if tipo_entrega == "envio" else 0)
                resp = (
                    f"📋 *Resumen de tu pedido*\n"
                    f"━━━━━━━━━━━━━━\n"
                    f"{resumen}\n\n"
                    f"👤 *Nombre:* {nombre_cl}\n"
                    f"{'🛵 *Envío a:* ' + direccion if tipo_entrega == 'envio' else '🏪 *Para recoger* en el local'}\n\n"
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

        # FASE: direccion — esperando dirección de envío
        if fase == "direccion":
            direccion = texto.strip()
            if len(direccion) >= 5:
                resp = "¿A nombre de quién registramos el pedido?"
                db.guardar_sesion(llave, historial, fase_pedido="nombre",
                                  direccion_entrega=direccion, carrito=carrito)
                nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="nombre",
                                  direccion_entrega=direccion, carrito=carrito)
                enviar_whatsapp(telefono, resp, token, phone_number_id)
                print(f"   [{nombre_neg}] Dirección capturada: {direccion[:40]}")
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
                else:
                    resp = "¿Cuál es tu dirección de entrega?"
                    db.guardar_sesion(llave, historial, fase_pedido="direccion",
                                      tipo_entrega="envio", carrito=carrito)
                    nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=resp)]
                    db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="direccion",
                                      tipo_entrega="envio", carrito=carrito)
                enviar_whatsapp(telefono, resp, token, phone_number_id)
                return

        # ── LLM CON HERRAMIENTAS ─────────────────────────────────────────────
        # El modelo maneja: saludo, mostrar menu, agregar/quitar productos del
        # carrito, y disparar el cierre del pedido cuando el cliente diga "ya es
        # todo" / "eso seria todo" / etc.

        # Herramientas disponibles para el modelo
        carrito_estado = list(carrito)  # mutable dentro del turno

        @tool
        def ver_menu() -> str:
            """Muestra el menu completo con precios al cliente."""
            return _formato_menu(menu)

        @tool
        def agregar_al_carrito(nombre_producto: str, cantidad: int = 1) -> str:
            """Agrega uno o varios productos al carrito del cliente.
            nombre_producto: nombre exacto o aproximado del producto segun el menu.
            cantidad: cuantos quiere (default 1)."""
            item = _buscar_en_menu(nombre_producto, menu)
            if not item:
                nombres = ", ".join(i["nombre"] for i in menu)
                return f"No encontré '{nombre_producto}' en el menú. Los productos disponibles son: {nombres}."
            # Si ya existe en el carrito, sumar cantidad
            for c in carrito_estado:
                if c["nombre"] == item["nombre"]:
                    c["cantidad"] += max(1, cantidad)
                    return f"Actualizado: {c['cantidad']}x {item['nombre']} en tu carrito."
            carrito_estado.append({
                "nombre":   item["nombre"],
                "precio":   float(item["precio"]),
                "cantidad": max(1, cantidad),
            })
            return f"Agregado: {cantidad}x {item['nombre']} (${item['precio']:.2f} c/u) 🌮"

        @tool
        def quitar_del_carrito(nombre_producto: str) -> str:
            """Quita un producto del carrito del cliente."""
            item = _buscar_en_menu(nombre_producto, menu)
            nombre_buscado = item["nombre"] if item else nombre_producto
            for i, c in enumerate(carrito_estado):
                if c["nombre"].lower() == nombre_buscado.lower():
                    carrito_estado.pop(i)
                    return f"Eliminado: {nombre_buscado} del carrito."
            return f"No encontré '{nombre_producto}' en tu carrito."

        @tool
        def ver_carrito() -> str:
            """Muestra el resumen actual del carrito."""
            return _formato_carrito(carrito_estado, costo_envio)

        @tool
        def cerrar_pedido() -> str:
            """Llama esta herramienta cuando el cliente diga que ya termino de
            pedir (frases como: 'eso es todo', 'ya es todo', 'eso seria todo',
            'nada mas', 'con eso', etc.). Inicia el flujo de cierre."""
            if not carrito_estado:
                return "El carrito está vacío. Pide algo del menú primero."
            total = _calcular_total(carrito_estado)
            if pedido_min > 0 and total < pedido_min:
                return (
                    f"El pedido mínimo es de ${pedido_min:.2f}. "
                    f"Tu pedido actual es de ${total:.2f}. "
                    f"¿Quieres agregar algo más?"
                )
            return "INICIAR_CIERRE"

        tools = [ver_menu, agregar_al_carrito, quitar_del_carrito, ver_carrito, cerrar_pedido]

        horarios = negocio.get("horarios_texto", "")
        base_conoc = negocio.get("base_conocimiento", "")
        bienvenida = negocio.get("mensaje_bienvenida", "¡Hola! ¿Qué te gustaría ordenar?")

        sistema = f"""Eres el asistente de pedidos de {nombre_neg}, una taquería que atiende por WhatsApp.
Tu trabajo es tomar el pedido del cliente de forma amigable y precisa.

Horarios: {horarios or 'Consultar con el negocio.'}
Tipos de servicio disponibles: {tipo_servicio}
Costo de envío: {'$' + str(costo_envio) if costo_envio > 0 else 'sin costo'}
Pedido mínimo: {'$' + str(pedido_min) if pedido_min > 0 else 'sin mínimo'}
Métodos de pago aceptados: {metodos_pago}

{('Información adicional: ' + base_conoc) if base_conoc else ''}

REGLAS IMPORTANTES:
- Usa SIEMPRE la herramienta agregar_al_carrito para añadir productos. NUNCA inventes precios.
- Si el cliente pide algo que no está en el menú, indícalo claramente y ofrece alternativas.
- Cuando el cliente diga que ya terminó de pedir (eso es todo / ya es todo / nada más / con eso / etc.), llama cerrar_pedido.
- Sé breve, amigable y usa emojis con moderación.
- Si el cliente saluda sin pedir nada, preséntate brevemente y muestra el menú.
- Responde siempre en español.
"""
        if not historial:
            sistema += f"\n\nMensaje de bienvenida sugerido: {bienvenida}"

        llm = ChatOpenAI(
            model="gpt-4o-mini",
            temperature=0,
            api_key=os.getenv("OPENAI_API_KEY"),
        ).bind_tools(tools)

        msgs_llm = [SystemMessage(content=sistema)] + historial[-MAX_HISTORIAL:] + [HumanMessage(content=texto)]
        resp_llm = llm.invoke(msgs_llm)

        # Procesar tool calls
        texto_respuesta = ""
        tool_results = []
        iniciar_cierre = False

        if resp_llm.tool_calls:
            for tc in resp_llm.tool_calls:
                fn_name = tc["name"]
                fn_args = tc["args"]
                print(f"   [{nombre_neg}] Tool: {fn_name} {fn_args}")
                fn_map = {
                    "ver_menu":           ver_menu,
                    "agregar_al_carrito": agregar_al_carrito,
                    "quitar_del_carrito": quitar_del_carrito,
                    "ver_carrito":        ver_carrito,
                    "cerrar_pedido":      cerrar_pedido,
                }
                resultado = fn_map[fn_name].invoke(fn_args) if fn_name in fn_map else "Herramienta no encontrada."
                if resultado == "INICIAR_CIERRE":
                    iniciar_cierre = True
                    resultado = "Procesando cierre del pedido..."
                tool_results.append(resultado)

            # Segunda llamada al LLM con los resultados de las herramientas
            if not iniciar_cierre:
                from langchain_core.messages import ToolMessage
                msgs_2 = msgs_llm + [resp_llm]
                for i, tc in enumerate(resp_llm.tool_calls):
                    msgs_2.append(ToolMessage(content=tool_results[i], tool_call_id=tc["id"]))
                resp_2 = ChatOpenAI(model="gpt-4o-mini", temperature=0,
                                    api_key=os.getenv("OPENAI_API_KEY")).invoke(msgs_2)
                texto_respuesta = resp_2.content.strip()
        else:
            texto_respuesta = resp_llm.content.strip()

        # Guardar carrito actualizado (puede haber cambiado por las tools)
        nuevo_h = historial + [HumanMessage(content=texto), AIMessage(content=texto_respuesta or "")]
        db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], carrito=carrito_estado, fase_pedido=fase)

        # Si la tool cerrar_pedido devolvio INICIAR_CIERRE, arrancamos el flujo
        # deterministico de cierre (tipo de entrega)
        if iniciar_cierre:
            if tipo_servicio == "recoger":
                resp_cierre = (
                    f"{_formato_carrito(carrito_estado)}\n\n"
                    "¿Es para recoger en el local?"
                )
                db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="nombre",
                                  tipo_entrega="recoger", carrito=carrito_estado)
                enviar_whatsapp(telefono, resp_cierre, token, phone_number_id)
            elif tipo_servicio == "envio":
                resp_cierre = (
                    f"{_formato_carrito(carrito_estado, costo_envio)}\n\n"
                    "¿Cuál es tu dirección de entrega?"
                )
                db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="direccion",
                                  tipo_entrega="envio", carrito=carrito_estado)
                enviar_whatsapp(telefono, resp_cierre, token, phone_number_id)
            else:
                # ambos: preguntar
                resp_cierre = (
                    f"{_formato_carrito(carrito_estado, costo_envio)}\n\n"
                    "¿Es para *recoger en el local* o *envío a domicilio*?"
                )
                db.guardar_sesion(llave, nuevo_h[-MAX_HISTORIAL:], fase_pedido="tipo",
                                  carrito=carrito_estado)
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
