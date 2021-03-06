"""Http related parsers and protocol."""

import asyncio
import collections
import socket
import zlib
from contextlib import suppress

from .abc import AbstractPayloadWriter
from .helpers import noop


__all__ = ('PayloadWriter', 'HttpVersion', 'HttpVersion10', 'HttpVersion11',
           'StreamWriter')

HttpVersion = collections.namedtuple('HttpVersion', ['major', 'minor'])
HttpVersion10 = HttpVersion(1, 0)
HttpVersion11 = HttpVersion(1, 1)


if hasattr(socket, 'TCP_CORK'):  # pragma: no cover
    CORK = socket.TCP_CORK
elif hasattr(socket, 'TCP_NOPUSH'):  # pragma: no cover
    CORK = socket.TCP_NOPUSH
else:  # pragma: no cover
    CORK = None


class StreamWriter:

    def __init__(self, protocol, transport, loop):
        self._protocol = protocol
        self._loop = loop
        self._tcp_nodelay = False
        self._tcp_cork = False
        self._socket = transport.get_extra_info('socket')
        self._waiters = []
        self.transport = transport

    @property
    def tcp_nodelay(self):
        return self._tcp_nodelay

    def set_tcp_nodelay(self, value):
        value = bool(value)
        if self._tcp_nodelay == value:
            return
        if self._socket is None:
            return
        if self._socket.family not in (socket.AF_INET, socket.AF_INET6):
            return

        # socket may be closed already, on windows OSError get raised
        with suppress(OSError):
            if self._tcp_cork:
                if CORK is not None:  # pragma: no branch
                    self._socket.setsockopt(socket.IPPROTO_TCP, CORK, False)
                    self._tcp_cork = False

            self._socket.setsockopt(
                socket.IPPROTO_TCP, socket.TCP_NODELAY, value)
            self._tcp_nodelay = value

    @property
    def tcp_cork(self):
        return self._tcp_cork

    def set_tcp_cork(self, value):
        value = bool(value)
        if self._tcp_cork == value:
            return
        if self._socket is None:
            return
        if self._socket.family not in (socket.AF_INET, socket.AF_INET6):
            return

        with suppress(OSError):
            if self._tcp_nodelay:
                self._socket.setsockopt(
                    socket.IPPROTO_TCP, socket.TCP_NODELAY, False)
                self._tcp_nodelay = False
            if CORK is not None:  # pragma: no branch
                self._socket.setsockopt(socket.IPPROTO_TCP, CORK, value)
                self._tcp_cork = value

    async def drain(self):
        """Flush the write buffer.

        The intended use is to write

          await w.write(data)
          await w.drain()
        """
        if self._protocol.transport is not None:
            await self._protocol._drain_helper()


class PayloadWriter(AbstractPayloadWriter):

    def __init__(self, stream, loop):
        self._stream = stream
        self._transport = None

        self.loop = loop
        self.length = None
        self.chunked = False
        self.buffer_size = 0
        self.output_size = 0

        self._eof = False
        self._compress = None
        self._drain_waiter = None
        self._transport = self._stream.transport

    async def get_transport(self):
        return self._transport

    def enable_chunking(self):
        self.chunked = True

    def enable_compression(self, encoding='deflate'):
        zlib_mode = (16 + zlib.MAX_WBITS
                     if encoding == 'gzip' else -zlib.MAX_WBITS)
        self._compress = zlib.compressobj(wbits=zlib_mode)

    def _write(self, chunk):
        size = len(chunk)
        self.buffer_size += size
        self.output_size += size

        if self._transport.is_closing():
            raise asyncio.CancelledError('Cannot write to closing transport')
        self._transport.write(chunk)

    def write(self, chunk, *, drain=True, LIMIT=64*1024):
        """Writes chunk of data to a stream.

        write_eof() indicates end of stream.
        writer can't be used after write_eof() method being called.
        write() return drain future.
        """
        if self._compress is not None:
            chunk = self._compress.compress(chunk)
            if not chunk:
                return noop()

        if self.length is not None:
            chunk_len = len(chunk)
            if self.length >= chunk_len:
                self.length = self.length - chunk_len
            else:
                chunk = chunk[:self.length]
                self.length = 0
                if not chunk:
                    return noop()

        if chunk:
            if self.chunked:
                chunk_len = ('%x\r\n' % len(chunk)).encode('ascii')
                chunk = chunk_len + chunk + b'\r\n'

            self._write(chunk)

            if self.buffer_size > LIMIT and drain:
                self.buffer_size = 0
                return self.drain()

        return noop()

    def write_headers(self, status_line, headers, SEP=': ', END='\r\n'):
        """Write request/response status and headers."""
        # status + headers
        headers = status_line + ''.join(
            [k + SEP + v + END for k, v in headers.items()])
        headers = headers.encode('utf-8') + b'\r\n'
        self._write(headers)

    async def write_eof(self, chunk=b''):
        if self._eof:
            return

        if self._compress:
            if chunk:
                chunk = self._compress.compress(chunk)

            chunk = chunk + self._compress.flush()
            if chunk and self.chunked:
                chunk_len = ('%x\r\n' % len(chunk)).encode('ascii')
                chunk = chunk_len + chunk + b'\r\n0\r\n\r\n'
        else:
            if self.chunked:
                if chunk:
                    chunk_len = ('%x\r\n' % len(chunk)).encode('ascii')
                    chunk = chunk_len + chunk + b'\r\n0\r\n\r\n'
                else:
                    chunk = b'0\r\n\r\n'

        if chunk:
            self._write(chunk)

        await self.drain()

        self._eof = True
        self._transport = None

    async def drain(self):
        await self._stream.drain()
