#!/usr/bin/env python3
import argparse
import json
import socket
import threading
import time
from typing import Any, Dict, Optional, List
from PyQt5 import QtCore, QtWidgets


def send_json_line(conn: socket.socket, obj: Dict[str, Any]) -> None:
    data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
    conn.sendall(data)


def recv_json_line(fileobj) -> Optional[Dict[str, Any]]:
    line = fileobj.readline()
    if not line:
        return None
    return json.loads(line)


class UiBus(QtCore.QObject):
    delivered = QtCore.pyqtSignal(str, str, int)          # from, text, ts
    presence = QtCore.pyqtSignal(str, str)               # contact, status
    contacts = QtCore.pyqtSignal(list, dict)             # contacts_list, presence_partial
    info = QtCore.pyqtSignal(str)
    error = QtCore.pyqtSignal(str)


class ChatClientWindow(QtWidgets.QMainWindow):
    def __init__(self, user: str, chat_host: str, chat_port: int, off_host: str, off_port: int):
        super().__init__()
        self.user = user
        self.chat_host = chat_host
        self.chat_port = chat_port
        self.off_host = off_host
        self.off_port = off_port

        self.bus = UiBus()
        self.bus.delivered.connect(self._on_deliver)
        self.bus.presence.connect(self._on_presence)
        self.bus.contacts.connect(self._on_contacts_update)
        self.bus.info.connect(lambda m: QtWidgets.QMessageBox.information(self, "Info", m))
        self.bus.error.connect(lambda m: QtWidgets.QMessageBox.critical(self, "Erro", m))

        # rede
        self.sock = socket.create_connection((chat_host, chat_port), timeout=10)
        self.sock.settimeout(None)  # ✅ deixa bloqueante (não estoura no readline)
        self.sock_lock = threading.Lock()
        self.f = self.sock.makefile("r", encoding="utf-8", newline="\n")

        # estado
        self.status = "OFFLINE"
        self.contacts_list: List[str] = []
        self.presence_map: Dict[str, str] = {}
        self.current_contact: Optional[str] = None
        self.chat_history: Dict[str, List[str]] = {}

        # handshake
        self._register()
        self._create_queue()

        # UI
        self.setWindowTitle(f"Chat - {self.user}")
        self.resize(900, 600)
        self._build_ui()
        self._refresh_contacts()

        # thread de leitura
        self._stop_evt = threading.Event()
        self.reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self.reader_thread.start()

    # ------------------ Rede / protocolo ------------------
    def _register(self):
        send_json_line(self.sock, {"type": "REGISTER", "user": self.user})
        resp = recv_json_line(self.f)
        if not resp or resp.get("type") != "REGISTERED":
            raise RuntimeError(f"Falha no REGISTER: {resp}")
        self.status = resp.get("status", "OFFLINE")
        self.contacts_list = resp.get("contacts", [])
        self.presence_map.update(resp.get("presence", {}))

    def _create_queue(self):
        with socket.create_connection((self.off_host, self.off_port), timeout=8) as s:
            send_json_line(s, {"type": "CREATE_QUEUE", "user": self.user})
            _ = recv_json_line(s.makefile("r", encoding="utf-8", newline="\n"))

    def _fetch_offline_async(self):
        def worker():
            try:
                with socket.create_connection((self.off_host, self.off_port), timeout=10) as s:
                    send_json_line(s, {"type": "FETCH", "user": self.user})
                    resp = recv_json_line(s.makefile("r", encoding="utf-8", newline="\n"))
                if resp and resp.get("type") == "FETCH_RESULT":
                    msgs = resp.get("messages", [])
                    for m in msgs:
                        frm = m.get("from")
                        text = m.get("text")
                        ts = int(m.get("ts") or int(time.time()))
                        if frm and text is not None:
                            self.bus.delivered.emit(str(frm), str(text), ts)
            except Exception as e:
                self.bus.error.emit(f"Falha ao buscar mensagens offline: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def _send_status(self, status: str):
        with self.sock_lock:
            send_json_line(self.sock, {"type": "SET_STATUS", "user": self.user, "status": status})

    def _send_message(self, to_user: str, text: str):
        payload = {"type": "SEND", "from": self.user, "to": to_user, "text": text, "ts": int(time.time())}
        with self.sock_lock:
            send_json_line(self.sock, payload)

    def _add_contact(self, contact: str):
        with self.sock_lock:
            send_json_line(self.sock, {"type": "ADD_CONTACT", "user": self.user, "contact": contact})

    def _remove_contact(self, contact: str):
        with self.sock_lock:
            send_json_line(self.sock, {"type": "REMOVE_CONTACT", "user": self.user, "contact": contact})

    # ------------------ UI ------------------
    def _build_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)

        root = QtWidgets.QVBoxLayout(central)

        # Top bar
        top = QtWidgets.QHBoxLayout()
        root.addLayout(top)

        self.lbl_user = QtWidgets.QLabel(f"Usuário: {self.user}")
        top.addWidget(self.lbl_user)

        top.addStretch(1)

        self.btn_toggle = QtWidgets.QPushButton(f"Status: {self.status}")
        self.btn_toggle.setCheckable(True)
        self.btn_toggle.setChecked(self.status == "ONLINE")
        self.btn_toggle.clicked.connect(self._toggle_status_clicked)
        top.addWidget(self.btn_toggle)

        self.btn_add = QtWidgets.QPushButton("Adicionar contato")
        self.btn_add.clicked.connect(self._ui_add_contact)
        top.addWidget(self.btn_add)

        self.btn_remove = QtWidgets.QPushButton("Remover contato")
        self.btn_remove.clicked.connect(self._ui_remove_contact)
        top.addWidget(self.btn_remove)

        # Main split
        mid = QtWidgets.QHBoxLayout()
        root.addLayout(mid, 1)

        # Left: contacts
        left = QtWidgets.QVBoxLayout()
        mid.addLayout(left, 0)

        left.addWidget(QtWidgets.QLabel("Contatos"))
        self.list_contacts = QtWidgets.QListWidget()
        self.list_contacts.itemSelectionChanged.connect(self._on_select_contact)
        self.list_contacts.setMinimumWidth(240)
        left.addWidget(self.list_contacts, 1)

        # Right: chat
        right = QtWidgets.QVBoxLayout()
        mid.addLayout(right, 1)

        self.lbl_chat_title = QtWidgets.QLabel("Selecione um contato")
        right.addWidget(self.lbl_chat_title)

        self.txt_chat = QtWidgets.QTextEdit()
        self.txt_chat.setReadOnly(True)
        right.addWidget(self.txt_chat, 1)

        bottom = QtWidgets.QHBoxLayout()
        right.addLayout(bottom)

        self.edt_msg = QtWidgets.QLineEdit()
        self.edt_msg.setPlaceholderText("Digite sua mensagem…")
        self.edt_msg.returnPressed.connect(self._ui_send)
        bottom.addWidget(self.edt_msg, 1)

        self.btn_send = QtWidgets.QPushButton("Enviar")
        self.btn_send.clicked.connect(self._ui_send)
        bottom.addWidget(self.btn_send)

    def _refresh_contacts(self):
        self.list_contacts.clear()
        for c in self.contacts_list:
            st = self.presence_map.get(c, "OFFLINE")
            bullet = "🟢" if st == "ONLINE" else "⚪"
            item = QtWidgets.QListWidgetItem(f"{bullet} {c}")
            item.setData(QtCore.Qt.UserRole, c)
            self.list_contacts.addItem(item)

    def _refresh_chat_view(self):
        c = self.current_contact
        self.txt_chat.clear()
        if not c:
            return
        for line in self.chat_history.get(c, []):
            self.txt_chat.append(line)
        self.txt_chat.moveCursor(self.txt_chat.textCursor().End)

    def _append_message(self, contact: str, line: str):
        self.chat_history.setdefault(contact, []).append(line)

    # ------------------ Slots (signals) ------------------
    @QtCore.pyqtSlot(str, str, int)
    def _on_deliver(self, frm: str, text: str, ts: int):
        self._append_message(frm, f"{frm} [{ts}]: {text}")
        if self.current_contact == frm:
            self._refresh_chat_view()

    @QtCore.pyqtSlot(str, str)
    def _on_presence(self, contact: str, status: str):
        self.presence_map[contact] = status
        self._refresh_contacts()

    @QtCore.pyqtSlot(list, dict)
    def _on_contacts_update(self, contacts_list: list, presence_partial: dict):
        self.contacts_list = contacts_list
        self.presence_map.update(presence_partial or {})
        self._refresh_contacts()

    # ------------------ Handlers ------------------
    def _on_select_contact(self):
        items = self.list_contacts.selectedItems()
        if not items:
            return
        c = items[0].data(QtCore.Qt.UserRole)
        self.current_contact = c
        self.lbl_chat_title.setText(f"Chat com: {c}")
        self._refresh_chat_view()

    def _toggle_status_clicked(self):
        self.status = "ONLINE" if self.btn_toggle.isChecked() else "OFFLINE"
        self.btn_toggle.setText(f"Status: {self.status}")
        try:
            self._send_status(self.status)
            if self.status == "ONLINE":
                self._fetch_offline_async()
        except Exception as e:
            self.bus.error.emit(f"Falha ao mudar status: {e}")

    def _ui_send(self):
        if not self.current_contact:
            self.bus.info.emit("Escolha um contato primeiro.")
            return
        text = self.edt_msg.text().strip()
        if not text:
            return
        to_user = self.current_contact
        self.edt_msg.clear()

        # mostra no próprio chat
        ts = int(time.time())
        self._append_message(to_user, f"Eu [{ts}]: {text}")
        self._refresh_chat_view()

        try:
            self._send_message(to_user, text)
        except Exception as e:
            self.bus.error.emit(f"Falha ao enviar: {e}")

    def _ui_add_contact(self):
        c, ok = QtWidgets.QInputDialog.getText(self, "Adicionar", "Nome do contato:")
        if not ok:
            return
        c = (c or "").strip()
        if not c or c == self.user:
            return
        try:
            self._add_contact(c)
        except Exception as e:
            self.bus.error.emit(f"Falha ao adicionar: {e}")

    def _ui_remove_contact(self):
        if not self.current_contact:
            self.bus.info.emit("Selecione um contato para remover.")
            return
        c = self.current_contact
        if QtWidgets.QMessageBox.question(self, "Remover", f"Remover {c} da sua lista?") != QtWidgets.QMessageBox.Yes:
            return
        try:
            self._remove_contact(c)
            self.current_contact = None
            self.lbl_chat_title.setText("Selecione um contato")
            self._refresh_chat_view()
        except Exception as e:
            self.bus.error.emit(f"Falha ao remover: {e}")

    # ------------------ Thread de leitura ------------------
    def _reader_loop(self):
        while not self._stop_evt.is_set():
            try:
                msg = recv_json_line(self.f)
                if msg is None:
                    break
                typ = msg.get("type")

                if typ == "DELIVER":
                    frm = str(msg.get("from", ""))
                    text = str(msg.get("text", ""))
                    ts = int(msg.get("ts") or int(time.time()))
                    if frm:
                        self.bus.delivered.emit(frm, text, ts)

                elif typ == "PRESENCE":
                    contact = msg.get("contact")
                    st = msg.get("status", "OFFLINE")
                    if contact:
                        self.bus.presence.emit(str(contact), str(st))

                elif typ == "CONTACTS":
                    contacts_list = msg.get("contacts", self.contacts_list)
                    presence_partial = msg.get("presence", {})
                    self.bus.contacts.emit(list(contacts_list), dict(presence_partial))

                # OK / SENT / ERROR: opcional tratar
            except Exception:
                break

    def closeEvent(self, event):
        try:
            self._stop_evt.set()
            try:
                self.sock.close()
            except Exception:
                pass
        finally:
            event.accept()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", required=True)
    ap.add_argument("--chat-host", default="127.0.0.1")
    ap.add_argument("--chat-port", type=int, default=5000)
    ap.add_argument("--offline-host", default="127.0.0.1")
    ap.add_argument("--offline-port", type=int, default=3000)
    args = ap.parse_args()

    app = QtWidgets.QApplication([])
    w = ChatClientWindow(args.user, args.chat_host, args.chat_port, args.offline_host, args.offline_port)
    w.show()
    app.exec_()


if __name__ == "__main__":
    main()
