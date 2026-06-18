"""
panel.py — Panel de administración de Agendify Pedidos
Rutas del panel web para el dueño del negocio.
"""
import html
import datetime
from typing import Optional
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
import database as db

router = APIRouter()

TZ_MX = datetime.timezone(datetime.timedelta(hours=-6))

# ── ESTILOS COMPARTIDOS ──────────────────────────────────────────────────────

_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       background: #f1f5f9; color: #1e293b; }
.topbar { background: #1e293b; color: #fff; padding: 14px 24px;
          display: flex; align-items: center; gap: 12px; }
.topbar h1 { font-size: 1.1rem; font-weight: 600; }
.topbar span { font-size: .85rem; color: #94a3b8; }
.container { max-width: 900px; margin: 32px auto; padding: 0 16px; }
.card { background: #fff; border-radius: 12px; padding: 24px;
        box-shadow: 0 1px 3px rgba(0,0,0,.08); margin-bottom: 24px; }
.card h2 { font-size: 1rem; font-weight: 600; margin-bottom: 16px;
           padding-bottom: 10px; border-bottom: 1px solid #e2e8f0; }
label { display: block; font-size: .85rem; font-weight: 500;
        color: #475569; margin-bottom: 4px; margin-top: 12px; }
input[type=text], input[type=email], input[type=number],
textarea, select {
    width: 100%; padding: 8px 12px; border: 1px solid #cbd5e1;
    border-radius: 8px; font-size: .9rem; color: #1e293b;
    background: #fff; transition: border .15s; }
input:focus, textarea:focus, select:focus {
    outline: none; border-color: #6366f1; }
textarea { resize: vertical; min-height: 80px; }
.btn { display: inline-block; padding: 9px 20px; border-radius: 8px;
       font-size: .9rem; font-weight: 500; cursor: pointer;
       border: none; text-decoration: none; }
.btn-primary { background: #6366f1; color: #fff; }
.btn-primary:hover { background: #4f46e5; }
.btn-danger  { background: #ef4444; color: #fff; font-size: .8rem;
               padding: 5px 12px; }
.btn-danger:hover  { background: #dc2626; }
.aviso { background: #f0fdf4; border: 1px solid #bbf7d0; color: #15803d;
         padding: 10px 14px; border-radius: 8px; font-size: .85rem;
         margin-top: 16px; }
.error { background: #fef2f2; border: 1px solid #fecaca; color: #dc2626;
         padding: 10px 14px; border-radius: 8px; font-size: .85rem;
         margin-top: 16px; }
table { width: 100%; border-collapse: collapse; font-size: .88rem; }
th { text-align: left; padding: 8px 10px; background: #f8fafc;
     border-bottom: 2px solid #e2e8f0; font-weight: 600; color: #475569; }
td { padding: 8px 10px; border-bottom: 1px solid #f1f5f9; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 99px;
         font-size: .75rem; font-weight: 600; }
.badge-nuevo      { background: #dbeafe; color: #1d4ed8; }
.badge-en_proceso { background: #fef9c3; color: #854d0e; }
.badge-listo      { background: #dcfce7; color: #15803d; }
.badge-entregado  { background: #e0e7ff; color: #4338ca; }
.badge-cancelado  { background: #fee2e2; color: #b91c1c; }
nav { display: flex; gap: 8px; margin-bottom: 24px; flex-wrap: wrap; }
nav a { padding: 8px 16px; border-radius: 8px; font-size: .88rem;
        text-decoration: none; color: #475569; background: #fff;
        border: 1px solid #e2e8f0; }
nav a.active, nav a:hover { background: #6366f1; color: #fff; border-color: #6366f1; }
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
@media(max-width:600px){ .grid2 { grid-template-columns: 1fr; } }
"""


def _auth(negocio, pwd: str) -> bool:
    return negocio and negocio.get("admin_password") == pwd


def _neg(phone_id: str):
    from main import _negocios_cache
    return _negocios_cache.get(phone_id)


# ── LOGIN ─────────────────────────────────────────────────────────────────────

@router.get("/admin/{phone_id}", response_class=HTMLResponse)
async def panel_login_get(phone_id: str, pwd: str = ""):
    negocio = _neg(phone_id)
    if not negocio:
        return HTMLResponse("Negocio no encontrado.", status_code=404)
    if _auth(negocio, pwd):
        return RedirectResponse(f"/admin/{phone_id}/config?pwd={pwd}")
    return HTMLResponse(f"""<!DOCTYPE html><html><head><meta charset='utf-8'>
    <meta name='viewport' content='width=device-width,initial-scale=1'>
    <title>Agendify Pedidos</title>
    <style>{_CSS}</style></head><body>
    <div class='topbar'><h1>🌮 Agendify Pedidos</h1>
    <span>{html.escape(negocio['nombre'])}</span></div>
    <div class='container' style='max-width:400px'>
    <div class='card' style='margin-top:48px'>
    <h2>Acceso al panel</h2>
    <form method='get'>
    <label>Contraseña de administrador</label>
    <input type='text' name='pwd' placeholder='••••••' autofocus>
    <br><br>
    <button class='btn btn-primary' type='submit' style='width:100%'>Entrar</button>
    </form></div></div></body></html>""")


# ── CONFIGURACION ─────────────────────────────────────────────────────────────

@router.get("/admin/{phone_id}/config", response_class=HTMLResponse)
async def panel_config_get(phone_id: str, pwd: str = "", guardado: str = ""):
    negocio = _neg(phone_id)
    if not negocio or not _auth(negocio, pwd):
        return RedirectResponse(f"/admin/{phone_id}?pwd={pwd}")

    aviso = f"<div class='aviso'>✅ Configuración guardada.</div>" if guardado == "1" else ""
    n = negocio

    def v(k, default=""):
        val = n.get(k, default)
        return html.escape(str(val) if val is not None else default)

    sel = lambda k, op: "selected" if str(n.get(k, "")) == op else ""

    return HTMLResponse(f"""<!DOCTYPE html><html><head><meta charset='utf-8'>
    <meta name='viewport' content='width=device-width,initial-scale=1'>
    <title>Config — {v('nombre')}</title>
    <style>{_CSS}</style></head><body>
    <div class='topbar'><h1>🌮 Agendify Pedidos</h1><span>{v('nombre')}</span></div>
    <div class='container'>
    <nav>
      <a href='/admin/{phone_id}/config?pwd={pwd}' class='active'>⚙️ Config</a>
      <a href='/admin/{phone_id}/menu?pwd={pwd}'>🍽️ Menú</a>
      <a href='/admin/{phone_id}/pedidos?pwd={pwd}'>📦 Pedidos</a>
    </nav>
    {aviso}
    <form method='post' action='/admin/{phone_id}/config?pwd={pwd}'>
    <div class='card'>
      <h2>Datos del negocio</h2>
      <div class='grid2'>
        <div>
          <label>Nombre del negocio</label>
          <input type='text' name='nombre' value='{v("nombre")}'>
        </div>
        <div>
          <label>Correo para notificaciones de pedidos</label>
          <input type='email' name='email_notificaciones' value='{v("email_notificaciones")}'>
        </div>
      </div>
      <label>Horarios de atención</label>
      <input type='text' name='horarios_texto' value='{v("horarios_texto")}'
             placeholder='Ej. Lun-Dom 10am-10pm'>
      <label>Mensaje de bienvenida</label>
      <textarea name='mensaje_bienvenida'>{v("mensaje_bienvenida")}</textarea>
      <label>Información adicional para el bot (alergenos, políticas, etc.)</label>
      <textarea name='base_conocimiento'>{v("base_conocimiento")}</textarea>
    </div>

    <div class='card'>
      <h2>Operación</h2>
      <div class='grid2'>
        <div>
          <label>Tipo de servicio</label>
          <select name='tipo_servicio'>
            <option value='ambos'   {sel("tipo_servicio","ambos")}>Recoger y envío</option>
            <option value='recoger' {sel("tipo_servicio","recoger")}>Solo recoger</option>
            <option value='envio'   {sel("tipo_servicio","envio")}>Solo envío</option>
          </select>
        </div>
        <div>
          <label>Métodos de pago aceptados</label>
          <input type='text' name='metodos_pago' value='{v("metodos_pago")}'
                 placeholder='Efectivo, Transferencia, Tarjeta'>
        </div>
        <div>
          <label>Tiempo estimado para recoger (minutos)</label>
          <input type='number' name='tiempo_recoger_min' value='{v("tiempo_recoger_min","20")}' min='1'>
        </div>
        <div>
          <label>Tiempo estimado para envío (minutos)</label>
          <input type='number' name='tiempo_envio_min' value='{v("tiempo_envio_min","40")}' min='1'>
        </div>
        <div>
          <label>Costo de envío ($)</label>
          <input type='number' name='costo_envio' value='{v("costo_envio","0")}' min='0' step='0.50'>
        </div>
        <div>
          <label>Pedido mínimo para envío ($)</label>
          <input type='number' name='pedido_minimo' value='{v("pedido_minimo","0")}' min='0' step='0.50'>
        </div>
      </div>
    </div>

    <button class='btn btn-primary' type='submit'>💾 Guardar configuración</button>
    </form>
    </div></body></html>""")


@router.post("/admin/{phone_id}/config", response_class=HTMLResponse)
async def panel_config_post(
    phone_id: str, pwd: str = "",
    nombre: str = Form(""),
    email_notificaciones: str = Form(""),
    horarios_texto: str = Form(""),
    mensaje_bienvenida: str = Form(""),
    base_conocimiento: str = Form(""),
    tipo_servicio: str = Form("ambos"),
    metodos_pago: str = Form("Efectivo"),
    tiempo_recoger_min: int = Form(20),
    tiempo_envio_min: int = Form(40),
    costo_envio: float = Form(0),
    pedido_minimo: float = Form(0),
):
    negocio = _neg(phone_id)
    if not negocio or not _auth(negocio, pwd):
        return RedirectResponse(f"/admin/{phone_id}?pwd={pwd}")
    db.actualizar_negocio(phone_id, {
        "nombre":              nombre.strip(),
        "email_notificaciones": email_notificaciones.strip() or None,
        "horarios_texto":      horarios_texto.strip(),
        "mensaje_bienvenida":  mensaje_bienvenida.strip(),
        "base_conocimiento":   base_conocimiento.strip(),
        "tipo_servicio":       tipo_servicio,
        "metodos_pago":        metodos_pago.strip(),
        "tiempo_recoger_min":  tiempo_recoger_min,
        "tiempo_envio_min":    tiempo_envio_min,
        "costo_envio":         costo_envio,
        "pedido_minimo":       pedido_minimo,
    })
    # Actualizar cache en memoria
    from main import _negocios_cache
    _negocios_cache[phone_id].update({
        "nombre": nombre.strip(), "email_notificaciones": email_notificaciones.strip() or None,
        "horarios_texto": horarios_texto.strip(), "mensaje_bienvenida": mensaje_bienvenida.strip(),
        "base_conocimiento": base_conocimiento.strip(), "tipo_servicio": tipo_servicio,
        "metodos_pago": metodos_pago.strip(), "tiempo_recoger_min": tiempo_recoger_min,
        "tiempo_envio_min": tiempo_envio_min, "costo_envio": costo_envio,
        "pedido_minimo": pedido_minimo,
    })
    return RedirectResponse(f"/admin/{phone_id}/config?pwd={pwd}&guardado=1", status_code=303)


# ── MENÚ ──────────────────────────────────────────────────────────────────────

@router.get("/admin/{phone_id}/menu", response_class=HTMLResponse)
async def panel_menu_get(phone_id: str, pwd: str = "", msg: str = ""):
    negocio = _neg(phone_id)
    if not negocio or not _auth(negocio, pwd):
        return RedirectResponse(f"/admin/{phone_id}?pwd={pwd}")
    from main import _menu_cache
    items = _menu_cache.get(negocio["id"], [])
    def _fila_item(i, pid, p):
        return (
            "<tr>"
            "<td>{}</td><td>{}</td><td>{}</td><td>${:.2f}</td>"
            "<td><form method='post' action='/admin/{}/menu/eliminar?pwd={}' style='display:inline'>"
            "<input type='hidden' name='item_id' value='{}'>"
            "<button class='btn btn-danger' type='submit'>Eliminar</button>"
            "</form></td></tr>"
        ).format(
            html.escape(i["nombre"]),
            html.escape(i.get("descripcion", "")),
            html.escape(i.get("categoria", "General")),
            float(i["precio"]),
            pid, p,
            i["id"],
        )

    filas = "".join(_fila_item(i, phone_id, pwd) for i in items) or \
            "<tr><td colspan='5' style='color:#94a3b8;text-align:center;padding:20px'>Sin productos aún</td></tr>"

    aviso = f"<div class='aviso'>✅ Producto agregado.</div>" if msg == "ok" else ""

    return HTMLResponse(f"""<!DOCTYPE html><html><head><meta charset='utf-8'>
    <meta name='viewport' content='width=device-width,initial-scale=1'>
    <title>Menú — {html.escape(negocio['nombre'])}</title>
    <style>{_CSS}</style></head><body>
    <div class='topbar'><h1>🌮 Agendify Pedidos</h1><span>{html.escape(negocio['nombre'])}</span></div>
    <div class='container'>
    <nav>
      <a href='/admin/{phone_id}/config?pwd={pwd}'>⚙️ Config</a>
      <a href='/admin/{phone_id}/menu?pwd={pwd}' class='active'>🍽️ Menú</a>
      <a href='/admin/{phone_id}/pedidos?pwd={pwd}'>📦 Pedidos</a>
    </nav>
    {aviso}
    <div class='card'>
      <h2>Productos en el menú</h2>
      <table>
        <tr><th>Nombre</th><th>Descripción</th><th>Categoría</th><th>Precio</th><th></th></tr>
        {filas}
      </table>
    </div>
    <div class='card'>
      <h2>Agregar producto</h2>
      <form method='post' action='/admin/{phone_id}/menu/agregar?pwd={pwd}'>
        <div class='grid2'>
          <div>
            <label>Nombre del producto *</label>
            <input type='text' name='nombre' placeholder='Ej. Taco de pastor' required>
          </div>
          <div>
            <label>Precio ($) *</label>
            <input type='number' name='precio' step='0.50' min='0' required>
          </div>
          <div>
            <label>Categoría</label>
            <input type='text' name='categoria' placeholder='Ej. Tacos, Bebidas, Postres'>
          </div>
          <div>
            <label>Descripción corta (opcional)</label>
            <input type='text' name='descripcion' placeholder='Ej. Con cilantro y cebolla'>
          </div>
        </div>
        <br>
        <button class='btn btn-primary' type='submit'>+ Agregar producto</button>
      </form>
    </div>
    </div></body></html>""")


@router.post("/admin/{phone_id}/menu/agregar")
async def panel_menu_agregar(
    phone_id: str, pwd: str = "",
    nombre: str = Form(""), precio: float = Form(0),
    categoria: str = Form("General"), descripcion: str = Form(""),
):
    negocio = _neg(phone_id)
    if not negocio or not _auth(negocio, pwd):
        return RedirectResponse(f"/admin/{phone_id}?pwd={pwd}")
    from main import _menu_cache
    db.agregar_item_menu(negocio["id"], nombre, precio, descripcion, categoria)
    _menu_cache[negocio["id"]] = db.cargar_menu(negocio["id"])
    return RedirectResponse(f"/admin/{phone_id}/menu?pwd={pwd}&msg=ok", status_code=303)


@router.post("/admin/{phone_id}/menu/eliminar")
async def panel_menu_eliminar(phone_id: str, pwd: str = "", item_id: int = Form(0)):
    negocio = _neg(phone_id)
    if not negocio or not _auth(negocio, pwd):
        return RedirectResponse(f"/admin/{phone_id}?pwd={pwd}")
    from main import _menu_cache
    db.eliminar_item_menu(item_id)
    _menu_cache[negocio["id"]] = db.cargar_menu(negocio["id"])
    return RedirectResponse(f"/admin/{phone_id}/menu?pwd={pwd}", status_code=303)


# ── PEDIDOS ───────────────────────────────────────────────────────────────────

@router.get("/admin/{phone_id}/pedidos", response_class=HTMLResponse)
async def panel_pedidos(phone_id: str, pwd: str = ""):
    negocio = _neg(phone_id)
    if not negocio or not _auth(negocio, pwd):
        return RedirectResponse(f"/admin/{phone_id}?pwd={pwd}")
    pedidos = db.obtener_pedidos_recientes(negocio["id"])

    def badge(estado):
        return f"<span class='badge badge-{estado}'>{estado.replace('_',' ').title()}</span>"

    def fmt_items(items):
        if not items: return "—"
        return ", ".join(f"{i['cantidad']}x {i['nombre']}" for i in items)

    filas = "".join(
        f"<tr>"
        f"<td>#{p['id']}</td>"
        f"<td>{p.get('nombre_cliente','—')}</td>"
        f"<td>{p.get('telefono','—')}</td>"
        f"<td>{fmt_items(p.get('items',[]))}</td>"
        f"<td>${float(p.get('total',0)):.2f}</td>"
        f"<td>{'🛵 Envío' if p.get('tipo_entrega')=='envio' else '🏪 Recoger'}</td>"
        f"<td>{badge(p.get('estado','nuevo'))}</td>"
        f"<td>{p.get('created_at','')[:16].replace('T',' ')}</td>"
        f"<td><form method='post' action='/admin/{phone_id}/pedidos/{p['id']}/estado?pwd={pwd}'>"
        f"<select name='estado' onchange='this.form.submit()' style='font-size:.8rem;padding:4px'>"
        + "".join(
            f"<option value='{s}' {'selected' if p.get('estado')==s else ''}>{s.replace('_',' ').title()}</option>"
            for s in ["nuevo","en_proceso","listo","entregado","cancelado"]
        )
        + f"</select></form></td></tr>"
        for p in pedidos
    ) or "<tr><td colspan='9' style='color:#94a3b8;text-align:center;padding:20px'>Sin pedidos aún</td></tr>"

    return HTMLResponse(f"""<!DOCTYPE html><html><head><meta charset='utf-8'>
    <meta name='viewport' content='width=device-width,initial-scale=1'>
    <title>Pedidos — {html.escape(negocio['nombre'])}</title>
    <style>{_CSS}</style></head><body>
    <div class='topbar'><h1>🌮 Agendify Pedidos</h1><span>{html.escape(negocio['nombre'])}</span></div>
    <div class='container' style='max-width:1100px'>
    <nav>
      <a href='/admin/{phone_id}/config?pwd={pwd}'>⚙️ Config</a>
      <a href='/admin/{phone_id}/menu?pwd={pwd}'>🍽️ Menú</a>
      <a href='/admin/{phone_id}/pedidos?pwd={pwd}' class='active'>📦 Pedidos</a>
    </nav>
    <div class='card'>
      <h2>Pedidos recientes</h2>
      <div style='overflow-x:auto'>
      <table>
        <tr><th>#</th><th>Cliente</th><th>Teléfono</th><th>Productos</th>
            <th>Total</th><th>Tipo</th><th>Estado</th><th>Hora</th><th>Cambiar estado</th></tr>
        {filas}
      </table>
      </div>
    </div>
    </div>
    <script>setInterval(()=>location.reload(),60000)</script>
    </body></html>""")


@router.post("/admin/{phone_id}/pedidos/{pedido_id}/estado")
async def panel_pedido_estado(phone_id: str, pedido_id: int, pwd: str = "",
                               estado: str = Form("nuevo")):
    negocio = _neg(phone_id)
    if not negocio or not _auth(negocio, pwd):
        return RedirectResponse(f"/admin/{phone_id}?pwd={pwd}")
    db.actualizar_estado_pedido(pedido_id, estado)
    return RedirectResponse(f"/admin/{phone_id}/pedidos?pwd={pwd}", status_code=303)
