# -*- coding: utf-8 -*-
"""Historico de FACTURACION MENSUAL (Odoo 16) -> dashboard HTML. SOLO LECTURA.

Replica el favorito "FACTURACION MENSUAL" de latinbien.com > Ventas:
    modelo   : sale.order
    dominio  : [('x_status_compra', '=', '4')]   -> "4. ENTREGA REALIZADA"
    agrupado : commitment_date (ano/mes) + user_id (ejecutivo)

No se escribe NADA en el ERP: OdooClient bloquea localmente cualquier metodo
de escritura (create/write/unlink/copy/...) antes de llamar al servidor, y aqui
solo se usan search_read / read / search_count / read_group.

Salidas (locales, no tocan el ERP ni el crm_cache.db):
    facturacion_mensual.html         dashboard autocontenido (solo agregados)
    facturacion_mensual_detalle.json cache local con las ordenes una a una

Uso:
    python facturacion_mensual.py              # ultimos 24 meses
    python facturacion_mensual.py --meses 36   # otra ventana
    python facturacion_mensual.py --sin-detalle
"""
import argparse
import json
import unicodedata
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

from odoo_client import OdooClient

BASE = Path(__file__).parent
MESES = {1: "enero", 2: "febrero", 3: "marzo", 4: "abril", 5: "mayo", 6: "junio",
         7: "julio", 8: "agosto", 9: "septiembre", 10: "octubre", 11: "noviembre",
         12: "diciembre"}

CAMPOS = ["name", "commitment_date", "date_order", "state", "amount_total",
          "amount_untaxed", "user_id", "source_id", "planes", "invoice_status",
          "invoice_count", "invoice_ids"]


def norm(texto):
    if not texto:
        return ""
    s = unicodedata.normalize("NFKD", str(texto))
    return "".join(ch for ch in s if not unicodedata.combining(ch)).lower().strip()


def mes_ini(anio, mes):
    return date(anio, mes, 1)


def rango_meses(n, hoy=None):
    """Devuelve [(anio, mes), ...] de los ultimos n meses, incluido el actual."""
    hoy = hoy or date.today()
    y, m = hoy.year, hoy.month
    out = []
    for _ in range(n):
        out.append((y, m))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return list(reversed(out))


def _nombre_m2(m2o):
    if not m2o:
        return ""
    if isinstance(m2o, (list, tuple)):
        return (m2o[1] if len(m2o) > 1 else "") or ""
    return str(m2o)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--meses", type=int, default=24, help="ventana historica en meses")
    ap.add_argument("--desde", default=None,
                    help="corta la ventana desde este mes, p.ej. --desde 2025-01 (saca 2024)")
    ap.add_argument("--sin-detalle", action="store_true", help="no escribir el JSON de ordenes")
    ap.add_argument("--salida", default="facturacion_mensual.html")
    ap.add_argument("--desde-detalle", action="store_true",
                    help="regenera el HTML desde facturacion_mensual_detalle.json "
                         "sin volver a consultar el ERP")
    args = ap.parse_args()

    cfg = json.load(open(BASE / "config.json", encoding="utf-8"))
    equipo = [n for n in cfg.get("executives_active", [])]

    if args.desde_detalle:
        det = json.load(open(BASE / "facturacion_mensual_detalle.json", encoding="utf-8"))
        ordenes = det["ordenes"]
        planes_sel = det.get("planes_sel") or {}
        facturas = det.get("facturas") or {}
        if args.desde:
            desde = args.desde.strip()
            ordenes = [o for o in ordenes if (o.get("commitment_date") or "")[:7] >= desde]
            print(f"Recortado a {desde} en adelante.")
        print(f"Regenerando desde el detalle local ({len(ordenes)} ordenes). ERP no consultado.")
    else:
        c = OdooClient(cfg["odoo"]["url"], cfg["odoo"]["db"], cfg["odoo"]["user"],
                       cfg["odoo"]["api_key"]).connect()
        print(f"Conectado a {cfg['odoo']['url']} (uid {c.uid}) - SOLO LECTURA")

        # Etiquetas legibles de los campos selection del modulo Ventas.
        fg = c.execute_kw("sale.order", "fields_get", [["planes"]],
                          {"attributes": ["selection"]})
        planes_sel = {k: v for k, v in (fg.get("planes", {}).get("selection") or [])}
        print("Planes:", planes_sel or "(sin catalogo)")

        # ---------- Ventana historica ----------
        meses = rango_meses(args.meses)
        if args.desde:
            desde = args.desde.strip()
            meses = [m for m in meses if f"{m[0]:04d}-{m[1]:02d}" >= desde]
            if not meses:
                raise SystemExit(f"--desde {desde} deja la ventana sin meses.")
        print(f"Ventana: {MESES[meses[0][1]]} {meses[0][0]} -> {MESES[meses[-1][1]]} {meses[-1][0]}")

        # ---------- Bajada SOLO LECTURA, mes a mes ----------
        # Dominio identico al del favorito, acotado por commitment_date.
        base_dom = [("x_status_compra", "=", "4")]
        ordenes = []
        for i, (y, m) in enumerate(meses):
            d0 = mes_ini(y, m)
            d1 = mes_ini(*((y + 1, 1) if m == 12 else (y, m + 1)))
            dom = base_dom + [("commitment_date", ">=", d0.strftime("%Y-%m-%d 00:00:00")),
                              ("commitment_date", "<", d1.strftime("%Y-%m-%d 00:00:00"))]
            filas = c.search_read("sale.order", dom, CAMPOS, order="commitment_date")
            ordenes += filas
            print(f"  {MESES[m]} {y}: {len(filas)} ordenes")

        # ---------- Facturas vinculadas (contabilidad), SOLO LECTURA ----------
        # Para poder cuadrar el monto de las ordenes contra las facturas reales.
        facturas = leer_facturas(c, ordenes)

    # ---------- Agregados: mes x ejecutivo x origen x plan ----------
    filas = agregar(ordenes, planes_sel, facturas)

    equipo = [n for n in cfg.get("executives_active", [])]
    mms = sorted(filas, key=lambda r: r["m"])
    ventana = (f"{MESES[int(mms[0]['m'][5:7])]} {mms[0]['m'][:4]} - "
               f"{MESES[int(mms[-1]['m'][5:7])]} {mms[-1]['m'][:4]}") if mms else "sin datos"
    payload = {
        "generado": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "erp": f"{cfg['odoo']['url']} (db {cfg['odoo']['db']})",
        "origen": "Venta > Favoritos > FACTURACION MENSUAL  |  sale.order con "
                  "x_status_compra = 4 (4. ENTREGA REALIZADA)  |  fecha: commitment_date",
        "ventana": ventana,
        "equipo": equipo,
        "filas": filas,
        "descuadre": descuadres(ordenes, facturas),
    }

    # ---------- Archivos locales ----------
    escribir_html(payload, args.salida)

    if not args.sin_detalle:
        det = BASE / "facturacion_mensual_detalle.json"
        det.write_text(json.dumps({"generado": payload["generado"], "planes_sel": planes_sel,
                                   "facturas": facturas, "ordenes": ordenes},
                                  ensure_ascii=False, indent=1), encoding="utf-8")
        print("Detalle local:", det.name, f"({len(ordenes)} ordenes)")

    total = sum(r["t"] for r in filas)
    tot_fac = sum(r["ti"] for r in filas)
    print(f"\nTotal {len({r['m'] for r in filas})} meses: {len(ordenes)} ordenes | "
          f"USD {total:,.2f} | facturas contables USD {tot_fac:,.2f} "
          f"| dif USD {total - tot_fac:,.2f}")
    print("Por año (monto de ordenes | monto de facturas):")
    for y in sorted({r["m"][:4] for r in filas}):
        fy = [r for r in filas if r["m"][:4] == y]
        print(f"   {y}: {sum(r['t'] for r in fy):>13,.2f} | {sum(r['ti'] for r in fy):>13,.2f}"
              f" | {sum(r['n'] for r in fy):>5} ordenes")
    print("El ERP no fue modificado: solo search_read / read / fields_get.")


def importe_firmado(fac):
    """Monto con signo contable: la nota credito RESTA, la cancelada vale 0."""
    if fac["e"] == "cancel":
        return 0.0
    return -abs(fac["a"]) if fac["t"] == "out_refund" else fac["a"]


def descuadres(ordenes, facturas):
    """Ordenes cuyo monto no coincide con el neto de sus facturas."""
    out = []
    for o in ordenes:
        ids = [str(i) for i in (o.get("invoice_ids") or []) if str(i) in facturas]
        neto = sum(importe_firmado(facturas[i]) for i in ids)
        monto = float(o.get("amount_total") or 0)
        if abs(neto - monto) > 0.01:
            out.append({"n": o.get("name") or "", "m": (o.get("commitment_date") or "")[:7],
                        "o": round(monto, 2), "f": round(neto, 2),
                        "d": round(neto - monto, 2)})
    out.sort(key=lambda r: -abs(r["d"]))
    return out


def leer_facturas(c, ordenes):
    """Lee (solo lectura) las facturas vinculadas a las ordenes.

    Devuelve {invoice_id: {"a": amount_total, "e": state, "t": move_type}}.
    Se leen en lotes de 300 para no pedir un payload gigante al ERP.
    """
    ids = sorted({i for o in ordenes for i in (o.get("invoice_ids") or [])})
    facturas = {}
    if not ids:
        print("Las ordenes no traen invoice_ids: no habra cuadre contable.")
        return facturas
    for i in range(0, len(ids), 300):
        lote = ids[i:i + 300]
        for r in c.read("account.move", lote, ["amount_total", "state", "move_type",
                                              "invoice_date", "name"]):
            facturas[str(r["id"])] = {"a": r.get("amount_total") or 0.0,
                                      "e": r.get("state") or "",
                                      "t": r.get("move_type") or "",
                                      "f": r.get("invoice_date") or "",
                                      "n": r.get("name") or ""}
    print(f"Facturas vinculadas leidas: {len(facturas)}")
    return facturas


def agregar(ordenes, planes_sel, facturas=None):
    """Agrega mes x ejecutivo x origen x plan.

    n = ordenes facturadas | tf = monto facturado
    ti = neto de las facturas contables (notas credito restan)
    ci = # facturas | cc = notas credito | cx = facturas canceladas
    """
    facturas = facturas or {}
    agg = {}
    for o in ordenes:
        cd = o.get("commitment_date")
        if not cd:
            continue
        dt = datetime.fromisoformat(cd)
        clave = (
            f"{dt.year:04d}-{dt.month:02d}",
            (_nombre_m2(o.get("user_id")) or "SIN ASESOR").strip(),
            (_nombre_m2(o.get("source_id")) or "SIN ORIGEN").strip(),
            planes_sel.get(o.get("planes"), o.get("planes") or "SIN PLAN") or "SIN PLAN",
        )
        a = agg.setdefault(clave, {"n": 0, "t": 0.0, "f": 0, "tf": 0.0,
                                   "ti": 0.0, "ci": 0, "cc": 0.0, "cx": 0.0})
        monto = float(o.get("amount_total") or 0.0)
        a["n"] += 1
        a["t"] += monto
        if (o.get("invoice_status") == "invoiced") or (o.get("invoice_count") or 0) > 0:
            a["f"] += 1
            a["tf"] += monto
        for iid in (o.get("invoice_ids") or []):
            fac = facturas.get(str(iid))
            if not fac:
                continue
            a["ci"] += 1
            if fac["t"] == "out_refund":
                a["cc"] += abs(fac["a"])
            if fac["e"] == "cancel":
                a["cx"] += abs(fac["a"])
            a["ti"] += importe_firmado(fac)
    filas = [{"m": k[0], "e": k[1], "o": k[2], "p": k[3],
              "n": v["n"], "t": round(v["t"], 2), "f": v["f"], "tf": round(v["tf"], 2),
              "ti": round(v["ti"], 2), "ci": v["ci"], "cc": round(v["cc"], 2),
              "cx": round(v["cx"], 2)}
             for k, v in agg.items()]
    filas.sort(key=lambda r: (r["m"], -r["t"]))
    return filas


def escribir_html(payload, salida):
    html = BASE / salida
    html.write_text(TEMPLATE.replace("/*__DATA__*/", json.dumps(payload, ensure_ascii=False)),
                    encoding="utf-8")
    print("Dashboard:", html.name)


TEMPLATE = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Facturacion Mensual - Latinbien (historico 24 meses)</title>
<style>
:root { --azul:#0f3b6e; --verde:#1e8e5a; --naranja:#e07b2a; --rojo:#8a1d1d;
        --gris:#eef1f5; --borde:#d3dae3; --tinta:#1c2733; --gris_txt:#56657a; }
* { box-sizing: border-box; }
body { font-family:"Segoe UI",Arial,sans-serif; margin:0; background:#f4f6f9; color:var(--tinta); }
header { background:var(--azul); color:#fff; padding:14px 22px; }
header h1 { font-size:18px; margin:0 0 3px; }
.sub { font-size:12px; opacity:.9; }
main { padding:20px; max-width:1340px; margin:0 auto; }
.barra { background:#fff; border:1px solid var(--borde); border-radius:10px; padding:14px 16px;
         margin-bottom:16px; display:flex; gap:14px; align-items:flex-end; flex-wrap:wrap; }
.barra label { font-size:12px; color:var(--gris_txt); display:flex; flex-direction:column; gap:4px; }
.barra select, .barra input { font-size:13px; padding:6px 8px; border:1px solid var(--borde); border-radius:6px; }
.btn { background:var(--azul); color:#fff; border:none; border-radius:6px; padding:8px 14px;
       font-size:13px; cursor:pointer; }
.btn:hover { filter:brightness(1.12); }
.btn.sec { background:#fff; color:var(--azul); border:1px solid var(--borde); }
.tarjeta { background:#fff; border:1px solid var(--borde); border-radius:10px; padding:16px; margin-bottom:18px; }
.tarjeta h2 { margin:0 0 4px; font-size:15px; color:var(--azul); }
.nota { font-size:12px; color:var(--gris_txt); margin:0 0 12px; }
.kpis { display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:12px; margin-bottom:18px; }
.kpi { background:#fff; border:1px solid var(--borde); border-radius:10px; padding:13px 15px; }
.kpi b { display:block; font-size:20px; margin-top:4px; }
.kpi span { font-size:11px; text-transform:uppercase; letter-spacing:.4px; color:var(--gris_txt); }
.up { color:var(--verde); } .down { color:var(--rojo); }
table { width:100%; border-collapse:collapse; font-size:13px; }
th, td { border-bottom:1px solid var(--gris); padding:7px 10px; text-align:left; white-space:nowrap; }
th { background:#f8fafc; color:var(--gris_txt); font-weight:600; position:sticky; top:0; }
td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; }
tr.total td { font-weight:700; background:#f2f7ff; }
tr:hover td { background:#fafcff; }
.bar-fondo { background:var(--gris); border-radius:7px; height:14px; min-width:70px; overflow:hidden; }
.bar-fondo > i { display:block; height:14px; border-radius:7px; background:var(--azul); }
.graf { display:flex; align-items:flex-end; gap:3px; height:190px; margin-top:10px; overflow-x:auto; }
.anual { display:flex; align-items:flex-end; gap:34px; height:230px; margin:14px 0 8px; }
.anual .barra-a { flex:1; max-width:200px; display:flex; flex-direction:column; justify-content:flex-end;
                  align-items:center; height:100%; gap:6px; }
.anual .barra-a i { display:block; width:74%; background:var(--azul); border-radius:5px 5px 0 0; min-height:2px; }
.anual .barra-a b { font-size:13px; color:var(--azul); }
.anual .barra-a span { font-size:12px; color:var(--gris_txt); }
.anual .sep { width:1px; align-self:stretch; background:var(--borde); }
.anual .barra-a.total i { background:var(--verde); }
.anual .barra-a.total b { color:var(--verde); font-size:16px; }
.anual .barra-a.encurso i { background:repeating-linear-gradient(45deg,#0f3b6e,#0f3b6e 6px,#2b6398 6px,#2b6398 12px); }
.anual .nota-a { font-size:11px; color:var(--rojo); }
details { margin-top:14px; border-top:1px solid var(--gris); padding-top:10px; }
details summary { cursor:pointer; font-size:13px; color:var(--azul); font-weight:600; }
details p.nota { margin-top:8px; }
.graf .col { flex:1 0 26px; display:flex; flex-direction:column; justify-content:flex-end;
             align-items:center; gap:3px; }
.graf .col i { display:block; width:70%; background:var(--azul); border-radius:3px 3px 0 0; min-height:1px; }
.graf .col i.aa { background:#b9c6d6; }
.graf .col span { font-size:9px; color:var(--gris_txt); white-space:nowrap; }
.tendencia { margin-top:10px; }
.tendencia svg { width:100%; height:auto; display:block; }
.tendencia text.eje { font-size:11px; fill:#56657a; }
.tendencia text.marca { font-size:11px; fill:#0f3b6e; font-weight:600; }
.apilado { display:flex; align-items:flex-end; gap:3px; height:190px; margin-top:10px; overflow-x:auto; }
.apilado .col { flex:1 0 26px; display:flex; flex-direction:column; justify-content:flex-end; height:100%; }
.apilado .seg { width:70%; margin:0 auto; }
.apilado .seg:first-child { border-radius:3px 3px 0 0; }
.legend { margin-top:8px; font-size:12px; }
.legend span i { display:inline-block; width:10px; height:10px; border-radius:2px; margin:0 4px 0 12px; }
.badge { font-size:10px; background:#e8f3ec; color:var(--verde); border:1px solid #bfe0cd;
         border-radius:9px; padding:1px 7px; margin-left:6px; }
.pestanas { display:flex; flex-wrap:wrap; gap:6px; margin-bottom:14px; }
.pestanas button { border:1px solid var(--borde); background:#fff; border-radius:6px;
                   padding:7px 13px; font-size:13px; cursor:pointer; color:#3a4a5c; }
.pestanas button.activa { background:var(--azul); color:#fff; border-color:var(--azul); font-weight:600; }
.oculto { display:none; }
.foot { color:#8a939c; font-size:11px; text-align:center; margin:18px 0 8px; }
.lectura { font-size:12px; color:var(--verde); font-weight:600; }
</style>
</head>
<body>
<header>
  <h1>Facturaci&oacute;n Mensual &#8226; Latinbien</h1>
  <div class="sub" id="sub"></div>
</header>
<main>
  <div class="barra">
    <label>Mes desde <select id="f-desde"></select></label>
    <label>Mes hasta <select id="f-hasta"></select></label>
    <label>Ejecutivo <select id="f-ejec"></select></label>
    <label>Origen <select id="f-origen"></select></label>
    <label>Plan <select id="f-plan"></select></label>
    <label class="chk"><input type="checkbox" id="f-fact"> solo facturadas</label>
    <button class="btn" id="btn-csv">Descargar CSV</button>
    <button class="btn sec" id="btn-reset">Limpiar filtros</button>
    <span class="lectura">&#128274; Datos leidos del ERP, sin modificar nada</span>
  </div>

  <div class="kpis" id="kpis"></div>

  <section class="tarjeta">
    <h2>Total por a&ntilde;o</h2>
    <p class="nota">Suma de cada a&ntilde;o dentro del rango seleccionado. La barra verde es el
      <b>consolidado</b> (suma de los a&ntilde;os). En rojo, la diferencia contra las facturas
      ya emitidas en contabilidad.</p>
    <div class="anual" id="graf-anual"></div>
    <div id="tabla-anual"></div>
    <div id="detalle-descuadre"></div>
  </section>

  <section class="tarjeta">
    <h2>Tendencia de facturado por mes</h2>
    <p class="nota">Linea azul: monto facturado de cada mes. Linea gris punteada: mismo mes del a&ntilde;o
      anterior. Pasa el cursor sobre cada punto para ver el monto y las &oacute;rdenes.
      El &uacute;ltimo mes aparece hueco porque est&aacute; en curso.</p>
    <div class="tendencia" id="graf-tendencia"></div>
    <div class="legend" id="legend-mes"></div>
  </section>

  <section class="tarjeta">
    <h2>Composici&oacute;n mensual por ejecutivo</h2>
    <p class="nota">Cada columna es un mes; los bloques son la parte que aporta cada ejecutivo.</p>
    <div class="apilado" id="graf-apilado"></div>
    <div class="legend" id="legend-ejec"></div>
  </section>

  <div class="pestanas">
    <button class="activa" data-t="mes">Resumen mensual</button>
    <button data-t="ejec">Por ejecutivo</button>
    <button data-t="matriz">Matriz mes x ejecutivo</button>
    <button data-t="origen">Por origen</button>
    <button data-t="plan">Por plan</button>
    <button data-t="inter">Comparaci&oacute;n interanual</button>
  </div>

  <section class="tarjeta" id="p-mes"><h2>Resumen mensual</h2><div class="scroll"></div></section>
  <section class="tarjeta oculto" id="p-ejec"><h2>Por ejecutivo</h2><div class="scroll"></div></section>
  <section class="tarjeta oculto" id="p-matriz"><h2>Matriz mes x ejecutivo (monto facturado)</h2><div class="scroll"></div></section>
  <section class="tarjeta oculto" id="p-origen"><h2>Por origen</h2><div class="scroll"></div></section>
  <section class="tarjeta oculto" id="p-plan"><h2>Por plan</h2><div class="scroll"></div></section>
  <section class="tarjeta oculto" id="p-inter"><h2>Comparaci&oacute;n interanual (mismo mes)</h2><div class="scroll"></div></section>

  <p class="foot" id="foot"></p>
</main>
<script>
const DATA = /*__DATA__*/;
const MES = {1:"enero",2:"febrero",3:"marzo",4:"abril",5:"mayo",6:"junio",7:"julio",
             8:"agosto",9:"septiembre",10:"octubre",11:"noviembre",12:"diciembre"};
const COLORES = ["#0f3b6e","#1e8e5a","#e07b2a","#8a1d1d","#5b4b8a","#0f7b8a","#a3761f",
                 "#b53d7a","#3d7a3d","#6b6b6b"];
const MESES_TODOS = [...new Set(DATA.filas.map(r=>r.m))].sort();
const EJEC_TODOS = [...new Set(DATA.filas.map(r=>r.e))].sort();
const ORIGEN_TODOS = [...new Set(DATA.filas.map(r=>r.o))].sort();
const PLAN_TODOS = [...new Set(DATA.filas.map(r=>r.p))].sort();
const norm = s => (s||"").normalize("NFD").replace(/[\u0300-\u036f]/g,"").toLowerCase().trim();
const EQUIPO = new Set((DATA.equipo||[]).map(norm));
const esEquipo = n => [...EQUIPO].some(e => norm(n).startsWith(e));
const MES_ACTUAL = (DATA.generado||"").slice(0,7);
const enCurso = m => m === MES_ACTUAL;
const nomMes = m => { const [y,mm]=m.split("-").map(Number); return MES[mm]+" "+y; };
const nomMesLarga = m => nomMes(m) + (enCurso(m) ? " (mes en curso)" : "");
const money = v => "$" + (v||0).toLocaleString("es-VE",{minimumFractionDigits:2,maximumFractionDigits:2});
const money0 = v => "$" + Math.round(v||0).toLocaleString("es-VE");
const money0s = v => (v<0?"-":"") + "$" + Math.round(Math.abs(v||0)).toLocaleString("es-VE");
const pct = v => (v===null||v===undefined||!isFinite(v)) ? "&mdash;" : (v>=0?"+":"")+v.toFixed(1)+"%";

function el(id){ return document.getElementById(id); }
function opciones(sel, lista, todos){
  sel.innerHTML = '<option value="">'+todos+'</option>' + lista.map(v=>'<option value="'+esc(v)+'">'+esc(v)+'</option>').join('');
}
function esc(s){ return (s==null?"":String(s)).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;"); }

/* ---------- filtros ---------- */
function pintarFiltros(){
  opciones(el("f-desde"), MESES_TODOS, "inicio");
  opciones(el("f-hasta"), MESES_TODOS, "fin");
  opciones(el("f-ejec"), EJEC_TODOS, "todos");
  opciones(el("f-origen"), ORIGEN_TODOS, "todos");
  opciones(el("f-plan"), PLAN_TODOS, "todos");
  el("f-desde").value = MESES_TODOS[0];
  el("f-hasta").value = MESES_TODOS[MESES_TODOS.length-1];
}
function filtrar(){
  const d = el("f-desde").value || MESES_TODOS[0];
  const h = el("f-hasta").value || MESES_TODOS[MESES_TODOS.length-1];
  const e = el("f-ejec").value, o = el("f-origen").value, p = el("f-plan").value;
  const soloF = el("f-fact").checked;
  return DATA.filas.filter(r => {
    if (r.m < d || r.m > h) return false;
    if (e && r.e !== e) return false;
    if (o && r.o !== o) return false;
    if (p && r.p !== p) return false;
    if (soloF) return r.f > 0;
    return true;
  }).map(r => soloF ? {m:r.m, e:r.e, o:r.o, p:r.p, n:r.f, t:r.tf, f:r.f} : r);
}

/* ---------- agregaciones ---------- */
function porClave(rows, clave){
  const m = new Map();
  rows.forEach(r => {
    const k = r[clave];
    const o = m.get(k) || {k, n:0, t:0, f:0};
    o.n += r.n; o.t += r.t; o.f += r.f;
    m.set(k, o);
  });
  return [...m.values()];
}
function porMes(rows){
  const m = new Map();
  rows.forEach(r => {
    const o = m.get(r.m) || {m:r.m, n:0, t:0, f:0, ti:0, ci:0, cc:0};
    o.n += r.n; o.t += r.t; o.f += r.f;
    o.ti += (r.ti||0); o.ci += (r.ci||0); o.cc += (r.cc||0);
    m.set(r.m, o);
  });
  return [...m.values()].sort((a,b)=>a.m<b.m?-1:1);
}

/* ---------- por ano: barras + consolidado ---------- */
function porAnio(rows){
  const ms = porMes(rows);
  const map = new Map();
  ms.forEach(m => {
    const y = m.m.slice(0,4);
    const o = map.get(y) || {y, n:0, t:0, ti:0, ci:0, cc:0, meses:0, enCurso:false};
    o.n += m.n; o.t += m.t; o.ti += (m.ti||0); o.ci += (m.ci||0); o.cc += (m.cc||0);
    o.meses += 1;
    if (enCurso(m.m)) o.enCurso = true;
    map.set(y, o);
  });
  return [...map.values()].sort((a,b)=>a.y<b.y?-1:1);
}
function anualGraf(rows){
  const anios = porAnio(rows);
  const tot = anios.reduce((a,x)=>a+x.t,0);
  const max = Math.max(1, ...anios.map(x=>x.t), tot);
  const bars = anios.map(a=>{
    const h = Math.max(2, a.t/max*160);
    const dif = a.ti - a.t;
    return '<div class="barra-a'+(a.enCurso?" encurso":"")+'" '
      + 'title="'+a.y+': '+money(a.t)+" | "+a.n+" ordenes | "+a.meses+' meses">'
      + '<b>'+money0(a.t)+'</b>'
      + '<i style="height:'+h+'px"></i>'
      + '<span>'+a.y+(a.enCurso?' <em>(en curso)</em>':'')+'<br>'+a.n+' &oacute;rdenes</span>'
      + '<span class="nota-a">'+(Math.abs(dif)<0.01?"":money0s(dif)+" vs facturas")+'</span>'
      + '</div>';
  }).join("");
  el("graf-anual").innerHTML = bars
    + '<div class="sep"></div>'
    + '<div class="barra-a total" title="Suma de los '+anios.length+' a&ntilde;os">'
    + '<b>'+money0(tot)+'</b><i style="height:'+Math.max(2, tot/max*160)+'px"></i>'
    + '<span>CONSOLIDADO<br>'+anios.length+' a&ntilde;os &middot; '
    + anios.reduce((a,x)=>a+x.n,0)+' &oacute;rdenes</span></div>';
}
function anualTabla(rows){
  const anios = porAnio(rows);
  const tot = anios.reduce((a,x)=>a+x.t,0);
  const totFac = anios.reduce((a,x)=>a+x.ti,0);
  const max = Math.max(1,...anios.map(x=>x.t));
  const body = anios.map(a=>{
    const dif = a.ti - a.t;
    return '<tr><td><b>'+a.y+'</b>'+(a.enCurso?'<span class="badge">en curso</span>':"")
      +'<div class="nota" style="font-weight:400">'+a.meses+' meses</div></td>'
      +'<td class="num">'+a.n+'</td>'
      +'<td class="num"><b>'+money0(a.t)+'</b></td>'
      +'<td class="num">'+money(a.n?a.t/a.n:0)+'</td>'
      +'<td class="num">'+(tot?(a.t/tot*100).toFixed(1)+"%":"")+'</td>'
      +'<td class="num">'+money0(a.ti)+'</td>'
      +'<td class="num '+(Math.abs(dif)<0.01?"":(dif>0?"up":"down"))+'">'
      +(Math.abs(dif)<0.01?"cuadra":money0s(dif))+'</td>'
      +'<td>'+barra(a.t,max)+'</td></tr>';
  }).join("");
  const f = '<tr class="total"><td>CONSOLIDADO</td>'
    +'<td class="num">'+anios.reduce((a,x)=>a+x.n,0)+'</td>'
    +'<td class="num">'+money(tot)+'</td>'
    +'<td class="num">'+money(anios.reduce((a,x)=>a+x.n,0)?tot/anios.reduce((a,x)=>a+x.n,0):0)+'</td>'
    +'<td class="num">100%</td><td class="num">'+money0(totFac)+'</td>'
    +'<td class="num">'+(Math.abs(totFac-tot)<0.01?"cuadra":money0s(totFac-tot))+'</td>'
    +'<td>'+barra(tot,max,"#1e8e5a")+'</td></tr>';
  el("tabla-anual").innerHTML =
    '<div style="overflow-x:auto"><table><thead><tr>'
    +'<th>A&ntilde;o</th><th class="num">&Oacute;rdenes</th><th class="num">Facturado</th>'
    +'<th class="num">Ticket prom.</th><th class="num">% consolidado</th>'
    +'<th class="num">Facturado (facturas netas)</th><th class="num">Dif. contable</th><th>Peso</th>'
    +'</tr></thead><tbody>'+body+'</tbody><tfoot>'+f+'</tfoot></table></div>';
}
function bloqueDescuadre(){
  const ds = DATA.descuadre || [];
  if (!ds.length){
    el("detalle-descuadre").innerHTML =
      '<p class="nota" style="color:#1e8e5a;margin-top:12px">&#10003; '
      + 'Todas las &oacute;rdenes cuadran contra el neto de sus facturas.</p>';
    return;
  }
  const suma = ds.reduce((a,x)=>a+x.d,0);
  const body = ds.map(x=>'<tr><td>'+esc(x.n)+'</td><td>'+nomMes(x.m)+'</td>'
    +'<td class="num">'+money(x.o)+'</td><td class="num">'+money(x.f)+'</td>'
    +'<td class="num '+(x.d<0?"down":"up")+'">'+money0s(x.d)+'</td></tr>').join("");
  el("detalle-descuadre").innerHTML =
    '<details><summary>&iquest;Por qu&eacute; no cuadra? '+ds.length
    +' &oacute;rdenes ('+money0s(suma)+')</summary>'
    +'<p class="nota">El monto de la orden (columna "Facturado") contra el neto de sus facturas '
    +'(facturas de cliente menos notas cr&eacute;dito). Causa t&iacute;pica: la factura fue '
    +'anulada con nota cr&eacute;dito, o el monto facturado se ajust&oacute; despu&eacute;s de '
    +'crear la orden.</p>'
    +'<div style="overflow-x:auto;max-height:340px"><table><thead><tr><th>Orden</th><th>Mes</th>'
    +'<th class="num">Monto orden</th><th class="num">Neto facturas</th>'
    +'<th class="num">Diferencia</th></tr></thead><tbody>'+body+'</tbody></table></div></details>';
}

/* ---------- grafica de tendencia con ejes (SVG, sin librerias) ---------- */
function compacto(v){
  if (v >= 1000000) return "$" + (v/1000000).toFixed(1).replace(".0","") + "M";
  if (v >= 1000) return "$" + Math.round(v/1000) + "k";
  return "$" + Math.round(v);
}
function tendenciaSVG(rows){
  const ms = porMes(rows);
  const caja = el("graf-tendencia");
  if (!ms.length){ caja.innerHTML = '<p class="nota">Sin datos en el rango.</p>'; return; }
  const W = 1060, H = 340, padL = 78, padR = 22, padT = 22, padB = 52;
  const w = W - padL - padR, h = H - padT - padB;
  const max = Math.max(...ms.map(x=>x.t), 1);
  const paso = Math.pow(10, Math.floor(Math.log10(max)));
  const top = Math.max(paso, Math.ceil(max/paso)*paso);
  const n = ms.length;
  const px = i => padL + (n === 1 ? w/2 : i*w/(n-1));
  const py = v => padT + h - (v/top)*h;
  let s = [];

  // Fondo del area y cuadricula del eje Y
  s.push('<rect x="'+padL+'" y="'+padT+'" width="'+w+'" height="'+h+'" fill="#fbfcfe"/>');
  for (let k = 0; k <= 4; k++){
    const v = top*k/4, y = py(v);
    s.push('<line x1="'+padL+'" y1="'+y+'" x2="'+(padL+w)+'" y2="'+y
      +'" stroke="#e6ebf2" stroke-width="1"/>');
    s.push('<text x="'+(padL-8)+'" y="'+(y+4)+'" text-anchor="end" class="eje">'+compacto(v)+'</text>');
  }
  // Linea del ano previo (mismo mes), segmentada
  let seg = [];
  const dibujaSeg = arr => {
    if (arr.length < 2) return;
    s.push('<polyline points="'+arr.map(p=>p[0].toFixed(1)+","+p[1].toFixed(1)).join(" ")
      +'" fill="none" stroke="#b9c6d6" stroke-width="2" stroke-dasharray="5,4"/>');
    arr.forEach(p=>s.push('<circle cx="'+p[0].toFixed(1)+'" cy="'+p[1].toFixed(1)
      +'" r="2.5" fill="#b9c6d6"><title>'+p[2]+": "+money(p[3])+'</title></circle>'));
  };
  ms.forEach((x,i)=>{
    const y = x.m.slice(0,4);
    const p = ms.find(z=>z.m === String(+y-1) + x.m.slice(4));
    if (p) seg.push([px(i), py(p.t), nomMes(p.m), p.t]);
    else { dibujaSeg(seg); seg = []; }
  });
  dibujaSeg(seg);

  // Ejes
  s.push('<line x1="'+padL+'" y1="'+padT+'" x2="'+padL+'" y2="'+(padT+h)+'" stroke="#8a939c" stroke-width="1.5"/>');
  s.push('<line x1="'+padL+'" y1="'+(padT+h)+'" x2="'+(padL+w)+'" y2="'+(padT+h)+'" stroke="#8a939c" stroke-width="1.5"/>');

  // Area + linea de tendencia
  let area = "M "+px(0)+","+(padT+h)+" ";
  ms.forEach((x,i)=>{ area += "L "+px(i).toFixed(1)+","+py(x.t).toFixed(1)+" "; });
  area += "L "+px(n-1)+","+(padT+h)+" Z";
  s.push('<path d="'+area+'" fill="rgba(15,59,110,0.10)"/>');
  s.push('<polyline points="'+ms.map((x,i)=>px(i).toFixed(1)+","+py(x.t).toFixed(1)).join(" ")
    +'" fill="none" stroke="#0f3b6e" stroke-width="2.5" stroke-linejoin="round"/>');

  // Puntos + etiquetas del eje X
  const cada = n > 20 ? 2 : (n > 12 ? 2 : 1);
  ms.forEach((x,i)=>{
    const cx = px(i), cy = py(x.t);
    s.push('<circle cx="'+cx.toFixed(1)+'" cy="'+cy.toFixed(1)+'" r="'
      +(enCurso(x.m)?"4":"3.2")+'" fill="'+(enCurso(x.m)?"#fff":"#0f3b6e")
      +'" stroke="#0f3b6e" stroke-width="2"><title>'+nomMes(x.m)+": "+money(x.t)
      +" ("+x.n+" ordenes)</title></circle>");
    if (i % cada === 0 || i === n-1){
      s.push('<text x="'+cx.toFixed(1)+'" y="'+(padT+h+18)+'" text-anchor="middle" class="eje">'
        +x.m.slice(5)+"/"+x.m.slice(2,4)+'</text>');
    }
  });
  // Marca del mes en curso
  const ult = ms[n-1];
  if (enCurso(ult.m)){
    s.push('<text x="'+(px(n-1)-6)+'" y="'+(py(ult.t)-12)+'" text-anchor="end" class="marca">en curso</text>');
  }
  // Rotulo del maximo
  const mejor = ms.reduce((a,b)=>b.t>a.t?b:a);
  s.push('<text x="'+px(ms.indexOf(mejor)).toFixed(1)+'" y="'+(py(mejor.t)-12)
    +'" text-anchor="middle" class="marca">'+money0(mejor.t)+'</text>');

  caja.innerHTML = '<svg viewBox="0 0 '+W+' '+H+'" preserveAspectRatio="xMidYMid meet" '
    + 'role="img" aria-label="Tendencia mensual">'+s.join("")+'</svg>';
  el("legend-mes").innerHTML = '<span><i style="background:#0f3b6e"></i>Mes seleccionado</span>'
    + '<span><i style="background:#b9c6d6"></i>Mismo mes del a&ntilde;o anterior</span>'
    + '<span><i style="background:#fff;border:2px solid #0f3b6e"></i>Mes en curso</span>';
}

/* ---------- KPIs ---------- */
function kpis(rows){
  const ms = porMes(rows);
  const tot = ms.reduce((a,x)=>a+x.t,0);
  const ti = ms.reduce((a,x)=>a+x.ti,0);
  const n = ms.reduce((a,x)=>a+x.n,0);
  const prom = ms.length ? tot/ms.length : 0;
  const best = ms.slice().sort((a,b)=>b.t-a.t)[0];
  const last = ms[ms.length-1];
  const ant = ms[ms.length-2];
  const y0 = last ? last.m.split("-")[0] : "";
  const base = ms.find(x => x.m === last.m.replace(y0, String(+y0-1)));
  const dif = ant && ant.t ? (last.t-ant.t)/ant.t*100 : null;
  const difAa = base && base.t ? (last.t-base.t)/base.t*100 : null;
  const ticket = n ? tot/n : 0;
  const anios = porAnio(rows);
  const mejor = anios.slice().sort((a,b)=>b.t-a.t)[0];
  const desc = (DATA.descuadre||[]).length;
  const html = [
    ["Total facturado (consolidado)", money0(tot), ""],
    ["A&ntilde;os en el rango", anios.map(a=>a.y).join(" &middot; ") || "&mdash;",
      anios.length + " a&ntilde;o(s), " + anios.reduce((a,x)=>a+x.meses,0) + " meses"],
    ["&Oacute;rdenes", n.toLocaleString("es-VE"), ""],
    ["Ticket promedio", money0(ticket), ""],
    ["Promedio mensual", money0(prom), ""],
    ["Mejor a&ntilde;o", mejor ? mejor.y+" ("+money0(mejor.t)+")" : "&mdash;", ""],
    ["Mejor mes", best ? nomMes(best.m)+" ("+money0(best.t)+")" : "&mdash;", ""],
    ["&Uacute;ltimo mes", last ? nomMesLarga(last.m)+" ("+money0(last.t)+")" : "&mdash;",
      ant ? '<span class="'+(dif>=0?"up":"down")+'">'+pct(dif)+" vs mes anterior</span>" : ""],
    ["Vs mismo mes del a&ntilde;o anterior", difAa===null ? "&mdash;" : pct(difAa),
      difAa===null ? "" : '<span class="'+(difAa>=0?"up":"down")+'">'+
        (base ? money0(base.t)+" en "+nomMes(base.m) : "")+"</span>"],
    ["Cuadre contable", Math.abs(tot-ti)<0.01 ? "Cuadra" : money0s(ti-tot),
      "Facturas netas: "+money0(ti)+(desc?" | "+desc+" &oacute;rdenes con descuadre":"")]
  ];
  el("kpis").innerHTML = html.map(k =>
    '<div class="kpi"><span>'+k[0]+'</span><b>'+k[1]+'</b>'+(k[2]?'<span>'+k[2]+'</span>':"")+'</div>').join("");
}

/* ---------- graficos ---------- */
/* ---------- barras apiladas por ejecutivo ---------- */
function grafApilado(rows){
  const ms = porMes(rows);
  const ejecutivos = porClave(rows,"e").sort((a,b)=>b.t-a.t).map(x=>x.k);
  const colores = new Map(ejecutivos.map((e,i)=>[e, COLORES[i%COLORES.length]]));
  const grid = new Map();
  rows.forEach(r => {
    const g = grid.get(r.m) || new Map();
    g.set(r.e, (g.get(r.e)||0) + r.t);
    grid.set(r.m, g);
  });
  const max = Math.max(1, ...ms.map(x=>x.t));
  el("graf-apilado").innerHTML = ms.map(x=>{
    const g = grid.get(x.m) || new Map();
    let acc = 0;
    const segs = ejecutivos.filter(e=>g.has(e)).map(e=>{
      const v = g.get(e), p0 = acc; acc += v;
      return '<div class="seg" style="height:'+(v/max*170)+'px;background:'+colores.get(e)
        +'" title="'+esc(e)+": "+money(v)+'"></div>';
    }).join("");
    return '<div class="col" title="'+nomMes(x.m)+": "+money(x.t)+'">'+segs
      + '<span style="font-size:9px;color:#56657a;text-align:center">'+x.m.slice(5)+"/"+x.m.slice(2,4)+'</span></div>';
  }).join("");
  el("legend-ejec").innerHTML = ejecutivos.slice(0,12).map(e=>
    '<span><i style="background:'+colores.get(e)+'"></i>'+esc(e)+'</span>').join("")
    + (ejecutivos.length>12 ? '<span>+'+(ejecutivos.length-12)+' mas</span>' : "");
}

/* ---------- tablas ---------- */
function tabla(head, body, total){
  return '<div style="overflow-x:auto;max-height:520px"><table><thead><tr>'
    + head.map(h=>'<th'+(h.num?' class="num"':'')+'>'+h.t+'</th>').join("")
    + '</tr></thead><tbody>'+body+'</tbody>'
    + (total ? '<tfoot>'+total+'</tfoot>' : "")+'</table></div>';
}
function barra(v, max, color){
  const p = max ? Math.round(v/max*100) : 0;
  return '<div class="bar-fondo"><i style="width:'+p+'%;background:'+(color||"#0f3b6e")+'"></i></div>';
}
function tMes(rows){
  const ms = porMes(rows);
  const max = Math.max(1,...ms.map(x=>x.t));
  let acc = 0;
  const body = ms.map((x,i)=>{
    acc += x.t;
    const prev = ms[i-1];
    const d = prev && prev.t ? (x.t-prev.t)/prev.t*100 : null;
    return '<tr><td>'+nomMesLarga(x.m)+'</td>'
      +'<td class="num">'+x.n+'</td><td class="num">'+x.f+'</td>'
      +'<td class="num">'+money(x.t)+'</td>'
      +'<td class="num">'+money(x.n?x.t/x.n:0)+'</td>'
      +'<td class="num">'+money0(acc)+'</td>'
      +'<td class="num">'+(d===null?"&mdash;":'<span class="'+(d>=0?"up":"down")+'">'+pct(d)+'</span>')+'</td>'
      +'<td>'+barra(x.t,max)+'</td></tr>';
  }).join("");
  const tot = ms.reduce((a,x)=>a+x.t,0), n = ms.reduce((a,x)=>a+x.n,0);
  const f = '<tr class="total"><td>TOTAL</td><td class="num">'+n+'</td>'
    +'<td class="num">'+ms.reduce((a,x)=>a+x.f,0)+'</td><td class="num">'+money(tot)+'</td>'
    +'<td class="num">'+money(n?tot/n:0)+'</td><td class="num"></td><td class="num"></td><td></td></tr>';
  el("p-mes").querySelector(".scroll").innerHTML =
    tabla([{t:"Mes"},{t:"&Oacute;rdenes",num:1},{t:"Facturadas",num:1},{t:"Facturado",num:1},
           {t:"Ticket prom.",num:1},{t:"Acumulado",num:1},{t:"&Delta; mes ant.",num:1},{t:""}], body, tot);
  el("p-mes").querySelector("h2").innerHTML = "Resumen mensual"
    + (enCurso(MESES_TODOS[MESES_TODOS.length-1])
        ? ' <span class="nota">&mdash; el &uacute;ltimo mes est&aacute; en curso, incompleto</span>' : '');
}
function tEjec(rows){
  const es = porClave(rows,"e").sort((a,b)=>b.t-a.t);
  const ms = porMes(rows);
  const tot = es.reduce((a,x)=>a+x.t,0);
  const max = Math.max(1,...es.map(x=>x.t));
  const g = new Map();
  rows.forEach(r => { const k = r.e+"|"+r.m; g.set(k, (g.get(k)||0)+r.t); });
  const body = es.map(x=>{
    const del = ms.map(m => g.get(x.k+"|"+m.m) || 0);
    const conDato = del.map((v,i)=>({v, m:ms[i].m})).filter(z=>z.v>0);
    const ultimo = conDato[conDato.length-1];
    const mejor = conDato.slice().sort((a,b)=>b.v-a.v)[0];
    return '<tr><td>'+esc(x.k)+(esEquipo(x.k)?'<span class="badge">equipo</span>':"")+'</td>'
      +'<td class="num">'+x.n+'</td><td class="num">'+money(x.t)+'</td>'
      +'<td class="num">'+money(x.n?x.t/x.n:0)+'</td>'
      +'<td class="num">'+(tot?money0(x.t/tot*100):"0")+'</td>'
      +'<td class="num">'+(ultimo?money0(ultimo.v):"&mdash;")+'</td>'
      +'<td>'+(mejor?nomMes(mejor.m)+" ("+money0(mejor.v)+")":"&mdash;")+'</td>'
      +'<td>'+barra(x.t,max, "#1e8e5a")+'</td></tr>';
  }).join("");
  el("p-ejec").querySelector(".scroll").innerHTML =
    tabla([{t:"Ejecutivo"},{t:"&Oacute;rdenes",num:1},{t:"Facturado",num:1},{t:"Ticket prom.",num:1},
           {t:"% del total",num:1},{t:"&Uacute;ltimo mes",num:1},{t:"Mejor mes"},{t:""}], body);
}
function tMatriz(rows){
  const ms = porMes(rows);
  const es = porClave(rows,"e").sort((a,b)=>b.t-a.t).map(x=>x.k);
  const g = new Map();
  rows.forEach(r=>{
    const k = r.m+"|"+r.e;
    g.set(k, (g.get(k)||0)+r.t);
  });
  const max = Math.max(1,...[...g.values()]);
  const head = [{t:"Mes"}].concat(es.map(e=>({t:esc(e)+(esEquipo(e)?' <span class="badge">eq</span>':""),num:1})))
                    .concat([{t:"Total",num:1}]);
  const body = ms.map(m=>{
    const tds = es.map(e=>{
      const v = g.get(m.m+"|"+e) || 0;
      if (!v) return '<td class="num" style="color:#c3ccd6">&mdash;</td>';
      const sh = Math.max(4, Math.round(v/max*100));
      return '<td class="num" style="background:rgba(15,59,110,'+(sh/100*0.85)+')" title="'+money(v)+'">'+money0(v)+'</td>';
    }).join("");
    const tot = es.reduce((a,e)=>a+(g.get(m.m+"|"+e)||0),0);
    return '<tr><td>'+nomMes(m.m)+'</td>'+tds+'<td class="num"><b>'+money0(tot)+'</b></td></tr>';
  }).join("");
  el("p-matriz").querySelector(".scroll").innerHTML = tabla(head, body);
}
function tDim(rows, clave, id, titulo){
  const d = porClave(rows,clave).sort((a,b)=>b.t-a.t);
  const tot = d.reduce((a,x)=>a+x.t,0);
  const max = Math.max(1,...d.map(x=>x.t));
  const body = d.map(x=>'<tr><td>'+esc(x.k)+'</td><td class="num">'+x.n+'</td>'
    +'<td class="num">'+x.f+'</td><td class="num">'+money(x.t)+'</td>'
    +'<td class="num">'+(tot?money0(x.t/tot*100):"0")+'</td>'
    +'<td>'+barra(x.t,max,"#e07b2a")+'</td></tr>').join("");
  const f = '<tr class="total"><td>TOTAL</td><td class="num">'+d.reduce((a,x)=>a+x.n,0)+'</td>'
    +'<td class="num">'+d.reduce((a,x)=>a+x.f,0)+'</td><td class="num">'+money(tot)+'</td>'
    +'<td class="num">100%</td><td></td></tr>';
  el(id).querySelector(".scroll").innerHTML =
    tabla([{t:titulo},{t:"&Oacute;rdenes",num:1},{t:"Facturadas",num:1},{t:"Facturado",num:1},
           {t:"% del total",num:1},{t:""}], body, f);
}
function tInter(rows){
  const ms = porMes(rows);
  const anios = [...new Set(ms.map(m=>m.m.slice(0,4)))].sort();
  const head = [{t:"Mes"}]
    .concat(anios.map(a=>({t:a,num:1})))
    .concat([{t:"&Delta; "+(anios.length>1?anios[anios.length-2]+" vs "+anios[anios.length-1]:""),num:1}]);
  const body = ms.map(m=>{
    const mes = m.m.slice(5);
    const v = anios.map(a=>{
      const f = ms.find(x=>x.m===a+"-"+mes);
      if (!f) return '<td class="num" style="color:#c3ccd6">&mdash;</td>';
      return '<td class="num"'+(enCurso(f.m)?' style="font-weight:700"':'')+'>'+money0(f.t)+'</td>';
    }).join("");
    const dos = ms.find(x=>x.m===anios[anios.length-2]+"-"+mes);
    const ult = ms.find(x=>x.m===anios[anios.length-1]+"-"+mes);
    const d = (dos && ult && dos.t) ? (ult.t-dos.t)/dos.t*100 : null;
    return '<tr><td>'+MES[Number(mes)]+'</td>'+v
      +'<td class="num">'+(d===null?"&mdash;":'<span class="'+(d>=0?"up":"down")+'">'+pct(d)+'</span>')+'</td></tr>';
  }).join("");
  el("p-inter").querySelector(".scroll").innerHTML = tabla(head, body);
  el("p-inter").querySelector("h2").innerHTML =
    "Comparaci&oacute;n interanual (mismo mes: "+anios[0]+" vs "+anios[anios.length-1]
    +"). En negrita el mes en curso.";
}

/* ---------- render ---------- */
function render(){
  const rows = filtrar();
  kpis(rows); anualGraf(rows); anualTabla(rows); bloqueDescuadre(); tendenciaSVG(rows); grafApilado(rows);
  tMes(rows); tEjec(rows); tMatriz(rows);
  tDim(rows,"o","p-origen","Origen (source_id)");
  tDim(rows,"p","p-plan","Plan (planes)");
  tInter(rows);
  el("sub").innerHTML = "Ventana: "+esc(DATA.ventana)+" &nbsp;|&nbsp; Origen: "+esc(DATA.origen)
    + "<br>Consultado el "+esc(DATA.generado)+" desde "+esc(DATA.erp)+" en modo solo lectura.";
}
function csv(){
  const rows = filtrar();
  const cab = ["mes","mes_nombre","ejecutivo","origen","plan","ordenes","facturadas",
             "monto_ordenes","monto_facturas","facturas","facturas_canceladas"];
  const out = [cab.join(";")].concat(rows.map(r=>[
    r.m, nomMes(r.m), r.e, r.o, r.p, r.n, r.f, r.t.toFixed(2),
    (r.ti||0).toFixed(2), r.ci||0, (r.cc||0).toFixed(2)].join(";")));
  const blob = new Blob(["\ufeff"+out.join("\r\n")], {type:"text/csv;charset=utf-8"});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "facturacion_mensual_"+rows.length+"filas.csv";
  a.click();
}
function pestanas(){
  document.querySelectorAll(".pestanas button").forEach(b=>{
    b.onclick = ()=>{
      document.querySelectorAll(".pestanas button").forEach(x=>x.classList.remove("activa"));
      b.classList.add("activa");
      ["mes","ejec","matriz","origen","plan","inter"].forEach(t=>
        el("p-"+t).classList.toggle("oculto", t!==b.dataset.t));
    };
  });
}
pintarFiltros(); pestanas();
["f-desde","f-hasta","f-ejec","f-origen","f-plan","f-fact"].forEach(i=>{
  el(i).addEventListener("change", render); });
el("btn-csv").onclick = csv;
el("btn-reset").onclick = ()=>{ pintarFiltros(); render(); };
render();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()