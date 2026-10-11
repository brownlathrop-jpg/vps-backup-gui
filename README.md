# VPS Backup

Windows-приложение для резервного копирования VPS (SSH/SFTP).

1. Загрузите эти файлы в репозиторий GitHub (ветка main).
2. Через 3–5 минут во вкладке **Releases** появится `VPSBackup.exe`.
3. Запустите, введите данные сервера, отметьте, что бэкапить, нажмите «Начать бэкап».

Пароль хранится в Диспетчере учётных данных Windows, а не в файле. Настройки: `%APPDATA%\VPSBackup\settings.json`.
Локальный запуск: `pip install -r requirements.txt && python vps_backup.py`.
