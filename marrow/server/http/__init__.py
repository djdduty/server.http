# encoding: utf-8

try:
    import fcntl
except ImportError:
    if os.name == 'nt':
        from marrow.io import win32_support as fcntl
    else:
        raise

from marrow.server.base import Server

from marrow.server.http.protocol import HTTPProtocol


__all__ = ['HTTPProtocol', 'HTTPServer']
log = __import__('logging').getLogger(__name__)



class HTTPServer(Server):
    protocol = HTTPProtocol
    
    def _socket(self):
        sock = super(HTTPServer, self)._socket()
        
        # TODO: This should be merged into the base Server if forking is enabled.
        flags = fcntl.fcntl(sock.fileno(), fcntl.F_GETFD)
        flags |= fcntl.FD_CLOEXEC
        fcntl.fcntl(sock.fileno(), fcntl.F_SETFD, flags)
        
        return sock


if __name__ == '__main__':
    import logging
    
    logging.basicConfig(level=logging.DEBUG)
    
    def hello(request):
        # request['wsgi.errors'].write("Oh noes!") # Example error, output through the logging module.  \o/
        return b'200 OK', [(b'Content-Type', b'text/plain'), (b'Content-Length', b'12')], [b'Hello world!']
    
    HTTPServer(None, 8080, fork=1, application=hello).start()
