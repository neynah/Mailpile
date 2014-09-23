#
# Mailpile's built-in HTTPD
#
###############################################################################
import Cookie
import mimetypes
import os
import random
import socket
import SocketServer
import time
import threading
from SimpleXMLRPCServer import SimpleXMLRPCServer, SimpleXMLRPCRequestHandler
from urllib import quote, unquote
from urlparse import parse_qs, urlparse

import mailpile.util
from mailpile.commands import Action
from mailpile.i18n import gettext as _
from mailpile.i18n import ngettext as _n
from mailpile.urlmap import UrlMap
from mailpile.util import *
from mailpile.ui import *

import capnp
capnp.remove_event_loop()
capnp.create_event_loop(threaded=True)
import hack_session_capnp
from mailpile.plugins.contacts import AddProfile

global WORD_REGEXP, STOPLIST, BORING_HEADERS, DEFAULT_PORT

DEFAULT_PORT = 33411

BLOCK_HTTPD_LOCK = UiRLock()
LIVE_HTTP_REQUESTS = 0

IS_PROFILE_SET = False

def Idle_HTTPD(allowed=1):
    with BLOCK_HTTPD_LOCK:
        sleep = 100
        while (sleep and
                not mailpile.ui.QUITTING and
                LIVE_HTTP_REQUESTS > allowed):
            time.sleep(0.05)
            sleep -= 1
        return BLOCK_HTTPD_LOCK


class HttpRequestHandler(SimpleXMLRPCRequestHandler):

    # We always recognize these extensions, no matter what the Python
    # mimetype module thinks.
    _MIMETYPE_MAP = dict([(ext, 'text/plain') for ext in (
        'c', 'cfg', 'conf', 'cpp', 'csv', 'h', 'hpp', 'log', 'md', 'me',
        'py', 'rb', 'rc', 'txt'
    )] + [(ext, 'application/x-font') for ext in (
        'pfa', 'pfb', 'gsf', 'pcf'
    )] + [
        ('css', 'text/css'),
        ('eot', 'application/vnd.ms-fontobject'),
        ('gif', 'image/gif'),
        ('html', 'text/html'),
        ('htm', 'text/html'),
        ('ico', 'image/x-icon'),
        ('jpg', 'image/jpeg'),
        ('jpeg', 'image/jpeg'),
        ('js', 'text/javascript'),
        ('json', 'application/json'),
        ('otf', 'font/otf'),
        ('png', 'image/png'),
        ('rss', 'application/rss+xml'),
        ('tif', 'image/tiff'),
        ('tiff', 'image/tiff'),
        ('ttf', 'font/ttf'),
        ('svg', 'image/svg+xml'),
        ('svgz', 'image/svg+xml'),
        ('woff', 'application/font-woff'),
    ])

    _ERROR_CONTEXT = {'lastq': '', 'csrf': '', 'path': ''},

    def http_host(self):
        """Return the current server host, e.g. 'localhost'"""
        #rsplit removes port
        return self.headers.get('host', 'localhost').rsplit(':', 1)[0]

    def http_session(self):
        """Fetch the session ID from a cookie, or assign a new one"""
        cookies = Cookie.SimpleCookie(self.headers.get('cookie'))
        session_id = cookies.get(self.server.session_cookie)
        if session_id:
            session_id = session_id.value
        else:
            session_id = self.server.make_session_id(self)
        return session_id

    def server_url(self):
        """Return the current server URL, e.g. 'http://localhost:33411/'"""
        return '%s://%s' % (self.headers.get('x-forwarded-proto', 'http'),
                            self.headers.get('host', 'localhost'))

    def send_http_response(self, code, msg):
        """Send the HTTP response header"""
        self.wfile.write('HTTP/1.1 %s %s\r\n' % (code, msg))

    def send_http_redirect(self, destination):
        self.send_http_response(302, 'Found')
        self.wfile.write(('Location: %s\r\n\r\n'
                          '<h1><a href="%s">Please look here!</a></h1>\n'
                          ) % (destination, destination))

    def send_standard_headers(self,
                              header_list=[],
                              cachectrl='private',
                              mimetype='text/html'):
        """
        Send common HTTP headers plus a list of custom headers:
        - Cache-Control
        - Content-Type

        This function does not send the HTTP/1.1 header, so
        ensure self.send_http_response() was called before

        Keyword arguments:
        header_list  -- A list of custom headers to send, containing
                        key-value tuples
        cachectrl    -- The value of the 'Cache-Control' header field
        mimetype     -- The MIME type to send as 'Content-Type' value
        """
        if mimetype.startswith('text/') and ';' not in mimetype:
            mimetype += ('; charset = utf-8')
        self.send_header('Cache-Control', cachectrl)
        self.send_header('Content-Type', mimetype)
        for header in header_list:
            self.send_header(header[0], header[1])
        session_id = self.session.ui.html_variables.get('http_session')
        if session_id:
            cookies = Cookie.SimpleCookie()
            cookies[self.server.session_cookie] = session_id
            cookies[self.server.session_cookie]['path'] = '/'
            cookies[self.server.session_cookie]['max-age'] = 24 * 3600
            self.send_header(*cookies.output().split(': ', 1))
            self.send_header('Cache-Control', 'no-cache="set-cookie"')
        self.end_headers()

    def send_full_response(self, message,
                           code=200, msg='OK',
                           mimetype='text/html', header_list=[],
                           suppress_body=False):
        """
        Sends the HTTP header and a response list

        message       -- The body of the response to send
        header_list   -- A list of custom headers to send,
                         containing key-value tuples
        code          -- The HTTP response code to send
        mimetype      -- The MIME type to send as 'Content-Type' value
        suppress_body -- Set this to True to ignore the message parameter
                              and not send any response body
        """
        message = unicode(message).encode('utf-8')
        self.log_request(code, message and len(message) or '-')
        # Send HTTP/1.1 header
        self.send_http_response(code, msg)
        # Send all headers
        if code == 401:
            self.send_header('WWW-Authenticate',
                             'Basic realm = MP%d' % (time.time() / 3600))
        # If suppress_body == True, we don't know the content length
        contentLengthHeaders = []
        if not suppress_body:
            contentLengthHeaders = [('Content-Length', len(message or ''))]
        self.send_standard_headers(header_list=(header_list +
                                                contentLengthHeaders),
                                   mimetype=mimetype,
                                   cachectrl="no-cache")
        # Response body
        if not suppress_body:
            self.wfile.write(message or '')

    def guess_mimetype(self, fpath):
        ext = os.path.basename(fpath).rsplit('.')[-1]
        return (self._MIMETYPE_MAP.get(ext.lower()) or
                mimetypes.guess_type(fpath, strict=False)[0] or
                'application/octet-stream')

    def send_file(self, config, filename):
        # FIXME: Do we need more security checks?
        if '..' in filename:
            code, msg = 403, "Access denied"
        else:
            try:
                tpl = config.sys.path.get(self.http_host(), 'html_theme')
                fpath, fd, mt = config.open_file(tpl, filename)
                mimetype = mt or self.guess_mimetype(fpath)
                message = fd.read()
                fd.close()
                code, msg = 200, "OK"
            except IOError, e:
                mimetype = 'text/plain'
                if e.errno == 2:
                    code, msg = 404, "File not found"
                elif e.errno == 13:
                    code, msg = 403, "Access denied"
                else:
                    code, msg = 500, "Internal server error"
                message = ""

        self.log_request(code, message and len(message) or '-')
        self.send_http_response(code, msg)
        self.send_standard_headers(header_list=[('Content-Length',
                                                len(message or ''))],
                                   mimetype=mimetype,
                                   cachectrl=("must-revalidate = False, "
                                              "max-age = 3600"))
        self.wfile.write(message or '')

    def csrf(self):
        """
        Generate a hashed token from the current timestamp
        and the server secret to avoid CSRF attacks
        """
        ts = '%x' % int(time.time() / 60)
        return '%s-%s' % (ts, b64w(sha1b64('-'.join([self.server.secret,
                                                     ts]))))
    def set_profile(self):
        global IS_PROFILE_SET

        if IS_PROFILE_SET:
            return

        config = self.server.session.config
        vcards = config.vcards
        if len(vcards) > 0:  # TODO: find better way to test if vcards is loaded
            IS_PROFILE_SET = True
            profiles = [vcard for vcard in vcards.values() if vcard.kind == 'profile']
            uids = set([vcard._random_uid() for vcard in profiles])
            for uid in uids:
                for vcard in profiles:
                    if vcard._random_uid() == uid:
                        profiles.remove(vcard)
                        break


            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect("/tmp/sandstorm-api")

            client = capnp.TwoPartyClient(s)
            session_cap = client.ez_restore('HackSessionContext').cast_as(hack_session_capnp.HackSessionContext)
            address = session_cap.getUserAddress().wait()

            index = 0
            if len(address.address) > 0:
                if len(profiles) < 2:
                    session = Session(config)
                    route_id = session.config.routes.keys()[0]
                    data = {
                        'email': [address.address],
                        'name': [address.name.decode('utf8')],
                        'route_id': [route_id]
                    }
                    AddProfile(session, 'vcards/add', [], data, {}).run()
                    profiles = [vcard for vcard in vcards.values() if vcard.kind == 'profile']

                profiles[index].fn = address.name.decode('utf8')
                profiles[index].email = address.address
                index += 1

            name = self.headers.get('x-sandstorm-username', '').decode('utf8')
            public_id = session_cap.getPublicId().wait()

            profiles[index].fn = name
            profiles[index].email = public_id.publicId + '@' + public_id.hostname

    def do_POST(self, method='POST'):
        self.set_profile()

        (scheme, netloc, path, params, query, frag) = urlparse(self.path)
        if path.startswith('/::XMLRPC::/'):
            raise ValueError(_('XMLRPC has been disabled for now.'))
            #return SimpleXMLRPCRequestHandler.do_POST(self)

        # Update thread name for debugging purposes
        threading.current_thread().name = 'POST:%s' % self.path.split('?')[0]

        self.session, config = self.server.session, self.server.session.config
        post_data = {}
        try:
            ue = 'application/x-www-form-urlencoded'
            clength = int(self.headers.get('content-length', 0))
            ctype, pdict = cgi.parse_header(self.headers.get('content-type',
                                                             ue))
            if ctype == 'multipart/form-data':
                post_data = cgi.FieldStorage(
                    fp=self.rfile,
                    headers=self.headers,
                    environ={'REQUEST_METHOD': method,
                             'CONTENT_TYPE': self.headers['Content-Type']}
                )
            elif ctype == ue:
                if clength > 5 * 1024 * 1024:
                    raise ValueError(_('OMG, input too big'))
                post_data = cgi.parse_qs(self.rfile.read(clength), 1)
            else:
                raise ValueError(_('Unknown content-type'))

        except (IOError, ValueError), e:
            import sys
            print >>sys.stderr, 'exception: ', e
            self.send_full_response(self.server.session.ui.render_page(
                config, self._ERROR_CONTEXT,
                body='POST geborked: %s' % e,
                title=_('Internal Error')
            ), code=500)
            return None
        return self.do_GET(post_data=post_data, method=method)

    def do_GET(self, *args, **kwargs):
        self.set_profile()

        global LIVE_HTTP_REQUESTS
        try:
            path = self.path.split('?')[0]

            threading.current_thread().name = 'WAIT:%s' % path
            with BLOCK_HTTPD_LOCK:
                LIVE_HTTP_REQUESTS += 1

            threading.current_thread().name = 'WORK:%s' % path
            return self._real_do_GET(*args, **kwargs)
        finally:
            LIVE_HTTP_REQUESTS -= 1

    def _real_do_GET(self, post_data={}, suppress_body=False, method='GET'):
        (scheme, netloc, path, params, query, frag) = urlparse(self.path)
        query_data = parse_qs(query)
        opath = path = unquote(path)

        # HTTP is stateless, so we create a new session for each request.
        self.session, config = self.server.session, self.server.session.config
        server_session = self.server.session

        if 'httpdata' in config.sys.debug:
            self.wfile = DebugFileWrapper(sys.stderr, self.wfile)

        # Static things!
        if path == '/favicon.ico':
            path = '/static/favicon.ico'
        if path.startswith('/_/'):
            path = path[2:]
        if path.startswith('/static/'):
            return self.send_file(config, path[len('/static/'):])

        self.session = session = Session(config)
        session.ui = HttpUserInteraction(self, config,
                                         log_parent=server_session.ui)

        if 'http' in config.sys.debug:
            session.ui.warning = server_session.ui.warning
            session.ui.notify = server_session.ui.notify
            session.ui.error = server_session.ui.error
            session.ui.debug = server_session.ui.debug
            session.ui.debug('%s: %s qs = %s post = %s'
                             % (method, opath, query_data, post_data))

        idx = session.config.index
        if session.config.loaded_config:
            name = session.config.get_profile().get('name', 'Chelsea Manning')
        else:
            name = 'Chelsea Manning'

        session.ui.html_variables = {
            'csrf': self.csrf(),
            'http_host': self.headers.get('host', 'localhost'),
            'http_hostname': self.http_host(),
            'http_method': method,
            'http_session': self.http_session(),
            'message_count': (idx and len(idx.INDEX) or 0),
            'name': name,
            'title': 'Mailpile dummy title',
            'url_protocol': self.headers.get('x-forwarded-proto', 'http'),
            'mailpile_size': idx and len(idx.INDEX) or 0
        }

        try:
            try:
                commands = UrlMap(session).map(
                    self, method, path, query_data, post_data,
                    authenticate=(not mailpile.util.TESTING))
            except UsageError:
                if (not path.endswith('/') and
                        not session.config.sys.debug and
                        method == 'GET'):
                    commands = UrlMap(session).map(self, method, path + '/',
                                                   query_data, post_data)
                    url = quote(path) + '/'
                    if query:
                        url += '?' + query
                    return self.send_http_redirect(url)
                else:
                    raise

            global LIVE_HTTP_REQUESTS
            hang_fix = 1 if ([1 for c in commands if c.IS_HANGING_ACTIVITY]
                             ) else 0
            try:
                LIVE_HTTP_REQUESTS -= hang_fix
                results = [cmd.run() for cmd in commands]
                session.ui.display_result(results[-1])
            finally:
                LIVE_HTTP_REQUESTS += hang_fix

        except UrlRedirectException, e:
            return self.send_http_redirect(e.url)
        except SuppressHtmlOutput:
            return None
        except AccessError:
            self.send_full_response(_('Access Denied'),
                                    code=403, mimetype='text/plain')
            return None
        except:
            e = traceback.format_exc()
            session.ui.debug(e)
            if not session.config.sys.debug:
                e = _('Internal error')
            self.send_full_response(e, code=500, mimetype='text/plain')
            return None

        mimetype, content = session.ui.render_response(session.config)
        self.send_full_response(content, mimetype=mimetype)

    def do_PUT(self):
        return self.do_POST(method='PUT')

    def do_UPDATE(self):
        return self.do_POST(method='UPDATE')

    def do_HEAD(self):
        return self.do_GET(suppress_body=True, method='HEAD')

    def log_message(self, fmt, *args):
        self.server.session.ui.notify(self.server_url() +
                                      ' ' + (fmt % args))


class HttpServer(SocketServer.ThreadingMixIn, SimpleXMLRPCServer):
    def __init__(self, session, sspec, handler):
        SimpleXMLRPCServer.__init__(self, sspec, handler)
        self.daemon_threads = True
        self.session = session
        self.sessions = {}
        self.session_cookie = None
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sspec = (sspec[0] or 'localhost', self.socket.getsockname()[1])

        # This hash includes the index ofuscation master key, which means
        # it should be very strongly unguessable.
        self.secret = b64w(sha512b64(
            '-'.join([str(x) for x in [session, time.time(),
                                       random.randint(0, 0xfffffff),
                                       session.config]])))

        # Generate a new unguessable session cookie name on startup
        while not self.session_cookie:
            rn = str(random.randint(0, 0xfffffff))
            self.session_cookie = CleanText(sha512b64(self.secret, rn),
                                            banned=CleanText.NONALNUM
                                            ).clean[:8].lower()

    def make_session_id(self, request):
        """Generate an unguessable and unauthenticated new session ID."""
        session_id = None
        while session_id in self.sessions or session_id is None:
            session_id = b64w(sha1b64('%s %s %x %s' % (
                self.secret,
                request and request.headers,
                random.randint(0, 0xffffffff),
                time.time())))
        return session_id

    def finish_request(self, request, client_address):
        try:
            SimpleXMLRPCServer.finish_request(self, request, client_address)
        except socket.error:
            pass
        if mailpile.util.QUITTING:
            self.shutdown()


class HttpWorker(threading.Thread):
    def __init__(self, session, sspec):
        threading.Thread.__init__(self)
        self.httpd = HttpServer(session, sspec, HttpRequestHandler)
        self.daemon = True
        self.session = session

    def run(self):
        self.httpd.serve_forever()

    def quit(self, join=False):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        self.httpd = None
