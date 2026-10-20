"""VPS Backup — GUI-приложение для резервного копирования VPS на локальный ПК."""
import gzip, json, os, re, socket, queue, shlex, shutil, tarfile, threading, time, subprocess, sys
from pathlib import Path
import customtkinter as ctk
from tkinter import filedialog, messagebox
import paramiko
import cloud

try:
    import keyring
except Exception:
    keyring = None

APP = "VPSBackup"
CFG_DIR = Path(os.environ.get("APPDATA", Path.home())) / APP
CFG_FILE = CFG_DIR / "settings.json"
WEB_PATHS = ["var/www", "home"]
# Что пропускать в режиме «без кэша»: всё это программы создают заново сами
SLIM_EXCLUDES = ["node_modules", ".npm", ".cache", ".yarn/cache", ".pnpm-store", ".pm2/logs",
                 "home/*/logs", "var/www/*/logs", "__pycache__", "*.sock"]
# После восстановления: ставим недостающие node_modules в каждом проекте и поднимаем pm2
NPM_CMD = r"""
command -v find >/dev/null || exit 0
N=0
while IFS= read -r f; do
  d=$(dirname "$f"); [ -d "$d/node_modules" ] && continue
  u=$(stat -c %U "$d"); N=$((N+1)); echo ">> $d  (пользователь $u)"
  su - "$u" -s /bin/bash -c "export NVM_DIR=\$HOME/.nvm; [ -s \$NVM_DIR/nvm.sh ] && . \$NVM_DIR/nvm.sh; cd '$d' || exit 1;
    command -v npm >/dev/null || { echo '   npm не найден — установите Node.js для этого пользователя'; exit 7; };
    if [ -f package-lock.json ]; then npm ci --no-audit --no-fund --loglevel=error || npm install --no-audit --no-fund --loglevel=error;
    else npm install --no-audit --no-fund --loglevel=error; fi && echo '   ✓ готово'" 2>&1 | tail -n 4
done < <(find /home /var/www -maxdepth 6 -name package.json -not -path '*/node_modules/*' -not -path '*/.*/*' 2>/dev/null)
[ "$N" = 0 ] && echo "Проектов без node_modules не найдено"
for h in /home/* /root; do
  [ -f "$h/.pm2/dump.pm2" ] || continue; u=$(stat -c %U "$h")
  su - "$u" -s /bin/bash -c 'export NVM_DIR=$HOME/.nvm; [ -s $NVM_DIR/nvm.sh ] && . $NVM_DIR/nvm.sh; command -v pm2 >/dev/null && pm2 resurrect >/dev/null 2>&1 && echo "pm2 запущен для '"$u"'"'
done
exit 0
"""
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
MYSQL_RESTORE_RAW = MYSQL_AUTH + "mysql $A"
PG_RESTORE = "command -v psql >/dev/null 2>&1 || { echo NOPG >&2; exit 3; }; " \
             "gunzip -c | su - postgres -c 'psql -q -o /dev/null' 2>&1 | grep -v 'already exists' | tail -50 >&2; exit 0"
PG_RESTORE_RAW = PG_RESTORE.replace("gunzip -c | ", "")
MYSQL_SIZE_CMD = MYSQL_AUTH + "mysql $A -N -e \"SELECT COALESCE(SUM(data_length+index_length),0) FROM information_schema.tables\""
PG_SIZE_CMD = ("command -v psql >/dev/null 2>&1 || exit 3; "
               "su - postgres -c \"psql -At -c 'SELECT COALESCE(SUM(pg_database_size(datname)),0) FROM pg_database'\" 2>/dev/null")


def human(n):
    n = float(n)
    for u in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if n < 1024 or u == "ТБ": return f"{n:.0f} {u}" if u in ("Б", "КБ") else f"{n:.1f} {u}"
        n /= 1024


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
SYS_MY = {"information_schema", "mysql", "performance_schema", "sys"}
SYS_PG = {"postgres", "template0", "template1"}
# Аудит целевого сервера: ТОЛЬКО ЧТЕНИЕ. Каждая строка — "ТИП:значение"
AUDIT_CMD = (r"""
for d in /home/*/; do [ -d "$d" ] || continue; u=$(basename "$d"); case "$u" in clp|lost+found) continue;; esac
  [ -n "$(ls -A "$d" 2>/dev/null | grep -v '^\.')" ] && echo "SITE:/home/$u"; done
for d in /var/www/*; do [ -e "$d" ] || continue; b=$(basename "$d")
  if [ "$b" = html ]; then n=$(ls -A "$d" 2>/dev/null | grep -vE '^index\.(nginx-debian\.)?html?$' | wc -l); [ "$n" -gt 0 ] && echo "WWW:$d"
  else echo "WWW:$d"; fi; done
[ -d /home/clp ] && echo "CLPDATA:yes"
(""" + MYSQL_AUTH + """mysql $A -N -e 'SHOW DATABASES' | sed 's/^/MYDB:/') 2>/dev/null
command -v psql >/dev/null 2>&1 && { su - postgres -c "psql -At -c 'SELECT datname FROM pg_database'" | sed 's/^/PGDB:/'
  su - postgres -c "psql -At -c 'SELECT rolname FROM pg_roles'" | sed 's/^/PGROLE:/'; } 2>/dev/null
for s in clpctl nginx apache2 httpd mysql mariadb psql php; do command -v $s >/dev/null 2>&1 && echo "SVC:$s"; done
true""")


def parse_audit(out):
    """Разбирает вывод AUDIT_CMD. level: green / yellow / red."""
    a = {"sites": [], "my": set(), "pg": set(), "roles": set(), "svc": [], "clpdata": False}
    for l in out.splitlines():
        k, _, v = l.strip().partition(":")
        v = v.strip()
        if not v: continue
        if k in ("SITE", "WWW"): a["sites"].append(v)
        elif k == "MYDB": a["my"].add(v)
        elif k == "PGDB": a["pg"].add(v)
        elif k == "PGROLE": a["roles"].add(v)
        elif k == "SVC": a["svc"].append(v)
        elif k == "CLPDATA": a["clpdata"] = True
    a["my_user"] = sorted(a["my"] - SYS_MY)
    a["pg_user"] = sorted(a["pg"] - SYS_PG)
    a["level"] = "red" if (a["sites"] or a["my_user"] or a["pg_user"]) else ("yellow" if a["svc"] else "green")
    return a


def audit_text(a):
    r = []
    if a["sites"]: r.append("Папки сайтов: " + ", ".join(a["sites"][:15]) + (" …" if len(a["sites"]) > 15 else ""))
    if a["my_user"]: r.append("Базы MySQL: " + ", ".join(a["my_user"][:15]))
    if a["pg_user"]: r.append("Базы PostgreSQL: " + ", ".join(a["pg_user"][:15]))
    if a["svc"]: r.append("Установлено: " + ", ".join(a["svc"]))
    return r


class DumpFilter:
    """Читает .sql.gz и пропускает разделы указанных баз (безопасный режим). Отдаёт несжатый SQL."""
    def __init__(self, src, kind, skip_db, skip_roles=()):
        self.g = gzip.GzipFile(fileobj=src); self.kind = kind
        self.skip = set(skip_db); self.roles = set(skip_roles)
        self.cur = None; self.buf = b""; self.eof = False; self.skipped = set()

    def _ok(self, line):
        if self.kind == "my":
            m = re.match(rb"-- Current Database: `(.+)`", line)
            if m: self.cur = m.group(1).decode("utf-8", "replace")
        else:
            m = re.match(rb"\\connect\s+(?:-reuse-previous=on\s+)?\"?([^\"\s]+)", line)
            if m: self.cur = m.group(1).decode("utf-8", "replace")
            m = re.match(rb"(CREATE|ALTER) (ROLE|DATABASE) \"?([^\"\s;]+)", line)
            if m:
                n = m.group(3).decode("utf-8", "replace")
                if (m.group(2) == b"ROLE" and n in self.roles) or (m.group(2) == b"DATABASE" and n in self.skip):
                    return False
        if self.cur in self.skip:
            self.skipped.add(self.cur); return False
        return True

    def read(self, n):
        while len(self.buf) < n and not self.eof:
            line = self.g.readline()
            if not line: self.eof = True; break
            if self._ok(line): self.buf += line
        d, self.buf = self.buf[:n], self.buf[n:]
        return d


PKG_CMDS = {"packages_dpkg.txt": "dpkg --get-selections", "packages_manual.txt": "apt-mark showmanual",
            "packages_rpm.txt": "rpm -qa", "packages_snap.txt": "snap list", "packages_pip.txt": "pip3 freeze"}



RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
DAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def autostart_cmd():
    if getattr(sys, "frozen", False): return f'"{sys.executable}" --tray'
    return f'"{sys.executable}" "{Path(__file__).resolve()}" --tray'


def set_autostart(on):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on: winreg.SetValueEx(k, APP, 0, winreg.REG_SZ, autostart_cmd())
        else:
            try: winreg.DeleteValue(k, APP)
            except FileNotFoundError: pass


def get_autostart():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP); return True
    except Exception: return False


def parse_hhmm(t):
    h, m = (t or "").strip().replace(".", ":").split(":")
    h, m = int(h), int(m)
    if not (0 <= h < 24 and 0 <= m < 60): raise ValueError
    return h, m


def is_due(now, hhmm, days, last_run):
    """Пора ли запускать: нужный день недели, время наступило (в пределах 6 ч), сегодня ещё не запускали."""
    if now.weekday() not in days: return False
    h, m = parse_hhmm(hhmm)
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if not (target <= now < target + __import__("datetime").timedelta(hours=6)): return False
    return last_run != now.strftime("%Y-%m-%d")


class Broken(Exception):
    """Архив или его часть повреждены / недокачаны."""


def gz_check(path, cancel=None):
    """Прочитать .gz до конца: проверяет CRC и что поток не оборван. Возвращает (ok, сообщение)."""
    try:
        if Path(path).stat().st_size == 0: return False, f"{Path(path).name}: файл пустой"
        with gzip.open(path, "rb") as f:
            while f.read(1 << 20):
                if cancel is not None and cancel.is_set(): raise Cancelled()
        return True, "ok"
    except Cancelled: raise
    except (EOFError, OSError, gzip.BadGzipFile, Exception) as e:
        return False, f"{Path(path).name}: файл оборван или повреждён ({e})"


def _walk_tar(t, cancel=None, nested=True):
    n = 0
    for m in t:
        if cancel is not None and cancel.is_set(): raise Cancelled()
        n += 1
        if not m.isfile(): continue
        f = t.extractfile(m)
        if nested and m.name.endswith("files.tar.gz"):
            with tarfile.open(fileobj=f, mode="r|gz") as inner: n += _walk_tar(inner, cancel, False)
            while f.read(1 << 20): pass
        elif nested and m.name.endswith(".gz"):
            g = gzip.GzipFile(fileobj=f)
            while g.read(1 << 20):
                if cancel is not None and cancel.is_set(): raise Cancelled()
        else:
            while f.read(1 << 20):
                if cancel is not None and cancel.is_set(): raise Cancelled()
    return n


def verify_archive(path, cancel=None):
    """Полная проверка бэкапа: внешний архив + каждая база + архив файлов внутри. (ok, сообщение)."""
    try:
        with tarfile.open(path, "r|gz") as t: n = _walk_tar(t, cancel)
        if n < 2: return False, "архив пустой или неполный"
        return True, f"архив целый, проверено объектов: {n}"
    except Cancelled: raise
    except Exception as e:
        return False, f"архив повреждён или недокачан ({type(e).__name__}: {e})"


class Cancelled(Exception):
    pass


def parse_os(text):
    """Из вывода OS_CMD -> (красивое имя, id, версия)."""
    lines = (text or "").strip().splitlines()
    name = lines[0].strip() if lines else ""
    kv = dict(l.split("=", 1) for l in lines[1:] if "=" in l)
    return name, kv.get("ID", "").strip(), kv.get("VERSION_ID", "").strip()


def gb(n): return f"{n/1073741824:.1f} ГБ"


def safe_name(t):
    t = re.sub(r'[\\/:*?"<>|\s]+', "_", (t or "").strip()).strip("._")
    return t[:60] or "server"


# Палитра в стиле Windows 11 (Fluent, светлая)
BG, CARD, BORDER = "#F4F6FB", "#FFFFFF", "#E6E9F2"
ACCENT, ACCENT_H = "#3D5AFE", "#5872FF"
TXT, MUTED = "#151A2D", "#6B7390"
OK, WARN, ERR = "#12925A", "#B7791F", "#E5484D"
SOFT, SOFT_H = "#EEF1FA", "#E1E6F5"
SIDE, SIDE_H, SIDE_T, SIDE_M = "#141A33", "#222A4D", "#FFFFFF", "#8A93B8"
FONT = "Segoe UI"


def card(master, title=None):
    f = ctk.CTkFrame(master, fg_color=CARD, border_color=BORDER, border_width=1, corner_radius=14)
    if title:
        ctk.CTkLabel(f, text=title, font=ctk.CTkFont(FONT, 15, "bold"), text_color=TXT
                     ).pack(anchor="w", padx=16, pady=(10, 2))
    return f


def btn(master, text, cmd, primary=True, **kw):
    if primary:
        return ctk.CTkButton(master, text=text, command=cmd, height=32, corner_radius=8, fg_color=ACCENT,
                             hover_color=ACCENT_H, text_color="#FFFFFF", font=ctk.CTkFont(FONT, 13, "bold"), **kw)
    return ctk.CTkButton(master, text=text, command=cmd, height=32, corner_radius=8, fg_color=SOFT, hover_color=SOFT_H,
                         text_color=TXT, font=ctk.CTkFont(FONT, 13), border_width=0, **kw)


def entry(master, **kw):
    return ctk.CTkEntry(master, height=30, corner_radius=8, fg_color="#FAFBFE", border_color=BORDER,
                        text_color=TXT, font=ctk.CTkFont(FONT, 13), **kw)


def chk(master, text, var):
    return ctk.CTkCheckBox(master, text=text, variable=var, text_color=TXT, fg_color=ACCENT, hover_color=ACCENT_H,
                           border_color="#B4BCD6", corner_radius=6, checkbox_width=20, checkbox_height=20,
                           font=ctk.CTkFont(FONT, 13))


class ConnForm(ctk.CTkFrame):
    """Карточка подключения по SSH со списком сохранённых серверов."""
    NEW = "+ Новый сервер"

    def __init__(self, master, app, title):
        super().__init__(master, fg_color=CARD, border_color=BORDER, border_width=1, corner_radius=14)
        self.app = app
        self.columnconfigure(1, weight=1)
        ctk.CTkLabel(self, text=title, font=ctk.CTkFont(FONT, 15, "bold"), text_color=TXT
                     ).grid(row=0, column=0, columnspan=3, sticky="w", padx=16, pady=(10, 4))
        self.r = 1
        pf = ctk.CTkFrame(self, fg_color="transparent")
        self.prof = ctk.CTkComboBox(pf, values=[self.NEW], state="readonly", command=self._pick_profile, height=32,
                                    corner_radius=6, border_color=BORDER, button_color=ACCENT,
                                    button_hover_color=ACCENT_H, fg_color="#FFFFFF", text_color=TXT,
                                    dropdown_fg_color="#FFFFFF", dropdown_text_color=TXT)
        self.prof.pack(side="left", fill="x", expand=True); self.prof.set(self.NEW)
        btn(pf, "💾 Сохранить", self._save_profile, width=110).pack(side="left", padx=(6, 0))
        btn(pf, "🗑", self._del_profile, primary=False, width=40).pack(side="left", padx=(6, 0))
        self.row("Сервер:", pf)
        nh = ctk.CTkFrame(self, fg_color="transparent")
        self.name = entry(nh, placeholder_text="например: Магазин"); self.name.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(nh, text="IP / хост:", text_color=MUTED).pack(side="left", padx=(12, 6))
        self.host = entry(nh); self.host.pack(side="left", fill="x", expand=True)
        self.row("Название:", nh)
        hp = ctk.CTkFrame(self, fg_color="transparent")
        self.port = entry(hp, width=80); self.port.insert(0, "22"); self.port.pack(side="left")
        ctk.CTkLabel(hp, text="Пользователь:", text_color=MUTED).pack(side="left", padx=(14, 6))
        self.user = entry(hp, width=140); self.user.insert(0, "root"); self.user.pack(side="left")
        ctk.CTkLabel(hp, text="Вход:", text_color=MUTED).pack(side="left", padx=(14, 6))
        self.auth = ctk.StringVar(value="password")
        self.authsw = ctk.CTkSegmentedButton(hp, values=["Пароль", "SSH-ключ"], command=self._auth_sw, height=30,
                                             selected_color=ACCENT, selected_hover_color=ACCENT_H,
                                             unselected_color=SOFT, unselected_hover_color=SOFT_H, text_color=TXT,
                                             fg_color=SOFT)
        self.authsw.set("Пароль"); self.authsw.pack(side="left")
        self.row("Порт:", hp)
        self.pwd_lbl = ctk.CTkLabel(self, text="Пароль:", text_color=MUTED)
        self.pwd_lbl.grid(row=self.r, column=0, sticky="w", padx=(14, 8), pady=3)
        self.pwd = entry(self, show="•"); self.pwd.grid(row=self.r, column=1, columnspan=2, sticky="ew", padx=(0, 14), pady=3)
        self.kf = kf = ctk.CTkFrame(self, fg_color="transparent")
        self.key = entry(kf); self.key.pack(side="left", fill="x", expand=True)
        self.keybtn = btn(kf, "Обзор", self._pick_key, primary=False, width=70); self.keybtn.pack(side="left", padx=(6, 0))
        kf.grid(row=self.r, column=1, columnspan=2, sticky="ew", padx=(0, 14), pady=3)
        self.kpwd = entry(kf, show="•", width=150, placeholder_text="фраза (если есть)"); self.kpwd.pack(side="left", padx=(6, 0))
        self.kpwd.bind("<KeyRelease>", lambda e: (self.pwd.delete(0, "end"), self.pwd.insert(0, self.kpwd.get())))
        self.r += 1; ctk.CTkFrame(self, height=4, fg_color="transparent").grid(row=self.r, column=0)
        self._auth()

    def row(self, label, w, sticky="ew"):
        ctk.CTkLabel(self, text=label, text_color=MUTED).grid(row=self.r, column=0, sticky="w", padx=(14, 8), pady=3)
        w.grid(row=self.r, column=1, columnspan=2, sticky=sticky, padx=(0, 14), pady=3); self.r += 1
        return w

    def _auth_sw(self, v):
        self.auth.set("key" if v == "SSH-ключ" else "password"); self._auth()

    def _auth(self):
        k = self.auth.get() == "key"
        self.authsw.set("SSH-ключ" if k else "Пароль")
        self.pwd_lbl.configure(text="Файл ключа:" if k else "Пароль:")
        if k:
            self.pwd.grid_remove(); self.kf.grid()
            self.kpwd.delete(0, "end"); self.kpwd.insert(0, self.pwd.get())
        else:
            self.kf.grid_remove(); self.pwd.grid()

    def _pick_key(self):
        p = filedialog.askopenfilename(title="SSH-ключ")
        if p: self.key.delete(0, "end"); self.key.insert(0, p)

    def kr_id(self): return f"{self.user.get().strip()}@{self.host.get().strip()}:{self.port.get().strip()}"

    def label(self): return self.name.get().strip() or self.host.get().strip()

    def cfg(self):
        return dict(host=self.host.get().strip(), port=int(self.port.get() or 22), user=self.user.get().strip(),
                    auth=self.auth.get(), pwd=self.pwd.get(), key=self.key.get(), name=self.label())

    def dump(self):
        if keyring and self.pwd.get() and self.host.get().strip():
            try: keyring.set_password(APP, self.kr_id(), self.pwd.get())
            except Exception as e: self.app.q.put(("log", f"[!] Не удалось сохранить пароль: {e}"))
        return {"name": self.name.get().strip(), "host": self.host.get().strip(), "port": self.port.get().strip(),
                "user": self.user.get().strip(), "auth": self.auth.get(), "key": self.key.get()}

    def load(self, d):
        self.name.delete(0, "end"); self.name.insert(0, d.get("name", ""))
        for w, k, dv in [(self.host, "host", ""), (self.port, "port", "22"), (self.user, "user", "root"), (self.key, "key", "")]:
            w.configure(state="normal"); w.delete(0, "end"); w.insert(0, d.get(k, dv))
        self.auth.set(d.get("auth", "password")); self.pwd.delete(0, "end")
        if keyring and d.get("host"):
            try:
                p = keyring.get_password(APP, self.kr_id())
                if p: self.pwd.insert(0, p)
            except Exception: pass
        self._auth()

    # ----- профили (общие для обеих вкладок) -----
    def refresh(self):
        names = sorted(self.app.servers, key=str.lower)
        self.prof.configure(values=[self.NEW] + names)
        cur = self.label()
        self.prof.set(cur if cur in self.app.servers else self.NEW)

    def _pick_profile(self, v):
        if v == self.NEW:
            self.load({}); self.name.focus()
        else:
            self.load(self.app.servers.get(v, {}))

    def _save_profile(self):
        if not self.host.get().strip(): return messagebox.showerror("Ошибка", "Укажите IP или хост сервера")
        if not self.name.get().strip(): self.name.insert(0, self.host.get().strip())
        d = self.dump(); old = self.prof.get()
        if old not in (self.NEW, d["name"]) and old in self.app.servers and \
                messagebox.askyesno("VPS Backup", f"Переименовать «{old}» в «{d['name']}»?"):
            self.app.servers.pop(old, None)
        prev = self.app.servers.get(old) or self.app.servers.get(d["name"]) or {}
        d["note"] = prev.get("note", ""); d["keep"] = prev.get("keep", 3); d["added"] = prev.get("added") or time.strftime("%Y-%m-%d")
        self.app.servers[d["name"]] = d; self.app.save_servers()
        self.app.q.put(("log", f"[+] Сервер «{d['name']}» сохранён в список"))

    def _del_profile(self):
        n = self.prof.get()
        if n == self.NEW or n not in self.app.servers: return
        if not messagebox.askyesno("VPS Backup", f"Удалить сервер «{n}» из списка?"): return
        d = self.app.servers.pop(n)
        if keyring:
            try: keyring.delete_password(APP, f"{d.get('user')}@{d.get('host')}:{d.get('port')}")
            except Exception: pass
        self.app.save_servers(); self.load({})


NET_ERRORS = (socket.error, socket.timeout, EOFError, ConnectionError, paramiko.SSHException)


def net_msg(e):
    return ("Связь с сервером прервана (сменилась сеть, отключился VPN или интернет). "
            f"Недокачанный файл удалён — просто запустите заново. Подробности: {e or type(e).__name__}")


def connect(c, L):
    ssh = paramiko.SSHClient(); ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    L(f"[*] Подключение к {c['user']}@{c['host']}:{c['port']}...")
    kw = dict(hostname=c["host"], port=c["port"], username=c["user"], timeout=20, allow_agent=False, look_for_keys=False)
    if c["auth"] == "key": kw.update(key_filename=c["key"], passphrase=c["pwd"] or None)
    else: kw["password"] = c["pwd"]
    ssh.connect(**kw); ssh.get_transport().set_keepalive(10); L("[+] Подключено")
    return ssh


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("light")
        self.title("VPS Backup")
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w, h = min(1180, sw - 40), min(900, sh - 90)
        self.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h - 50) // 2)}")
        self.minsize(min(900, sw - 40), min(600, sh - 90))
        self.configure(fg_color=BG)
        try:
            ico = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "icon.ico"
            if ico.exists(): self.iconbitmap(str(ico))
        except Exception: pass
        self.servers = {}; self.sched = {}; self.sched_queue = []; self.auto_mode = False
        self.q = queue.Queue()
        self.busy = False
        self.cancel = threading.Event()
        self.chan = None
        self.archive_os = None
        self._ui()
        self._google_creds_load()
        self.load()
        self.after(100, self._poll)
        self.after(5000, self._sched_tick)
        if "--tray" in sys.argv: self.after(200, self.iconify)

    def _ui(self):
        # ----- боковое меню -----
        side = ctk.CTkFrame(self, fg_color=SIDE, corner_radius=0, width=230); side.pack(side="left", fill="y")
        side.pack_propagate(False)
        lg = ctk.CTkFrame(side, fg_color="transparent"); lg.pack(fill="x", padx=20, pady=(26, 30))
        ctk.CTkLabel(lg, text="🛡", font=ctk.CTkFont(FONT, 30), text_color=SIDE_T, width=46, height=46,
                     fg_color=ACCENT, corner_radius=12).pack(side="left")
        tt = ctk.CTkFrame(lg, fg_color="transparent"); tt.pack(side="left", padx=12)
        ctk.CTkLabel(tt, text="VPS Backup", font=ctk.CTkFont(FONT, 18, "bold"), text_color=SIDE_T).pack(anchor="w")
        ctk.CTkLabel(tt, text="версия 4.11", font=ctk.CTkFont(FONT, 11), text_color=SIDE_M).pack(anchor="w")
        main = ctk.CTkFrame(self, fg_color=BG, corner_radius=0); main.pack(side="left", fill="both", expand=True)
        self.page_title = ctk.CTkLabel(main, text="", font=ctk.CTkFont(FONT, 20, "bold"), text_color=TXT)
        self.page_title.pack(anchor="w", padx=28, pady=(12, 0))
        self.page_sub = ctk.CTkLabel(main, text="", font=ctk.CTkFont(FONT, 13), text_color=MUTED)
        self.page_sub.pack(anchor="w", padx=28, pady=(0, 4))
        host = ctk.CTkFrame(main, fg_color="transparent"); host.pack(fill="both", expand=True, padx=16)
        self.pages, self.navb = {}, {}
        pages = [("servers", "🖥   Мои серверы", "Все сохранённые серверы: что это за сервер, когда был последний бэкап"),
                 ("backup", "💾   Создать бэкап", "Резервная копия сервера на ваш компьютер"),
                 ("restore", "🚀   Восстановление", "Развернуть бэкап на другом сервере — безопасно"),
                 ("sched", "⏰   Расписание", "Автоматические бэкапы в заданное время"),
                 ("cloud", "☁   Облако", "Копия бэкапа в Яндекс Диск или Облако Mail.ru — настраивается один раз")]
        for key, label, sub in pages:
            pg = ctk.CTkScrollableFrame(host, fg_color="transparent", scrollbar_button_color=SOFT_H,
                                        scrollbar_button_hover_color="#C9D0E6")
            self.pages[key] = (pg, label.split("   ")[1], sub)
            b = ctk.CTkButton(side, text=label, anchor="w", height=44, corner_radius=10, fg_color="transparent",
                              hover_color=SIDE_H, text_color=SIDE_M, font=ctk.CTkFont(FONT, 14),
                              command=lambda k=key: self.show(k))
            b.pack(fill="x", padx=14, pady=3); self.navb[key] = b
        ctk.CTkLabel(side, text="Пароли хранятся в защищённом\nхранилище Windows", font=ctk.CTkFont(FONT, 11),
                     text_color=SIDE_M, justify="left").pack(side="bottom", anchor="w", padx=22, pady=20)
        tb, tr, ts = (self.pages[k][0] for k in ("backup", "restore", "sched"))
        self._main = main
        self._sched_ui(ts)
        self._cloud_ui(self.pages["cloud"][0])
        self._srv_ui(self.pages["servers"][0])

        # ----- вкладка бэкапа -----
        self.conn = ConnForm(tb, self, "Сервер"); self.conn.pack(fill="x", padx=12, pady=(0, 8))
        c = card(tb, "Что сохранить"); c.pack(fill="x", padx=12, pady=(0, 8))
        g = ctk.CTkFrame(c, fg_color="transparent"); g.pack(fill="x", padx=12, pady=(2, 6))
        g.columnconfigure((0, 1), weight=1)
        self.opt = {k: ctk.BooleanVar(value=True) for k in ("db", "web", "conf", "pkgs")}
        for i, (k, t) in enumerate([("db", "🗄  Базы данных (MySQL / PostgreSQL)"), ("web", "🌐  Файлы сайтов (/var/www, /home)"),
                                    ("conf", "⚙  Настройки служб (nginx, cron…)"), ("pkgs", "📦  Список программ")]):
            chk(g, t, self.opt[k]).grid(row=i // 2, column=i % 2, sticky="w", padx=4, pady=3)
        self.slim = ctk.BooleanVar(value=True)
        chk(c, "⚡  Без кэша: пропускать node_modules, кэши и логи (архив в разы меньше)", self.slim).pack(anchor="w", padx=16, pady=(0, 6))
        df = ctk.CTkFrame(c, fg_color="transparent"); df.pack(fill="x", padx=14, pady=(0, 6))
        ctk.CTkLabel(df, text="Папка:", text_color=MUTED).pack(side="left", padx=(0, 8))
        self.dest = entry(df); self.dest.pack(side="left", fill="x", expand=True)
        btn(df, "Обзор", self._pick_dir, primary=False, width=70).pack(side="left", padx=(6, 0))
        self.to_cloud = ctk.BooleanVar(value=False)
        chk(c, "☁  Дублировать в облако (настройка — в разделе «Облако»)", self.to_cloud).pack(anchor="w", padx=16, pady=(0, 10))
        b = ctk.CTkFrame(tb, fg_color="transparent"); b.pack(fill="x", padx=12, pady=(0, 8))
        self.start = btn(b, "▶  Начать бэкап", self.run, width=180); self.start.pack(side="left")
        self.openb = btn(b, "📂  Открыть папку с бэкапами", self.open_dir, primary=False, state="disabled")
        self.openb.pack(side="left", padx=8)
        self.poweroff = ctk.BooleanVar(value=False)
        chk(tb, "⏻  Выключить компьютер после успешного бэкапа", self.poweroff).pack(anchor="w", padx=16, pady=(0, 8))

        # ----- вкладка восстановления -----
        af = card(tr, "Файл бэкапа"); af.pack(fill="x", padx=12, pady=(0, 8))
        r = ctk.CTkFrame(af, fg_color="transparent"); r.pack(fill="x", padx=14, pady=(2, 6))
        self.arch = entry(r); self.arch.pack(side="left", fill="x", expand=True)
        btn(r, "Обзор", self._pick_archive, primary=False, width=70).pack(side="left", padx=(6, 0))
        btn(r, "🔍 Проверить целостность", self._verify_click, primary=False, width=170).pack(side="left", padx=(6, 0))
        self.oslbl = ctk.CTkLabel(af, text="Выберите файл — программа покажет, с какого он сервера", justify="left",
                                  anchor="w", text_color=MUTED, fg_color=SOFT, corner_radius=8)
        self.oslbl.pack(fill="x", padx=14, pady=(0, 8), ipady=4, ipadx=8)
        self.rconn = ConnForm(tr, self, "Новый сервер (куда разворачивать)"); self.rconn.pack(fill="x", padx=12, pady=(0, 8))
        rc = card(tr, "Что восстановить"); rc.pack(fill="x", padx=12, pady=(0, 8))
        self.ropt = {k: ctk.BooleanVar(value=v) for k, v in (("clp", False), ("web", True), ("conf", True), ("db", True), ("npm", True))}
        for k, t in [("clp", "🧩  Чистый сервер: сначала установить CloudPanel (Nginx, MySQL, PHP)"),
                     ("web", "🌐  Файлы сайтов (/var/www, /home)"),
                     ("conf", "⚙  Настройки служб (/etc/...)"),
                     ("db", "🗄  Базы данных (MySQL / PostgreSQL)")]:
            pass
        rg = ctk.CTkFrame(rc, fg_color="transparent"); rg.pack(fill="x", padx=12, pady=(0, 8)); rg.columnconfigure((0, 1), weight=1)
        rl = [("clp", "🧩  Чистый сервер: установить CloudPanel"), ("web", "🌐  Файлы сайтов (/var/www, /home)"),
              ("conf", "⚙  Настройки служб (/etc/...)"), ("db", "🗄  Базы данных (MySQL / PostgreSQL)")]
        for i, (k, t) in enumerate(rl):
            chk(rg, t, self.ropt[k]).grid(row=i // 2, column=i % 2, sticky="w", padx=4, pady=3)
        rb = ctk.CTkFrame(tr, fg_color="transparent"); rb.pack(fill="x", padx=12, pady=(0, 8))
        self.checkb = btn(rb, "🔍  Проверить сервер", lambda: self.restore(check_only=True), primary=False)
        self.checkb.pack(side="left")
        self.rstart = btn(rb, "▶  Начать восстановление", self.restore, width=200); self.rstart.pack(side="left", padx=8)

        # ----- общая нижняя часть -----
        bot = card(self._main); bot.pack(fill="x", padx=28, pady=(4, 12))
        top = ctk.CTkFrame(bot, fg_color="transparent"); top.pack(fill="x", padx=12, pady=(8, 2))
        self.stopb = ctk.CTkButton(top, text="■  Остановить", height=30, corner_radius=8, state="disabled",
                                   fg_color=ERR, hover_color="#C93A3F", text_color="#FFFFFF", command=self.stop,
                                   font=ctk.CTkFont(FONT, 13, "bold"))
        self.stopb.pack(side="left")
        self.remember = ctk.BooleanVar(value=True)
        chk(top, "Запоминать настройки", self.remember).pack(side="left", padx=12)
        self.status = ctk.CTkLabel(top, text="Готов к работе", text_color=MUTED); self.status.pack(side="right")
        self.bar = ctk.CTkProgressBar(bot, height=6, corner_radius=3, progress_color=ACCENT, fg_color=SOFT)
        self.bar.pack(fill="x", padx=12, pady=4); self.bar.set(0)
        self.log = ctk.CTkTextbox(bot, height=110, font=("Cascadia Mono", 12), fg_color="#FAFBFE", text_color=TXT,
                                  border_width=0, corner_radius=10)
        self.log.pack(fill="both", expand=True, padx=12, pady=(2, 10))
        for tag, col in (("ok", OK), ("warn", WARN), ("err", ERR), ("info", ACCENT)):
            self.log.tag_config(tag, foreground=col)
        self.show("backup")

    def show(self, key):
        for k, (pg, _, _) in self.pages.items():
            pg.pack_forget()
            self.navb[k].configure(fg_color=SIDE_H if k == key else "transparent",
                                   text_color=SIDE_T if k == key else SIDE_M)
        pg, t, sub = self.pages[key]
        pg.pack(fill="both", expand=True)
        if key == "servers": self._srv_fill()
        self.page_title.configure(text=t); self.page_sub.configure(text=sub)

    # ----- страница «Мои серверы» -----
    def _srv_ui(self, pg):
        top = ctk.CTkFrame(pg, fg_color="transparent"); top.pack(fill="x", padx=12, pady=(0, 8))
        btn(top, "＋  Добавить сервер", self._srv_new, width=180).pack(side="left")
        self.srv_count = ctk.CTkLabel(top, text="", text_color=MUTED); self.srv_count.pack(side="left", padx=12)
        self.srv_list = ctk.CTkFrame(pg, fg_color="transparent"); self.srv_list.pack(fill="x")

    def _srv_new(self):
        self.conn.prof.set(ConnForm.NEW); self.conn.load({}); self.show("backup"); self.conn.name.focus()

    def _srv_stats(self, n):
        """Бэкапы этого сервера в папке на компьютере: (последний, количество, общий размер)."""
        try:
            d = Path(self.dest.get().strip() or ".")
            fs = sorted(d.glob(f"vps_backup_{safe_name(n)}_*.tar.gz"), key=lambda p: p.stat().st_mtime)
        except Exception: return None, 0, 0
        return (fs[-1] if fs else None), len(fs), sum(p.stat().st_size for p in fs)

    def _srv_note(self, n, tb):
        if n in self.servers:
            self.servers[n]["note"] = tb.get("1.0", "end").strip(); self.save()

    def _srv_keep(self, n, v):
        if n in self.servers:
            self.servers[n]["keep"] = 0 if v == "все" else int(v); self.save()

    def _keep_of(self, server):
        try: return int((self.servers.get(server) or {}).get("keep", 3))
        except (TypeError, ValueError): return 3

    def _copies(self, dest, server):
        try: return sorted(Path(dest).glob(f"vps_backup_{safe_name(server)}_*.tar.gz"), key=lambda p: p.stat().st_mtime)
        except Exception: return []

    def _rotate(self, dest, server, keep, L, protect=None):
        """Оставляет только keep последних копий сервера, остальные (самые старые) удаляет."""
        if keep <= 0: return
        fs = [p for p in self._copies(dest, server) if p != protect]
        extra = len(fs) - (keep - 1 if protect else keep)
        for p in fs[:max(0, extra)]:
            try:
                sz = p.stat().st_size; p.unlink()
                L(f"[i] Ротация: удалена старая копия {p.name} ({human(sz)}), храним последних: {keep}")
            except Exception as e: L(f"[!] Не удалось удалить старую копию {p.name}: {e}")

    def _srv_use(self, n, where):
        f = self.conn if where == "backup" else self.rconn
        f.prof.set(n); f.load(self.servers.get(n, {})); self.show(where)

    def _srv_del(self, n):
        self.conn.prof.set(n); self.conn._del_profile()

    def _srv_fill(self):
        for w in self.srv_list.winfo_children(): w.destroy()
        names = sorted(self.servers, key=str.lower)
        self.srv_count.configure(text=f"Серверов в списке: {len(names)}")
        if not names:
            c = card(self.srv_list, "Список пуст"); c.pack(fill="x", padx=12, pady=(0, 8))
            ctk.CTkLabel(c, text="Нажмите «Добавить сервер», заполните подключение и нажмите «💾 Сохранить».",
                         text_color=MUTED).pack(anchor="w", padx=16, pady=(0, 12))
            return
        for n in names:
            d = self.servers[n]
            c = ctk.CTkFrame(self.srv_list, fg_color=CARD, border_color=BORDER, border_width=1, corner_radius=14)
            c.pack(fill="x", padx=12, pady=(0, 8)); c.columnconfigure(1, weight=1)
            ctk.CTkLabel(c, text="🖥", font=ctk.CTkFont(FONT, 22), width=44, height=44, fg_color=SOFT,
                         corner_radius=10).grid(row=0, column=0, rowspan=3, sticky="n", padx=(14, 12), pady=12)
            ctk.CTkLabel(c, text=n, font=ctk.CTkFont(FONT, 15, "bold"), text_color=TXT
                         ).grid(row=0, column=1, sticky="w", pady=(10, 0))
            auth = "по ключу" if d.get("auth") == "key" else "по паролю"
            last, cnt, size = self._srv_stats(n)
            info = f"{d.get('user', 'root')}@{d.get('host', '')}:{d.get('port', '22')}   ·   вход {auth}"
            if d.get("added"): info += f"   ·   добавлен {d['added']}"
            ctk.CTkLabel(c, text=info, text_color=MUTED).grid(row=1, column=1, sticky="w")
            if last:
                bt, col = (f"✔ Последний бэкап: {time.strftime('%d.%m.%Y %H:%M', time.localtime(last.stat().st_mtime))}"
                           f"   ·   копий в папке: {cnt} ({size/1073741824:.1f} ГБ)" if size >= 107374182 else f"   ·   копий в папке: {cnt} ({size/1048576:.0f} МБ)"), OK
            else: bt, col = "Бэкапов в папке пока нет", WARN
            kr = ctk.CTkFrame(c, fg_color="transparent"); kr.grid(row=2, column=1, sticky="w")
            ctk.CTkLabel(kr, text=bt, text_color=col).pack(side="left")
            ctk.CTkLabel(kr, text="   ·   хранить копий:", text_color=MUTED).pack(side="left")
            kv = {0: "все"}.get(int(d.get("keep", 3)), str(d.get("keep", 3)))
            km = ctk.CTkOptionMenu(kr, values=["1", "2", "3", "5", "7", "10", "все"], width=70, height=24,
                                   command=lambda v, n=n: self._srv_keep(n, v))
            km.set(kv); km.pack(side="left", padx=6)
            tb = ctk.CTkTextbox(c, height=46, fg_color="#FAFBFE", border_color=BORDER, border_width=1,
                                corner_radius=8, text_color=TXT, font=ctk.CTkFont(FONT, 12), wrap="word")
            tb.grid(row=3, column=1, sticky="ew", pady=(6, 4))
            if d.get("note"): tb.insert("1.0", d["note"])
            else:
                ph = "Комментарий: что это за сервер (например: интернет-магазин, Timeweb)"
                tb.insert("1.0", ph); tb.configure(text_color=MUTED)
                def _clr(e, tb=tb, ph=ph):
                    if tb.get("1.0", "end").strip() == ph: tb.delete("1.0", "end"); tb.configure(text_color=TXT)
                tb.bind("<FocusIn>", _clr)
                tb.bind("<FocusOut>", lambda e, n=n, tb=tb, ph=ph:
                        None if tb.get("1.0", "end").strip() in ("", ph) else self._srv_note(n, tb))
            if d.get("note"): tb.bind("<FocusOut>", lambda e, n=n, tb=tb: self._srv_note(n, tb))
            b = ctk.CTkFrame(c, fg_color="transparent"); b.grid(row=0, column=2, rowspan=4, sticky="ne", padx=14, pady=12)
            btn(b, "💾  Бэкап", lambda n=n: self._srv_use(n, "backup"), width=170).pack(pady=(0, 6))
            btn(b, "🚀  Восстановить сюда", lambda n=n: self._srv_use(n, "restore"), primary=False, width=170).pack(pady=(0, 6))
            btn(b, "🗑  Удалить", lambda n=n: self._srv_del(n), primary=False, width=170).pack()
            ctk.CTkFrame(c, height=6, fg_color="transparent").grid(row=4, column=1)

    def _sched_ui(self, ts):
        c = card(ts, "Автоматический бэкап по расписанию"); c.pack(fill="x", padx=12, pady=(0, 8))
        ctk.CTkLabel(c, text="Программа сама начнёт бэкап в выбранное время, если она запущена (можно свернуть). "
                     "Пароли берутся из сохранённого списка серверов.", text_color=MUTED, wraplength=800,
                     justify="left").pack(anchor="w", padx=14, pady=(0, 8))
        self.s_on = ctk.BooleanVar(value=False)
        chk(c, "Включить бэкап по расписанию", self.s_on).pack(anchor="w", padx=16, pady=4)
        r = ctk.CTkFrame(c, fg_color="transparent"); r.pack(fill="x", padx=16, pady=6)
        ctk.CTkLabel(r, text="Время (ЧЧ:ММ):", text_color=MUTED).pack(side="left")
        self.s_time = entry(r, width=80); self.s_time.insert(0, "03:00"); self.s_time.pack(side="left", padx=8)
        ctk.CTkLabel(r, text="Дни:", text_color=MUTED).pack(side="left", padx=(16, 6))
        self.s_days = [ctk.BooleanVar(value=True) for _ in DAYS]
        for i, d in enumerate(DAYS):
            ctk.CTkCheckBox(r, text=d, variable=self.s_days[i], width=52, text_color=TXT, fg_color=ACCENT,
                            hover_color=ACCENT_H, checkbox_width=18, checkbox_height=18).pack(side="left")
        ctk.CTkLabel(c, text="Какие серверы бэкапить:", text_color=MUTED).pack(anchor="w", padx=16, pady=(8, 2))
        self.s_list = ctk.CTkFrame(c, fg_color=SOFT, corner_radius=8); self.s_list.pack(fill="x", padx=16, pady=(0, 6))
        self.s_srv = {}
        self.s_info = ctk.CTkLabel(c, text="", text_color=MUTED); self.s_info.pack(anchor="w", padx=16, pady=(0, 6))
        b = ctk.CTkFrame(c, fg_color="transparent"); b.pack(fill="x", padx=16, pady=(0, 12))
        btn(b, "💾  Сохранить расписание", self._sched_save, width=200).pack(side="left")
        btn(b, "▶  Проверить сейчас", lambda: self._sched_fire(manual=True), primary=False).pack(side="left", padx=8)
        ctk.CTkLabel(c, text="Что сохранять и в какую папку — берётся с вкладки «Создать бэкап».",
                     text_color=MUTED).pack(anchor="w", padx=16, pady=(0, 10))

        a = card(ts, "Запуск вместе с Windows"); a.pack(fill="x", padx=12, pady=(0, 8))
        self.auto = ctk.BooleanVar(value=get_autostart())
        chk(a, "Запускать программу при входе в Windows (свёрнутой)", self.auto).pack(anchor="w", padx=16, pady=4)
        self.auto.trace_add("write", lambda *_: self._toggle_autostart())
        ctk.CTkLabel(a, text="Чтобы бэкап по расписанию срабатывал, компьютер должен быть включён в это время.",
                     text_color=MUTED).pack(anchor="w", padx=16, pady=(0, 12))

    def _sched_servers(self):
        for w in self.s_list.winfo_children(): w.destroy()
        prev = {k: v.get() for k, v in self.s_srv.items()}
        chosen = set(self.sched.get("servers", []))
        self.s_srv = {}
        if not self.servers:
            ctk.CTkLabel(self.s_list, text="Список пуст — сохраните сервер кнопкой «💾 Сохранить» на вкладке бэкапа",
                         text_color=WARN).pack(anchor="w", padx=10, pady=8); return
        for n in sorted(self.servers, key=str.lower):
            v = ctk.BooleanVar(value=prev.get(n, n in chosen)); self.s_srv[n] = v
            h = self.servers[n].get("host", "")
            chk(self.s_list, f"{n}  ({h})" if h and h != n else n, v).pack(anchor="w", padx=10, pady=4)

    def _sched_load(self, d):
        self.sched = d.get("schedule", {}) or {}
        self.s_on.set(bool(self.sched.get("on")))
        self.s_time.delete(0, "end"); self.s_time.insert(0, self.sched.get("time", "03:00"))
        days = self.sched.get("days", list(range(7)))
        for i, v in enumerate(self.s_days): v.set(i in days)
        self._sched_servers(); self._sched_info()

    def _sched_save(self):
        try: parse_hhmm(self.s_time.get())
        except Exception: return messagebox.showerror("Ошибка", "Время укажите в виде ЧЧ:ММ, например 03:00")
        servers = [n for n, v in self.s_srv.items() if v.get()]
        if self.s_on.get() and not servers: return messagebox.showerror("Ошибка", "Отметьте хотя бы один сервер")
        self.sched.update(on=self.s_on.get(), time=self.s_time.get().strip(), servers=servers,
                          days=[i for i, v in enumerate(self.s_days) if v.get()])
        self.save(); self._sched_info()
        self.q.put(("log", "[+] Расписание сохранено" if self.s_on.get() else "[i] Расписание выключено"))

    def _sched_info(self):
        sc = self.sched
        if not sc.get("on"): t = "Расписание выключено"
        else:
            t = f"Включено: в {sc.get('time')} ({', '.join(DAYS[i] for i in sc.get('days', []))}), " \
                f"серверов: {len(sc.get('servers', []))}"
            if sc.get("last"): t += f"  •  последний запуск: {sc['last']}"
        self.s_info.configure(text=t, text_color=OK if sc.get("on") else MUTED)

    # ---------- облако ----------
    def _cloud_ui(self, pg):
        c = card(pg, "Облачное хранилище"); c.pack(fill="x", padx=12, pady=(0, 8))
        ctk.CTkLabel(c, text="После каждого бэкапа программа сама отправит копию в облако (и при бэкапе по расписанию). "
                     "Если интернет пропадёт — копия на компьютере останется целой.", text_color=MUTED,
                     wraplength=800, justify="left").pack(anchor="w", padx=16, pady=(0, 8))
        self.c_prov = ctk.StringVar(value="Яндекс Диск")
        ctk.CTkSegmentedButton(c, values=list(cloud.PROVIDERS.values()), variable=self.c_prov, height=36,
                               selected_color=ACCENT, selected_hover_color=ACCENT_H, font=ctk.CTkFont(FONT, 13),
                               command=lambda *_: self._cloud_hint()).pack(anchor="w", padx=16, pady=6)
        g = ctk.CTkFrame(c, fg_color="transparent"); g.pack(fill="x", padx=16, pady=4); g.columnconfigure(1, weight=1)
        rows = [("Почта / логин:", "c_login", {}), ("Пароль приложения:", "c_pwd", {"show": "•"}),
                ("Папка в облаке:", "c_folder", {})]
        for i, (t, attr, kw) in enumerate(rows):
            ctk.CTkLabel(g, text=t, text_color=MUTED).grid(row=i, column=0, sticky="w", pady=4, padx=(0, 10))
            e = entry(g, **kw); e.grid(row=i, column=1, sticky="ew", pady=4); setattr(self, attr, e)
        self.c_folder.insert(0, "/VPS_Backups")
        r = ctk.CTkFrame(c, fg_color="transparent"); r.pack(fill="x", padx=16, pady=4)
        ctk.CTkLabel(r, text="Хранить последних копий каждого сервера:", text_color=MUTED).pack(side="left")
        self.c_keep = entry(r, width=60); self.c_keep.insert(0, "5"); self.c_keep.pack(side="left", padx=8)
        ctk.CTkLabel(r, text="(старые удаляются сами, 0 — не удалять)", text_color=MUTED).pack(side="left")
        self.g_row = ctk.CTkFrame(c, fg_color="transparent")
        gr = ctk.CTkFrame(self.g_row, fg_color="transparent"); gr.pack(fill="x", padx=16, pady=(4, 0))
        gr.columnconfigure(1, weight=1)
        ctk.CTkLabel(gr, text="Client ID:", text_color=MUTED).grid(row=0, column=0, sticky="w", pady=3, padx=(0, 10))
        self.g_cid = entry(gr); self.g_cid.grid(row=0, column=1, sticky="ew", pady=3)
        ctk.CTkLabel(gr, text="Client Secret:", text_color=MUTED).grid(row=1, column=0, sticky="w", pady=3, padx=(0, 10))
        self.g_secret = entry(gr, show="•"); self.g_secret.grid(row=1, column=1, sticky="ew", pady=3)
        gr2 = ctk.CTkFrame(self.g_row, fg_color="transparent"); gr2.pack(fill="x", padx=16, pady=4)
        btn(gr2, "💾  Сохранить коды Google", self._google_creds_save, primary=False).pack(side="left")
        self.g_stat = ctk.CTkLabel(gr2, text="", text_color=MUTED); self.g_stat.pack(side="left", padx=8)
        self.c_hint = ctk.CTkLabel(c, text="", justify="left", anchor="w", text_color=TXT, fg_color=SOFT,
                                   corner_radius=8, wraplength=780)
        self.c_hint.pack(fill="x", padx=16, pady=8, ipady=8, ipadx=8)
        b = ctk.CTkFrame(c, fg_color="transparent"); b.pack(fill="x", padx=16, pady=(0, 14))
        self.g_btn = btn(b, "🔑  Войти через Google", self._google_login, width=200)
        btn(b, "🔌  Проверить подключение", self._cloud_test, width=220).pack(side="left")
        btn(b, "💾  Сохранить", self._cloud_save, primary=False).pack(side="left", padx=8)
        self.c_stat = ctk.CTkLabel(b, text="", text_color=MUTED); self.c_stat.pack(side="left", padx=8)
        self._cloud_hint()

    def _cloud_key(self):
        return {v: k for k, v in cloud.PROVIDERS.items()}.get(self.c_prov.get(), "yandex")

    def _google_login(self):
        if not cloud.google_ready():
            return messagebox.showinfo("Google Диск", "Сначала вставьте коды приложения в поля «Client ID» и "
                                       "«Client Secret» выше, нажмите «Сохранить коды Google», затем «Войти через Google».")
        self.c_stat.configure(text="Открыл окно Google в браузере — разрешите доступ...", text_color=MUTED)
        def job():
            try:
                email, tok = cloud.google_login()
                def ok():
                    for w in (self.c_login, self.c_pwd): w.configure(state="normal")
                    self.c_login.delete(0, "end"); self.c_login.insert(0, email)
                    self.c_pwd.delete(0, "end"); self.c_pwd.insert(0, tok)
                    self._cloud_save(quiet=True); self._cloud_hint(); self._cloud_test()
                self.after(0, ok)
            except Exception as e:
                t = str(e) if isinstance(e, cloud.CloudError) else net_msg(e)
                self.after(0, lambda: self.c_stat.configure(text=f"✗ {t}", text_color=ERR))
        threading.Thread(target=job, daemon=True).start()

    def _cloud_hint(self):
        g = self._cloud_key() == "google"
        if hasattr(self, "g_btn"):
            if g: self.g_btn.pack(side="left", padx=(0, 8), before=self.g_btn.master.winfo_children()[1])
            else: self.g_btn.pack_forget()
        if hasattr(self, "g_row"):
            if g: self.g_row.pack(fill="x", before=self.c_hint)
            else: self.g_row.pack_forget()
        for w in (self.c_login, self.c_pwd):
            w.configure(state="disabled" if g else "normal")
        if g:
            t = ("Как подключить (один раз, ~5 минут):\n1. console.cloud.google.com → создайте проект → «API и сервисы» "
                 "→ «Библиотека» → включите Google Drive API.\n2. «Экран согласия OAuth» → External → добавьте свою почту "
                 "как тестового пользователя.\n3. «Учётные данные» → «Создать учётные данные» → «ID клиента OAuth» → "
                 "«Приложение для компьютера».\n4. Скопируйте Client ID и Client Secret в поля выше → «Сохранить коды "
                 "Google».\n5. Нажмите «Войти через Google» и разрешите доступ. Если Google напишет «Приложение не "
                 "проверено» — «Дополнительно» → «Перейти в VPS Backup».\nПрограмма видит только свои файлы на Диске. "
                 "Бесплатно 15 ГБ (общие с Gmail и Фото). При обрыве связи загрузка продолжается с того же места.")
            self.c_hint.configure(text=t); return
        if self._cloud_key() == "yandex":
            t = ("Как получить пароль приложения (1 минута):\n1. Откройте id.yandex.ru → «Безопасность» → «Пароли приложений».\n"
                 "2. Создайте пароль для «Файлы (WebDAV)» и вставьте его сюда.\nЛогин — ваша почта Яндекса. Работает бесплатно.")
        else:
            t = ("Как получить пароль приложения (1 минута):\n1. Откройте почту Mail.ru → Настройки → «Безопасность» → "
                 "«Пароли для внешних приложений».\n2. Создайте пароль (доступ: «Полный») и вставьте его сюда.\n"
                 "Работает на бесплатном тарифе. Обычный пароль от почты не подойдёт.")
        self.c_hint.configure(text=t)

    def _cloud_cfg(self):
        try: keep = max(0, int(self.c_keep.get() or 0))
        except ValueError: keep = 5
        return dict(provider=self._cloud_key(), login=self.c_login.get().strip(),
                    folder=self.c_folder.get().strip() or "/VPS_Backups", keep=keep)

    def _cloud_pwd(self, cc):
        if not keyring: return ""
        try: return keyring.get_password(APP, f"cloud:{cc['provider']}:{cc['login']}") or ""
        except Exception: return ""

    def _google_creds_load(self):
        raw = ""
        if keyring:
            try: raw = keyring.get_password(APP, "google_oauth") or ""
            except Exception: raw = ""
        if not raw:
            try:
                f = CFG_DIR / "google_oauth.json"
                if f.exists(): raw = f.read_text(encoding="utf-8")
            except Exception: raw = ""
        try: d = json.loads(raw) if raw else {}
        except Exception: d = {}
        cloud.set_google_credentials(d.get("client_id", ""), d.get("client_secret", ""))
        for e, k in ((self.g_cid, "client_id"), (self.g_secret, "client_secret")):
            if d.get(k) and not e.get(): e.insert(0, d[k])

    def _google_creds_save(self):
        cid, cs = self.g_cid.get().strip(), self.g_secret.get().strip()
        if not cid or not cs:
            return messagebox.showerror("Google Диск", "Заполните оба поля: Client ID и Client Secret.")
        cloud.set_google_credentials(cid, cs)
        raw = json.dumps({"client_id": cid, "client_secret": cs})
        if keyring:
            try: keyring.set_password(APP, "google_oauth", raw)
            except Exception: pass
        else:
            try:
                CFG_DIR.mkdir(parents=True, exist_ok=True)
                (CFG_DIR / "google_oauth.json").write_text(raw, encoding="utf-8")
            except Exception: pass
        self.g_stat.configure(text="✓ Коды сохранены — теперь нажмите «Войти через Google»", text_color=OK)


    def _cloud_save(self, quiet=False):
        cc = self._cloud_cfg(); pwd = self.c_pwd.get()
        if keyring and cc["login"] and pwd:
            try: keyring.set_password(APP, f"cloud:{cc['provider']}:{cc['login']}", pwd)
            except Exception: pass
        self.cloudcfg = cc; self.save()
        if not quiet: self.c_stat.configure(text="Сохранено", text_color=OK)

    def _cloud_load(self, d):
        cc = d.get("cloud") or {}; self.cloudcfg = cc
        self.c_prov.set(cloud.PROVIDERS.get(cc.get("provider", "yandex"), "Яндекс Диск"))
        for w in (self.c_login, self.c_pwd): w.configure(state="normal")
        for e, k, dv in ((self.c_login, "login", ""), (self.c_folder, "folder", "/VPS_Backups"), (self.c_keep, "keep", 5)):
            e.delete(0, "end"); e.insert(0, str(cc.get(k, dv)))
        if cc.get("login"):
            self.c_pwd.delete(0, "end"); self.c_pwd.insert(0, self._cloud_pwd(cc))
        self.to_cloud.set(bool(d.get("to_cloud")))
        self._cloud_hint()

    def _cloud_test(self):
        cc = self._cloud_cfg(); pwd = self.c_pwd.get()
        self.c_stat.configure(text="Проверяю...", text_color=MUTED)
        def job():
            try:
                c = cloud.open_cloud(cc["provider"], cc["login"], pwd); free, total = c.quota()
                t, col = (f"✓ Подключено. Свободно {gb(free)} из {gb(total)}" if total else "✓ Подключено"), OK
            except Exception as e:
                t, col = f"✗ {e if isinstance(e, cloud.CloudError) else net_msg(e)}", ERR
            self.after(0, lambda: self.c_stat.configure(text=t, text_color=col))
            if col == OK: self.after(0, lambda: self._cloud_save(quiet=True))
        threading.Thread(target=job, daemon=True).start()

    def _cloud_upload(self, path, server):
        cc = dict(self.cloudcfg or {}); L = lambda m: self.q.put(("log", m))
        pwd = self._cloud_pwd(cc) if cc.get("login") else ""
        if not cc.get("login") or not pwd:
            L("[!] Облако не настроено — копия осталась только на компьютере (раздел «Облако»)"); return
        def cb(done, total):
            self.q.put(("bar", done / total if total else 1))
            self.q.put(("status", f"Облако: {done/1048576:.0f} из {total/1048576:.0f} МБ"))
        try:
            cloud.send_backup(cc["provider"], cc["login"], pwd, cc.get("folder", "/VPS_Backups"), str(path),
                              f"vps_backup_{safe_name(server)}_", int(cc.get("keep", 5)), L, cb, self.cancel)
        except Exception as e:
            msg = e if isinstance(e, cloud.CloudError) else net_msg(e)
            L(f"[!] Не удалось отправить в облако: {msg}. Копия на компьютере сохранена.")

    def _toggle_autostart(self):
        try: set_autostart(self.auto.get())
        except Exception as e: messagebox.showerror("Ошибка", f"Не удалось изменить автозапуск: {e}")

    def _sched_tick(self):
        try:
            import datetime
            sc = self.sched
            if sc.get("on") and not self.busy and \
                    is_due(datetime.datetime.now(), sc.get("time", "03:00"), sc.get("days", []), sc.get("last_day")):
                self._sched_fire()
        except Exception as e:
            self.q.put(("log", f"[!] Ошибка расписания: {e}"))
        self.after(30000, self._sched_tick)

    def _sched_fire(self, manual=False):
        if self.busy: return
        names = [n for n in self.sched.get("servers", []) if n in self.servers] if not manual else \
            [n for n, v in self.s_srv.items() if v.get()]
        if not names:
            if manual: messagebox.showerror("Ошибка", "Отметьте серверы для расписания")
            return
        self.sched["last_day"] = time.strftime("%Y-%m-%d"); self.sched["last"] = time.strftime("%d.%m.%Y %H:%M")
        self.save(); self._sched_info()
        self.sched_queue = list(names); self.auto_mode = True
        self._begin(); self.q.put(("log", f"[i] Бэкап по расписанию: {', '.join(names)}"))
        self._sched_next()

    def _sched_next(self):
        n = self.sched_queue.pop(0)
        d = dict(self.servers[n]); pwd = ""
        if keyring:
            try: pwd = keyring.get_password(APP, f"{d.get('user')}@{d.get('host')}:{d.get('port')}") or ""
            except Exception: pass
        cfg = dict(host=d.get("host", ""), port=int(d.get("port") or 22), user=d.get("user") or "root",
                   auth=d.get("auth", "password"), pwd=pwd, key=d.get("key", ""), name=n,
                   dest=self.dest.get(), opt={k: v.get() for k, v in self.opt.items()}, cloud=self.to_cloud.get(), slim=self.slim.get())
        self.q.put(("log", f"\n===== Сервер «{n}» ====="))
        threading.Thread(target=self._worker, args=(cfg,), daemon=True).start()

    def _pick_dir(self):
        p = filedialog.askdirectory()
        if p: self.dest.delete(0, "end"); self.dest.insert(0, p)

    def _verify_click(self):
        p = self.arch.get().strip()
        if not p or not Path(p).is_file(): return messagebox.showerror("Ошибка", "Выберите файл бэкапа (.tar.gz)")
        if self.busy: return
        self.q.put(("log", f"[*] Проверяю целостность: {Path(p).name} (может занять несколько минут)..."))
        def job():
            try:
                ok, msg = verify_archive(p)
            except Exception as e:
                ok, msg = False, str(e)
            self.q.put(("log", ("[✓] Архив исправен: " if ok else "[✗] Архив ПОВРЕЖДЁН: ") + msg))
            self.after(0, lambda: (messagebox.showinfo if ok else messagebox.showerror)(
                "Проверка целостности", ("Архив целый, его можно восстанавливать.\n\n" if ok else
                "Архив повреждён или недокачан — восстанавливать его нельзя.\n\n") + msg))
        threading.Thread(target=job, daemon=True).start()

    def _pick_archive(self):
        p = filedialog.askopenfilename(title="Файл бэкапа", initialdir=self.dest.get() or None,
                                       filetypes=[("Бэкап", "*.tar.gz"), ("Все файлы", "*.*")])
        if p:
            self.arch.delete(0, "end"); self.arch.insert(0, p); self._read_os(p)

    def _read_os(self, p):
        self.archive_os = None; meta = {}; self.archive_meta = {}
        try:
            with tarfile.open(p, "r|gz") as t:
                for i, m in enumerate(t):
                    if i > 3: break
                    if m.name.endswith("/backup_meta.json"):
                        meta = json.loads(t.extractfile(m).read().decode("utf-8", "replace"))
                    elif m.name.endswith("/os_release.txt"):
                        self.archive_os = parse_os(t.extractfile(m).read().decode("utf-8", "replace"))
        except Exception as e:
            self.oslbl.configure(text=f"Не удалось прочитать архив: {e}", text_color=ERR); return
        self.archive_meta = meta
        if not self.archive_os and meta.get("os"):
            self.archive_os = (meta["os"], meta.get("os_id", ""), meta.get("os_version", ""))
        lines = []
        if meta:
            src = meta.get("server_name") or meta.get("host", "?")
            if meta.get("host") and meta.get("host") != src: src += f"  ({meta['host']})"
            lines.append(f"🖥  Сервер-источник:  {src}")
            if meta.get("hostname"): lines.append(f"🏷  Имя машины:  {meta['hostname']}")
            if meta.get("created"): lines.append(f"📅  Создан:  {meta['created']}")
        if self.archive_os:
            lines.append(f"🐧  ОС:  {self.archive_os[0]}  — закажите новый VPS с этой же системой")
        if meta.get("contents"): lines.append("📦  Содержимое:  " + ", ".join(meta["contents"]))
        if meta: lines.append("⚡  Тип:  " + (meta.get("kind") or "полный") +
                              ("  — при восстановлении программа сама выполнит npm install" if meta.get("slim") else ""))
        if lines:
            self.oslbl.configure(text="\n".join(lines), text_color=OK if meta else TXT)
        else:
            self.oslbl.configure(text="Сведения о сервере не найдены (бэкап сделан старой версией программы)",
                                 text_color=WARN)

    def open_dir(self):
        d = self.dest.get()
        if os.name == "nt": os.startfile(d)
        else: subprocess.Popen(["xdg-open", d])

    # ---------- settings ----------
    def save_servers(self):
        self.save(); self.conn.refresh(); self.rconn.refresh(); self._sched_servers()
        if hasattr(self, "srv_list"): self._srv_fill()

    def save(self):
        CFG_DIR.mkdir(parents=True, exist_ok=True)
        data = self.conn.dump()
        data.update(dest=self.dest.get(), opt={k: v.get() for k, v in self.opt.items()}, restore=self.rconn.dump(),
                    servers=self.servers, schedule=self.sched, cloud=getattr(self, "cloudcfg", {}) or {},
                    to_cloud=self.to_cloud.get(), slim=self.slim.get(), poweroff=self.poweroff.get())
        CFG_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def load(self):
        try: d = json.loads(CFG_FILE.read_text(encoding="utf-8"))
        except Exception: d = {}
        self.servers = d.get("servers", {}) or {}
        if not self.servers and d.get("host"):   # перенос старых настроек в список
            n = d.get("name") or d["host"]
            self.servers[n] = {k: d.get(k, "") for k in ("host", "port", "user", "auth", "key")} | {"name": n}
            d["name"] = n
        self.conn.load(d); self.rconn.load(d.get("restore", {})); self._cloud_load(d)
        self.conn.refresh(); self.rconn.refresh()
        self.dest.delete(0, "end"); self.dest.insert(0, d.get("dest", str(Path.home() / "VPS_Backups")))
        for k, v in d.get("opt", {}).items():
            if k in self.opt: self.opt[k].set(v)
        if "slim" in d: self.slim.set(bool(d["slim"]))
        self.poweroff.set(bool(d.get("poweroff")))
        self._sched_load(d)

    # ---------- общее ----------
    def _ask_dialog(self, a):
        """Окно-блокировка: сервер не пустой. Результат кладётся в self.decision."""
        w = ctk.CTkToplevel(self); w.title("Сервер не пустой!"); w.geometry("640x620"); w.configure(fg_color=BG)
        w.transient(self); w.grab_set(); w.resizable(False, False)
        res = {"v": None}
        ctk.CTkLabel(w, text="🔴  На сервере уже есть чужие данные", font=("Segoe UI", 18, "bold"),
                     text_color=ERR).pack(anchor="w", padx=20, pady=(18, 4))
        ctk.CTkLabel(w, text="Восстановление может их перезаписать. Проверьте список и выберите, как поступить.",
                     text_color=MUTED, wraplength=600, justify="left").pack(anchor="w", padx=20)
        tb = ctk.CTkTextbox(w, height=150, fg_color=CARD, border_color=BORDER, border_width=1, text_color=TXT)
        tb.pack(fill="x", padx=20, pady=10); tb.insert("end", "\n".join("•  " + x for x in audit_text(a))); tb.configure(state="disabled")
        mode = ctk.StringVar(value="safe")
        f = card(w, "Режим восстановления"); f.pack(fill="x", padx=20, pady=4)
        ctk.CTkRadioButton(f, text="🛡  Безопасный: только добавить. Существующие файлы и базы НЕ трогать",
                           variable=mode, value="safe", text_color=TXT, fg_color=ACCENT).pack(anchor="w", padx=16, pady=4)
        ctk.CTkRadioButton(f, text="⚠  Полная перезапись совпадающих файлов и баз",
                           variable=mode, value="overwrite", text_color=ERR, fg_color=ERR).pack(anchor="w", padx=16, pady=(4, 10))
        rb = ctk.BooleanVar(value=True)
        chk(w, "💾  Сначала скачать копию текущих данных этого сервера на компьютер (для отката)", rb).pack(anchor="w", padx=24, pady=8)
        cf = ctk.CTkFrame(w, fg_color="transparent"); cf.pack(fill="x", padx=24)
        lbl = ctk.CTkLabel(cf, text="Для перезаписи введите слово ПОДТВЕРЖДАЮ:", text_color=TXT)
        ent = entry(cf, width=200)
        go = btn(w, "Продолжить", None, width=160)
        def upd(*_):
            ov = mode.get() == "overwrite"
            if ov: lbl.pack(side="left"); ent.pack(side="left", padx=8)
            else: lbl.pack_forget(); ent.pack_forget()
            go.configure(state="normal" if (not ov or ent.get().strip().upper() == "ПОДТВЕРЖДАЮ") else "disabled")
        mode.trace_add("write", upd); ent.bind("<KeyRelease>", upd)
        def ok():
            if mode.get() == "overwrite" and not rb.get():
                if not messagebox.askyesno("Без копии?", "Перезапись БЕЗ резервной копии текущих данных.\n"
                                           "Если что-то пойдёт не так — восстановить будет нечем. Продолжить?",
                                           icon="warning", parent=w): return
            res["v"] = {"mode": mode.get(), "rollback": rb.get()}; w.destroy()
        go.configure(command=ok)
        bb = ctk.CTkFrame(w, fg_color="transparent"); bb.pack(fill="x", padx=20, pady=16, side="bottom")
        go.pack(in_=bb, side="right")
        btn(bb, "Отмена", w.destroy, primary=False, width=110).pack(side="right", padx=8)
        upd()
        self.wait_window(w)
        self.decision = res["v"]; self.decision_ev.set()

    def _begin(self):
        if self.remember.get(): self.save()
        self.busy = True; self.cancel.clear(); self.decision_ev = threading.Event(); self.decision = None
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
        cfg = self.conn.cfg(); cfg.update(dest=self.dest.get(), opt={k: v.get() for k, v in self.opt.items()}, cloud=self.to_cloud.get(), slim=self.slim.get())
        threading.Thread(target=self._worker, args=(cfg,), daemon=True).start()

    def _stream(self, ssh, cmd, path, label, expect=0):
        """Выполняет команду на сервере и пишет её вывод прямо в локальный файл. Возвращает (код, stderr)."""
        if self.cancel.is_set(): raise Cancelled()
        ch = ssh.get_transport().open_session(); self.chan = ch
        ch.exec_command("bash -c " + shlex.quote(cmd))
        done, t0, last, err = 0, time.time(), 0, b""
        idle = time.time()
        with open(path, "wb") as f:
            while True:
                if self.cancel.is_set(): ch.close(); raise Cancelled()
                got = False
                if ch.recv_ready():
                    d = ch.recv(262144)
                    if d: f.write(d); done += len(d); got = True
                if ch.recv_stderr_ready():
                    err += ch.recv_stderr(65536); got = True
                if got: idle = time.time()
                else:
                    if ch.exit_status_ready() and not ch.recv_ready() and not ch.recv_stderr_ready():
                        # процесс завершён, но хвост потока может быть ещё в пути — дочитываем до EOF
                        ch.settimeout(30)
                        try:
                            while True:
                                d = ch.recv(262144)
                                if not d: break
                                f.write(d); done += len(d)
                            while True:
                                e = ch.recv_stderr(65536)
                                if not e: break
                                err += e
                        except socket.timeout:
                            raise ConnectionError("сервер не прислал конец данных")
                        break
                    tr = ssh.get_transport()
                    if tr is None or not tr.is_active(): raise ConnectionError("SSH-соединение закрыто")
                    if ch.closed and not ch.recv_ready():
                        raise ConnectionError("канал закрыт до завершения команды")
                    if time.time() - idle > 90: raise ConnectionError("сервер не присылает данные 90 секунд")
                    if time.time() - idle > 15 and time.time() - last > 1:
                        last = time.time()
                        self.q.put(("status", f"{label}: {done/1048576:.1f} МБ — нет данных уже {int(time.time()-idle)} с"))
                        continue
                    time.sleep(0.02)
                if time.time() - last > 0.3:
                    last = time.time(); sp = done / max(last - t0, 0.1) / 1048576
                    tail = f" из ~{human(expect)}" if expect else ""
                    self.q.put(("status", f"{label}: {human(done)}{tail}  ({sp:.1f} МБ/с)"))
        if self.cancel.is_set(): raise Cancelled()
        code = ch.recv_exit_status(); self.chan = None; ch.close()
        return code, err.decode("utf-8", "replace").strip()

    def _wait_net(self, c, L, secs=30):
        """Ждёт, пока сервер снова станет доступен. False — если пользователь нажал «Остановить»."""
        t0 = time.time()
        while not self.cancel.is_set():
            left = int(secs - (time.time() - t0))
            if left <= 0:
                try:
                    socket.create_connection((c["host"], int(c.get("port") or 22)), timeout=5).close(); return True
                except OSError:
                    self.q.put(("status", "Нет связи с сервером — жду появления сети...")); t0 = time.time() - secs + 10
            else: self.q.put(("status", f"Связь потеряна. Повтор через {left} с..."))
            time.sleep(1)
        return False

    def _estimate(self, ssh, c, dest):
        """Быстрая оценка объёма данных на сервере до скачивания. Возвращает ожидаемый размер файлов (сжатый)."""
        L = lambda m: self.q.put(("log", m))
        o = c["opt"]; parts = []; lo = hi = 0; files_exp = 0
        self.q.put(("status", "Оценка размера данных на сервере..."))
        try:
            if o.get("db"):
                db = 0
                for cmd in (MYSQL_SIZE_CMD, PG_SIZE_CMD):
                    out, code = self._exec(ssh, cmd, timeout=40)
                    if code == 0:
                        try: db += int((out.strip().splitlines() or ["0"])[-1].strip() or 0)
                        except ValueError: pass
                if db:
                    parts.append(f"базы ~{human(db)}"); lo += db / 8; hi += db / 3
            paths = (WEB_PATHS if o.get("web") else []) + (CONF_PATHS if o.get("conf") else [])
            if paths:
                ex = "".join(f"--exclude={shlex.quote(x)} " for x in (SLIM_EXCLUDES if c.get("slim") else ["*.sock"]))
                plist = " ".join(shlex.quote(p) for p in paths)
                cmd = (f'cd / && P=""; for p in {plist}; do [ -e "/$p" ] && P="$P $p"; done; '
                       '[ -z "$P" ] && { echo 0; exit 0; }; du -scb ' + ex + '$P 2>/dev/null | tail -1 | cut -f1')
                out, code = self._exec(ssh, cmd, timeout=120)
                try: fs = int(out.strip().split()[0])
                except (ValueError, IndexError): fs = 0
                if fs:
                    parts.append(f"файлы ~{human(fs)}"); lo += fs * 0.35; hi += fs * 0.9
                    files_exp = fs * 0.6
        except Exception as e:
            L(f"[i] Не удалось оценить размер заранее ({e}) — продолжаю бэкап"); return 0
        self._need_hi = hi
        if not parts: return 0
        try: free = shutil.disk_usage(dest).free
        except Exception: free = 0
        L(f"[i] Оценка данных на сервере: {', '.join(parts)}. "
          f"Ожидаемый архив: {human(lo)}–{human(hi)}" + (f" (свободно на диске: {human(free)})" if free else ""))
        if free and free < hi * 1.1:
            if free < lo:
                L(f"[!] ВНИМАНИЕ: на диске почти наверняка не хватит места (нужно ~{human(lo)}–{human(hi)}, свободно {human(free)})")
            else:
                L(f"[!] Места на диске впритык — бэкап может не поместиться (свободно {human(free)})")
        return files_exp

    def _worker(self, c, attempt=1):
        L = lambda m: self.q.put(("log", m))
        ssh = None; tmp = final = None; retry = False
        try:
            Path(c["dest"]).mkdir(parents=True, exist_ok=True)
            ssh = connect(c, L); self.q.put(("bar", 0.05))
            _, o, _ = ssh.exec_command("hostname"); hn = o.read().decode().strip() or c["host"]
            name = f"vps_backup_{safe_name(c.get('name') or hn)}_{time.strftime('%Y-%m-%d_%H-%M')}"
            final = Path(c["dest"]) / (name + ".tar.gz"); n = 2
            while final.exists() or (Path(c["dest"]) / (name + ".part")).exists():
                name = name.rstrip("_0123456789") if n > 2 else name
                name = f"vps_backup_{safe_name(c.get('name') or hn)}_{time.strftime('%Y-%m-%d_%H-%M')}_{n}"
                final = Path(c["dest"]) / (name + ".tar.gz"); n += 1
            final = None
            tmp = Path(c["dest"]) / (name + ".part"); tmp.mkdir()
            out, _ = self._exec(ssh, OS_CMD); osi = parse_os(out)
            (tmp / "os_release.txt").write_text(out, encoding="utf-8")
            L(f"[i] Сервер: {c.get('name') or c['host']}  •  ОС: {osi[0]}")
            names = {"db": "базы данных", "web": "файлы сайтов", "conf": "настройки служб", "pkgs": "список программ"}
            meta = dict(format=1, app="VPS Backup v4", server_name=c.get("name") or c["host"], host=c["host"],
                        port=c["port"], user=c["user"], hostname=hn, created=time.strftime("%d.%m.%Y %H:%M"),
                        os=osi[0], os_id=osi[1], os_version=osi[2],
                        contents=[v for k, v in names.items() if c["opt"].get(k)], slim=bool(c.get("slim")),
                        kind="без кэша (node_modules и кэши создаются заново)" if c.get("slim") else "полный")
            (tmp / "backup_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            self._need_hi = 0
            files_exp = self._estimate(ssh, c, c["dest"])
            srv = c.get("name") or hn; keep = self._keep_of(srv)
            try: free = shutil.disk_usage(c["dest"]).free
            except Exception: free = 0
            old = self._copies(c["dest"], srv)
            # места мало: заранее удаляем самые старые копии, которые всё равно ушли бы по ротации
            while free and self._need_hi and free < self._need_hi * 1.1 and keep >= 2 and len(old) >= keep:
                p = old.pop(0)
                try:
                    sz = p.stat().st_size; p.unlink(); free += sz
                    L(f"[i] Мало места: заранее удалена самая старая копия {p.name} ({human(sz)})")
                except Exception as e: L(f"[!] Не удалось удалить {p.name}: {e}"); break
            L("[*] Режим «на лету»: на диске сервера ничего не создаётся")
            errors = []
            o = c["opt"]
            if o["db"]:
                L("[*] Дамп MySQL/MariaDB...")
                code, err = self._stream(ssh, MYSQL_CMD, tmp / "mysql_all.sql.gz", "MySQL")
                if code == 3: L("[-] MySQL не найден"); (tmp / "mysql_all.sql.gz").unlink()
                elif code == 0:
                    ok, msg = gz_check(tmp / "mysql_all.sql.gz", self.cancel)
                    if not ok: raise Broken(msg)
                    L("[+] MySQL готово, целостность проверена")
                else: L(f"[!] Ошибка дампа MySQL: {err[-300:]}"); errors.append("MySQL: " + err)
                self.q.put(("bar", 0.15))
                L("[*] Дамп PostgreSQL...")
                code, err = self._stream(ssh, PG_CMD, tmp / "postgres_all.sql.gz", "PostgreSQL")
                if code == 3: L("[-] PostgreSQL не найден"); (tmp / "postgres_all.sql.gz").unlink()
                elif code == 0:
                    ok, msg = gz_check(tmp / "postgres_all.sql.gz", self.cancel)
                    if not ok: raise Broken(msg)
                    L("[+] PostgreSQL готово, целостность проверена")
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
            ex = "".join(f"--exclude={shlex.quote(x)} " for x in (SLIM_EXCLUDES if c.get("slim") else ["*.sock"]))
            if paths:
                plist = " ".join(shlex.quote(p) for p in paths)
                cmd = (f'cd / && P=""; for p in {plist}; do [ -e "/$p" ] && P="$P $p"; done; '
                       '[ -z "$P" ] && exit 5; tar -czf - --ignore-failed-read --warning=no-file-changed '
                       '--warning=no-file-ignored ' + ex + '$P')
                if c.get("slim"): L("[i] Режим «без кэша»: node_modules, кэши и логи пропускаются")
                L("[*] Архивация и скачивание файлов сайтов/конфигураций (может занять долго)...")
                self.q.put(("bar", 0.35))
                code, err = self._stream(ssh, cmd, tmp / "files.tar.gz", "Файлы", files_exp)
                if code == 5: L("[-] Папки не найдены"); (tmp / "files.tar.gz").unlink()
                elif code in (0, 1):
                    L(f"[+] Файлы скачаны ({(tmp/'files.tar.gz').stat().st_size/1048576:.1f} МБ), проверяю целостность...")
                    self.q.put(("status", "Проверка архива файлов..."))
                    try:
                        with tarfile.open(tmp / "files.tar.gz", "r|gz") as tt: nf = _walk_tar(tt, self.cancel, False)
                    except Cancelled: raise
                    except Exception as e: raise Broken(f"архив файлов оборван или повреждён ({e})")
                    L(f"[+] Архив файлов целый ({nf} объектов)")
                else:
                    e2 = "\n".join(l for l in err.splitlines() if "socket ignored" not in l)
                    raise Broken(f"архивация на сервере завершилась с ошибкой (код {code}): {e2[-300:]}")
                err = "\n".join(l for l in err.splitlines() if "socket ignored" not in l)
                if err and code in (0, 1): errors.append("tar warnings: " + err)
            if errors: (tmp / "errors.log").write_text("\n\n".join(errors), encoding="utf-8")
            self.q.put(("bar", 0.95)); L("[*] Упаковка в один файл...")
            done_path = Path(c["dest"]) / (name + ".tar.gz")
            final = Path(c["dest"]) / (name + ".tar.gz.writing")
            # паспорт и ОС кладём первыми — так программа быстро читает ОС из архива
            first = {"backup_meta.json": 0, "os_release.txt": 1}
            items = sorted(tmp.iterdir(), key=lambda p: (first.get(p.name, 2), p.name))
            with tarfile.open(final, "w:gz", compresslevel=1) as t:
                for p in items:
                    if self.cancel.is_set(): raise Cancelled()
                    t.add(p, arcname=f"{name}/{p.name}")
            L("[*] Проверка целостности итогового архива...")
            self.q.put(("status", "Проверка целостности..."))
            ok, msg = verify_archive(final, self.cancel)
            if not ok: raise Broken(msg)
            os.replace(final, done_path); final = done_path
            shutil.rmtree(tmp, ignore_errors=True); tmp = None
            L(f"[✓] Целостность подтверждена: {msg}")
            L(f"[✓] ГОТОВО: {final} ({final.stat().st_size/1048576:.1f} МБ)")
            self._rotate(c["dest"], srv, keep, L, protect=final)
            if c.get("cloud"): self._cloud_upload(final, c.get("name") or hn)
            self.q.put(("bar", 1.0)); self.q.put(("done", ("backup", True)))
        except Cancelled:
            L("[■] Бэкап остановлен пользователем. На сервере ничего не осталось.")
            if final and final.exists():
                try: final.unlink()
                except Exception: pass
            self.q.put(("bar", 0)); self.q.put(("done", ("backup", None)))
        except Exception as e:
            if final and final.exists():
                try: final.unlink()
                except Exception: pass
            if self.cancel.is_set(): L("[■] Бэкап остановлен пользователем."); self.q.put(("done", ("backup", None)))
            elif isinstance(e, Broken):
                L(f"[✗] БЭКАП НЕ СОХРАНЁН: {e}"); L("[i] Повреждённый файл удалён, чтобы не перепутать его с целым. Запустите бэкап ещё раз.")
                self.q.put(("done", ("backup", False)))
            elif isinstance(e, NET_ERRORS) and attempt < 3:
                L(f"[!] {net_msg(e)}"); L(f"[*] Подожду связь и начну бэкап заново (попытка {attempt + 1} из 3)")
                retry = True
            elif isinstance(e, NET_ERRORS): L(f"[✗] {net_msg(e)} Попытки закончились."); self.q.put(("done", ("backup", False)))
            else: L(f"[✗] ОШИБКА: {e}"); self.q.put(("done", ("backup", False)))
        finally:
            if tmp: shutil.rmtree(tmp, ignore_errors=True)
            self.chan = None
            if ssh:
                try: ssh.close()
                except Exception: pass
        if retry:
            if self._wait_net(c, L): self._worker(c, attempt + 1)
            else: L("[■] Бэкап остановлен пользователем."); self.q.put(("done", ("backup", None)))

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
            # --- аудит: есть ли на сервере чужие данные ---
            L("[*] Проверяю, нет ли на сервере чужих сайтов и баз...")
            out, _ = self._exec(ssh, AUDIT_CMD, timeout=120); a = parse_audit(out)
            for x in audit_text(a): L("    " + x)
            if a["level"] == "green": L("[✓] 🟢 Сервер чистый — можно разворачивать")
            elif a["level"] == "yellow": L("[!] 🟡 Установлены программы, но чужих сайтов и баз нет")
            else: L("[✗] 🔴 СЕРВЕР НЕ ПУСТОЙ: найдены сайты или базы. Нужна защита от перезаписи")
            clp_here = "clpctl" in a["svc"]
            if o["clp"] and not clp_here and any(x in a["svc"] for x in ("nginx", "apache2", "httpd", "mysql", "mariadb")):
                L("[✗] Установить CloudPanel нельзя: на сервере уже есть веб-сервер или MySQL. "
                  "CloudPanel ставится только на чистую систему. Снимите галочку или возьмите чистый сервер.")
                self.q.put(("done", ("check" if c["check_only"] else "restore", False))); return
            if c["check_only"]:
                L("[✓] Проверка завершена, на сервере ничего не изменено"); self.q.put(("bar", 1.0))
                self.q.put(("done", ("check", True))); return
            mode, rollback = "overwrite", False
            if a["level"] == "red":
                self.q.put(("ask", a)); self.decision_ev.wait()
                if not self.decision or self.cancel.is_set():
                    L("[■] Отменено. На сервере ничего не изменено."); self.q.put(("done", ("restore", None))); return
                mode, rollback = self.decision["mode"], self.decision["rollback"]
                L(f"[i] Режим: {'🛡 безопасный (только добавление)' if mode == 'safe' else '⚠ перезапись'}")
            if rollback:
                rdir = arch.parent / f"rollback_{safe_name(c.get('host', 'server'))}_{time.strftime('%Y-%m-%d_%H-%M')}"
                rdir.mkdir(parents=True, exist_ok=True)
                L(f"[*] Сохраняю копию текущих данных сервера в {rdir} ...")
                tp = " ".join(shlex.quote(x) for x in (WEB_PATHS if o["web"] else []) + (CONF_PATHS if o["conf"] else []))
                if tp:
                    code, err = self._stream(ssh, f"cd / && E=''; for x in {tp}; do [ -e \"$x\" ] && E=\"$E $x\"; done; "
                                             "[ -n \"$E\" ] && tar -czpf - --numeric-owner --ignore-failed-read $E",
                                             rdir / "files.tar.gz", "Копия файлов")
                    if code not in (0, 1): raise RuntimeError(f"не удалось сохранить копию файлов: {err[-200:]}")
                if o["db"] and a["my_user"]:
                    code, err = self._stream(ssh, MYSQL_CMD, rdir / "mysql_all.sql.gz", "Копия MySQL")
                    if code != 0: raise RuntimeError(f"не удалось сохранить копию MySQL: {err[-200:]}")
                if o["db"] and a["pg_user"]:
                    code, err = self._stream(ssh, PG_CMD, rdir / "postgres_all.sql.gz", "Копия PostgreSQL")
                    if code != 0: raise RuntimeError(f"не удалось сохранить копию PostgreSQL: {err[-200:]}")
                L("[+] Копия для отката сохранена. Только после этого начинаю запись.")
            safe = mode == "safe"
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
                        cmd = "tar -xzpf - -C / --numeric-owner" + (" --skip-old-files" if safe else "") + \
                              ("" if both else f" --wildcards {pats}")
                        if safe: L("    существующие файлы на сервере пропускаются")
                        code, err = self._send(ssh, cmd, t.extractfile(m), "Файлы", m.size)
                        if code in (0, 2): L("[+] Файлы восстановлены")
                        else: L(f"[!] Ошибка распаковки: {err[-300:]}")
                        if err: warn.append(err)
                        self.q.put(("bar", 0.6))
                    elif fn == "mysql_all.sql.gz" and o["db"]:
                        L("[*] Импорт баз MySQL/MariaDB...")
                        if safe:
                            src = DumpFilter(t.extractfile(m), "my", a["my"] | SYS_MY)
                            code, err = self._send(ssh, MYSQL_RESTORE_RAW, src, "MySQL", 0)
                            if src.skipped: L("    не тронуты (уже есть на сервере): " + ", ".join(sorted(src.skipped)))
                            L("[!] Пользователи MySQL не перенесены (безопасный режим) — сайтам может понадобиться создать их в панели")
                        else:
                            code, err = self._send(ssh, MYSQL_RESTORE, t.extractfile(m), "MySQL", m.size)
                        if code == 3: L("[-] На сервере нет MySQL — пропущено (отметьте установку CloudPanel)")
                        elif code == 0: L("[+] MySQL восстановлен")
                        else: L(f"[!] Ошибка импорта MySQL: {err[-300:]}")
                        self.q.put(("bar", 0.8))
                    elif fn == "postgres_all.sql.gz" and o["db"]:
                        L("[*] Импорт баз PostgreSQL...")
                        if safe:
                            src = DumpFilter(t.extractfile(m), "pg", a["pg"] | SYS_PG, a["roles"])
                            code, err = self._send(ssh, PG_RESTORE_RAW, src, "PostgreSQL", 0)
                            if src.skipped: L("    не тронуты (уже есть на сервере): " + ", ".join(sorted(src.skipped)))
                        else:
                            code, err = self._send(ssh, PG_RESTORE, t.extractfile(m), "PostgreSQL", m.size)
                        if code == 3: L("[-] На сервере нет PostgreSQL — пропущено")
                        else: L("[+] PostgreSQL восстановлен")
                        self.q.put(("bar", 0.9))
            if o["web"]:  # решается автоматически: скрипт трогает только проекты без node_modules
                L("[*] Node.js: ищу проекты без node_modules и при необходимости ставлю их сам...")
                self._run_log(ssh, NPM_CMD)
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
            elif isinstance(e, NET_ERRORS):
                L("[✗] Связь с сервером прервана (сменилась сеть / VPN). Часть данных могла записаться — "
                  f"запустите восстановление заново. Подробности: {e}"); self.q.put(("done", ("restore", False)))
            else: L(f"[✗] ОШИБКА: {e}"); self.q.put(("done", ("restore", False)))
        finally:
            self.chan = None
            if ssh: ssh.close()

    def _shutdown(self):
        """Надёжное выключение: /f закрывает зависшие программы, 60 с на отмену."""
        self.q.put(("log", "[i] Бэкап успешен — компьютер выключится через 60 секунд"))
        try:
            subprocess.run(["shutdown.exe", "/s", "/f", "/t", "60", "/c", "VPS Backup: бэкап завершён, выключение"],
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as e:
            self.q.put(("log", f"[✗] Не удалось выключить компьютер: {e}")); return
        w = ctk.CTkToplevel(self); w.title("VPS Backup"); w.attributes("-topmost", True); w.resizable(False, False)
        lbl = ctk.CTkLabel(w, text="", font=ctk.CTkFont(FONT, 15)); lbl.pack(padx=30, pady=(24, 12))
        st = {"t": 60, "on": True}
        def cancel():
            st["on"] = False
            subprocess.run(["shutdown.exe", "/a"], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.q.put(("log", "[i] Выключение компьютера отменено")); w.destroy()
        def tick():
            if not st["on"] or not w.winfo_exists(): return
            lbl.configure(text=f"✔ Бэкап завершён.\nКомпьютер выключится через {st['t']} с")
            st["t"] -= 1
            if st["t"] >= 0: w.after(1000, tick)
        btn(w, "Отменить выключение", cancel, width=220).pack(padx=30, pady=(0, 24))
        w.protocol("WM_DELETE_WINDOW", cancel); tick()

    def _poll(self):
        try:
            while True:
                k, v = self.q.get_nowait()
                if k == "log":
                    tag = {"[✓": "ok", "[+": "ok", "[!": "warn", "[■": "warn", "[✗": "err", "[i": "info"}.get(v[:2])
                    self.log.insert("end", v + "\n", tag); self.log.see("end")
                elif k == "bar": self.bar.set(v)
                elif k == "ask": self._ask_dialog(v)
                elif k == "status": self.status.configure(text=v)
                elif k == "done":
                    kind, ok = v
                    if kind == "backup" and ok is not True: self._all_ok = False
                    if self.auto_mode and kind == "backup":
                        if ok is not None and self.sched_queue:
                            self._sched_next(); continue
                        self.auto_mode = False; self.sched_queue = []
                        self.busy = False
                        for w in (self.start, self.openb, self.rstart, self.checkb): w.configure(state="normal")
                        self.stopb.configure(state="disabled")
                        self.status.configure(text="Бэкап по расписанию завершён" if ok is not None else "Остановлено")
                        allok = getattr(self, "_all_ok", True); self._all_ok = True
                        if allok and self.poweroff.get(): self._shutdown()
                        continue
                    self.busy = False
                    for w in (self.start, self.openb, self.rstart, self.checkb): w.configure(state="normal")
                    self.stopb.configure(state="disabled")
                    self.status.configure(text={True: "Готово", False: "Ошибка", None: "Остановлено"}[ok])
                    title = {"backup": "Бэкап", "restore": "Восстановление", "check": "Проверка"}[kind]
                    allok = getattr(self, "_all_ok", True); self._all_ok = True
                    if kind == "backup" and ok is True and allok and self.poweroff.get(): self._shutdown()
                    elif ok is True: messagebox.showinfo("VPS Backup", f"{title}: успешно")
                    elif ok is False: messagebox.showerror("VPS Backup", f"{title}: не удалось, см. лог")
        except queue.Empty: pass
        self.after(100, self._poll)


def single_instance():
    """True — можно запускаться. Если программа уже открыта — показываем её окно и выходим."""
    if os.name != "nt": return True
    import ctypes
    k = ctypes.windll.kernel32; u = ctypes.windll.user32
    k.CreateMutexW(None, False, "Local\\VPSBackup_single_instance")
    if k.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        h = u.FindWindowW(None, "VPS Backup")
        if h:
            u.ShowWindow(h, 9); u.SetForegroundWindow(h)
        return False
    return True


if __name__ == "__main__":
    if single_instance():
        app = App()
        try:
            import pyi_splash; pyi_splash.close()
        except Exception: pass
        app.mainloop()
    else:
        try:
            import pyi_splash; pyi_splash.close()
        except Exception: pass
