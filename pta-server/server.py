#!/usr/bin/env python3
# Servidor PTA, seguindo o protocolo do pta.pdf.

import argparse
import os
from pathlib import Path
import re
import select
import socket
import socketserver
import sys


BASE_DIR = Path(__file__).resolve().parent
MAX_REQUEST = 65536
REQUEST_GAP = 0.05
CHUNK_SIZE = 65536
SEQUENCE = re.compile(rb"-?[0-9]+\Z")


class PTAServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, users_path=BASE_DIR / "users.txt",
                 files_dir=BASE_DIR / "files"):
        self.users = frozenset(Path(users_path).read_text(encoding="ascii").splitlines())
        self.files_dir = Path(files_dir).resolve()
        super().__init__(address, PTAHandler)


class PTAHandler(socketserver.BaseRequestHandler):
    # Cada cliente tem sua própria identificação e sequência.

    def setup(self):
        self.ready = False
        self.expected_sequence = None
        self.pending = bytearray()
        self.read_eof = False
        self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def read_request(self):
        # O cliente do professor não manda quebra de linha. Esperamos uma pausa
        # curta pra juntar os dados, já que um recv pode trazer só parte do pedido.
        while True:
            if b"\n" in self.pending:
                line, _, remaining = self.pending.partition(b"\n")
                if len(line) > MAX_REQUEST:
                    raise ValueError("Pedido excede o limite de 64 KiB")
                self.pending = bytearray(remaining)
                return bytes(line).removesuffix(b"\r")
            if len(self.pending) > MAX_REQUEST:
                raise ValueError("Pedido excede o limite de 64 KiB")
            if self.pending:
                readable, _, _ = select.select([self.request], [], [], REQUEST_GAP)
                if not readable:
                    message = bytes(self.pending)
                    self.pending.clear()
                    return message
            chunk = self.request.recv(4096)
            if not chunk:
                if self.pending:
                    message = bytes(self.pending)
                    self.pending.clear()
                    return message
                return None
            self.pending.extend(chunk)

    def ignore_busy_requests(self, data):
        # Ignora os pedidos que chegam durante a resposta, mas conta a sequência
        # quando dá pra ler, pra não rejeitar o próximo pedido do cliente.
        for line in data.splitlines():
            sequence = line.partition(b" ")[0]
            if SEQUENCE.fullmatch(sequence):
                try:
                    number = int(sequence)
                except ValueError:
                    continue
                if self.expected_sequence is not None and number >= self.expected_sequence:
                    self.expected_sequence = number + 1

    def send_response(self, chunks):
        # send pode mandar só parte dos bytes. O restante fica pra próxima volta.
        if self.pending:
            self.ignore_busy_requests(bytes(self.pending))
            self.pending.clear()
        self.request.setblocking(False)
        try:
            for chunk in chunks:
                remaining = memoryview(chunk)
                while remaining:
                    readable, writable, _ = select.select(
                        [] if self.read_eof else [self.request], [self.request], [], 30
                    )
                    if not readable and not writable:
                        raise TimeoutError("Cliente não recebe a resposta")
                    if readable:
                        try:
                            discarded = self.request.recv(MAX_REQUEST)
                        except BlockingIOError:
                            discarded = None
                        if discarded == b"":
                            self.read_eof = True
                        if discarded:
                            self.ignore_busy_requests(discarded)
                    if writable:
                        try:
                            sent = self.request.send(remaining)
                        except BlockingIOError:
                            continue
                        if sent == 0:
                            raise ConnectionError("Cliente desconectado")
                        remaining = remaining[sent:]
        finally:
            self.request.setblocking(True)

    def reply(self, sequence, answer):
        self.send_response([(sequence + " " + answer).encode("ascii")])

    def parse(self, message):
        # Se o pedido estiver errado, devolve None junto da sequência da resposta.
        raw_sequence = message.partition(b" ")[0]
        fallback = str(self.expected_sequence if self.expected_sequence is not None else 0)
        if not SEQUENCE.fullmatch(raw_sequence):
            return fallback, None
        sequence = raw_sequence.decode("ascii")
        try:
            number = int(sequence)
        except ValueError:
            return fallback, None
        if self.expected_sequence is not None and number != self.expected_sequence:
            return sequence, None
        self.expected_sequence = number + 1
        try:
            fields = message.decode("ascii").split(" ")
        except UnicodeDecodeError:
            return sequence, None
        if len(fields) < 2 or any(not field or not field.isprintable() for field in fields):
            return sequence, None
        return sequence, fields[1:]

    def list_files(self, sequence):
        try:
            names = sorted(
                path.name for path in self.server.files_dir.iterdir()
                if path.is_file() and path.resolve().parent == self.server.files_dir
            )
            listing = ",".join(names)
            response = f"{sequence} ARQS {len(names)} {listing}".encode("ascii")
        except (OSError, UnicodeError):
            self.reply(sequence, "NOK")
            return
        self.send_response([response])

    def send_file(self, sequence, name):
        # Só deixa baixar arquivos de files/, sem caminhos como ../users.txt.
        path = self.server.files_dir / name
        try:
            if (name in {".", ".."} or "/" in name or "\\" in name
                    or path.resolve().parent != self.server.files_dir or not path.is_file()):
                self.reply(sequence, "NOK")
                return
            source = path.open("rb")
        except OSError:
            self.reply(sequence, "NOK")
            return

        with source:
            size = os.fstat(source.fileno()).st_size

            def chunks():
                # O cliente original espera o cabeçalho junto dos primeiros bytes.
                first = source.read(min(size, CHUNK_SIZE))
                yield f"{sequence} ARQ {size} ".encode("ascii") + first
                remaining = size - len(first)
                while remaining:
                    data = source.read(min(remaining, CHUNK_SIZE))
                    if not data:
                        raise ConnectionError("Arquivo alterado durante a transferência")
                    remaining -= len(data)
                    yield data

            self.send_response(chunks())

    def handle(self):
        try:
            while True:
                message = self.read_request()
                if message is None:
                    return
                sequence, fields = self.parse(message)
                if not self.ready:
                    if (fields is None or len(fields) != 2 or fields[0] != "CUMP"
                            or fields[1] not in self.server.users):
                        self.reply(sequence, "NOK")
                        return
                    self.ready = True
                    self.reply(sequence, "OK")
                    continue

                if fields == ["TERM"]:
                    self.reply(sequence, "OK")
                    return
                if fields == ["LIST"]:
                    self.list_files(sequence)
                elif fields and fields[0] == "PEGA" and len(fields) >= 2:
                    # Se vier mais de um nome, só usa o primeiro, como pede o PDF.
                    self.send_file(sequence, fields[1].split(",", 1)[0])
                else:
                    self.reply(sequence, "NOK")
        except ValueError:
            self.reply(str(self.expected_sequence or 0), "NOK")
        except (OSError, ConnectionError):
            # Se um cliente cair, os outros continuam sendo atendidos.
            return


def main():
    parser = argparse.ArgumentParser(description="Servidor PTA sobre TCP.")
    parser.add_argument("--host", default="0.0.0.0", help="Interface (padrão: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=11550, help="Porta TCP (padrão: 11550)")
    args = parser.parse_args()
    try:
        with PTAServer((args.host, args.port)) as server:
            print(f"Servidor PTA em {server.server_address[0]}:{server.server_address[1]}", flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
    except (OSError, UnicodeError, OverflowError) as error:
        print(f"Não foi possível iniciar o servidor: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
