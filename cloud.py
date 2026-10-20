"""Загрузка бэкапов в облако: Яндекс Диск (WebDAV) и Облако Mail.ru (API мобильного клиента)."""
import os, posixpath, re
import xml.etree.ElementTree as ET
from urllib.parse import quote, unquote
import requests

TIMEOUT = (20, 120)
PROVIDERS = {"yandex": "Яндекс Диск", "mailru": "Облако Mail.ru"}


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


def open_cloud(provider, login, pwd):
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
    c.upload(local, folder, name, cb, cancel)
    log("[+] Копия в облаке сохранена")
    for n in old_copies(c.list(folder), server_prefix, keep):
        c.delete(folder, n); log(f"[i] Удалена старая копия в облаке: {n}")
