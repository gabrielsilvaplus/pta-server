import concurrent.futures
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest

from server import BASE_DIR, PTAServer


def receive_exact(connection, size):
    result = bytearray()
    while len(result) < size:
        chunk = connection.recv(size - len(result))
        if not chunk:
            raise AssertionError("Conexão fechada antes de completar a resposta")
        result.extend(chunk)
    return bytes(result)


def receive_file(connection, sequence):
    header = bytearray()
    while header.count(b" ") < 3:
        header.extend(receive_exact(connection, 1))
    fields = header.split(b" ")
    if fields[:2] != [str(sequence).encode(), b"ARQ"]:
        raise AssertionError(f"Cabeçalho inesperado: {header!r}")
    return receive_exact(connection, int(fields[2]))


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = PTAServer(("127.0.0.1", 0))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def connect(self):
        connection = socket.create_connection(self.server.server_address, timeout=3)
        self.addCleanup(connection.close)
        return connection

    def exchange(self, connection, message, expected):
        connection.sendall(message)
        self.assertEqual(receive_exact(connection, len(expected)), expected)

    def login(self, connection, sequence=0, user="user1"):
        self.exchange(connection, f"{sequence} CUMP {user}".encode(),
                      f"{sequence} OK".encode())

    def expected_listing(self, sequence):
        names = sorted(p.name for p in (BASE_DIR / "files").iterdir() if p.is_file())
        return f"{sequence} ARQS {len(names)} {','.join(names)}".encode()

    def test_all_valid_users(self):
        for user in ("user1", "user2", "user3"):
            with self.subTest(user=user):
                self.login(self.connect(), 52, user)

    def test_unknown_and_wrong_case_users_close_connection(self):
        for user in ("laser1212", "User1", "USER1"):
            with self.subTest(user=user):
                connection = self.connect()
                self.exchange(connection, f"31 CUMP {user}".encode(), b"31 NOK")
                self.assertEqual(connection.recv(1), b"")

    def test_commands_before_login_close_connection(self):
        for command in (b"LIST", b"PEGA missing", b"TERM", b"TRAP"):
            with self.subTest(command=command):
                connection = self.connect()
                self.exchange(connection, b"8 " + command, b"8 NOK")
                self.assertEqual(connection.recv(1), b"")

    def test_listing(self):
        connection = self.connect()
        self.login(connection)
        self.exchange(connection, b"1 LIST", self.expected_listing(1))

    def test_every_original_file_byte_for_byte(self):
        connection = self.connect()
        self.login(connection, 100)
        for sequence, path in enumerate(sorted((BASE_DIR / "files").iterdir()), 101):
            with self.subTest(file=path.name):
                connection.sendall(f"{sequence} PEGA {path.name}".encode())
                self.assertEqual(receive_file(connection, sequence), path.read_bytes())

    def test_missing_files_and_paths_keep_connection(self):
        connection = self.connect()
        self.login(connection)
        for sequence, name in enumerate(("missing", "../users.txt", "/etc/passwd", ".", "..", "a\\b"), 1):
            self.exchange(connection, f"{sequence} PEGA {name}".encode(),
                          f"{sequence} NOK".encode())
        self.exchange(connection, b"7 LIST", self.expected_listing(7))

    def test_invalid_commands_keep_connection(self):
        connection = self.connect()
        self.login(connection)
        commands = (b"TRAP", b"CUMP user1", b"LIST extra", b"TERM extra",
                    b"PEGA", b"list", b" LIST", b"LIST ", b"LIST\t", b"CUMP \xff")
        for sequence, command in enumerate(commands, 1):
            self.exchange(connection, str(sequence).encode() + b" " + command,
                          f"{sequence} NOK".encode())
        self.exchange(connection, b"11 TERM", b"11 OK")
        self.assertEqual(connection.recv(1), b"")

    def test_invalid_first_messages(self):
        for message, reply in ((b"oops", b"0 NOK"), (b"7", b"7 NOK"),
                               (b"7 CUMP", b"7 NOK"), (b"7  CUMP user1", b"7 NOK"),
                               (b"7 CUMP user1 extra", b"7 NOK")):
            with self.subTest(message=message):
                connection = self.connect()
                self.exchange(connection, message, reply)
                self.assertEqual(connection.recv(1), b"")

    def test_sequence_validation_and_recovery(self):
        connection = self.connect()
        self.login(connection, 45)
        self.exchange(connection, b"45 LIST", b"45 NOK")
        self.exchange(connection, b"48 LIST", b"48 NOK")
        self.exchange(connection, b"not-a-number LIST", b"46 NOK")
        self.exchange(connection, b"46 LIST", self.expected_listing(46))
        self.exchange(connection, b"47 TERM", b"47 OK")

    def test_term_closes_connection(self):
        connection = self.connect()
        self.login(connection)
        self.exchange(connection, b"1 TERM", b"1 OK")
        self.assertEqual(connection.recv(1), b"")

    def test_fragmented_request(self):
        connection = self.connect()
        for part in (b"123 ", b"CU", b"MP us", b"er1"):
            connection.sendall(part)
            time.sleep(0.005)
        self.assertEqual(receive_exact(connection, 6), b"123 OK")
        self.exchange(connection, b"124 TERM", b"124 OK")

    def test_lf_and_crlf(self):
        connection = self.connect()
        self.exchange(connection, b"10 CUMP user1\n", b"10 OK")
        self.exchange(connection, b"11 LIST\r\n", self.expected_listing(11))
        self.exchange(connection, b"12 TERM\n", b"12 OK")

    def test_extra_file_requests_are_ignored(self):
        path = next(p for p in sorted((BASE_DIR / "files").iterdir()) if p.stat().st_size)
        connection = self.connect()
        self.login(connection)
        for sequence, suffix in ((1, " missing"), (2, ",missing")):
            connection.sendall(f"{sequence} PEGA {path.name}{suffix}".encode())
            self.assertEqual(receive_file(connection, sequence), path.read_bytes())

    def test_concurrent_clients_have_independent_sessions(self):
        address = self.server.server_address

        def session(index):
            with socket.create_connection(address, timeout=3) as connection:
                sequence = index * 100
                connection.sendall(f"{sequence} CUMP user1".encode())
                if receive_exact(connection, len(f"{sequence} OK")) != f"{sequence} OK".encode():
                    return False
                connection.sendall(f"{sequence + 1} LIST".encode())
                expected = self.expected_listing(sequence + 1)
                return receive_exact(connection, len(expected)) == expected

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            self.assertTrue(all(pool.map(session, range(8))))

    def test_write_half_close_still_receives_reply(self):
        connection = self.connect()
        connection.sendall(b"0 CUMP user1")
        connection.shutdown(socket.SHUT_WR)
        self.assertEqual(receive_exact(connection, 4), b"0 OK")
        self.assertEqual(connection.recv(1), b"")


class FixtureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.files = self.root / "files"
        self.files.mkdir()
        users = self.root / "users.txt"
        users.write_text("alice\n", encoding="ascii")
        self.server = PTAServer(("127.0.0.1", 0), users, self.files)
        self.server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8192)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.connection = socket.create_connection(self.server.server_address, timeout=5)
        self.addCleanup(self.connection.close)
        self.connection.sendall(b"0 CUMP alice")
        self.assertEqual(receive_exact(self.connection, 4), b"0 OK")

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_empty_directory(self):
        self.connection.sendall(b"1 LIST")
        self.assertEqual(receive_exact(self.connection, 9), b"1 ARQS 0 ")

    def test_listing_failure(self):
        self.files.rmdir()
        self.connection.sendall(b"1 LIST")
        self.assertEqual(receive_exact(self.connection, 5), b"1 NOK")

    def test_large_listing(self):
        names = [f"file-{i:03d}-{'x' * 70}.txt" for i in range(100)]
        for name in names:
            (self.files / name).touch()
        self.connection.sendall(b"1 LIST")
        expected = f"1 ARQS 100 {','.join(names)}".encode()
        self.assertGreater(len(expected), 2048)
        self.assertEqual(receive_exact(self.connection, len(expected)), expected)

    def test_large_binary_file(self):
        content = bytes(range(256)) * 8192
        (self.files / "binary.dat").write_bytes(content)
        self.connection.sendall(b"1 PEGA binary.dat")
        self.assertEqual(receive_file(self.connection, 1), content)

    def test_symlink_outside_files_is_not_served_or_listed(self):
        (self.root / "private.txt").write_text("private")
        (self.files / "link.txt").symlink_to(self.root / "private.txt")
        self.connection.sendall(b"1 PEGA link.txt")
        self.assertEqual(receive_exact(self.connection, 5), b"1 NOK")
        self.connection.sendall(b"2 LIST")
        self.assertEqual(receive_exact(self.connection, 9), b"2 ARQS 0 ")

    def test_pipelined_command_during_operation_is_ignored(self):
        self.connection.sendall(b"1 LIST\n2 TERM\n")
        self.assertEqual(receive_exact(self.connection, 9), b"1 ARQS 0 ")
        self.connection.sendall(b"3 LIST")
        self.assertEqual(receive_exact(self.connection, 9), b"3 ARQS 0 ")

    def test_command_received_during_file_transfer_is_ignored(self):
        content = bytes(range(256)) * 8192
        (self.files / "large.bin").write_bytes(content)
        self.connection.sendall(b"1 PEGA large.bin")
        header = bytearray()
        while header.count(b" ") < 3:
            header.extend(receive_exact(self.connection, 1))
        self.assertEqual(header, f"1 ARQ {len(content)} ".encode())
        self.connection.sendall(b"2 TERM")
        time.sleep(0.02)
        self.assertEqual(receive_exact(self.connection, len(content)), content)
        self.connection.sendall(b"3 TERM")
        self.assertEqual(receive_exact(self.connection, 4), b"3 OK")
        self.assertEqual(self.connection.recv(1), b"")

    def test_oversized_request_is_rejected(self):
        self.connection.sendall(b"1 " + b"x" * 65536)
        self.assertEqual(receive_exact(self.connection, 5), b"1 NOK")
        self.assertEqual(self.connection.recv(1), b"")


if __name__ == "__main__":
    unittest.main()
