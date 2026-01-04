#!/usr/bin/env python3
import argparse
import json
import socket
import threading
import ssl
from typing import Any, Dict, List, Optional

import pika

# ✅ Constante pedida
AMQP_URL = "amqps://pdlqfxoa:dlvC_6LIuO0T0bb4Nzp8ad6VPE1CZIua@leopard.lmq.cloudamqp.com/pdlqfxoa"


def send_json_line(conn: socket.socket, obj: Dict[str, Any]) -> None:
    data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
    conn.sendall(data)


def recv_json_line(fileobj) -> Optional[Dict[str, Any]]:
    line = fileobj.readline()
    if not line:
        return None
    return json.loads(line)


def run_with_timeout(fn, seconds: int):
    """
    Executa fn() em thread e corta em `seconds`.
    Se passar do tempo, levanta TimeoutError.
    Se fn lançar erro, repassa o erro.
    """
    result = {"ok": False, "value": None, "err": None}

    def worker():
        try:
            result["value"] = fn()
            result["ok"] = True
        except Exception as e:
            result["err"] = e

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(seconds)

    if t.is_alive():
        raise TimeoutError(f"AMQP timeout após {seconds}s")
    if not result["ok"]:
        raise result["err"]
    return result["value"]


class RabbitMOM:
    """
    Wrapper RabbitMQ (pika) com reconexão e timeouts.
    """
    def __init__(self, amqp_url: str):
        self.amqp_url = amqp_url
        self._lock = threading.Lock()
        self._conn: Optional[pika.BlockingConnection] = None
        self._ch: Optional[pika.channel.Channel] = None

    def _qname(self, user: str) -> str:
        return f"queue.{user}"

    def _connect(self) -> None:
        params = pika.URLParameters(self.amqp_url)
        # Timeouts para evitar travamentos longos
        params.socket_timeout = 5
        params.blocked_connection_timeout = 5
        params.connection_attempts = 2
        params.retry_delay = 1
        params.heartbeat = 30

        # AMQPS (TLS)
        if self.amqp_url.startswith("amqps://"):
            ctx = ssl.create_default_context()
            params.ssl_options = pika.SSLOptions(ctx)

        self._conn = pika.BlockingConnection(params)
        self._ch = self._conn.channel()

    def _ensure_connected(self) -> None:
        if self._conn is None or self._ch is None or self._conn.is_closed or self._ch.is_closed:
            self._connect()

    # Operações MOM (não coloque timeout aqui; o timeout "forte" é no run_with_timeout)
    def ensure_queue(self, user: str) -> None:
        q = self._qname(user)
        with self._lock:
            self._ensure_connected()
            assert self._ch is not None
            self._ch.queue_declare(queue=q, durable=True)

    def enqueue(self, to_user: str, msg: Dict[str, Any]) -> None:
        q = self._qname(to_user)
        body = json.dumps(msg, ensure_ascii=False).encode("utf-8")
        with self._lock:
            self._ensure_connected()
            assert self._ch is not None
            self._ch.queue_declare(queue=q, durable=True)
            self._ch.basic_publish(
                exchange="",
                routing_key=q,
                body=body,
                properties=pika.BasicProperties(delivery_mode=2),
            )

    def fetch_all(self, user: str, limit: int = 500) -> List[Dict[str, Any]]:
        q = self._qname(user)
        out: List[Dict[str, Any]] = []
        with self._lock:
            self._ensure_connected()
            assert self._ch is not None
            self._ch.queue_declare(queue=q, durable=True)

            for _ in range(limit):
                method_frame, props, body = self._ch.basic_get(queue=q, auto_ack=False)
                if method_frame is None:
                    break
                try:
                    out.append(json.loads(body.decode("utf-8")))
                finally:
                    self._ch.basic_ack(delivery_tag=method_frame.delivery_tag)
        return out

    def close(self) -> None:
        with self._lock:
            try:
                if self._ch and self._ch.is_open:
                    self._ch.close()
            except Exception:
                pass
            try:
                if self._conn and self._conn.is_open:
                    self._conn.close()
            except Exception:
                pass


class OfflineServer:
    def __init__(self, host: str, port: int, amqp_url: str):
        self.host = host
        self.port = port
        self.mom = RabbitMOM(amqp_url)

    def handle_client(self, conn: socket.socket, addr):
        # timeout da leitura do socket (não da operação AMQP)
        conn.settimeout(30)

        with conn:
            f = conn.makefile("r", encoding="utf-8", newline="\n")

            while True:
                req = recv_json_line(f)
                if req is None:
                    return

                typ = req.get("type")

                try:
                    if typ == "CREATE_QUEUE":
                        user = str(req.get("user", "")).strip()
                        if not user:
                            send_json_line(conn, {"type": "ERROR", "message": "user vazio"})
                            continue

                        # ✅ timeout forte (nunca pendura)
                        run_with_timeout(lambda: self.mom.ensure_queue(user), 8)

                        send_json_line(conn, {"type": "OK", "action": "CREATE_QUEUE", "user": user})

                    elif typ == "ENQUEUE":
                        to_user = str(req.get("to", "")).strip()
                        if not to_user:
                            send_json_line(conn, {"type": "ERROR", "message": "to vazio"})
                            continue

                        msg = {
                            "from": req.get("from"),
                            "to": to_user,
                            "text": req.get("text"),
                            "ts": req.get("ts"),
                        }

                        run_with_timeout(lambda: self.mom.enqueue(to_user, msg), 8)

                        send_json_line(conn, {"type": "OK", "action": "ENQUEUE", "to": to_user})

                    elif typ == "FETCH":
                        user = str(req.get("user", "")).strip()
                        if not user:
                            send_json_line(conn, {"type": "ERROR", "message": "user vazio"})
                            continue

                        msgs = run_with_timeout(lambda: self.mom.fetch_all(user), 10)

                        send_json_line(conn, {"type": "FETCH_RESULT", "user": user, "messages": msgs})

                    else:
                        send_json_line(conn, {"type": "ERROR", "message": f"tipo desconhecido: {typ}"})

                except Exception as e:
                    # ✅ responde sempre (sem travar)
                    send_json_line(conn, {"type": "ERROR", "message": f"AMQP falhou: {e}"})

    def serve_forever(self):
        print(f"[offline_server] listening {self.host}:{self.port}")
        with socket.create_server((self.host, self.port), reuse_port=True) as srv:
            while True:
                conn, addr = srv.accept()
                t = threading.Thread(target=self.handle_client, args=(conn, addr), daemon=True)
                t.start()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=3000)
    ap.add_argument("--amqp", default=AMQP_URL)
    args = ap.parse_args()

    server = OfflineServer(args.host, args.port, args.amqp)
    try:
        server.serve_forever()
    finally:
        server.mom.close()


if __name__ == "__main__":
    main()
