"""VPS Backup — GUI-приложение для резервного копирования VPS на локальный ПК."""
import json, os, queue, shlex, threading, time, subprocess, sys
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
MARK = "@@ARCHIVE@@"

WEB_PATHS = ["var/www", "home"]
CONF_PATHS = ["etc/nginx", "etc/apache2", "etc/httpd", "etc/mysql", "etc/postgresql",
              "etc/php", "etc/letsencrypt", "etc/ssh/sshd_config", "etc/crontab",
              "etc/cron.d", "var/spool/cron", "etc/systemd/system", "etc/hosts", "etc/fstab"]


def build_script(opts):
    w = '"$W"'
    s = ["set -u", "TS=$(date +%Y-%m-%d_%H-%M-%S)", "W=/tmp/vpsbackup_$TS",
         "ARCH=/tmp/vps_backup_$(hostname)_$TS.tar.gz", "mkdir -p $W/meta",
         "echo '[*] Рабочая папка: '$W"]
    if opts["db"]:
        s += ["if command -v mysqldump >/dev/null 2>&1; then echo '[*] Дамп MySQL/MariaDB...';"
              f" mysqldump --all-databases --single-transaction --routines --events > {w}/meta/mysql_all.sql 2>>{w}/meta/errors.log"
              " && echo '[+] MySQL готово' || echo '[!] Ошибка дампа MySQL (см. errors.log)'; else echo '[-] MySQL не найден'; fi",
              "if command -v pg_dumpall >/dev/null 2>&1; then echo '[*] Дамп PostgreSQL...';"
              f" (su - postgres -c pg_dumpall || sudo -u postgres pg_dumpall) > {w}/meta/postgres_all.sql 2>>{w}/meta/errors.log"
              " && echo '[+] PostgreSQL готово' || echo '[!] Ошибка дампа PostgreSQL'; else echo '[-] PostgreSQL не найден'; fi"]
    if opts["pkgs"]:
        s += ["echo '[*] Список пакетов...'",
              f"(command -v dpkg >/dev/null && dpkg --get-selections > {w}/meta/packages_dpkg.txt) || true",
              f"(command -v apt-mark >/dev/null && apt-mark showmanual > {w}/meta/packages_manual.txt) || true",
              f"(command -v rpm >/dev/null && rpm -qa > {w}/meta/packages_rpm.txt) || true",
              f"(command -v snap >/dev/null && snap list > {w}/meta/packages_snap.txt) || true",
              f"(command -v pip3 >/dev/null && pip3 freeze > {w}/meta/packages_pip.txt 2>/dev/null) || true"]
    paths = (WEB_PATHS if opts["web"] else []) + (CONF_PATHS if opts["conf"] else [])
    plist = " ".join(shlex.quote(p) for p in paths)
    s += ["P=\"${W#/}\"", f"for p in {plist}; do [ -e \"/$p\" ] && P=\"$P $p\"; done",
          "echo '[*] Архивация: '$P", "cd / && tar -czf $ARCH --ignore-failed-read --warning=no-file-changed $P 2>&1 | tail -n 20",
          "rm -rf $W", "echo '[+] Архив создан: '$(du -h $ARCH | cut -f1)", f"echo {MARK}$ARCH"]
    return "\n".join(s)


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("dark")
        self.title("VPS Backup")
        self.geometry("820x760")
        self.q = queue.Queue()
        self.busy = False
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
        self.busy = True; self.start.configure(state="disabled"); self.openb.configure(state="disabled")
        self.log.delete("1.0", "end"); self.bar.set(0)
        cfg = dict(host=self.host.get().strip(), port=int(self.port.get() or 22), user=self.user.get().strip(),
                   auth=self.auth.get(), pwd=self.pwd.get(), key=self.key.get(), dest=self.dest.get(),
                   opt={k: v.get() for k, v in self.opt.items()})
        threading.Thread(target=self._worker, args=(cfg,), daemon=True).start()

    def _worker(self, c):
        L = lambda m: self.q.put(("log", m))
        ssh = paramiko.SSHClient(); ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
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
            stdin, out, _ = ssh.exec_command("bash -s", get_pty=False)
            stdin.channel.set_combine_stderr(True)
            stdin.write(build_script(c["opt"])); stdin.channel.shutdown_write()
            archive = None
            for line in iter(out.readline, ""):
                line = line.rstrip()
                if line.startswith(MARK): archive = line[len(MARK):]
                elif line: L(line)
            if out.channel.recv_exit_status() != 0 or not archive:
                raise RuntimeError("Не удалось создать архив на сервере")
            self.q.put(("bar", 0.4))
            local = str(Path(c["dest"]) / os.path.basename(archive))
            L(f"[*] Скачивание {archive} → {local}")
            sftp = ssh.open_sftp(); t0 = [time.time()]
            def cb(done, total):
                if total:
                    self.q.put(("bar", 0.4 + 0.58 * done / total))
                    sp = done / max(time.time() - t0[0], 0.1) / 1048576
                    self.q.put(("status", f"{done/1048576:.1f} / {total/1048576:.1f} МБ  ({sp:.1f} МБ/с)"))
            sftp.get(archive, local, callback=cb)
            if os.path.getsize(local) != sftp.stat(archive).st_size:
                raise RuntimeError("Размер скачанного файла не совпадает — архив на сервере сохранён")
            sftp.remove(archive); sftp.close()
            L("[+] Временный архив удалён с сервера")
            L(f"[✓] ГОТОВО: {local}"); self.q.put(("bar", 1.0)); self.q.put(("done", True))
        except Exception as e:
            L(f"[✗] ОШИБКА: {e}"); self.q.put(("done", False))
        finally:
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
                    self.status.configure(text="Готово" if v else "Ошибка")
                    (messagebox.showinfo if v else messagebox.showerror)("VPS Backup", "Бэкап завершён" if v else "Бэкап не удался, см. лог")
        except queue.Empty: pass
        self.after(100, self._poll)


if __name__ == "__main__":
    App().mainloop()
