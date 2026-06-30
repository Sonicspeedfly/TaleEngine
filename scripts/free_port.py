"""
Печатает первый свободный TCP-порт из списка кандидатов.
Используется в start.bat, чтобы не падать, когда порт 8000 уже занят.
"""
import socket

CANDIDATES = (8000, 8010, 8080, 8800, 8765, 7860)

for port in CANDIDATES:
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", port))
    except OSError:
        # Порт занят — пробуем следующий.
        continue
    finally:
        sock.close()
    print(port)
    break
