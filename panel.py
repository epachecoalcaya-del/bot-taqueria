"""
panel.py — Panel de administración de Agendify Pedidos
Rutas del panel web para el dueño del negocio.
"""
import html
import datetime
from typing import Optional
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
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


_DIAS = ["lunes","martes","miercoles","jueves","viernes","sabado","domingo"]
_DIAS_ES = {"lunes":"Lunes","martes":"Martes","miercoles":"Miércoles",
             "jueves":"Jueves","viernes":"Viernes","sabado":"Sábado","domingo":"Domingo"}

def _get_horarios(negocio: dict) -> dict:
    """Devuelve el dict de horarios, con defaults si no existe."""
    h = negocio.get("horarios_json") or {}
    if isinstance(h, str):
        import json as _json
        try: h = _json.loads(h)
        except: h = {}
    defaults = {"abierto": True, "apertura": "18:00", "cierre": "00:00"}
    return {dia: h.get(dia, defaults) for dia in _DIAS}

def _html_horarios(negocio: dict) -> str:
    horarios = _get_horarios(negocio)
    partes = []
    for dia in _DIAS:
        info     = horarios[dia]
        abierto  = info.get("abierto", True)
        apertura = info.get("apertura", "18:00")
        cierre   = info.get("cierre",   "00:00")
        chk      = "checked" if abierto else ""
        partes.append(
            f"<div class='dia-card'>"
            f"<div class='switch'>"
            f"<input type='checkbox' class='chk-dia' name='dia_{dia}' {chk}>"
            f"<h4>{_DIAS_ES[dia]}</h4>"
            f"</div>"
            f"<label>Apertura</label>"
            f"<input type='time' name='apertura_{dia}' value='{apertura}'>"
            f"<label>Cierre</label>"
            f"<input type='time' name='cierre_{dia}' value='{cierre}'>"
            f"</div>"
        )
    return "\n".join(partes)

def _alerta_cerrado_hoy(negocio: dict, phone_id: str, pwd: str) -> str:
    cerrado = negocio.get("cerrado_hoy", False)
    if cerrado:
        return (
            f"<div class='alerta-cerrado'>⚠️ <b>Cerrado hoy activado</b> — el bot está avisando a "
            f"los clientes que no están tomando pedidos. "
            f"<a href='/admin/{phone_id}/cerrado_hoy?pwd={pwd}&estado=0' "
            f"style='color:#b91c1c;font-weight:600'>Reactivar</a></div>"
        )
    return (
        f"<div style='text-align:right;margin-bottom:12px'>"
        f"<a href='/admin/{phone_id}/cerrado_hoy?pwd={pwd}&estado=1' "
        f"class='btn' style='background:#f59e0b;color:#fff'>🔴 Cerrar hoy</a>"
        f"&nbsp;"
        f"<span style='font-size:.8rem;color:#94a3b8'>¿No vas a abrir hoy? Avisa a tus clientes con un clic.</span>"
        f"</div>"
    )

@router.get("/admin/{phone_id}/cerrado_hoy")
async def toggle_cerrado_hoy(phone_id: str, pwd: str = "", estado: int = 0):
    negocio = _neg(phone_id)
    if not negocio or not _auth(negocio, pwd):
        return RedirectResponse(f"/admin/{phone_id}?pwd={pwd}")
    cerrado = estado == 1
    db.actualizar_negocio(phone_id, {"cerrado_hoy": cerrado})
    from main import _negocios_cache
    _negocios_cache[phone_id]["cerrado_hoy"] = cerrado
    return RedirectResponse(f"/admin/{phone_id}/config?pwd={pwd}&guardado=1", status_code=303)


def _alerta_modo_lluvia(negocio: dict, phone_id: str, pwd: str) -> str:
    if not negocio.get("costo_envio_dinamico"):
        return ""  # solo aplica si el negocio usa tarifa dinamica por distancia
    lluvia = negocio.get("modo_lluvia", False)
    if lluvia:
        return (
            f"<div class='alerta-cerrado' style='background:#eff6ff;border-color:#bfdbfe;color:#1d4ed8'>"
            f"🌧️ <b>Modo lluvia activado</b> — se está cobrando la tarifa de envío con recargo por lluvia. "
            f"<a href='/admin/{phone_id}/modo_lluvia?pwd={pwd}&estado=0' "
            f"style='color:#1d4ed8;font-weight:600'>Desactivar</a></div>"
        )
    return (
        f"<div style='text-align:right;margin-bottom:12px'>"
        f"<a href='/admin/{phone_id}/modo_lluvia?pwd={pwd}&estado=1' "
        f"class='btn' style='background:#0ea5e9;color:#fff'>🌧️ Activar modo lluvia</a>"
        f"&nbsp;"
        f"<span style='font-size:.8rem;color:#94a3b8'>Si está lloviendo, activa esto para cobrar la tarifa de envío con recargo.</span>"
        f"</div>"
    )

@router.get("/admin/{phone_id}/modo_lluvia")
async def toggle_modo_lluvia(phone_id: str, pwd: str = "", estado: int = 0):
    negocio = _neg(phone_id)
    if not negocio or not _auth(negocio, pwd):
        return RedirectResponse(f"/admin/{phone_id}?pwd={pwd}")
    lluvia = estado == 1
    db.actualizar_negocio(phone_id, {"modo_lluvia": lluvia})
    from main import _negocios_cache
    _negocios_cache[phone_id]["modo_lluvia"] = lluvia
    return RedirectResponse(f"/admin/{phone_id}/config?pwd={pwd}&guardado=1", status_code=303)


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
    <style>{_CSS}
    .horarios-grid {{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-top:12px}}
    .dia-card {{background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:12px}}
    .dia-card.cerrado {{opacity:.5}}
    .dia-card h4 {{font-size:.85rem;font-weight:600;margin-bottom:8px;text-transform:capitalize}}
    .dia-card label {{margin-top:6px;font-size:.8rem}}
    .dia-card input[type=time] {{padding:6px 8px;font-size:.85rem}}
    .switch {{display:flex;align-items:center;gap:8px;margin-bottom:8px}}
    .switch input[type=checkbox] {{width:16px;height:16px;cursor:pointer}}
    .badge-cerrado {{background:#fee2e2;color:#b91c1c;padding:2px 8px;border-radius:99px;font-size:.75rem}}
    .alerta-cerrado {{background:#fef9c3;border:1px solid #fde68a;color:#854d0e;
                      padding:12px 16px;border-radius:8px;margin-bottom:16px;font-size:.9rem}}
    </style></head><body>
    <div class='topbar'><h1>🌮 Agendify Pedidos</h1><span>{v('nombre')}</span></div>
    <div class='container'>
    <nav>
      <a href='/admin/{phone_id}/config?pwd={pwd}' class='active'>⚙️ Config</a>
      <a href='/admin/{phone_id}/menu?pwd={pwd}'>🍽️ Menú</a>
      <a href='/admin/{phone_id}/pedidos?pwd={pwd}'>📦 Pedidos</a>
    </nav>
    {aviso}
    {_alerta_cerrado_hoy(negocio, phone_id, pwd)}
    {_alerta_modo_lluvia(negocio, phone_id, pwd)}
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
      <label>Mensaje de bienvenida</label>
      <textarea name='mensaje_bienvenida'>{v("mensaje_bienvenida")}</textarea>
      <label>Mensaje cuando estamos cerrados</label>
      <textarea name='mensaje_cerrado'>{v("mensaje_cerrado")}</textarea>
      <label>Información adicional para el bot (alergenos, políticas, etc.)</label>
      <textarea name='base_conocimiento'>{v("base_conocimiento")}</textarea>
    </div>

    <div class='card'>
      <h2>🕐 Horarios de atención</h2>
      <p style='font-size:.85rem;color:#64748b;margin-bottom:12px'>
        Activa los días que abres y define el horario. Si un día va a cerrar excepcionalmente,
        usa el botón de "Cerrado hoy" en la parte de arriba.
      </p>
      <div class='horarios-grid'>
        {_html_horarios(negocio)}
      </div>
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
    </div>
    <script>
    // Habilitar/deshabilitar campos de hora segun el checkbox de cada dia
    document.querySelectorAll('.chk-dia').forEach(chk => {{
        chk.addEventListener('change', () => {{
            const card = chk.closest('.dia-card');
            card.querySelectorAll('input[type=time]').forEach(i => i.disabled = !chk.checked);
            card.classList.toggle('cerrado', !chk.checked);
        }});
        // Estado inicial
        const card = chk.closest('.dia-card');
        card.querySelectorAll('input[type=time]').forEach(i => i.disabled = !chk.checked);
        if (!chk.checked) card.classList.add('cerrado');
    }});
    </script>
    </body></html>""")


@router.post("/admin/{phone_id}/config", response_class=HTMLResponse)
async def panel_config_post(
    phone_id: str, pwd: str = "",
    request: Request = None,
    nombre: str = Form(""),
    email_notificaciones: str = Form(""),
    mensaje_bienvenida: str = Form(""),
    mensaje_cerrado: str = Form(""),
    base_conocimiento: str = Form(""),
    tipo_servicio: str = Form("ambos"),
    metodos_pago: str = Form("Efectivo"),
    tiempo_recoger_min: int = Form(20),
    tiempo_envio_min: int = Form(40),
    costo_envio: float = Form(0),
    pedido_minimo: float = Form(0),
):
    import json as _json
    negocio = _neg(phone_id)
    if not negocio or not _auth(negocio, pwd):
        return RedirectResponse(f"/admin/{phone_id}?pwd={pwd}")

    # Reconstruir horarios desde los campos del formulario
    form_data = await request.form()
    horarios_json = {}
    for dia in _DIAS:
        abierto  = f"dia_{dia}" in form_data
        apertura = form_data.get(f"apertura_{dia}", "18:00")
        cierre   = form_data.get(f"cierre_{dia}", "00:00")
        horarios_json[dia] = {
            "abierto":  abierto,
            "apertura": apertura,
            "cierre":   cierre,
        }

    datos = {
        "nombre":              nombre.strip(),
        "email_notificaciones": email_notificaciones.strip() or None,
        "mensaje_bienvenida":  mensaje_bienvenida.strip(),
        "mensaje_cerrado":     mensaje_cerrado.strip(),
        "base_conocimiento":   base_conocimiento.strip(),
        "tipo_servicio":       tipo_servicio,
        "metodos_pago":        metodos_pago.strip(),
        "tiempo_recoger_min":  tiempo_recoger_min,
        "tiempo_envio_min":    tiempo_envio_min,
        "costo_envio":         costo_envio,
        "pedido_minimo":       pedido_minimo,
        "horarios_json":       horarios_json,
    }
    db.actualizar_negocio(phone_id, datos)
    from main import _negocios_cache
    _negocios_cache[phone_id].update(datos)
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
      <a href='/admin/{phone_id}/cocina?pwd={pwd}' style='background:#0f1115;color:#f59e0b;border-color:#f59e0b'>📺 Vista Cocina</a>
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


# ── VISTA DE COCINA (KDS) ─────────────────────────────────────────────────────

@router.get("/admin/{phone_id}/cocina", response_class=HTMLResponse)
async def panel_cocina(phone_id: str, pwd: str = ""):
    negocio = _neg(phone_id)
    if not negocio or not _auth(negocio, pwd):
        return RedirectResponse(f"/admin/{phone_id}?pwd={pwd}")
    nombre = html.escape(negocio["nombre"])
    return HTMLResponse(f"""<!DOCTYPE html><html lang='es'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Cocina — {nombre}</title>
<style>
* {{ box-sizing:border-box; margin:0; padding:0; }}
:root {{
  --bg:#0f1115; --panel:#171a21; --nuevo:#f59e0b; --proceso:#3b82f6;
  --listo:#22c55e; --texto:#f8fafc; --tenue:#94a3b8; --linea:#262b36;
}}
body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  background:var(--bg); color:var(--texto); padding:16px; min-height:100vh; }}
.barra {{ display:flex; align-items:center; justify-content:space-between;
  margin-bottom:20px; padding-bottom:14px; border-bottom:2px solid var(--linea); }}
.barra h1 {{ font-size:1.6rem; font-weight:800; letter-spacing:-.5px; }}
.barra .meta {{ display:flex; gap:20px; align-items:center; }}
.contador {{ font-size:1.1rem; color:var(--tenue); }}
.contador b {{ color:var(--nuevo); font-size:1.6rem; }}
.reloj {{ font-size:1.3rem; font-weight:700; font-variant-numeric:tabular-nums; }}
.sonido-btn {{ background:var(--panel); border:1px solid var(--linea); color:var(--texto);
  padding:8px 14px; border-radius:10px; font-size:.9rem; cursor:pointer; }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(300px,1fr)); gap:16px; }}
.card {{ background:var(--panel); border-radius:16px; padding:18px;
  border-top:6px solid var(--nuevo); box-shadow:0 4px 16px rgba(0,0,0,.3);
  display:flex; flex-direction:column; gap:12px; animation:pop .3s ease; }}
.card.proceso {{ border-top-color:var(--proceso); }}
.card.listo {{ border-top-color:var(--listo); opacity:.75; }}
.card.nuevo-flash {{ animation:flash 1s ease infinite; }}
@keyframes flash {{ 0%,100%{{box-shadow:0 0 0 0 rgba(245,158,11,.7);}} 50%{{box-shadow:0 0 0 8px rgba(245,158,11,0);}} }}
@keyframes pop {{ from{{transform:scale(.95);opacity:0;}} to{{transform:scale(1);opacity:1;}} }}
.card-top {{ display:flex; justify-content:space-between; align-items:flex-start; }}
.num {{ font-size:1.7rem; font-weight:800; }}
.tiempo {{ font-size:1.1rem; font-weight:700; padding:4px 10px; border-radius:8px;
  background:rgba(255,255,255,.08); font-variant-numeric:tabular-nums; }}
.tiempo.urgente {{ background:rgba(239,68,68,.25); color:#fca5a5; }}
.cliente {{ font-size:1.05rem; font-weight:600; }}
.tipo {{ font-size:.95rem; color:var(--tenue); }}
.tipo.envio {{ color:#fbbf24; }}
.items {{ list-style:none; display:flex; flex-direction:column; gap:6px;
  border-top:1px solid var(--linea); padding-top:10px; }}
.items li {{ font-size:1.1rem; display:flex; gap:8px; }}
.items .cant {{ font-weight:800; color:var(--nuevo); min-width:32px; }}
.nota {{ background:rgba(239,68,68,.15); border:1px solid rgba(239,68,68,.4);
  color:#fca5a5; padding:8px 10px; border-radius:8px; font-size:.95rem; }}
.dir {{ font-size:.9rem; color:var(--tenue); }}
.acciones {{ display:flex; gap:8px; margin-top:auto; }}
.btn {{ flex:1; padding:12px; border:none; border-radius:10px; font-size:1rem;
  font-weight:700; cursor:pointer; color:#fff; transition:transform .1s; }}
.btn:active {{ transform:scale(.95); }}
.btn-proceso {{ background:var(--proceso); }}
.btn-listo {{ background:var(--listo); }}
.btn-entregado {{ background:#475569; }}
.btn-copiar {{ background:#0ea5e9; width:100%; margin-top:4px; }}
.vacio {{ text-align:center; color:var(--tenue); padding:80px 20px; font-size:1.2rem; }}
</style></head><body>
<div class='barra'>
  <h1>🌮 {nombre} · Cocina</h1>
  <div class='meta'>
    <span class='contador'><b id='num-activos'>0</b> pedidos activos</span>
    <button class='sonido-btn' id='btn-sonido' onclick='toggleSonido()'>🔔 Sonido: ON</button>
    <span class='reloj' id='reloj'>--:--</span>
  </div>
</div>
<div class='grid' id='grid'></div>
<div class='vacio' id='vacio' style='display:none'>Sin pedidos activos por ahora 🍽️</div>

<script>
const PHONE='{phone_id}', PWD='{pwd}';
let sonidoOn=true, conocidos=new Set(), primeraCarga=true;

function toggleSonido(){{
  sonidoOn=!sonidoOn;
  document.getElementById('btn-sonido').textContent='🔔 Sonido: '+(sonidoOn?'ON':'OFF');
}}

// Sonido de alerta generado con Web Audio (sin archivos externos)
function alerta(){{
  if(!sonidoOn) return;
  try {{
    const ctx=new (window.AudioContext||window.webkitAudioContext)();
    [0,0.15,0.3].forEach(t=>{{
      const o=ctx.createOscillator(), g=ctx.createGain();
      o.connect(g); g.connect(ctx.destination);
      o.frequency.value=880; o.type='sine';
      g.gain.setValueAtTime(0.0001,ctx.currentTime+t);
      g.gain.exponentialRampToValueAtTime(0.3,ctx.currentTime+t+0.02);
      g.gain.exponentialRampToValueAtTime(0.0001,ctx.currentTime+t+0.12);
      o.start(ctx.currentTime+t); o.stop(ctx.currentTime+t+0.13);
    }});
  }} catch(e){{}}
}}

function reloj(){{
  const d=new Date();
  document.getElementById('reloj').textContent=
    d.toLocaleTimeString('es-MX',{{hour:'2-digit',minute:'2-digit'}});
}}
setInterval(reloj,1000); reloj();

function minutos(creado){{
  return Math.floor((Date.now()-new Date(creado).getTime())/60000);
}}

function tarjeta(p){{
  const min=minutos(p.created_at);
  const urgente=min>=20?'urgente':'';
  const flash=(p.estado==='nuevo')?'nuevo-flash':'';
  const clase=p.estado==='nuevo'?'nuevo':p.estado;
  const items=(p.items||[]).map(i=>
    `<li><span class='cant'>${{i.cantidad}}x</span> ${{i.nombre}}</li>`).join('');
  const nota=p.notas?`<div class='nota'>📝 ${{p.notas}}</div>`:'';
  const tipo=p.tipo_entrega==='envio'
    ?`<div class='tipo envio'>🛵 Envío</div><div class='dir'>${{p.direccion||''}}</div>`
    :`<div class='tipo'>🏪 Para recoger</div>`;
  let btn='';
  if(p.estado==='nuevo')   btn=`<button class='btn btn-proceso' onclick="cambiar(${{p.id}},'en_proceso')">Empezar</button>`;
  if(p.estado==='en_proceso') btn=`<button class='btn btn-listo' onclick="cambiar(${{p.id}},'listo')">Marcar listo</button>`;
  if(p.estado==='listo')   btn=`<button class='btn btn-entregado' onclick="cambiar(${{p.id}},'entregado')">Entregar</button>`;
  // Boton de copiar para repartidores: solo en pedidos de envio
  const btnCopiar = p.tipo_entrega==='envio'
    ? `<button class='btn btn-copiar' onclick='copiarReparto(${{p.id}})'>📋 Copiar p/ repartidor</button>`
    : '';
  return `<div class='card ${{clase}} ${{flash}}' data-id='${{p.id}}'>
    <div class='card-top'><div class='num'>#${{p.id}}</div>
      <div class='tiempo ${{urgente}}'>${{min}} min</div></div>
    <div class='cliente'>${{p.nombre_cliente||'Cliente'}}</div>
    ${{tipo}}
    <ul class='items'>${{items}}</ul>
    ${{nota}}
    <div class='acciones'>${{btn}}</div>
    ${{btnCopiar}}
  </div>`;
}}

// Guardamos los pedidos en memoria para armar el mensaje al copiar
let pedidosMem={{}};

function copiarReparto(id){{
  const p=pedidosMem[id];
  if(!p) return;
  const lineas=[];
  lineas.push(`🛵 *PEDIDO #${{p.id}} - ENVÍO*`);
  lineas.push('');
  lineas.push(`👤 ${{p.nombre_cliente||'Cliente'}}`);
  lineas.push(`📍 ${{p.direccion||'(sin dirección)'}}`);
  lineas.push(`📞 ${{p.telefono||''}}`);
  lineas.push('');
  lineas.push('🍽️ *Pedido:*');
  (p.items||[]).forEach(i=> lineas.push(`  • ${{i.cantidad}}x ${{i.nombre}}`));
  if(p.notas) lineas.push(`\\n📝 Notas: ${{p.notas}}`);
  lineas.push('');
  lineas.push(`💰 Total: $${{Number(p.total).toFixed(2)}}`);
  lineas.push(`💵 Pago: ${{p.metodo_pago||'Efectivo'}}`);
  const texto=lineas.join('\\n');

  // Copiar al portapapeles
  if(navigator.clipboard && navigator.clipboard.writeText){{
    navigator.clipboard.writeText(texto).then(()=>avisoCopiado(id))
      .catch(()=>copiarFallback(texto,id));
  }} else {{
    copiarFallback(texto,id);
  }}
}}

function copiarFallback(texto,id){{
  const ta=document.createElement('textarea');
  ta.value=texto; ta.style.position='fixed'; ta.style.opacity='0';
  document.body.appendChild(ta); ta.select();
  try{{ document.execCommand('copy'); avisoCopiado(id); }}catch(e){{}}
  document.body.removeChild(ta);
}}

function avisoCopiado(id){{
  const card=document.querySelector(`.card[data-id='${{id}}'] .btn-copiar`);
  if(!card) return;
  const orig=card.textContent;
  card.textContent='✅ ¡Copiado! Pégalo en el grupo';
  card.style.background='#22c55e';
  setTimeout(()=>{{ card.textContent=orig; card.style.background=''; }},2500);
}}

async function cargar(){{
  try {{
    const r=await fetch(`/admin/${{PHONE}}/cocina/datos?pwd=${{PWD}}`);
    const data=await r.json();
    const activos=data.filter(p=>['nuevo','en_proceso','listo'].includes(p.estado));
    // Guardar en memoria para el boton de copiar
    pedidosMem={{}};
    activos.forEach(p=> pedidosMem[p.id]=p);
    document.getElementById('num-activos').textContent=activos.length;

    // Detectar pedidos nuevos para sonar alerta
    const idsActuales=new Set(activos.map(p=>p.id));
    let hayNuevo=false;
    activos.forEach(p=>{{ if(!conocidos.has(p.id) && p.estado==='nuevo') hayNuevo=true; }});
    if(hayNuevo && !primeraCarga) alerta();
    conocidos=idsActuales;
    primeraCarga=false;

    const grid=document.getElementById('grid'), vacio=document.getElementById('vacio');
    if(activos.length===0){{ grid.innerHTML=''; vacio.style.display='block'; }}
    else {{ vacio.style.display='none'; grid.innerHTML=activos.map(tarjeta).join(''); }}
  }} catch(e){{ console.error(e); }}
}}

async function cambiar(id,estado){{
  const fd=new FormData(); fd.append('estado',estado);
  await fetch(`/admin/${{PHONE}}/pedidos/${{id}}/estado?pwd=${{PWD}}`,{{method:'POST',body:fd}});
  cargar();
}}

cargar();
setInterval(cargar,8000);  // refresca cada 8 segundos
</script>
</body></html>""")


@router.get("/admin/{phone_id}/cocina/datos")
async def panel_cocina_datos(phone_id: str, pwd: str = ""):
    negocio = _neg(phone_id)
    if not negocio or not _auth(negocio, pwd):
        return JSONResponse([], status_code=403)
    pedidos = db.obtener_pedidos_recientes(negocio["id"], limite=30)
    return JSONResponse(pedidos)
