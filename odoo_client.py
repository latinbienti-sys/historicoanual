# -*- coding: utf-8 -*-
"""Cliente JSON-RPC de Odoo 16 (SOLO LECTURA).

Garantias de seguridad:
- Sesion con cookies (evita el bug de website_sale_wishlist que rompe XML-RPC).
- Bloqueo de metodos de escritura: create/write/unlink/copy/etc. se rechazan
  localmente ANTES de enviar cualquier llamada.
- Exclusivamente consultas: authenticate, search, search_read, read, search_count.
"""
import json
import time
import unicodedata
import urllib.request
import urllib.error
import http.cookiejar
from collections import defaultdict
from datetime import datetime


def _norm(texto):
    """Normaliza nombre: minusculas y sin acentos (Asesoria = Asesoría)."""
    if not texto:
        return ""
    s = unicodedata.normalize("NFKD", str(texto))
    return "".join(ch for ch in s if not unicodedata.combining(ch)).lower().strip()


class OdooClient:
    # Metodos permitidos: SOLO LECTURA.
    _READ_ONLY_METHODS = {
        "search", "search_read", "read", "search_count",
        "name_get", "check_access_rights", "fields_get", "get_metadata",
        "exists", "web_read", "web_search_read", "read_group",
    }

    def __init__(self, url, db, user, api_key, retries=4, backoff=4):
        self.url = url.rstrip("/")
        self.db = db
        self.user = user
        self.api_key = api_key
        self.retries = retries
        self.backoff = backoff
        self._cj = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._cj)
        )

    # ------------------------------------------------------------------ #
    def _call(self, method, service, params):
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "call",
            "params": {"service": service, "method": method, "args": params},
        }
        req = urllib.request.Request(
            self.url + "/jsonrpc",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        last_err = None
        for attempt in range(self.retries):
            try:
                with self._opener.open(req, timeout=120) as resp:
                    data = json.loads(resp.read().decode())
                if "error" in data:
                    err = data["error"]
                    detail = err
                    if isinstance(err, dict):
                        detail = err.get("message", err)
                        if err.get("data") and isinstance(err["data"], dict):
                            detail = err["data"].get("message", detail)
                    raise RuntimeError(detail)
                return data.get("result")
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last_err = e
                if attempt < self.retries - 1:
                    time.sleep(self.backoff * (attempt + 1))
        raise RuntimeError(f"No se pudo contactar Odoo tras {self.retries} intentos: {last_err}")

    def connect(self):
        last = None
        for _ in range(3):
            try:
                self.uid = self._call(
                    "authenticate", "common",
                    [self.db, self.user, self.api_key, {}],
                )
                if self.uid:
                    return self
            except RuntimeError as e:
                # Bug de website_sale_wishlist: la sesion rota se resuelve
                # reintentando la autenticacion (sigue siendo SOLO LECTURA).
                if "session" in str(e).lower() or "Request" in str(e):
                    last = e
                    time.sleep(3)
                    continue
                raise
        raise ConnectionError(
            "Autenticacion fallida en Odoo. Revisa url, db, usuario y api key. "
            + (f" ({last})" if last else "")
        )

    # ------------------------------------------------------------------ #
    def execute_kw(self, model, method, args=None, kwargs=None):
        if method not in self._READ_ONLY_METHODS:
            raise PermissionError(
                f"[SEGURIDAD] Metodo '{method}' sobre '{model}' NO permitido. "
                "El sistema es SOLO CONSULTA."
            )
        args = args or []
        kwargs = kwargs or {}
        try:
            return self._call(
                "execute_kw", "object",
                [self.db, self.uid, self.api_key, model, method, args, kwargs],
            )
        except RuntimeError as e:
            # Bug de website_sale_wishlist: la sesion puede expirar a media
            # ejecucion. Re-autenticamos (sigue siendo SOLO LECTURA) y reintento.
            if "session" in str(e).lower() or "Request" in str(e):
                self.connect()
                return self._call(
                    "execute_kw", "object",
                    [self.db, self.uid, self.api_key, model, method, args, kwargs],
                )
            raise

    def search(self, model, domain, limit=0):
        kwargs = {"limit": limit} if limit else {}
        return self.execute_kw(model, "search", [domain], kwargs)

    def search_read(self, model, domain, fields, limit=0, order=""):
        kwargs = {"fields": fields}
        if limit:
            kwargs["limit"] = limit
        if order:
            kwargs["order"] = order
        return self.execute_kw(model, "search_read", [domain], kwargs)

    def read(self, model, ids, fields=None):
        kwargs = {"fields": fields} if fields else {}
        return self.execute_kw(model, "read", [ids], kwargs)

    # ------------------------------------------------------------------ #
    def get_stages(self):
        rows = self.search_read("crm.stage", [], ["id", "name"], order="sequence")
        return {r["id"]: r["name"] for r in rows}

    def get_leads(self, fields):
        domain = [("active", "=", True)]
        return self.search_read("crm.lead", domain, fields)

    def get_lead_created_in_range(self, since, until, fields):
        since_str = since.strftime("%Y-%m-%d 00:00:00")
        domain = [("create_date", ">=", since_str)]
        if until:
            domain.append(("create_date", "<", until.strftime("%Y-%m-%d 00:00:00")))
        return self.search_read("crm.lead", domain, fields)

    def get_lead_touched_in_range(self, since, until, fields):
        since_str = since.strftime("%Y-%m-%d 00:00:00")
        domain = [("write_date", ">=", since_str)]
        if until:
            domain.append(("write_date", "<", until.strftime("%Y-%m-%d 00:00:00")))
        return self.search_read("crm.lead", domain, fields)

    def get_activities_in_range(self, since, until):
        """Actividades (seguimiento) sobre leads en el rango, solo lectura."""
        since_str = since.strftime("%Y-%m-%d 00:00:00")
        domain = [("res_model", "=", "crm.lead"), ("create_date", ">=", since_str)]
        if until:
            domain.append(("create_date", "<", until.strftime("%Y-%m-%d 00:00:00")))
        return self.search_read("mail.activity", domain, ["create_date", "user_id"])

    def get_activity_type_id(self, name):
        """Id del tipo de actividad por su nombre exacto (ej. 'Atención Puerta')."""
        rows = self.search_read("mail.activity.type", [("name", "=", name)], ["id"])
        return rows[0]["id"] if rows else None

    def get_store_contact_activities_in_range(self, since, until, activity_type_id):
        """Actividades 'Atencion Puerta' (Contacto Tienda) en el rango, solo lectura."""
        since_str = since.strftime("%Y-%m-%d 00:00:00")
        domain = [("activity_type_id", "=", activity_type_id),
                  ("create_date", ">=", since_str)]
        if until:
            domain.append(("create_date", "<", until.strftime("%Y-%m-%d 00:00:00")))
        return self.search_read("mail.activity", domain, ["create_date", "user_id"])

    # Subtipos de mensajes que registran movimiento de etapa en el CRM.
    _STAGE_CHANGED_SUBTYPES = [6, 11, 29, 44, 64]

    def _tid(self, ids):
        """mail.tracking_value_ids puede venir como int (1 valor) o lista."""
        if ids is None:
            return []
        if isinstance(ids, int):
            return [ids]
        return [x[0] if isinstance(x, (list, tuple)) else x for x in ids]

    def get_stage_moves_in_range(self, since, until):
        """Movimientos de etapa en el rango.

        Devuelve (mensajes, {tracking_id: etapa_destino}, {partner_id: nombre}).
        El autor del mensaje (mail.message.author_id) es un res.partner, NO el
        res.users del ejecutivo; por eso tambien se resuelven los nombres.
        Solo lecturas: mail.message (stage changed) + mail.tracking.value.
        """
        since_str = since.strftime("%Y-%m-%d 00:00:00")
        domain = [("model", "=", "crm.lead"),
                  ("subtype_id", "in", self._STAGE_CHANGED_SUBTYPES),
                  ("create_date", ">=", since_str)]
        if until:
            domain.append(("create_date", "<", until.strftime("%Y-%m-%d 00:00:00")))
        msgs = self.search_read("mail.message", domain,
                                ["tracking_value_ids", "author_id", "create_date", "res_id"])
        tv_ids = {tv for m in msgs for tv in self._tid(m.get("tracking_value_ids"))}
        tracking = {}
        if tv_ids:
            ids = list(tv_ids)
            for i in range(0, len(ids), 300):
                batch = ids[i:i + 300]
                rows = self.search_read("mail.tracking.value", [("id", "in", batch)],
                                        ["new_value_char", "mail_message_id"])
                for r in rows:
                    if r.get("mail_message_id"):
                        tracking[r["mail_message_id"][0]] = r.get("new_value_char") or ""
        # Nombres de los autores (res.partner) para atribuir por nombre.
        authors = {}
        partner_ids = sorted({
            m.get("author_id")[0] for m in msgs if m.get("author_id")
        })
        if partner_ids:
            for i in range(0, len(partner_ids), 300):
                batch = partner_ids[i:i + 300]
                rows = self.read("res.partner", batch, ["name"])
                for r in rows:
                    authors[r["id"]] = r.get("name") or ""
        return msgs, tracking, authors

    def get_executives(self):
        rows = self.search_read(
            "res.users", [("active", "=", True), ("share", "=", False)],
            ["id", "name", "team_id"],
        )
        return [{"id": r["id"], "name": r["name"]} for r in rows]

    # ------------------------------------------------------------------ #
    @staticmethod
    def funnel_from_leads(leads, stage_map, funnel_stages, stage_mapping, tz):
        """Contar leads por etapa del embudo usando el mapeo configurado.

        stage_mapping: {nombre_embudo: [nombres_crm, ...]} e.g.
            {"Oportunidad": ["LC Aprobada", "Contrato", ...]}. [] = no CRM.
        Devuelve {nombre_embudo: {user_id: n}}.
        Tambien retorna stages CRM no mapeados (para avisar).
        """
        if not stage_mapping:
            stage_mapping = {s: [s] for s in funnel_stages}
        crm_a_funnel = {}
        for funnel_name, crm_names in stage_mapping.items():
            if not crm_names:
                continue
            for crm_name in crm_names:
                crm_a_funnel.setdefault(_norm(crm_name), set()).add(funnel_name)

        unmapped = set()
        funnel = {name: defaultdict(int) for name in funnel_stages}
        for lead in leads:
            stage_id = lead.get("stage_id")[0] if lead.get("stage_id") else None
            name = stage_map.get(stage_id) if stage_id else None
            if not name:
                continue
            funnel_names = crm_a_funnel.get(_norm(name))
            if not funnel_names:
                unmapped.add(name)
                continue
            user_id = lead.get("user_id")[0] if lead.get("user_id") else None
            for fn in funnel_names:
                if fn not in funnel:
                    funnel[fn] = defaultdict(int)
                funnel[fn][user_id or 0] += 1
        return funnel, unmapped

    @staticmethod
    def created_by_day(leads, tz):
        out = defaultdict(lambda: defaultdict(int))
        for lead in leads:
            create_date = lead.get("create_date")
            if not create_date:
                continue
            dt = datetime.fromisoformat(create_date).astimezone(tz).date()
            user_id = lead.get("user_id")[0] if lead.get("user_id") else 0
            out[dt][user_id] += 1
        return out

    @staticmethod
    def touched_by_day(leads, tz):
        out = defaultdict(lambda: defaultdict(int))
        for lead in leads:
            write_date = lead.get("write_date")
            if not write_date:
                continue
            dt = datetime.fromisoformat(write_date).astimezone(tz).date()
            user_id = lead.get("user_id")[0] if lead.get("user_id") else 0
            out[dt][user_id] += 1
        return out

    @staticmethod
    def activities_by_day(activities, tz):
        out = defaultdict(lambda: defaultdict(int))
        for a in activities:
            create_date = a.get("create_date")
            if not create_date:
                continue
            dt = datetime.fromisoformat(create_date).astimezone(tz).date()
            user_id = a.get("user_id")[0] if a.get("user_id") else 0
            out[dt][user_id] += 1
        return out

    @staticmethod
    def puerta_by_day(activities, tz):
        """Contacto Tienda: actividades 'Atencion Puerta' por dia y ejecutivo."""
        out = defaultdict(lambda: defaultdict(int))
        for a in activities:
            create_date = a.get("create_date")
            if not create_date:
                continue
            dt = datetime.fromisoformat(create_date).astimezone(tz).date()
            user_id = a.get("user_id")[0] if a.get("user_id") else 0
            out[dt][user_id] += 1
        return out

    @staticmethod
    def moves_by_day(msgs, tracking, stage_mapping, funnel_stages, tz, authors=None):
        """Flujo de etapa por dia.

        stage_mapping: {embudo: [nombres_crm]}. tracking: {msg_id: etapa_destino_crm}.
        authors: {partner_id: nombre} del autor (res.partner). El resultado se
        agrupa por NOMBRE NORMALIZADO del autor, porque author_id no es el uid.
        Devuelve {dia: {nombre_autor: {nombre_embudo: n}}}.
        """
        if not stage_mapping:
            stage_mapping = {s: [s] for s in funnel_stages}
        destinos = {}
        for funnel_name, crm_names in stage_mapping.items():
            if not crm_names:
                continue
            for crm_name in crm_names:
                destinos[_norm(crm_name)] = funnel_name

        out = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
        for m in msgs:
            dt = datetime.fromisoformat(m["create_date"]).astimezone(tz).date()
            author_id = m.get("author_id")[0] if m.get("author_id") else 0
            autor = _norm((authors or {}).get(author_id, ""))
            if not autor:
                continue
            nuevo = tracking.get(m.get("id"))
            if not nuevo:
                continue
            funnel_name = destinos.get(_norm(nuevo))
            if not funnel_name:
                continue
            out[dt][autor][funnel_name] += 1
        return out