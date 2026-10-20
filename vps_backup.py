"""VPS Backup — GUI-приложение для резервного копирования VPS на локальный ПК."""
import json, os, queue, shlex, shutil, tarfile, threading, time, subprocess, sys
from pathlib import Path
import customtkinter as ctk
from tkinter import filedialog, messagebox
import paramiko

try:
    import keyring
except Exception:
    keyring = None

APP = "VPSBackup"
CFG_DIR = Path(os.environ.get("APPDATA", Path.home())) / APP
CFG_FILE = CFG_DIR / "settings.json"
WEB_PATHS = ["var/www", "home"]
CONF_PATHS = ["etc/nginx", "etc/apache2", "etc/httpd", "etc/mysql", "etc/postgresql",
              "etc/php", "etc/letsencrypt", "etc/ssh/sshd_config", "etc/crontab",
              "etc/cron.d", "var/spool/cron", "etc/systemd/system", "etc/hosts", "etc/fstab"]

# Поиск доступа к MySQL: обычный, /root/.my.cnf, debian.cnf, CloudPanel. Результат — в переменной $A
MYSQL_AUTH = r"""
command -v mysql >/dev/null 2>&1 || { echo NOMYSQL >&2; exit 3; }
A=""
if mysql -e "SELECT 1" >/dev/null 2>&1; then A=" "; else
  for f in /root/.my.cnf /etc/mysql/debian.cnf /home/clp/.my.cnf; do
    [ -r "$f" ] && mysql --defaults-extra-file="$f" -e "SELECT 1" >/dev/null 2>&1 && { A="--defaults-extra-file=$f"; break; }
  done
fi
if [ -z "$A" ] && command -v clpctl >/dev/null 2>&1; then
  C=$(clpctl db:show:master-credentials 2>/dev/null)
  U=$(echo "$C" | grep -iE '^\|? *user' | head -1 | sed -E 's/.*[:|] *([^ |]+) *\|?$/\1/')
  P=$(echo "$C" | grep -iE '^\|? *password' | head -1 | sed -E 's/.*[:|] *([^ |]+) *\|?$/\1/')
  H=$(echo "$C" | grep -iE '^\|? *host' | head -1 | sed -E 's/.*[:|] *([^ |]+) *\|?$/\1/')
  [ -n "$U" ] && [ -n "$P" ] && { export MYSQL_PWD="$P"; A="-h ${H:-127.0.0.1} -u $U"; }
fi
[ -z "$A" ] && { echo "Не найден пароль MySQL (нет доступа без пароля, /root/.my.cnf, debian.cnf, CloudPanel)" >&2; exit 4; }
set -o pipefail
"""
MYSQL_CMD = MYSQL_AUTH.replace("command -v mysql ", "command -v mysqldump ") + \
    "mysqldump $A --all-databases --single-transaction --routines --events --quick | gzip -c"
MYSQL_RESTORE = MYSQL_AUTH + "gunzip -c | mysql $A"
PG_RESTORE = "command -v psql >/dev/null 2>&1 || { echo NOPG >&2; exit 3; }; " \
             "gunzip -c | su - postgres -c 'psql -q -o /dev/null' 2>&1 | grep -v 'already exists' | tail -50 >&2; exit 0"
OS_CMD = ". /etc/os-release 2>/dev/null; echo \"${PRETTY_NAME:-$(uname -sr)}\"; echo \"ID=$ID\"; echo \"VERSION_ID=$VERSION_ID\""
CLP_INSTALL = ("command -v clpctl >/dev/null 2>&1 && { echo 'CloudPanel уже установлен'; exit 0; }; "
               "export DEBIAN_FRONTEND=noninteractive; apt-get update -y && apt-get install -y curl wget sudo && "
               "curl -sS https://installer.cloudpanel.io/ce/v2/install.sh -o /root/clp_install.sh && "
               "DB_ENGINE=MYSQL_8.0 bash /root/clp_install.sh; R=$?; rm -f /root/clp_install.sh; exit $R")
RESTART_CMD = ("for s in nginx apache2 httpd mysql mariadb postgresql cron; do systemctl is-enabled $s >/dev/null 2>&1 && "
               "systemctl restart $s 2>/dev/null && echo \"перезапущен $s\"; done; "
               "for s in $(systemctl list-unit-files 'php*-fpm.service' --no-legend 2>/dev/null | awk '{print $1}'); do "
               "systemctl restart $s 2>/dev/null && echo \"перезапущен $s\"; done; true")
PG_CMD = "command -v pg_dumpall >/dev/null 2>&1 || { echo NOPG >&2; exit 3; }; " \
         "set -o pipefail; (su - postgres -c pg_dumpall 2>/dev/null || sudo -u postgres pg_dumpall) | gzip -c"
PKG_CMDS = {"packages_dpkg.txt": "dpkg --get-selections", "packages_manual.txt": "apt-mark showmanual",
            "packages_rpm.txt": "rpm -qa", "packages_snap.txt": "snap list", "packages_pip.txt": "pip3 freeze"}



class Cancelled(Exception):
    pass


def parse_os(text):
    """Из вывода OS_CMD -> (красивое имя, id, версия)."""
    lines = (text or "").strip().splitlines()
    name = lines[0].strip() if lines else ""
    kv = dict(l.split("=", 1) for l in lines[1:] if "=" in l)
    return name, kv.get("ID", "").strip(), kv.get("VERSION_ID", "").strip()


def gb(n): return f"{n/1073741824:.1f} ГБ"


class ConnForm(ctk.CTkFrame):
    """Блок полей подключения по SSH (используется на обеих вкладках)."""
    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.columnconfigure(1, weight=1); self.r = 0
        self.host = self.row("IP / хост:", ctk.CTkEntry(self))
        self.port = self.row("Порт:", ctk.CTkEntry(self)); self.port.insert(0, "22")
        self.user = self.row("Пользователь:", ctk.CTkEntry(self)); self.user.insert(0, "root")
        self.auth = ctk.StringVar(value="password")
        af = ctk.CTkFrame(self, fg_color="transparent")
        ctk.CTkRadioButton(af, text="Пароль", variable=self.auth, value="password", command=self._auth).pack(side="left", padx=4)
        ctk.CTkRadioButton(af, text="SSH-ключ", variable=self.auth, value="key", command=self._auth).pack(side="left", padx=4)
        self.row("Авторизация:", af)
        self.pwd = self.row("Пароль / passphrase:", ctk.CTkEntry(self, show="•"))
        kf = ctk.CTkFrame(self, fg_color="transparent")
        self.key = ctk.CTkEntry(kf); self.key.pack(side="left", fill="x", expand=True)
        self.keybtn = ctk.CTkButton(kf, text="…", width=40, command=self._pick_key); self.keybtn.pack(side="left", padx=4)
        self.row("Файл ключа:", kf)
        self._auth()

    def row(self, label, w):
        ctk.CTkLabel(self, text=label).grid(row=self.r, column=0, sticky="w", padx=8, pady=3)
        w.grid(row=self.r, column=1, columnspan=2, sticky="ew", padx=8, pady=3); self.r += 1
        return w

    def _auth(self):
        k = self.auth.get() == "key"
        self.key.configure(state="normal" if k else "disabled"); self.keybtn.configure(state="normal" if k else "disabled")

    def _pick_key(self):
        p = filedialog.askopenfilename(title="SSH-ключ")
        if p: self.key.delete(0, "end"); self.key.insert(0, p)

    def kr_id(self): return f"{self.user.get()}@{self.host.get()}:{self.port.get()}"

    def cfg(self):
        return dict(host=self.host.get().strip(), port=int(self.port.get() or 22), user=self.user.get().strip(),
                    auth=self.auth.get(), pwd=self.pwd.get(), key=self.key.get())

    def dump(self):
        if keyring and self.pwd.get():
            try: keyring.set_password(APP, self.kr_id(), self.pwd.get())
            except Exception as e: self.app.q.put(("log", f"[!] Не удалось сохранить пароль: {e}"))
        return {"host": self.host.get(), "port": self.port.get(), "user": self.user.get(), "auth": self.auth.get(), "key": self.key.get()}

    def load(self, d):
        for w, k, dv in [(self.host, "host", ""), (self.port, "port", "22"), (self.user, "user", "root"), (self.key, "key", "")]:
            w.configure(state="normal"); w.delete(0, "end"); w.insert(0, d.get(k, dv))
        self.auth.set(d.get("auth", "password")); self.pwd.delete(0, "end")
        if keyring and d.get("host"):
            try:
                p = keyring.get_password(APP, self.kr_id())
                if p: self.pwd.insert(0, p)
            except Exception: pass
        self._auth()


def connect(c, L):
    ssh = paramiko.SSHClient(); ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    L(f"[*] Подключение к {c['user']}@{c['host']}:{c['port']}...")
    kw = dict(hostname=c["host"], port=c["port"], username=c["user"], timeout=20, allow_agent=False, look_for_keys=False)
    if c["auth"] == "key": kw.update(key_filename=c["key"], passphrase=c["pwd"] or None)
    else: kw["password"] = c["pwd"]
    ssh.connect(**kw); ssh.get_transport().set_keepalive(30); L("[+] Подключено")
    return ssh


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("dark")
        self.title("VPS Backup")
        self.geometry("880x980")
        self.q = queue.Queue()
        self.busy = False
        self.cancel = threading.Event()
        self.chan = None
        self.archive_os = None
        self._ui()
        self.load()
        self.after(100, self._poll)

    def _ui(self):
        self.tabs = ctk.CTkTabview(self, height=560); self.tabs.pack(fill="x", padx=12, pady=(6, 0))
        tb = self.tabs.add("Создать бэкап"); tr = self.tabs.add("Восстановить на сервер")

        # ----- вкладка бэкапа -----
        self.conn = ConnForm(tb, self); self.conn.pack(fill="x")
        df = ctk.CTkFrame(self.conn, fg_color="transparent")
        self.dest = ctk.CTkEntry(df); self.dest.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(df, text="Обзор", width=70, command=self._pick_dir).pack(side="left", padx=4)
        self.conn.row("Папка бэкапов:", df)
        c = ctk.CTkFrame(tb); c.pack(fill="x", pady=6)
        self.opt = {k: ctk.BooleanVar(value=True) for k in ("db", "web", "conf", "pkgs")}
        for k, t in [("db", "Базы данных (автодамп MySQL / PostgreSQL)"), ("web", "Файлы сайтов (/var/www, /home)"),
                     ("conf", "Конфигурации сервисов (/etc/nginx, /etc/apache2, cron, systemd…)"),
                     ("pkgs", "Список установленных пакетов")]:
            ctk.CTkCheckBox(c, text=t, variable=self.opt[k]).pack(anchor="w", padx=12, pady=2)
        b = ctk.CTkFrame(tb, fg_color="transparent"); b.pack(fill="x")
        self.start = ctk.CTkButton(b, text="Начать бэкап", height=36, command=self.run); self.start.pack(side="left")
        self.openb = ctk.CTkButton(b, text="Открыть папку с бэкапами", height=36, state="disabled", command=self.open_dir)
        self.openb.pack(side="left", padx=8)

        # ----- вкладка восстановления -----
        af = ctk.CTkFrame(tr); af.pack(fill="x")
        ctk.CTkLabel(af, text="Файл бэкапа:").pack(side="left", padx=8, pady=6)
        self.arch = ctk.CTkEntry(af); self.arch.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(af, text="Обзор", width=70, command=self._pick_archive).pack(side="left", padx=8)
        self.oslbl = ctk.CTkLabel(tr, text="ОС в архиве: — выберите файл бэкапа", font=ctk.CTkFont(weight="bold"),
                                  text_color="#7fb8ff"); self.oslbl.pack(anchor="w", padx=8, pady=(4, 2))
        ctk.CTkLabel(tr, text="Новый сервер (куда разворачивать):").pack(anchor="w", padx=8)
        self.rconn = ConnForm(tr, self); self.rconn.pack(fill="x")
        rc = ctk.CTkFrame(tr); rc.pack(fill="x", pady=6)
        self.ropt = {k: ctk.BooleanVar(value=v) for k, v in (("clp", False), ("web", True), ("conf", True), ("db", True))}
        for k, t in [("clp", "Чистый сервер: сначала установить CloudPanel (Nginx, MySQL, PHP) автоматически"),
                     ("web", "Восстановить файлы сайтов (/var/www, /home)"),
                     ("conf", "Восстановить конфигурации сервисов (/etc/...)"),
                     ("db", "Восстановить базы данных (MySQL / PostgreSQL)")]:
            ctk.CTkCheckBox(rc, text=t, variable=self.ropt[k]).pack(anchor="w", padx=12, pady=2)
        rb = ctk.CTkFrame(tr, fg_color="transparent"); rb.pack(fill="x")
        self.checkb = ctk.CTkButton(rb, text="Проверить сервер", height=36, command=lambda: self.restore(check_only=True))
        self.checkb.pack(side="left")
        self.rstart = ctk.CTkButton(rb, text="Начать восстановление", height=36, command=self.restore)
        self.rstart.pack(side="left", padx=8)

        # ----- общая нижняя часть -----
        bot = ctk.CTkFrame(self, fg_color="transparent"); bot.pack(fill="x", padx=12, pady=6)
        self.stopb = ctk.CTkButton(bot, text="Остановить", height=34, state="disabled", fg_color="#a33", hover_color="#c44",
                                   command=self.stop); self.stopb.pack(side="left")
        self.status = ctk.CTkLabel(bot, text=""); self.status.pack(side="right")
        self.remember = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(bot, text="Запомнить настройки (пароли — в защищённом хранилище Windows)",
                        variable=self.remember).pack(side="left", padx=12)
        self.bar = ctk.CTkProgressBar(self); self.bar.pack(fill="x", padx=12); self.bar.set(0)
        self.log = ctk.CTkTextbox(self, font=("Consolas", 12)); self.log.pack(fill="both", expand=True, padx=12, pady=10)

    def _pick_dir(self):
        p = filedialog.askdirectory()
        if p: self.dest.delete(0, "end"); self.dest.insert(0, p)

    def _pick_archive(self):
        p = filedialog.askopenfilename(title="Файл бэкапа", initialdir=self.dest.get() or None,
                                       filetypes=[("Бэкап", "*.tar.gz"), ("Все файлы", "*.*")])
        if p:
            self.arch.delete(0, "end"); self.arch.insert(0, p); self._read_os(p)

    def _read_os(self, p):
        self.archive_os = None
        try:
            with tarfile.open(p, "r|gz") as t:
                m = next(iter(t), None)
                if m and m.name.endswith("/os_release.txt"):
                    self.archive_os = parse_os(t.extractfile(m).read().decode("utf-8", "replace"))
        except Exception as e:
            self.oslbl.configure(text=f"Не удалось прочитать архив: {e}", text_color="#ff7f7f"); return
        if self.archive_os:
            self.oslbl.configure(text=f"ОС в архиве: {self.archive_os[0]}  — закажите новый VPS с этой же системой",
                                 text_color="#7fff9f")
        else:
            self.oslbl.configure(text="ОС в архиве: не указана (бэкап сделан старой версией программы)", text_color="#ffd27f")

    def open_dir(self):
        d = self.dest.get()
        if os.name == "nt": os.startfile(d)
        else: subprocess.Popen(["xdg-open", d])

    # ---------- settings ----------
    def save(self):
        CFG_DIR.mkdir(parents=True, exist_ok=True)
        data = self.conn.dump()
        data.update(dest=self.dest.get(), opt={k: v.get() for k, v in self.opt.items()}, restore=self.rconn.dump())
        CFG_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def load(self):
        try: d = json.loads(CFG_FILE.read_text(encoding="utf-8"))
        except Exception: d = {}
        self.conn.load(d); self.rconn.load(d.get("restore", {}))
        self.dest.delete(0, "end"); self.dest.insert(0, d.get("dest", str(Path.home() / "VPS_Backups")))
        for k, v in d.get("opt", {}).items():
            if k in self.opt: self.opt[k].set(v)

    # ---------- общее ----------
    def _begin(self):
        if self.remember.get(): self.save()
        self.busy = True; self.cancel.clear()
        for w in (self.start, self.openb, self.rstart, self.checkb): w.configure(state="disabled")
        self.stopb.configure(state="normal"); self.log.delete("1.0", "end"); self.bar.set(0)

    def stop(self):
        if not self.busy: return
        if not messagebox.askyesno("VPS Backup", "Остановить текущую операцию?"): return
        self.cancel.set(); self.stopb.configure(state="disabled")
        self.q.put(("log", "[!] Остановка..."))
        ch = self.chan
        if ch is not None:
            try: ch.close()
            except Exception: pass

    def _exec(self, ssh, cmd, timeout=60):
        _, o, e = ssh.exec_command("bash -c " + shlex.quote(cmd), timeout=timeout)
        return o.read().decode("utf-8", "replace"), o.channel.recv_exit_status()

    # ---------- backup ----------
    def run(self):
        if self.busy: return
        if not self.conn.host.get().strip(): return messagebox.showerror("Ошибка", "Укажите IP или хост сервера")
        if not any(v.get() for v in self.opt.values()): return messagebox.showerror("Ошибка", "Выберите, что бэкапить")
        self._begin()
        cfg = self.conn.cfg(); cfg.update(dest=self.dest.get(), opt={k: v.get() for k, v in self.opt.items()})
        threading.Thread(target=self._worker, args=(cfg,), daemon=True).start()

    def _stream(self, ssh, cmd, path, label):
        """Выполняет команду на сервере и пишет её вывод прямо в локальный файл. Возвращает (код, stderr)."""
        if self.cancel.is_set(): raise Cancelled()
        ch = ssh.get_transport().open_session(); self.chan = ch
        ch.exec_command("bash -c " + shlex.quote(cmd))
        done, t0, last, err = 0, time.time(), 0, b""
        with open(path, "wb") as f:
            while True:
                if self.cancel.is_set(): ch.close(); raise Cancelled()
                got = False
                if ch.recv_ready():
                    d = ch.recv(262144)
                    if d: f.write(d); done += len(d); got = True
                if ch.recv_stderr_ready():
                    err += ch.recv_stderr(65536); got = True
                if not got:
                    if ch.exit_status_ready() and not ch.recv_ready() and not ch.recv_stderr_ready(): break
                    if ch.closed and not ch.recv_ready(): break
                    time.sleep(0.02)
                if time.time() - last > 0.3:
                    last = time.time(); sp = done / max(last - t0, 0.1) / 1048576
                    self.q.put(("status", f"{label}: {done/1048576:.1f} МБ  ({sp:.1f} МБ/с)"))
        if self.cancel.is_set(): raise Cancelled()
        code = ch.recv_exit_status(); self.chan = None; ch.close()
        return code, err.decode("utf-8", "replace").strip()

    def _worker(self, c):
        L = lambda m: self.q.put(("log", m))
        ssh = None; tmp = final = None
        try:
            Path(c["dest"]).mkdir(parents=True, exist_ok=True)
            ssh = connect(c, L); self.q.put(("bar", 0.05))
            _, o, _ = ssh.exec_command("hostname"); hn = o.read().decode().strip() or c["host"]
            name = f"vps_backup_{hn}_{time.strftime('%Y-%m-%d_%H-%M-%S')}"
            tmp = Path(c["dest"]) / (name + ".part"); tmp.mkdir()
            out, _ = self._exec(ssh, OS_CMD)
            (tmp / "os_release.txt").write_text(out, encoding="utf-8")
            L(f"[i] ОС сервера: {parse_os(out)[0]}")
            L("[*] Режим «на лету»: на диске сервера ничего не создаётся")
            errors = []
            o = c["opt"]
            if o["db"]:
                L("[*] Дамп MySQL/MariaDB...")
                code, err = self._stream(ssh, MYSQL_CMD, tmp / "mysql_all.sql.gz", "MySQL")
                if code == 3: L("[-] MySQL не найден"); (tmp / "mysql_all.sql.gz").unlink()
                elif code == 0: L("[+] MySQL готово")
                else: L(f"[!] Ошибка дампа MySQL: {err[-300:]}"); errors.append("MySQL: " + err)
                self.q.put(("bar", 0.15))
                L("[*] Дамп PostgreSQL...")
                code, err = self._stream(ssh, PG_CMD, tmp / "postgres_all.sql.gz", "PostgreSQL")
                if code == 3: L("[-] PostgreSQL не найден"); (tmp / "postgres_all.sql.gz").unlink()
                elif code == 0: L("[+] PostgreSQL готово")
                else: L(f"[!] Ошибка дампа PostgreSQL: {err[-300:]}"); errors.append("PostgreSQL: " + err)
                self.q.put(("bar", 0.25))
            if o["pkgs"]:
                L("[*] Список пакетов...")
                for fn, cmd in PKG_CMDS.items():
                    exe = cmd.split()[0]
                    code, _ = self._stream(ssh, f"command -v {exe} >/dev/null 2>&1 && {cmd} 2>/dev/null", tmp / fn, "Пакеты")
                    if code != 0 or (tmp / fn).stat().st_size == 0: (tmp / fn).unlink()
                L("[+] Список пакетов сохранён"); self.q.put(("bar", 0.3))
            paths = (WEB_PATHS if o["web"] else []) + (CONF_PATHS if o["conf"] else [])
            if paths:
                plist = " ".join(shlex.quote(p) for p in paths)
                cmd = (f'cd / && P=""; for p in {plist}; do [ -e "/$p" ] && P="$P $p"; done; '
                       '[ -z "$P" ] && exit 5; tar -czf - --ignore-failed-read --warning=no-file-changed $P')
                L("[*] Архивация и скачивание файлов сайтов/конфигураций (может занять долго)...")
                self.q.put(("bar", 0.35))
                code, err = self._stream(ssh, cmd, tmp / "files.tar.gz", "Файлы")
                if code == 5: L("[-] Папки не найдены"); (tmp / "files.tar.gz").unlink()
                elif code in (0, 1): L(f"[+] Файлы скачаны ({(tmp/'files.tar.gz').stat().st_size/1048576:.1f} МБ)")
                else: L(f"[!] Ошибка архивации: {err[-300:]}"); errors.append("tar: " + err)
                if err and code in (0, 1): errors.append("tar warnings: " + err)
            if errors: (tmp / "errors.log").write_text("\n\n".join(errors), encoding="utf-8")
            self.q.put(("bar", 0.95)); L("[*] Упаковка в один файл...")
            final = Path(c["dest"]) / (name + ".tar.gz")
            # os_release.txt кладём первым — так программа быстро читает ОС из архива
            items = sorted(tmp.iterdir(), key=lambda p: (p.name != "os_release.txt", p.name))
            with tarfile.open(final, "w:gz", compresslevel=1) as t:
                for p in items:
                    if self.cancel.is_set(): raise Cancelled()
                    t.add(p, arcname=f"{name}/{p.name}")
            shutil.rmtree(tmp, ignore_errors=True); tmp = None
            L(f"[✓] ГОТОВО: {final} ({final.stat().st_size/1048576:.1f} МБ)")
            self.q.put(("bar", 1.0)); self.q.put(("done", ("backup", True)))
        except Cancelled:
            L("[■] Бэкап остановлен пользователем. На сервере ничего не осталось.")
            if final and final.exists():
                try: final.unlink()
                except Exception: pass
            self.q.put(("bar", 0)); self.q.put(("done", ("backup", None)))
        except Exception as e:
            if self.cancel.is_set(): L("[■] Бэкап остановлен пользователем."); self.q.put(("done", ("backup", None)))
            else: L(f"[✗] ОШИБКА: {e}"); self.q.put(("done", ("backup", False)))
        finally:
            if tmp: shutil.rmtree(tmp, ignore_errors=True)
            self.chan = None
            if ssh: ssh.close()

    # ---------- restore ----------
    def restore(self, check_only=False):
        if self.busy: return
        p = self.arch.get().strip()
        if not p or not Path(p).is_file(): return messagebox.showerror("Ошибка", "Выберите файл бэкапа (.tar.gz)")
        if not self.rconn.host.get().strip(): return messagebox.showerror("Ошибка", "Укажите IP нового сервера")
        o = {k: v.get() for k, v in self.ropt.items()}
        if not check_only:
            if not any(o[k] for k in ("web", "conf", "db", "clp")): return messagebox.showerror("Ошибка", "Выберите, что восстанавливать")
            if not messagebox.askyesno("Подтверждение",
                    f"Развернуть бэкап на сервер {self.rconn.host.get().strip()}?\n\n"
                    "Файлы, настройки и базы на ЭТОМ сервере будут перезаписаны данными из бэкапа.\n"
                    "Убедитесь, что это новый сервер, а не рабочий!", icon="warning"):
                return
        self._begin()
        cfg = self.rconn.cfg(); cfg.update(archive=p, opt=o, check_only=check_only)
        threading.Thread(target=self._rworker, args=(cfg,), daemon=True).start()

    def _send(self, ssh, cmd, src, label, total):
        """Передаёт поток src на stdin команды на сервере. Возвращает (код, stderr+stdout)."""
        if self.cancel.is_set(): raise Cancelled()
        ch = ssh.get_transport().open_session(); self.chan = ch
        ch.exec_command("bash -c " + shlex.quote(cmd))
        done, t0, last, out = 0, time.time(), 0, b""
        def drain():
            nonlocal out
            while ch.recv_ready(): out += ch.recv(65536)
            while ch.recv_stderr_ready(): out += ch.recv_stderr(65536)
            out = out[-20000:]
        while True:
            if self.cancel.is_set(): ch.close(); raise Cancelled()
            d = src.read(262144)
            if not d: break
            ch.sendall(d); done += len(d); drain()
            if time.time() - last > 0.3:
                last = time.time(); sp = done / max(last - t0, 0.1) / 1048576
                pct = f" / {total/1048576:.0f}" if total else ""
                self.q.put(("status", f"{label}: {done/1048576:.1f}{pct} МБ  ({sp:.1f} МБ/с)"))
        ch.shutdown_write()
        while not ch.exit_status_ready():
            if self.cancel.is_set(): ch.close(); raise Cancelled()
            drain(); time.sleep(0.1)
        drain(); code = ch.recv_exit_status(); self.chan = None; ch.close()
        return code, out.decode("utf-8", "replace").strip()

    def _run_log(self, ssh, cmd):
        """Выполняет долгую команду и транслирует её вывод в лог построчно."""
        ch = ssh.get_transport().open_session(); self.chan = ch
        ch.get_pty(); ch.exec_command("bash -c " + shlex.quote(cmd))
        buf = b""
        while True:
            if self.cancel.is_set(): ch.close(); raise Cancelled()
            if ch.recv_ready():
                buf += ch.recv(65536)
                *lines, buf = buf.replace(b"\r", b"\n").split(b"\n")
                for l in lines:
                    t = l.decode("utf-8", "replace").strip()
                    if t: self.q.put(("log", "    " + t[:200]))
            elif ch.exit_status_ready(): break
            else: time.sleep(0.1)
        code = ch.recv_exit_status(); self.chan = None; ch.close()
        return code

    def _rworker(self, c):
        L = lambda m: self.q.put(("log", m))
        ssh = None; o = c["opt"]
        try:
            arch = Path(c["archive"]); asize = arch.stat().st_size
            ssh = connect(c, L); self.q.put(("bar", 0.05))
            # --- ОС ---
            out, _ = self._exec(ssh, OS_CMD); tos = parse_os(out)
            L(f"[i] ОС нового сервера: {tos[0]}")
            if self.archive_os:
                L(f"[i] ОС в архиве:        {self.archive_os[0]}")
                if (tos[1], tos[2]) == self.archive_os[1:]: L("[✓] Системы совпадают")
                else: L("[!] ВНИМАНИЕ: система отличается от той, что в архиве. Возможны проблемы совместимости.")
            # --- место ---
            out, _ = self._exec(ssh, "df -B1 --output=avail / | tail -1; free -b | awk '/Mem:/{print $2}'")
            nums = [int(x) for x in out.split() if x.isdigit()]
            free = nums[0] if nums else 0; ram = nums[1] if len(nums) > 1 else 0
            need = asize * 3 + (4 if o["clp"] else 1) * 1073741824
            L(f"[i] Размер бэкапа: {gb(asize)}; свободно на диске: {gb(free)}; нужно примерно: {gb(need)}; память: {gb(ram)}")
            if free < need:
                L(f"[✗] МЕСТА НЕ ХВАТАЕТ: не хватает около {gb(need - free)}. Восстановление не начато.")
                self.q.put(("done", ("restore", False))); return
            L("[✓] Места достаточно")
            if o["clp"] and tos[1] not in ("ubuntu", "debian"):
                L("[!] CloudPanel устанавливается только на Ubuntu или Debian")
            if c["check_only"]:
                L("[✓] Проверка завершена, на сервере ничего не изменено"); self.q.put(("bar", 1.0))
                self.q.put(("done", ("check", True))); return
            # --- CloudPanel ---
            if o["clp"]:
                L("[*] Установка CloudPanel (обычно 5–15 минут)...")
                if self._run_log(ssh, CLP_INSTALL) != 0:
                    L("[✗] Установка CloudPanel не удалась (см. лог выше). Восстановление остановлено.")
                    self.q.put(("done", ("restore", False))); return
                L("[+] CloudPanel установлен")
            self.q.put(("bar", 0.3))
            # --- файлы и базы: читаем архив по порядку ---
            prefixes = (WEB_PATHS if o["web"] else []) + (CONF_PATHS if o["conf"] else [])
            warn = []
            with tarfile.open(arch, "r|gz") as t:
                for m in t:
                    fn = m.name.rsplit("/", 1)[-1]
                    if fn == "files.tar.gz" and prefixes:
                        L("[*] Распаковка файлов на сервер (может занять долго)...")
                        pats = " ".join(shlex.quote(p) for p in prefixes)
                        both = o["web"] and o["conf"]
                        cmd = "tar -xzpf - -C / --numeric-owner" + ("" if both else f" --wildcards {pats}")
                        code, err = self._send(ssh, cmd, t.extractfile(m), "Файлы", m.size)
                        if code in (0, 2): L("[+] Файлы восстановлены")
                        else: L(f"[!] Ошибка распаковки: {err[-300:]}")
                        if err: warn.append(err)
                        self.q.put(("bar", 0.6))
                    elif fn == "mysql_all.sql.gz" and o["db"]:
                        L("[*] Импорт баз MySQL/MariaDB...")
                        code, err = self._send(ssh, MYSQL_RESTORE, t.extractfile(m), "MySQL", m.size)
                        if code == 3: L("[-] На сервере нет MySQL — пропущено (отметьте установку CloudPanel)")
                        elif code == 0: L("[+] MySQL восстановлен")
                        else: L(f"[!] Ошибка импорта MySQL: {err[-300:]}")
                        self.q.put(("bar", 0.8))
                    elif fn == "postgres_all.sql.gz" and o["db"]:
                        L("[*] Импорт баз PostgreSQL...")
                        code, err = self._send(ssh, PG_RESTORE, t.extractfile(m), "PostgreSQL", m.size)
                        if code == 3: L("[-] На сервере нет PostgreSQL — пропущено")
                        else: L("[+] PostgreSQL восстановлен")
                        self.q.put(("bar", 0.9))
            L("[*] Перезапуск служб...")
            out, _ = self._exec(ssh, RESTART_CMD, timeout=300)
            for l in out.splitlines(): L("    " + l)
            L("[✓] ВОССТАНОВЛЕНИЕ ЗАВЕРШЕНО. Проверьте сайты и переключите домены на новый IP.")
            self.q.put(("bar", 1.0)); self.q.put(("done", ("restore", True)))
        except Cancelled:
            L("[■] Восстановление остановлено пользователем. Часть данных могла успеть записаться.")
            self.q.put(("bar", 0)); self.q.put(("done", ("restore", None)))
        except Exception as e:
            if self.cancel.is_set(): L("[■] Остановлено пользователем."); self.q.put(("done", ("restore", None)))
            else: L(f"[✗] ОШИБКА: {e}"); self.q.put(("done", ("restore", False)))
        finally:
            self.chan = None
            if ssh: ssh.close()

    def _poll(self):
        try:
            while True:
                k, v = self.q.get_nowait()
                if k == "log": self.log.insert("end", v + "\n"); self.log.see("end")
                elif k == "bar": self.bar.set(v)
                elif k == "status": self.status.configure(text=v)
                elif k == "done":
                    kind, ok = v
                    self.busy = False
                    for w in (self.start, self.openb, self.rstart, self.checkb): w.configure(state="normal")
                    self.stopb.configure(state="disabled")
                    self.status.configure(text={True: "Готово", False: "Ошибка", None: "Остановлено"}[ok])
                    title = {"backup": "Бэкап", "restore": "Восстановление", "check": "Проверка"}[kind]
                    if ok is True: messagebox.showinfo("VPS Backup", f"{title}: успешно")
                    elif ok is False: messagebox.showerror("VPS Backup", f"{title}: не удалось, см. лог")
        except queue.Empty: pass
        self.after(100, self._poll)


if __name__ == "__main__":
    App().mainloop()
