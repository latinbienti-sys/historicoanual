# Embudo de Ventas - Dashboard + PDF mensual (Odoo 16, SOLO LECTURA)

Dashboard dinamico que consulta el **embudo de ventas del CRM de Odoo 16**, lo
muestra **por dia y por ejecutivo** y genera un **PDF visual mensual**.
El sistema **NUNCA modifica datos en el ERP**: usa la API **JSON-RPC** con
metodos de lectura, una vez al dia, y guarda un cache local.

## Como funciona

```
Odoo (solo lectura, JSON-RPC con sesion y cookies)
     | 1 vez al dia (Tarea programada de Windows: agendar_sync.bat)
     v
SQLite local (crm_cache.db)  <---------- el dashboard SOLO lee de aqui
     |                  ^
     v                  |
_Dashboard Flask_  ___boton [+/-] "Contacto Tienda" por ejecutivo___
   http://127.0.0.1:8080        (se guarda en el cache local, no en Odoo)
     |
     v
__PDF mensual visual (matplotlib)__
```

- La sincronizacion diaria baja (todo de sola lectura): ejecutivos, etapas del
  CRM, leads por etapa, leads creados/atendidos por dia, **actividades** de
  seguimiento, y **movimientos de etapa** (flujo).
- **Embudo = FLUJO DEL MES**: cuenta los *movimientos* de cada etapa por
  ejecutivo en el mes (quien entro a Cada etapa, oportunidades ganadas,
  facturados), no el stock acumulado.
- **Contacto Tienda**: se mide con la actividad **"Atención Puerta"** que el
  equipo registra en Odoo sobre el prospecto TRAFICO (por dia y por ejecutivo).
  El boton +/- del tablero local queda como ajuste manual opcional por si faltó
  registrar la actividad. No ensucia el embudo real de Odoo.
- **Ejecutivos activos**: solo se muestran los ejecutivos de ventas configurados
  (whitelist `executives_active`); se excluyen usuarios de sistema/pools
  (`executives_exclude`).
- **Gestión diaria**: cada ejecutivo ve su meta por etapa (config `daily_meta`),
  lo logrado HOY (flujo de hoy) y lo pendiente por cubrir; más la **meta de
  venta mensual** (`sales_meta`, eq. RODEO 120,000) vs el monto de leads en
  Cierre y lo pendiente.
- **PDF mensual por ejecutivo**: `pdf_mensual.bat` genera GLOBAL + un PDF por
  cada ejecutivo activo; quedan visibles y descargables en la web
  (GitHub Pages) y en el tablero local.

## Seguridad

- `config.json` (con credenciales reales) esta **excluido de Git**. Aun no
  lo agregues ni lo edites en el repositorio. Solo se publica
  `config.example.json` con valores de ejemplo.
- El cliente rechaza cualquier metodo de escritura (`create`, `write`, `unlink`,
  `copy`, etc.) localmente, antes de enviarlo al ERP.
- Si la sesion del servidor expira a mitad de trabajo, re-autentica
  automaticamente (sigue siendo solo lectura).

## Puesta en marcha

1. Instalar Python 3.11+.
2. `instalar.bat` (instala flask y matplotlib).
3. Copiar `config.example.json` a `config.json` y completar:
   - `odoo.url`, `odoo.db`, `odoo.user`, `odoo.api_key` (API key Odoo 16).
   - `stage_mapping`: nombre de tu embudo -> etapas del CRM (normalizado sin
     tildes; "Contacto Tienda" y "Seguimiento whatsapp" no usan etapas).
   - `executives_active`: nombres de los ejecutivos que se muestran.
4. `sincronizar.bat` (primer llenado: ultimos 45 dias; luego a diario).
5. `dashboard.bat` y abrir http://127.0.0.1:8080
6. `agendar_sync.bat` una sola vez para que la sincronizacion corra CADA dia a
   las 06:30 sin afectar la operacion.
7. `pdf_mensual.bat` escribe el PDF del mes; tambien se descarga desde el tablero.

## Historico de Facturacion Mensual (dashboard HTML)

`facturacion_mensual.py` arma un historico de **2 anos** de la facturacion
mensual, leyendo el ERP en modo **solo lectura** (mismo favorites de
latinbien.com &gt; Ventas: *FACTURACION MENSUAL*).

- Modelo: `sale.order` con `x_status_compra = 4` ("4. ENTREGA REALIZADA"),
  fecha `commitment_date`; agrupa por mes, ejecutivo (`user_id`), origen
  (`source_id`) y plan (`planes`).
- Salidas: `facturacion_mensual.html` (dashboard autocontenido con barras por año,
  grafica de tendencia mensual con ejes, KPIs, tablas y descarga CSV) y
  `facturacion_mensual_detalle.json` (las ordenes una a una, **local y fuera de
  Git** porque trae clientes).
- Uso:
  - `python facturacion_mensual.py --desde 2025-01` consulta el ERP y regenera el
    dashboard desde ese mes (asi se saca 2024 del historico).
  - `python facturacion_mensual.py --meses 36` otra ventana.
  - `python facturacion_mensual.py --desde-detalle` regenera el HTML desde el
    JSON local **sin volver a consultar el ERP**.
- **Cuadre contable**: ademas del monto de las ordenes, el dashboard trae el neto
  facturado (facturas de cliente **menos** notas credito, leidas de `account.move`
  en solo lectura) y la lista de ordenes cuyo monto no cuadra, para que el total
  se pueda auditar.
- El HTML solo lleva agregados (sin nombres de clientes ni de ordenes), asi que
  es seguro publicarlo en `docs/`. El mes en curso aparece marcado como
  incompleto para no comparar un mes parcial contra uno cerrado.