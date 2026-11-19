"""A purifier's Bluetooth, held ready on this machine for every TARS process: what an Atom beside a purifier in another
room does (firmware/atom-lite-bt/src/main.cpp), here in Python with bleak, for a purifier in this machine's own range.

Made one connection a request, as the purifier asks, a command waited about eight seconds for its connection (the
Pi's Bluetooth, through its aluminium case); held ready, about a second and a half. And TARS's voice, Telegram and
web UI are three processes, while the purifier takes one connection at a time: so one holder, which they all use, as
they use an Atom. It speaks the Atom's protocol (purifier.Atom is its client) on 127.0.0.1 only:

    [[home.devices]]
    name = "living room air purifier"
    kind = "my_house.purifier"
    address = "48:87:2D:1F:FC:9B"
    atom = "127.0.0.1"                    # through the bridge: python -m examples.my_house.bridge <address>

The key is the Atoms', PURIFIER_ATOM_KEY in .env. As a service: tars-purifier-bridge.service, beside this file.

What the purifier does, found on 2026-11-18, and why it's held so: a request's commands are taken only on a fresh
connection, after the hello frames, so each connection carries one request and is replaced after; and it drops a
connection after about 13 seconds without a hello, so the ready one gets hellos every few seconds."""

import argparse
import asyncio
import hashlib
import hmac
import secrets
import time
from pathlib import Path

from .purifier import ATOM_KEY, ATOM_PORT, CONNECT_S, HELLO, LIVE, WRITE, frame, power

KEEP_S = 5.0  # a hello this often keeps the ready connection up
STATUS_S = 2.5  # a new connection counts once it's reported a status
TRIES = 3  # when a request is waiting for one
STUCK_S = 120.0  # failing to connect this long, this machine's Bluetooth is switched off and on, once in a while


class Bridge:
    """The connection to the purifier at `address`, kept ready, and the sessions that use it, one at a time."""

    def __init__(self, address: str, key: str, client_class=None):
        if client_class is None:
            from bleak import BleakClient

            client_class = BleakClient
        self.address, self._key, self._client_class = address, key, client_class
        self.client = None
        self.used = False  # carried a request: replaced after it
        self.latest = b""  # its last status
        self.last_heard = 0.0  # time.monotonic() of its last status; 0, never
        self.listener: asyncio.Queue[bytes] | None = None  # the session's, while one holds it
        self.lock = asyncio.Lock()  # a session, or the keeping, at a time
        self._kept = 0.0
        self._failing_since = 0.0

    def linked(self) -> bool:
        return self.client is not None and self.client.is_connected

    def _live(self, _, data: bytearray) -> None:
        self.latest, self.last_heard = bytes(data), time.monotonic()
        if self.listener is not None:
            self.listener.put_nowait(bytes(data))

    async def drop(self) -> None:
        client, self.client, self.used = self.client, None, False
        if client is not None:
            try:
                await client.disconnect()
            except Exception as e:  # noqa: BLE001 - gone either way
                print(f"(purifier bridge: letting go: {e!r})")

    async def connect(self, tries: int) -> str:
        """Connected, subscribed and a status heard on it: "", or why not."""
        await self.drop()
        why = "it didn't answer"
        for _ in range(tries):
            client = self._client_class(self.address, timeout=CONNECT_S)
            heard = self.last_heard
            try:
                await client.connect()
                await client.start_notify(LIVE, self._live)
                deadline = time.monotonic() + STATUS_S
                while self.last_heard == heard and time.monotonic() < deadline:
                    await asyncio.sleep(0.05)
                if self.last_heard != heard:
                    self.client, self._kept, self._failing_since = client, 0.0, 0.0
                    return ""
                why = "it sent no status"
            except Exception as e:  # noqa: BLE001 - bleak's errors, BlueZ's and timeouts: tried again
                why = f"{type(e).__name__}: {e}"
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001, S110 - it may never have connected
                pass
        return why

    async def hello(self) -> None:
        for _ in range(2):
            await self.client.write_gatt_char(WRITE, frame(HELLO, 0, 1), response=True)
            await asyncio.sleep(0.12)

    async def keep(self) -> None:
        """For as long as it runs: a ready connection, made again whenever it's gone, and kept up with hellos."""
        while True:
            async with self.lock:
                try:
                    await self._keep_once()
                except Exception as e:  # noqa: BLE001 - kept trying
                    print(f"(purifier bridge: {e!r})")
                    await self.drop()
            await asyncio.sleep(0.5)

    async def _keep_once(self) -> None:
        if self.client is not None and not self.client.is_connected:
            await self.drop()  # it let go
        if self.client is None:
            why = await self.connect(1)
            if why:
                now = time.monotonic()
                self._failing_since = self._failing_since or now
                if now - self._failing_since >= STUCK_S:
                    print(f"(purifier bridge: no connection for {STUCK_S:.0f} s ({why}); restarting Bluetooth)")
                    await asyncio.to_thread(power, "off")
                    await asyncio.sleep(2)
                    await asyncio.to_thread(power, "on")
                    self._failing_since = now
                await asyncio.sleep(1.0)
            return
        if time.monotonic() - self._kept >= KEEP_S:
            await self.hello()
            self._kept = time.monotonic()

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """One TARS process's request, in the Atom's protocol (main.cpp says it)."""

        def say(line: str) -> None:
            writer.write(f"{line}\n".encode())

        nonce = secrets.token_hex(16)
        say(f"tars-bt 1 {nonce}")
        try:
            proof = (await asyncio.wait_for(reader.readline(), 5.0)).decode().strip().removeprefix("auth ")
        except TimeoutError, UnicodeDecodeError, ConnectionError:
            writer.close()
            return
        expected = hmac.new(self._key.encode(), nonce.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(proof, expected):
            say("refused")
            writer.close()
            return
        say("ok")
        async with self.lock:
            requesting = False
            queue: asyncio.Queue[bytes] = asyncio.Queue()
            pump = asyncio.create_task(self._pump(queue, say))
            try:
                while line := (await reader.readline()).decode(errors="replace").strip():
                    if line == "link":
                        why = "" if self.linked() and not self.used else await self.connect(TRIES)
                        if why:
                            say(f"failed {why}")
                            continue
                        requesting = self.used = True
                        self.listener = queue
                        say("linked")
                        say(f"live {self.latest.hex()}")
                    elif line == "unlink":
                        requesting, self.listener = False, None
                        say("unlinked")  # first: letting go takes the Pi's Bluetooth a couple of seconds
                        await writer.drain()
                        await self.drop()
                    elif line == "heard":
                        say(f"heard {int((time.monotonic() - self.last_heard) * 1000) if self.last_heard else -1}")
                    elif line.startswith("send "):
                        if not (requesting and self.linked()):
                            say("failed not linked")
                            continue
                        try:
                            await self.client.write_gatt_char(WRITE, bytes.fromhex(line[5:]), response=True)
                            say("sent")
                        except Exception as e:  # noqa: BLE001 - said to TARS, which says it
                            say(f"failed {type(e).__name__}: {e}")
                    else:
                        say("failed no such command")
                    await writer.drain()
            except ConnectionError:
                pass
            finally:
                pump.cancel()
                self.listener = None
                if requesting:
                    await self.drop()
                writer.close()

    @staticmethod
    async def _pump(queue: asyncio.Queue[bytes], say) -> None:
        while True:
            say(f"live {(await queue.get()).hex()}")


async def run(address: str, key: str, port: int = ATOM_PORT) -> None:
    bridge = Bridge(address, key)
    server = await asyncio.start_server(bridge.serve, "127.0.0.1", port)
    print(f"Purifier bridge for {address} on 127.0.0.1:{port}")
    async with server:
        await asyncio.gather(server.serve_forever(), bridge.keep())


def main() -> None:
    from dotenv import dotenv_values

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("address", help="the purifier's Bluetooth address")
    parser.add_argument("--env", default=".env", help="where PURIFIER_ATOM_KEY is (default: .env)")
    args = parser.parse_args()
    key = dotenv_values(Path(args.env)).get(ATOM_KEY)
    if not key:
        raise SystemExit(f"Put the key in {args.env} as {ATOM_KEY} (examples/my_house/README.md)")
    asyncio.run(run(args.address, key))


if __name__ == "__main__":
    main()
