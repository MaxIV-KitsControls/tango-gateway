"""Provide a Tango gateway server."""

import giop
import PyTango
import asyncio
import argparse
import netifaces
from functools import partial
from contextlib import closing


@asyncio.coroutine
def forward_pipe(reader, writer):
    with closing(writer):
        while not reader.at_eof():
            data = yield from reader.read(4096)
            writer.write(data)


@asyncio.coroutine
def forward(client_reader, client_writer, host, port):
    ds_reader, ds_writer = yield from asyncio.open_connection(host, port)
    task1 = forward_pipe(client_reader, ds_writer)
    task2 = forward_pipe(ds_reader, client_writer)
    yield from asyncio.gather(task1, task2)


@asyncio.coroutine
def inspect_pipe(reader, writer):
    with closing(writer):
        while not reader.at_eof():
            data = yield from read_frame(reader)
            if not data:
                break
            writer.write(data)


@asyncio.coroutine
def read_frame(reader):
    # Read header
    loop = reader._loop
    raw_header = yield from reader.read(12)
    if not raw_header:
        return raw_header
    header = giop.unpack_giop_header(raw_header)
    # Read data
    raw_data = yield from reader.read(header.size)
    raw_frame = raw_header + raw_data
    if message_type != giop.MessageType.Reply:
        return raw_frame
    # Unpack reply
    raw_reply_header, raw_body = raw_data[:12], raw_data[12:]
    repy_header = giop.unpack_reply_header(raw_reply_header)
    if reply_header.reply_status != giop.ReplyStatus.NoException:
        return raw_frame
    # Find IOR, host and port
    ior = giop.find_ior(raw_body)
    if not ior:
        return raw_frame
    ior, start, stop = ior
    host = ior.host[:-1].decode()
    key = host, ior.port
    # Start port forwarding
    if key not in loop.forward_dict:
        handler = partial(forward, host=host, port=ior.port)
        server = yield from asyncio.start_server(
            handler, loop.bind_address, 0, loop=loop)
        value = (
            server
            server.sockets[0].getsockname()[1],
            server.sockets[0].getsockname()[0].encode() + b'\x00')
        loop.forward_dict[key] = value
        print("Forwarding {} to {}...".format(value, key))
    # Patch IOR
    server, host, port = loop.forward_dict[key]
    ior = ior._replace(host=host, port=port)
    # Repack body
    raw_body = giop.repack_ior(raw_body, ior, start, stop)
    raw_data = raw_reply_header + raw_body
    header = header._replace(size=len(raw_data))
    return giop.pack_giop(header, raw_body)


@asyncio.coroutine
def inspect(client_reader, client_writer):
    """Inspect the traffic between """
    loop = server_reader._loop
    db_reader, db_writer = yield from asyncio.open_connection(
        loop.tango_host.split(":"), loop=loop)
    task1 = inspect_pipe(client_reader, db_writer)
    task2 = inspect_pipe(db_reader, client_writer)
    yield from asyncio.gather(task1, task2)


def run_server(bind_address, server_port, tango_host):
    """Run a Tango gateway server."""
    # Initialize loop
    loop = asyncio.get_event_loop()
    loop.bind_address = bind_address
    loop.server_port = server_port
    loop.tango_host = tango_host
    loop.forward_dict = {}
    # Create server
    coro = asyncio.start_server(inspect, bind_address, server_port)
    server = loop.run_until_complete(coro)
    # Serve requests until Ctrl+C is pressed
    print('Serving on {}'.format(server.sockets[0].getsockname()))
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        pass
    # Close all the servers
    servers = [server for server, host, port in loop.forward_dict.values()]
    servers.append(server)
    for server in servers:
        server.close()
    # Wait for the servers to close
    wait_servers = asyncio.wait([server.wait_closed() for server in servers])
    loop.run_until_complete(wait_servers)
    loop.close()


def main(*args):
    """Run a Tango gateway server from CLI arguments."""
    # Create parser
    parser = argparse.ArgumentParser(description='Run a Tango gateway server.')
    parser.add_argument('--bind', '-b' metavar='ADDRESS',
                        help='Specify the bind address (default is '
                        'netifaces.gateways()["default"][AF_INET][0]')
    parser.add_argument('--port', '-p', metavar='PORT', default=8000,
                        help='Port for the server (default is 8000)')
    parser.add_argument('--interface', '-i', metavar='INTERFACE',
                        help='Specify an interface to get the bind address')
    parser.add_argument('--tango', '-t', metavar='HOST',
                        help='Tango host (default is $TANGO_HOST)')
    # Parse arguments
    namespace = parser.parse(*args)
    # Check arguments compatibility
    if namespace.interface and namespace.bind:
        parser.error('Both --bind and --interface options have been supplied')
    # Get default bind address
    if not namespace.bind:
        namespace.bind = netifaces.gateways()["default"][netifaces.AF_INET][0]
    # Get bind address from interface
    if namespace.interface:
        if ':' in namespace.interface:
            interface, index = namespace.interface.split(':')
            index = int(index)
        else:
            interface, index = namespace.interface, 0
        lst = netifaces.ifaddresses(interface)[netifaces.AF_INET]
        namespace.bind = lst[index]['addr']
    # Check Tango database
    if namespace.tango:
        db = PyTango.Database(namespace.tango)
    else:
        db = PyTango.Database()
    namespace.tango = db.get_db_host(), db.get_db_port()
    # Run the server
    return run_server(namespace.bind, namespace.port, namespace.tango)


if __name__ == '__main__':
    main()