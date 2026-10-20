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

# Дамп MySQL: перебираем способы входа (обычный, /root/.my.cnf, debian.cnf, CloudPanel) — ничего не пишем на диск сервера
MYSQL_CMD = r"""
command -v mysqldump >/dev/null 2>&1 || { echo NOMYSQL >&2; exit 3; }
O="--all-databases --single-transaction --routines --events --quick"
mysql -e "SELECT 1" >/dev/null 2>&1 && exec bash -c 'set -o pipefail; mysqldump '"$O"' | gzip -c'
for f in /root/.my.cnf /etc/mysql/debian.cnf /home/clp/.my.cnf; do
  [ -r "$f" ] && mysql --defaults-extra-file="$f" -e "SELECT 1" >/dev/null 2>&1 && exec bash -c 'set -o pipefail; mysqldump --defaults-extra-file='"$f"' '"$O"' | gzip -c'
done
if command -v clpctl >/dev/null 2>&1; then
  C=$(clpctl db:show:master-credentials 2>/dev/null)
  U=$(echo "$C" | grep -iE '^\|? *user' | head -1 | sed -E 's/.*[:|] *([^ |]+) *\|?$/\1/')
  P=$(echo "$C" | grep -iE '^\|? *password' | head -1 | sed -E 's/.*[:|] *([^ |]+) *\|?$/\1/')
  H=$(echo "$C" | grep -iE '^\|? *host' | head -1 | sed -E 's/.*[:|] *([^ |]+) *\|?$/\1/')
  if [ -n "$U" ] && [ -n "$P" ]; then exec env MYSQL_PWD="$P" bash -c 'set -o pipefail; mysqldump -h '"${H:-127.0.0.1}"' -u '"$U"' '"$O"' | gzip -c'; fi
fi
echo "Не найден пароль MySQL (нет доступа без пароля, /root/.my.cnf, debian.cnf, CloudPanel)" >&2; exit 4
"""
PG_CMD = "command -v pg_dumpall >/dev/null 2>&1 || { echo NOPG >&2; exit 3; }; " \
         "set -o pipefail; (su - postgres -c pg_dumpall 2>/dev/null || sudo -u postgres pg_dumpall) | gzip -c"
PKG_CMDS = {"packages_dpkg.txt": "dpkg --get-selections", "packages_manual.txt": "apt-mark showmanual",
            "packages_rpm.txt": "rpm -qa", "packages_snap.txt": "snap list", "packages_pip.txt": "pip3 freeze"}


class Cancelled(Exception):
    pass


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("dark")
        self.title("VPS Backup")
        self.geometry("820x760")
        self.q = queue.Queue()
        self.busy = False
        self.cancel = threading.Event()
        self.chan = None
        self._ui()
        self.load()
        self.after(100, self._poll)

    def _ui(self):
        f = ctk.CTkFrame(self); f.pack(fill="x", padx=12, pady=10)
        def row(r, label, w):
            ctk.CTkLabel(f, text=label).grid(row=r, column=0, sticky="w", padx=8, pady=4)
            w.grid(row=r, column=1, columnspan=2, sticky="ew", padx=8, pady=4)
            return w
        f.columnconfigure(1, weight=1)
        self.host = row(0, "IP / хост:", ctk.CTkEntry(f))
        self.port = row(1, "Порт:", ctk.CTkEntry(f)); self.port.insert(0, "22")
        self.user = row(2, "Пользователь:", ctk.CTkEntry(f)); self.user.insert(0, "root")
        self.auth = ctk.StringVar(value="password")
        af = ctk.CTkFrame(f, fg_color="transparent")
        ctk.CTkRadioButton(af, text="Пароль", variable=self.auth, value="password", command=self._auth).pack(side="left", padx=4)
        ctk.CTkRadioButton(af, text="SSH-ключ", variable=self.auth, value="key", command=self._auth).pack(side="left", padx=4)
        row(3, "Авторизация:", af)
        self.pwd = row(4, "Пароль / passphrase:", ctk.CTkEntry(f, show="•"))
        kf = ctk.CTkFrame(f, fg_color="transparent")
        self.key = ctk.CTkEntry(kf); self.key.pack(side="left", fill="x", expand=True)
        self.keybtn = ctk.CTkButton(kf, text="…", width=40, command=self._pick_key); self.keybtn.pack(side="left", padx=4)
        row(5, "Файл ключа:", kf)
        df = ctk.CTkFrame(f, fg_color="transparent")
        self.dest = ctk.CTkEntry(df); self.dest.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(df, text="Обзор", width=70, command=self._pick_dir).pack(side="left", padx=4)
        row(6, "Папка бэкапов:", df)
        self.remember = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(f, text="Запомнить настройки (пароль — в защищённом хранилище Windows)",
                        variable=self.remember).grid(row=7, column=0, columnspan=3, sticky="w", padx=8, pady=4)

        c = ctk.CTkFrame(self); c.pack(fill="x", padx=12)
        ctk.CTkLabel(c, text="Что бэкапить:", font=ctk.CTkFont(weight="bold")).pack(anchor="w", padx=8, pady=(6, 0))
        self.opt = {k: ctk.BooleanVar(value=True) for k in ("db", "web", "conf", "pkgs")}
        for k, t in [("db", "Базы данных (автодамп MySQL / PostgreSQL)"), ("web", "Файлы сайтов (/var/www, /home)"),
                     ("conf", "Конфигурации сервисов (/etc/nginx, /etc/apache2, cron, systemd…)"),
                     ("pkgs", "Список установленных пакетов")]:
            ctk.CTkCheckBox(c, text=t, variable=self.opt[k]).pack(anchor="w", padx=16, pady=3)

        b = ctk.CTkFrame(self, fg_color="transparent"); b.pack(fill="x", padx=12, pady=8)
        self.start = ctk.CTkButton(b, text="Начать бэкап", height=38, command=self.run); self.start.pack(side="left")
        self.stopb = ctk.CTkButton(b, text="Остановить", height=38, state="disabled", fg_color="#a33", hover_color="#c44",
                                   command=self.stop); self.stopb.pack(side="left", padx=8)
        self.openb = ctk.CTkButton(b, text="Открыть папку с бэкапами", height=38, state="disabled", command=self.open_dir)
        self.openb.pack(side="left", padx=8)
        self.status = ctk.CTkLabel(b, text=""); self.status.pack(side="right")
        self.bar = ctk.CTkProgressBar(self); self.bar.pack(fill="x", padx=12); self.bar.set(0)
        self.log = ctk.CTkTextbox(self, font=("Consolas", 12)); self.log.pack(fill="both", expand=True, padx=12, pady=10)
        self._auth()

    def _auth(self):
        k = self.auth.get() == "key"
        self.key.configure(state="normal" if k else "disabled"); self.keybtn.configure(state="normal" if k else "disabled")

    def _pick_key(self):
        p = filedialog.askopenfilename(title="SSH-ключ")
        if p: self.key.delete(0, "end"); self.key.insert(0, p)

    def _pick_dir(self):
        p = filedialog.askdirectory()
        if p: self.dest.delete(0, "end"); self.dest.insert(0, p)

    def open_dir(self):
        d = self.dest.get()
        if os.name == "nt": os.startfile(d)
        else: subprocess.Popen(["xdg-open", d])

    # ---------- settings ----------
    def _kr_id(self): return f"{self.user.get()}@{self.host.get()}:{self.port.get()}"

    def save(self):
        CFG_DIR.mkdir(parents=True, exist_ok=True)
        data = {"host": self.host.get(), "port": self.port.get(), "user": self.user.get(), "auth": self.auth.get(),
                "key": self.key.get(), "dest": self.dest.get(), "opt": {k: v.get() for k, v in self.opt.items()}}
        CFG_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        if keyring and self.pwd.get():
            try: keyring.set_password(APP, self._kr_id(), self.pwd.get())
            except Exception as e: self.q.put(("log", f"[!] Не удалось сохранить пароль: {e}"))

    def load(self):
        default = str(Path.home() / "VPS_Backups")
        try: d = json.loads(CFG_FILE.read_text(encoding="utf-8"))
        except Exception: d = {}
        for w, k, dv in [(self.host, "host", ""), (self.port, "port", "22"), (self.user, "user", "root"),
                         (self.key, "key", ""), (self.dest, "dest", default)]:
            w.configure(state="normal"); w.delete(0, "end"); w.insert(0, d.get(k, dv))
        self.auth.set(d.get("auth", "password"))
        for k, v in d.get("opt", {}).items():
            if k in self.opt: self.opt[k].set(v)
        if keyring and d.get("host"):
            try:
                p = keyring.get_password(APP, self._kr_id())
                if p: self.pwd.insert(0, p)
            except Exception: pass
        self._auth()

    # ---------- backup ----------
    def run(self):
        if self.busy: return
        if not self.host.get().strip(): return messagebox.showerror("Ошибка", "Укажите IP или хост сервера")
        if not any(v.get() for v in self.opt.values()): return messagebox.showerror("Ошибка", "Выберите, что бэкапить")
        if self.remember.get(): self.save()
        self.busy = True; self.cancel.clear(); self.start.configure(state="disabled"); self.openb.configure(state="disabled")
        self.stopb.configure(state="normal")
        self.log.delete("1.0", "end"); self.bar.set(0)
        cfg = dict(host=self.host.get().strip(), port=int(self.port.get() or 22), user=self.user.get().strip(),
                   auth=self.auth.get(), pwd=self.pwd.get(), key=self.key.get(), dest=self.dest.get(),
                   opt={k: v.get() for k, v in self.opt.items()})
        threading.Thread(target=self._worker, args=(cfg,), daemon=True).start()

    def stop(self):
        if not self.busy: return
        if not messagebox.askyesno("VPS Backup", "Остановить бэкап? Недокачанные файлы будут удалены."): return
        self.cancel.set(); self.stopb.configure(state="disabled")
        self.q.put(("log", "[!] Остановка..."))
        ch = self.chan
        if ch is not None:
            try: ch.close()
            except Exception: pass

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
        ssh = paramiko.SSHClient(); ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        tmp = final = None
        try:
            Path(c["dest"]).mkdir(parents=True, exist_ok=True)
            L(f"[*] Подключение к {c['user']}@{c['host']}:{c['port']}...")
            kw = dict(hostname=c["host"], port=c["port"], username=c["user"], timeout=20, allow_agent=False, look_for_keys=False)
            if c["auth"] == "key":
                kw.update(key_filename=c["key"], passphrase=c["pwd"] or None)
            else:
                kw["password"] = c["pwd"]
            ssh.connect(**kw); L("[+] Подключено"); self.q.put(("bar", 0.05))
            ssh.get_transport().set_keepalive(30)
            _, o, _ = ssh.exec_command("hostname"); hn = o.read().decode().strip() or c["host"]
            name = f"vps_backup_{hn}_{time.strftime('%Y-%m-%d_%H-%M-%S')}"
            tmp = Path(c["dest"]) / (name + ".part"); tmp.mkdir()
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
            with tarfile.open(final, "w:gz", compresslevel=1) as t:
                for p in sorted(tmp.iterdir()):
                    if self.cancel.is_set(): raise Cancelled()
                    t.add(p, arcname=f"{name}/{p.name}")
            shutil.rmtree(tmp, ignore_errors=True); tmp = None
            L(f"[✓] ГОТОВО: {final} ({final.stat().st_size/1048576:.1f} МБ)")
            self.q.put(("bar", 1.0)); self.q.put(("done", True))
        except Cancelled:
            L("[■] Бэкап остановлен пользователем. На сервере ничего не осталось.")
            if final and final.exists():
                try: final.unlink()
                except Exception: pass
            self.q.put(("bar", 0)); self.q.put(("done", None))
        except Exception as e:
            if self.cancel.is_set():
                L("[■] Бэкап остановлен пользователем."); self.q.put(("done", None))
            else:
                L(f"[✗] ОШИБКА: {e}"); self.q.put(("done", False))
        finally:
            if tmp: shutil.rmtree(tmp, ignore_errors=True)
            self.chan = None
            ssh.close()

    def _poll(self):
        try:
            while True:
                k, v = self.q.get_nowait()
                if k == "log": self.log.insert("end", v + "\n"); self.log.see("end")
                elif k == "bar": self.bar.set(v)
                elif k == "status": self.status.configure(text=v)
                elif k == "done":
                    self.busy = False; self.start.configure(state="normal"); self.openb.configure(state="normal")
                    self.stopb.configure(state="disabled")
                    txt = {True: "Готово", False: "Ошибка", None: "Остановлено"}[v]
                    self.status.configure(text=txt)
                    if v is True: messagebox.showinfo("VPS Backup", "Бэкап завершён")
                    elif v is False: messagebox.showerror("VPS Backup", "Бэкап не удался, см. лог")
        except queue.Empty: pass
        self.after(100, self._poll)


if __name__ == "__main__":
    App().mainloop()
