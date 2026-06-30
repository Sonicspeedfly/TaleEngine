"""Печатает локальный IP машины в сети (для доступа с других устройств)."""
import socket

try:
    # UDP-сокет не шлёт пакетов, но даёт узнать IP исходящего интерфейса.
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect(("8.8.8.8", 80))
    print(s.getsockname()[0])
    s.close()
except OSError:
    print("127.0.0.1")
