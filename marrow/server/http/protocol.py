# encoding: utf-8

import logging

import time
from functools import partial
from email.utils import formatdate

from marrow.server.protocol import Protocol

from marrow.util.object import LoggingFile
from marrow.util.compat import binary, unicode, native, bytestring, uvalues, IO, unquote

from marrow.server.http import release


__all__ = ['HTTPProtocol']
log = logging.getLogger(__name__)


CRLF = b"\r\n"
dCRLF = b"\r\n\r\n"
HTTP_INTERNAL_ERROR = b" 500 Internal Server Error\r\nContent-Type: text/plain\r\nContent-Length: 48\r\n\r\nThe server encountered an unrecoverable error.\r\n"
VERSION_STRING = b'marrow.httpd/' + release.release.encode('iso-8859-1')
nCRLF = native(CRLF)
errorlog = LoggingFile(logging.getLogger('wsgi.errors'))



class HTTPProtocol(Protocol):
    def __init__(self, server, testing, application, ingress=None, egress=None, encoding="utf8", **options):
        super(HTTPProtocol, self).__init__(server, testing, **options)
        
        self.application = application
        self.ingress = ingress if ingress else []
        self.egress = egress if egress else []
        self.encoding = encoding
        
        self._name = server.name
        self._addr = server.address[0] if isinstance(server.address, tuple) else ''
        self._port = str(server.address[1]) if isinstance(server.address, tuple) else '80'
    
    def accept(self, client):
        self.Connection(self.server, self, client)
    
    class Connection(object):
        def __init__(self, server, protocol, client):
            self.server = server
            self.protocol = protocol
            self.client = client
            
            env = dict()
            env['REMOTE_ADDR'] = client.address[0]
            env['SERVER_NAME'] = protocol._name
            env['SERVER_ADDR'] = protocol._addr
            env['SERVER_PORT'] = protocol._port
            env['SCRIPT_NAME'] = unicode()
            
            env['wsgi.input'] = IO()
            env['wsgi.errors'] = errorlog
            env['wsgi.version'] = (2, 0)
            env['wsgi.multithread'] = getattr(server, 'threaded', False) # TODO: Temporary hack until marrow.server 1.0 release.
            env['wsgi.multiprocess'] = server.fork != 1
            env['wsgi.run_once'] = False
            env['wsgi.url_scheme'] = 'http'
            env['wsgi.async'] = False # TODO
            
            if self.server.threaded is not False:
                env['wsgi.executor'] = self.server.executor # pimp out the concurrent.futures thread pool executor
            
            # env['wsgi.script_name'] = b''
            # env['wsgi.path_info'] = b''
            
            self.environ = None
            self.environ_template = env
            
            self.finished = False
            self.pipeline = protocol.options.get('pipeline', True) # TODO
            
            client.read_until(dCRLF, self.headers)
        
        def finish(self):
            assert not self.finished, "Attempt complete an already completed request."
            
            self.finished = True
            
            if not self.client.writing():
                self._finish()
        
        def headers(self, data):
            """Process HTTP headers, and pull in the body as needed."""
            
            # THREADING TODO: Experiment with threading this callback.
            
            # log.debug("Received: %r", data)
            self.environ = environ = dict(self.environ_template)
            
            line = data[:data.index(CRLF)].split()
            environ['REQUEST_URI'] = line[1]
            
            remainder, _, fragment = line[1].partition(b'#')
            remainder, _, query = remainder.partition(b'?')
            path, _, param = remainder.partition(b';')
            
            if b"://" in path:
                scheme, _, path = path.partition(b'://')
                host, _, path = path.partition(b'/')
                path = b'/' + path
                
                environ['wsgi.url_scheme'] = native(scheme)
                environ['HTTP_HOST'] = host
            
            environ['REQUEST_METHOD'] = native(line[0])
            environ['CONTENT_TYPE'] = None
            environ['FRAGMENT'] = fragment
            environ['SERVER_PROTOCOL'] = native(line[2])
            environ['CONTENT_LENGTH'] = None
            
            environ['PATH_INFO'] = unquote(path)
            environ['PARAMETERS'] = param
            environ['QUERY_STRING'] = query
            
            if environ['REQUEST_METHOD'] == 'HEAD':
                environ['marrow.head'] = True
                environ['REQUEST_METHOD'] = 'GET'
            
            _ = ('PATH_INFO', 'PARAMETERS', 'QUERY_STRING', 'FRAGMENT')
            environ['wsgi.uri_encoding'], __ = uvalues([environ[i] for i in _], self.protocol.encoding)
            environ.update(zip(_, __))
            del _, __
            
            current, header = None, None
            noprefix = dict(CONTENT_TYPE=True, CONTENT_LENGTH=True)
            
            # All keys and values are native strings.
            data = native(data) if str is unicode else data
            
            for line in data.split(nCRLF)[1:]:
                if not line: break
                assert current is not None or line[0] != ' ' # TODO: Do better than dying abruptly.
                
                if line[0] == ' ':
                    _ = line.lstrip()
                    environ[current] += _
                    continue
                
                header, _, value = line.partition(': ')
                current = header.replace('-', '_').upper()
                if current not in noprefix: current = 'HTTP_' + current
                environ[current] = value
            
            # TODO: Proxy support.
            # for h in ("X-Real-Ip", "X-Real-IP", "X-Forwarded-For"):
            #     self.remote_ip = self.engiron.get(h, None)
            #     if self.remote_ip is not None:
            #         break
            
            if environ.get("HTTP_EXPECT", None) == "100-continue":
                self.client.write(b"HTTP/1.1 100 (Continue)\r\n\r\n")
            
            if environ['CONTENT_LENGTH'] is None:
                if environ.get('HTTP_TRANSFER_ENCODING', '').lower() == 'chunked':
                    self.client.read_until(CRLF, self.body_chunked)
                    return
                
                self.body_finished()
                return
            
            length = int(environ['CONTENT_LENGTH'])
            
            if length > self.client.max_buffer_size:
                # TODO: Return appropriate HTTP response in addition to logging the error.
                raise Exception("Content-Length too long.")
            
            self.client.read_bytes(length, self.body)
        
        def body(self, data):
            # log.debug("Received body: %r", data)
            self.environ['wsgi.input'] = IO(data)
            self.body_finished()
        
        def body_chunked(self, data):
            # log.debug("Received chunk header: %r", data)
            length = int(data.strip(CRLF).split(b';')[0], 16)
            # log.debug("Chunk length: %r", length)
            
            if length == 0:
                self.client.read_until(CRLF, self.body_trailers)
                return
            
            self.client.read_bytes(length + 2, self.body_chunk)
        
        def body_chunk(self, data):
            # log.debug("Received chunk: %r", data)
            self.environ['wsgi.input'].write(data[:-2])
            self.client.read_until(CRLF, self.body_chunked)
        
        def body_trailers(self, data):
            # log.debug("Received chunk trailers: %r", data)
            self.environ['wsgi.input'].seek(0)
            # TODO: Update headers with additional headers.
            self.body_finished()
        
        def body_finished(self):
            if self.server.threaded is not False:
                # log.debug("Deferring response composition.")
                future = self.server.executor.submit(self.compose_response)
                
                def callback(future):
                    # log.debug("Deferred response composition finished.")
                    
                    try:
                        # log.debug("Retreiving composed response.")
                        response = future.result()
                    
                    except:
                        log.exception("Unhandled application exception.")
                        self.client.write(self.environ['SERVER_PROTOCOL'].encode('iso-8859-1') + HTTP_INTERNAL_ERROR, self.finish)
                    
                    # log.debug("Delivering response.")
                    self.deliver(response)
                
                future.add_done_callback(callback)
                return
            
            try:
                self.deliver(self.compose_response())
            
            except:
                log.exception("Unhandled application exception.")
                self.client.write(self.environ['SERVER_PROTOCOL'].encode('iso-8859-1') + HTTP_INTERNAL_ERROR, self.finish)
        
        def compose_response(self):
            # log.debug("Composing response.")
            env = self.environ
            
            for filter_ in self.protocol.ingress:
                filter_(env)
            
            status, headers, body = self.protocol.application(env)
            
            for filter_ in self.protocol.egress:
                status, headers, body = filter_(env, status, headers, body)
            
            # Canonicalize the names of the headers returned by the application.
            present = [i[0].lower() for i in headers]
            
            # These checks are optional; if the application is well-behaved they can be disabled.
            # Of course, if disabled, m.s.http isn't WSGI 2 compliant. (But it is faster!)
            assert isinstance(status, binary), "Response status must be a bytestring."
            
            for i, j in headers:
                assert isinstance(i, binary), "Response header names must be bytestrings."
                assert isinstance(j, binary), "Response header values must be bytestrings."
            
            assert b'transfer-encoding' not in present, "Applications must not set the Transfer-Encoding header."
            assert b'connection' not in present, "Applications must not set the Connection header."
            
            if b'server' not in present:
                headers.append((b'Server', VERSION_STRING))
            
            if b'date' not in present:
                headers.append((b'Date', bytestring(formatdate(time.time(), False, True))))
            
            is_head = env.get('marrow.head', False)
            if is_head:
                try: body.close()
                except AttributeError: pass
                
                body = []
            
            if env['SERVER_PROTOCOL'] == "HTTP/1.1" and b'content-length' not in present:
                headers.append((b"Transfer-Encoding", b"chunked"))
                headers = env['SERVER_PROTOCOL'].encode('iso-8859-1') + b" " + status + CRLF + CRLF.join([(i + b': ' + j) for i, j in headers]) + dCRLF
                return headers, partial(self.write_body if is_head else self.write_body_chunked, body, iter(body))
            
            headers = env['SERVER_PROTOCOL'].encode('iso-8859-1') + b" " + status + CRLF + CRLF.join([(i + b': ' + j) for i, j in headers]) + dCRLF
            return headers, partial(self.write_body, body, iter(body))
        
        def deliver(self, response):
            # TODO: expand with 'self.client.writer' callable to support threading efficiently.
            # Single-threaded we can write directly to the stream, multi-threaded we need to queue responses for the main thread to deliver.
            
            # log.debug("Delivering the response.")
            
            self.client.write(*response)
        
        def write_body(self, original, body):
            try:
                chunk = next(body)
                assert isinstance(chunk, binary), "Body iterators must yield bytestrings."
                # log.debug('Sending body: %s', chunk)
                self.client.write(chunk, partial(self.write_body, original, body))
            
            except StopIteration:
                self.finish()
                
                try:
                    original.close()
                except AttributeError: # pragma: no cover
                    pass
            
            except:
                try:
                    original.close()
                except AttributeError:
                    pass
                raise
        
        def write_body_chunked(self, original, body):
            try:
                chunk = next(body)
                assert isinstance(chunk, binary), "Body iterators must yield bytestrings."
                chunk = bytestring(hex(len(chunk))[2:]) + CRLF + chunk + CRLF
                self.client.write(chunk, partial(self.write_body_chunked, original, body))
            
            except StopIteration:
                try:
                    original.close()
                except AttributeError:
                    pass
                
                self.client.write(b"0" + dCRLF, self.finish)
            
            except: # pragma: no cover TODO EDGE CASE
                try:
                    original.close()
                except AttributeError:
                    pass
                raise
        
        def _finish(self):
            # TODO: Pre-calculate this and pass self.client.close as the body writer callback only if we need to disconnect.
            # TODO: Execute self.client.read_until in write_body if we aren't disconnecting.
            # These are to support threading, where the body writer callback is executed in the main thread.
            env = self.environ
            disconnect = True
            
            if self.pipeline:
                if env['SERVER_PROTOCOL'] == 'HTTP/1.1':
                    disconnect = env.get('HTTP_CONNECTION', None) == "close"
                
                elif env['CONTENT_LENGTH'] is not None or env['REQUEST_METHOD'] in ('HEAD', 'GET'):
                    disconnect = env.get('HTTP_CONNECTION', '').lower() != 'keep-alive'
            
            self.finished = False
            
            # log.debug("Disconnect client? %r", disconnect)
            
            if disconnect:
                self.client.close()
                return
            
            self.client.read_until(dCRLF, self.headers)
