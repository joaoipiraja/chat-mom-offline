#!/usr/bin/env python3
import argparse
import json
import os
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Set

CONTACTS_FILE = "contacts.json"


def send_json_line(conn: socket.socket, obj: Dict[str, Any]) -> None:
    data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
    conn.sendall(data)


def recv_json_line(fileobj) -> Optional[Dict[str, Any]]:
    line = fileobj.readline()
    if not line:
        return None
    return json.loads(line)


def load_contacts() -> Dict[str, Set[str]]:
    if not os.path.exists(CONTACTS_FILE):
        return {}
    with open(CONTACTS_FILE, "r", encoding="utf-8") as f:
        raw = json.load(f)
    return {u: set(lst) for u, lst in raw.items()}


def save_contacts(contacts: Dict[str, Set[str]]) -> None:
    raw = {u: sorted(list(s)) for u, s in contacts.items()}
    with open(CONTACTS_FILE, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)


@dataclass
class ClientConn:
    user: str
    conn: socket.socket
    lock: threading.Lock  # para send thread-safe


class ChatServer:
    def __init__(self, host: str, port: int, offline_host: str, offline_port: int):
        self.host = host
        self.port = port
        self.offline_host = offline_host
        self.offline_port = offline_port

        self._clients: Dict[str, ClientConn] = {}
        self._status: Dict[str, str] = {}  # ONLINE/OFFLINE
        self._contacts: Dict[str, Set[str]] = load_contacts()
        self._lock = threading.Lock()

    def _presence_broadcast(self, user: str, status: str) -> None:
        # avisa quem tem "user" na lista de contatos
        with self._lock:
            targets = [owner for owner, friends in self._contacts.items()
                       if user in friends and owner in self._clients]
            client_objs = {owner: self._clients[owner] for owner in targets}

        for owner, cc in client_objs.items():
            try:
                with cc.lock:
                    send_json_line(cc.conn, {"type": "PRESENCE", "contact": user, "status": status})
            except Exception:
                # ignora falhas de envio (cliente pode ter caído)
                pass

    def _send_to_user(self, to_user: str, payload: Dict[str, Any]) -> bool:
        with self._lock:
            cc = self._clients.get(to_user)
        if not cc:
            return False
        try:
            with cc.lock:
                send_json_line(cc.conn, payload)
            return True
        except Exception:
            return False

    def _is_online(self, user: str) -> bool:
        with self._lock:
            return self._status.get(user) == "ONLINE" and user in self._clients

    def _offline_enqueue(self, msg: Dict[str, Any]) -> None:
        # ChatServer -> OfflineMsgServer via socket
        with socket.create_connection((self.offline_host, self.offline_port), timeout=5) as s:
            send_json_line(s, {"type": "ENQUEUE", **msg})
            # opcional: lê resposta (se não houver, segue)
            try:
                s.settimeout(5)
                f = s.makefile("r", encoding="utf-8", newline="\n")
                _ = recv_json_line(f)
            except Exception:
                pass

    def handle_client(self, conn: socket.socket, addr):
        """
        ✅ Correção: não "explode" por timeout.
        - Mantém um timeout (ex.: 120s) para conexões que não enviam nada.
        - Se estourar timeout em readline(), encerra a conexão silenciosamente.
        """
        conn.settimeout(120)
        user: Optional[str] = None
        f = conn.makefile("r", encoding="utf-8", newline="\n")

        try:
            while True:
                try:
                    req = recv_json_line(f)
                except (TimeoutError, socket.timeout):
                    # cliente conectou e não mandou nada a tempo -> fecha sem traceback
                    return
                except Exception:
                    # qualquer erro de parsing/leitura -> fecha
                    return

                if req is None:
                    return

                typ = req.get("type")

                if typ == "REGISTER":
                    user = str(req.get("user", "")).strip()
                    if not user:
                        send_json_line(conn, {"type": "ERROR", "message": "user vazio"})
                        continue

                    with self._lock:
                        self._clients[user] = ClientConn(user=user, conn=conn, lock=threading.Lock())
                        self._status.setdefault(user, "OFFLINE")
                        self._contacts.setdefault(user, set())
                        save_contacts(self._contacts)

                        # snapshot para responder sem segurar lock depois
                        contacts_list = sorted(list(self._contacts[user]))
                        presence_map = {c: self._status.get(c, "OFFLINE") for c in self._contacts[user]}
                        current_status = self._status[user]

                    send_json_line(conn, {
                        "type": "REGISTERED",
                        "user": user,
                        "status": current_status,
                        "contacts": contacts_list,
                        "presence": presence_map,
                    })
                    self._presence_broadcast(user, current_status)

                elif typ == "SET_STATUS":
                    if not user:
                        send_json_line(conn, {"type": "ERROR", "message": "não registrado"})
                        continue
                    st = str(req.get("status", "")).upper()
                    if st not in ("ONLINE", "OFFLINE"):
                        send_json_line(conn, {"type": "ERROR", "message": "status inválido"})
                        continue
                    with self._lock:
                        self._status[user] = st
                    send_json_line(conn, {"type": "OK", "action": "SET_STATUS", "status": st})
                    self._presence_broadcast(user, st)

                elif typ == "ADD_CONTACT":
                    if not user:
                        send_json_line(conn, {"type": "ERROR", "message": "não registrado"})
                        continue
                    contact = str(req.get("contact", "")).strip()
                    if not contact or contact == user:
                        send_json_line(conn, {"type": "ERROR", "message": "contact inválido"})
                        continue

                    with self._lock:
                        self._contacts.setdefault(user, set()).add(contact)
                        save_contacts(self._contacts)
                        contacts_list = sorted(list(self._contacts[user]))
                        presence = self._status.get(contact, "OFFLINE")

                    send_json_line(conn, {
                        "type": "CONTACTS",
                        "contacts": contacts_list,
                        "presence": {contact: presence},
                    })

                elif typ == "REMOVE_CONTACT":
                    if not user:
                        send_json_line(conn, {"type": "ERROR", "message": "não registrado"})
                        continue
                    contact = str(req.get("contact", "")).strip()
                    with self._lock:
                        self._contacts.setdefault(user, set()).discard(contact)
                        save_contacts(self._contacts)
                        contacts_list = sorted(list(self._contacts[user]))
                    send_json_line(conn, {"type": "CONTACTS", "contacts": contacts_list})

                elif typ == "SEND":
                    if not user:
                        send_json_line(conn, {"type": "ERROR", "message": "não registrado"})
                        continue

                    to_user = str(req.get("to", "")).strip()
                    text = str(req.get("text", ""))
                    ts = req.get("ts", int(time.time()))
                    if not to_user or not text:
                        send_json_line(conn, {"type": "ERROR", "message": "to/text inválidos"})
                        continue

                    msg = {"from": user, "to": to_user, "text": text, "ts": ts}

                    if self._is_online(to_user):
                        ok = self._send_to_user(to_user, {
                            "type": "DELIVER",
                            "from": user,
                            "text": text,
                            "ts": ts
                        })
                        send_json_line(conn, {"type": "SENT", "mode": "ONLINE" if ok else "OFFLINE"})
                        if not ok:
                            try:
                                self._offline_enqueue(msg)
                            except Exception:
                                # se offline server não responder, não derruba este handler
                                pass
                    else:
                        try:
                            self._offline_enqueue(msg)
                            send_json_line(conn, {"type": "SENT", "mode": "OFFLINE"})
                        except Exception as e:
                            send_json_line(conn, {"type": "ERROR", "message": f"offline enqueue falhou: {e}"})

                else:
                    send_json_line(conn, {"type": "ERROR", "message": f"tipo desconhecido: {typ}"})

        finally:
            if user:
                with self._lock:
                    self._clients.pop(user, None)
                    # Se quiser manter status OFFLINE ao desconectar:
                    self._status[user] = "OFFLINE"
                self._presence_broadcast(user, "OFFLINE")
            try:
                conn.close()
            except Exception:
                pass

    def serve_forever(self):
        print(f"[chat_server] listening {self.host}:{self.port} (offline={self.offline_host}:{self.offline_port})")
        with socket.create_server((self.host, self.port), reuse_port=True) as srv:
            while True:
                conn, addr = srv.accept()
                t = threading.Thread(target=self.handle_client, args=(conn, addr), daemon=True)
                t.start()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--offline-host", default="127.0.0.1")
    ap.add_argument("--offline-port", type=int, default=6000)
    args = ap.parse_args()

    server = ChatServer(args.host, args.port, args.offline_host, args.offline_port)
    server.serve_forever()


if __name__ == "__main__":
    main()
