# Chat MOM Offline (Python)

Aplicação de chat distribuído em Python com:

* **Cliente com interface gráfica (PyQt5)**
* **Servidor principal (TCP Socket)** para presença e entrega em tempo real
* **Servidor remoto de mensagens offline (TCP Socket)** que armazena mensagens usando **MOM (RabbitMQ/AMQP via CloudAMQP)**
* **Filas por usuário** (`queue.<usuario>`) para mensagens offline
* **Lista de contatos** com **adicionar/remover** e indicação **online/offline**

## Arquitetura

* `chat_server.py` (porta padrão **5000**)

  * registra usuários
  * mantém presença online/offline
  * entrega instantânea se destinatário estiver online
  * encaminha para o servidor offline se destinatário estiver offline

* `offline_server.py` (porta padrão **6000**)

  * cria fila do usuário ao entrar (`CREATE_QUEUE`)
  * enfileira mensagens offline (`ENQUEUE`)
  * devolve mensagens pendentes (`FETCH`)
  * usa **AMQPS** para publicar/consumir do RabbitMQ (CloudAMQP)

* `client_gui.py`

  * interface gráfica com PyQt5
  * toggle online/offline
  * lista de contatos sempre visível
  * envia mensagens e exibe mensagens recebidas
  * ao ficar online, busca mensagens offline (`FETCH`)

---

## Requisitos

* Python **3.10+** (recomendado 3.11/3.12)
* Conta RabbitMQ (CloudAMQP) **ou** RabbitMQ local
* Dependências Python:

  * `PyQt5`
  * `pika`

---

## Instalação

### 1) Clonar e entrar na pasta

```bash
git clone <seu-repo.git>
cd chat-mom-offline
```

### 2) Criar ambiente virtual (recomendado)

#### macOS / Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
```

#### Windows (PowerShell)

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
```

### 3) Instalar dependências

```bash
pip install -U pip
pip install PyQt5 pika
```

---

## Configuração do RabbitMQ (AMQP)

### Opção A — CloudAMQP (AMQPS)

No arquivo `offline_server.py`, a constante `AMQP_URL` já aponta para o endpoint do CloudAMQP.

> ⚠️ Recomendação: para projetos públicos, não exponha credenciais no GitHub.
> Use variável de ambiente e leia via `os.getenv()`.

### Opção B — RabbitMQ local (Docker)

Se preferir rodar localmente:

```bash
docker run -it --rm -p 5672:5672 -p 15672:15672 rabbitmq:3-management
```

A URL local (não TLS) seria:

```text
amqp://guest:guest@localhost:5672/%2F
```

---

## Executando o projeto

Abra **3 terminais** (ou abas).

### Terminal 1 — Servidor Offline (porta 6000)

```bash
python offline_server.py --port 6000
```

Saída esperada:

```
[offline_server] listening 0.0.0.0:6000
```

### Terminal 2 — Servidor Principal (porta 5000)

```bash
python chat_server.py --port 5000 --offline-host 127.0.0.1 --offline-port 6000
```

Saída esperada:

```
[chat_server] listening 0.0.0.0:5000 ...
```

### Terminal 3 — Cliente 1 (PyQt5)

```bash
python client_gui.py --user ana
```

### Terminal 4 — Cliente 2 (PyQt5)

```bash
python client_gui.py --user bia
```

---

## Como testar (roteiro de demonstração)

1. Abra o cliente `ana` e `bia`.
2. Em `ana`, clique em **Adicionar contato** e adicione `bia`.
3. Em `bia`, deixe o status **OFFLINE** (botão Status).
4. Em `ana`, envie mensagens para `bia`:

   * as mensagens vão para a fila `queue.bia` no RabbitMQ (offline).
5. Em `bia`, altere para **ONLINE**:

   * o cliente faz `FETCH` e carrega todas as mensagens pendentes.
6. Com ambos **ONLINE**, a entrega passa a ser instantânea.

---

## Portas e argumentos (opcional)

### Cliente

```bash
python client_gui.py --user joao --chat-host 127.0.0.1 --chat-port 5000 --offline-host 127.0.0.1 --offline-port 6000
```

### Servidor principal

```bash
python chat_server.py --host 0.0.0.0 --port 5000 --offline-host 127.0.0.1 --offline-port 6000
```

### Servidor offline

```bash
python offline_server.py --host 0.0.0.0 --port 6000
```

---

## Solução de problemas

### 1) Cliente trava/timeout no REGISTER

* Confirme se o `chat_server.py` está rodando na porta 5000.
* Teste resposta:

```bash
python - <<'PY'
import socket, json
s = socket.create_connection(("127.0.0.1", 5000), timeout=5)
s.sendall((json.dumps({"type":"REGISTER","user":"teste"})+"\n").encode())
print(s.recv(4096).decode())
PY
```

### 2) Timeout no CREATE_QUEUE (offline_server)

* Use timeout maior no teste (AMQPS pode demorar):

```bash
python - <<'PY'
import socket, json
s = socket.create_connection(("127.0.0.1", 6000), timeout=30)
s.settimeout(30)
s.sendall((json.dumps({"type":"CREATE_QUEUE","user":"teste"})+"\n").encode())
print(s.recv(4096).decode())
PY
```

### 3) Ver quem está usando a porta 6000 (macOS/Linux)

```bash
lsof -nP -iTCP:6000 -sTCP:LISTEN
```

---

## Estrutura do repositório

```
chat-mom-offline/
├── chat_server.py
├── offline_server.py
└── client_gui.py
```

---

## Licença

Uso acadêmico / educacional.
