"""Загрузка бэкапов в облако: Яндекс Диск (WebDAV), Облако Mail.ru (API клиента) и Google Диск (OAuth)."""
import os, posixpath, re
import xml.etree.ElementTree as ET
from urllib.parse import quote, unquote
import requests

TIMEOUT = (20, 120)
PROVIDERS = {"yandex": "Яндекс Диск", "mailru": "Облако Mail.ru", "google": "Google Диск"}


class CloudError(Exception):
    pass


class _ProgressFile:
    """Файл для отправки с отчётом о прогрессе и возможностью остановки."""
    def __init__(self, path, cb=None, cancel=None):
        self.f = open(path, "rb"); self.size = os.path.getsize(path); self.done = 0
        self.cb, self.cancel = cb, cancel

    def __len__(self): return self.size

    def read(self, n=-1):
        if self.cancel and self.cancel.is_set(): raise CloudError("Остановлено пользователем")
        b = self.f.read(1024 * 1024 if n is None or n < 0 else n)
        self.done += len(b)
        if self.cb: self.cb(self.done, self.size)
        return b

    def close(self): self.f.close()


def _norm(folder):
    f = "/" + (folder or "").strip().strip("/")
    return "/" if f == "/" else f


class Yandex:
    URL = "https://webdav.yandex.ru"

    def __init__(self, login, pwd):
        self.s = requests.Session(); self.s.auth = (login, pwd)
        self.s.headers["User-Agent"] = "VPSBackup"

    def _u(self, p): return self.URL + quote(p)

    def _chk(self, r, ok=(200, 201, 204, 207)):
        if r.status_code == 401: raise CloudError("Неверный логин или пароль приложения Яндекса")
        if r.status_code == 507: raise CloudError("В облаке закончилось место")
        if r.status_code not in ok: raise CloudError(f"Яндекс Диск ответил ошибкой {r.status_code}")
        return r

    def quota(self):
        body = ('<?xml version="1.0"?><propfind xmlns="DAV:"><prop><quota-available-bytes/>'
                '<quota-used-bytes/></prop></propfind>')
        r = self._chk(self.s.request("PROPFIND", self.URL + "/", data=body, headers={"Depth": "0"}, timeout=TIMEOUT))
        t = r.text
        free = re.search(r"quota-available-bytes>(\d+)<", t); used = re.search(r"quota-used-bytes>(\d+)<", t)
        free = int(free.group(1)) if free else 0; used = int(used.group(1)) if used else 0
        return free, free + used

    def mkdirs(self, folder):
        cur = ""
        for part in [p for p in folder.split("/") if p]:
            cur += "/" + part
            r = self.s.request("MKCOL", self._u(cur), timeout=TIMEOUT)
            self._chk(r, ok=(201, 405))

    def list(self, folder):
        r = self.s.request("PROPFIND", self._u(folder + "/"), headers={"Depth": "1"}, timeout=TIMEOUT)
        if r.status_code == 404: return []
        self._chk(r)
        out = []
        for resp in ET.fromstring(r.content).findall("{DAV:}response"):
            href = unquote(resp.findtext("{DAV:}href") or "").rstrip("/")
            name = posixpath.basename(href)
            if name and href.rstrip("/") != folder.rstrip("/"): out.append(name)
        return out

    def upload(self, local, folder, name, cb=None, cancel=None):
        f = _ProgressFile(local, cb, cancel)
        try:
            r = self.s.put(self._u(f"{folder}/{name}"), data=f, timeout=(20, 600),
                           headers={"Content-Type": "application/octet-stream"})
        finally: f.close()
        self._chk(r, ok=(200, 201, 204))

    def delete(self, folder, name):
        self._chk(self.s.delete(self._u(f"{folder}/{name}"), timeout=TIMEOUT), ok=(200, 202, 204, 404))


class MailRu:
    """Работает на бесплатном тарифе: тот же способ входа, что у приложения Облака для компьютера."""
    API = "https://cloud.mail.ru/api/m1"
    CLIENT = "cloud-win"

    def __init__(self, login, pwd):
        self.login = login if "@" in login else login + "@mail.ru"
        self.s = requests.Session(); self.s.headers["User-Agent"] = "CloudDiskOWindows 17.12.0009 beta WzBbt1Ygbm"
        r = self.s.post("https://o2.mail.ru/token", timeout=TIMEOUT, data=dict(
            client_id=self.CLIENT, grant_type="password", username=self.login, password=pwd))
        try: j = r.json()
        except Exception: j = {}
        if r.status_code != 200 or "access_token" not in j:
            raise CloudError("Mail.ru не пустил: проверьте почту и пароль для внешних приложений "
                             f"({j.get('error_description') or j.get('error') or r.status_code})")
        self.tok = j["access_token"]

    def _api(self, method, path, **kw):
        p = kw.pop("params", {}); p["access_token"] = self.tok
        r = self.s.request(method, self.API + path, params=p, timeout=TIMEOUT, **kw)
        if r.status_code == 404: return None
        if r.status_code == 507: raise CloudError("В облаке закончилось место")
        if r.status_code >= 400: raise CloudError(f"Облако Mail.ru ответило ошибкой {r.status_code}: {r.text[:200]}")
        try: return r.json().get("body")
        except Exception: return None

    def quota(self):
        b = self._api("GET", "/user") or {}
        sp = (b.get("cloud") or {}).get("space") or {}
        total = int(sp.get("bytes_total") or 0); used = int(sp.get("bytes_used") or 0)
        return max(0, total - used), total

    def mkdirs(self, folder):
        cur = ""
        for part in [p for p in folder.split("/") if p]:
            cur += "/" + part
            if self._api("GET", "/file", params={"home": cur, "limit": 1}) is None:
                self._api("POST", "/folder/add", data={"home": cur, "conflict": "strict"})

    def list(self, folder):
        b = self._api("GET", "/file", params={"home": folder, "limit": 65535}) or {}
        return [i.get("name") for i in b.get("list", []) if i.get("kind") == "file"]

    def upload(self, local, folder, name, cb=None, cancel=None):
        d = self.s.get("https://dispatcher.cloud.mail.ru/u", params={"access_token": self.tok}, timeout=TIMEOUT)
        if d.status_code != 200 or not d.text.strip():
            raise CloudError("Не удалось получить сервер загрузки Mail.ru")
        shard = d.text.split()[0]
        f = _ProgressFile(local, cb, cancel)
        try:
            r = self.s.put(shard, params={"client_id": self.CLIENT, "token": self.tok}, data=f, timeout=(20, 600))
        finally: f.close()
        h = r.text.strip()
        if r.status_code not in (200, 201) or not re.fullmatch(r"[0-9A-Fa-f]{40}", h):
            raise CloudError(f"Ошибка загрузки в Mail.ru ({r.status_code})")
        self._api("POST", "/file/add", data={"home": f"{folder}/{name}", "hash": h.upper(),
                                             "size": os.path.getsize(local), "conflict": "rewrite"})

    def delete(self, folder, name):
        self._api("POST", "/file/remove", data={"home": f"{folder}/{name}"})




# ---------------- Google Диск ----------------
# Коды приложения (Client ID и Client Secret) пользователь вставляет сам в настройках
# программы (вкладка «Облако»). Взять их можно в Google Cloud Console: ID клиента OAuth,
# тип «Приложение для компьютера». Для настольных программ Google не считает Client Secret
# секретным: доступ даёт только согласие пользователя.
GOOGLE_CLIENT_ID = ""
GOOGLE_CLIENT_SECRET = ""
G_SCOPE = "https://www.googleapis.com/auth/drive.file openid email"
G_API = "https://www.googleapis.com/drive/v3"
G_UP = "https://www.googleapis.com/upload/drive/v3/files"
G_FOLDER = "application/vnd.google-apps.folder"
CHUNK = 8 * 1024 * 1024


def set_google_credentials(client_id, client_secret):
    """Задаёт коды приложения Google (вводятся в самой программе, без правки кода)."""
    global GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET
    GOOGLE_CLIENT_ID = (client_id or "").strip()
    GOOGLE_CLIENT_SECRET = (client_secret or "").strip()


def google_ready():
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)


def google_login(timeout=300):
    """Открывает окно входа Google в браузере. Возвращает (email, refresh_token)."""
    import base64, hashlib, secrets, socket, threading, webbrowser
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import urlencode, urlparse, parse_qs
    if not google_ready():
        raise CloudError("Не заданы коды Google — вставьте Client ID и Client Secret в настройках облака")
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16); got = {}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            q = parse_qs(urlparse(self.path).query)
            if q.get("state", [""])[0] == state:
                got["code"] = q.get("code", [""])[0]; got["error"] = q.get("error", [""])[0]
            ok = bool(got.get("code"))
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers()
            msg = "Готово! Вернитесь в программу VPS Backup — это окно можно закрыть." if ok else \
                  "Вход не выполнен. Вернитесь в программу и попробуйте ещё раз."
            self.wfile.write(f"<html><body style='font-family:Segoe UI;padding:40px;font-size:18px'>{msg}</body></html>".encode())

        def log_message(self, *a): pass

    srv = HTTPServer(("127.0.0.1", 0), H); port = srv.server_address[1]
    redirect = f"http://127.0.0.1:{port}"
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(dict(
        client_id=GOOGLE_CLIENT_ID, redirect_uri=redirect, response_type="code", scope=G_SCOPE,
        access_type="offline", prompt="consent", state=state,
        code_challenge=challenge, code_challenge_method="S256"))
    srv.timeout = 1
    webbrowser.open(url)
    import time as _t
    end = _t.time() + timeout
    try:
        while not got and _t.time() < end: srv.handle_request()
    finally: srv.server_close()
    if not got.get("code"):
        raise CloudError("Вход в Google не завершён" + (f" ({got['error']})" if got.get("error") else " (истекло время)"))
    r = requests.post("https://oauth2.googleapis.com/token", timeout=TIMEOUT, data=dict(
        code=got["code"], client_id=GOOGLE_CLIENT_ID, client_secret=GOOGLE_CLIENT_SECRET,
        redirect_uri=redirect, grant_type="authorization_code", code_verifier=verifier))
    j = r.json() if r.content else {}
    if r.status_code != 200 or "refresh_token" not in j:
        raise CloudError(f"Google не выдал доступ ({j.get('error_description') or j.get('error') or r.status_code})")
    email = ""
    try:
        u = requests.get("https://openidconnect.googleapis.com/v1/userinfo", timeout=TIMEOUT,
                         headers={"Authorization": "Bearer " + j["access_token"]}).json()
        email = u.get("email", "")
    except Exception: pass
    return email or "Google", j["refresh_token"]


class GDrive:
    def __init__(self, login, refresh_token):
        if not google_ready(): raise CloudError("Не заданы коды Google — вставьте Client ID и Client Secret в настройках облака")
        self.rt = refresh_token; self.s = requests.Session(); self._token()

    def _token(self):
        r = requests.post("https://oauth2.googleapis.com/token", timeout=TIMEOUT, data=dict(
            client_id=GOOGLE_CLIENT_ID, client_secret=GOOGLE_CLIENT_SECRET,
            refresh_token=self.rt, grant_type="refresh_token"))
        j = r.json() if r.content else {}
        if r.status_code != 200 or "access_token" not in j:
            raise CloudError("Вход в Google устарел — нажмите «Войти через Google» ещё раз")
        self.s.headers["Authorization"] = "Bearer " + j["access_token"]

    def _req(self, method, url, ok=(200,), **kw):
        kw.setdefault("timeout", TIMEOUT)
        r = self.s.request(method, url, **kw)
        if r.status_code == 401: self._token(); r = self.s.request(method, url, **kw)
        if r.status_code == 403 and "storageQuotaExceeded" in r.text: raise CloudError("На Google Диске закончилось место")
        if r.status_code not in ok: raise CloudError(f"Google Диск ответил ошибкой {r.status_code}: {r.text[:200]}")
        return r

    def quota(self):
        q = self._req("GET", G_API + "/about", params={"fields": "storageQuota"}).json().get("storageQuota", {})
        total = int(q.get("limit") or 0); used = int(q.get("usage") or 0)
        return (max(0, total - used) if total else 0), total

    def _find(self, name, parent, folder=True):
        nm = name.replace("\\", "\\\\").replace("'", "\\'")
        q = f"name='{nm}' and '{parent}' in parents and trashed=false"
        if folder: q += f" and mimeType='{G_FOLDER}'"
        f = self._req("GET", G_API + "/files", params={"q": q, "fields": "files(id)", "pageSize": 10}).json().get("files", [])
        return f[0]["id"] if f else None

    def _folder_id(self, folder, create):
        cur = "root"
        for part in [p for p in folder.split("/") if p]:
            fid = self._find(part, cur)
            if not fid:
                if not create: return None
                fid = self._req("POST", G_API + "/files", json={"name": part, "mimeType": G_FOLDER, "parents": [cur]},
                                params={"fields": "id"}).json()["id"]
            cur = fid
        return cur

    def mkdirs(self, folder): self._folder_id(folder, True)

    def _files(self, folder):
        fid = self._folder_id(folder, False)
        if not fid: return {}
        out, tok = {}, None
        while True:
            p = {"q": f"'{fid}' in parents and trashed=false and mimeType!='{G_FOLDER}'",
                 "fields": "nextPageToken,files(id,name)", "pageSize": 1000}
            if tok: p["pageToken"] = tok
            j = self._req("GET", G_API + "/files", params=p).json()
            for f in j.get("files", []): out[f["name"]] = f["id"]
            tok = j.get("nextPageToken")
            if not tok: return out

    def list(self, folder): return list(self._files(folder))

    def upload(self, local, folder, name, cb=None, cancel=None):
        """Загрузка частями: при обрыве связи продолжает с того же места."""
        import time as _t
        size = os.path.getsize(local); parent = self._folder_id(folder, True)
        r = self._req("POST", G_UP, params={"uploadType": "resumable"}, json={"name": name, "parents": [parent]},
                      headers={"X-Upload-Content-Type": "application/gzip", "X-Upload-Content-Length": str(size)})
        loc = r.headers["Location"]; pos = 0; fails = 0
        with open(local, "rb") as f:
            while pos < size:
                if cancel and cancel.is_set(): raise CloudError("Остановлено пользователем")
                f.seek(pos); data = f.read(CHUNK); end = pos + len(data) - 1
                try:
                    r = self.s.put(loc, data=data, timeout=(20, 300),
                                   headers={"Content-Range": f"bytes {pos}-{end}/{size}"})
                    if r.status_code in (200, 201): pos = size
                    elif r.status_code == 308:
                        rg = r.headers.get("Range"); pos = int(rg.split("-")[1]) + 1 if rg else 0
                    elif r.status_code == 401: self._token(); continue
                    else: raise CloudError(f"Google Диск ответил ошибкой {r.status_code}")
                    fails = 0
                except (requests.ConnectionError, requests.Timeout):
                    fails += 1
                    if fails > 20: raise CloudError("Нет связи с Google Диском больше 10 минут")
                    _t.sleep(30)
                    try:  # узнаём, сколько уже дошло, и продолжаем с этого места
                        s = self.s.put(loc, headers={"Content-Range": f"bytes */{size}"}, timeout=TIMEOUT)
                        if s.status_code in (200, 201): pos = size
                        elif s.status_code == 308:
                            rg = s.headers.get("Range"); pos = int(rg.split("-")[1]) + 1 if rg else 0
                    except (requests.ConnectionError, requests.Timeout): pass
                if cb: cb(pos, size)

    def delete(self, folder, name):
        fid = self._files(folder).get(name)
        if fid: self._req("DELETE", f"{G_API}/files/{fid}", ok=(200, 204, 404))


def open_cloud(provider, login, pwd):
    if provider == "google":
        if not pwd: raise CloudError("Нажмите «Войти через Google»")
        return GDrive(login, pwd)
    if not login or not pwd: raise CloudError("Укажите логин и пароль приложения")
    return Yandex(login, pwd) if provider == "yandex" else MailRu(login, pwd)


def old_copies(names, prefix, keep):
    """Какие копии удалить, чтобы осталось keep последних (имена содержат дату — сортируются по времени)."""
    mine = sorted(n for n in names if n.startswith(prefix) and n.endswith(".tar.gz"))
    return mine[:-keep] if keep > 0 and len(mine) > keep else []


def send_backup(provider, login, pwd, folder, local, server_prefix, keep, log, cb=None, cancel=None):
    folder = _norm(folder)
    c = open_cloud(provider, login, pwd)
    size = os.path.getsize(local)
    free, _ = c.quota()
    if free and free < size:
        raise CloudError(f"В облаке мало места: свободно {free/1073741824:.1f} ГБ, нужно {size/1073741824:.1f} ГБ")
    c.mkdirs(folder)
    name = os.path.basename(local)
    log(f"[*] Загрузка в {PROVIDERS[provider]}: {folder}/{name}")
    import time as _t
    for att in range(1, 4):  # обрыв сети: ждём и пробуем снова (Google сам продолжает с места)
        try: c.upload(local, folder, name, cb, cancel); break
        except (requests.ConnectionError, requests.Timeout):
            if att == 3 or (cancel and cancel.is_set()): raise
            log(f"[!] Связь с облаком пропала. Повтор через 30 с (попытка {att + 1} из 3)...")
            for _ in range(30):
                if cancel and cancel.is_set(): raise CloudError("Остановлено пользователем")
                _t.sleep(1)
            c = open_cloud(provider, login, pwd)
    log("[+] Копия в облаке сохранена")
    for n in old_copies(c.list(folder), server_prefix, keep):
        c.delete(folder, n); log(f"[i] Удалена старая копия в облаке: {n}")
