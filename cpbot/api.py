import json
import logging
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


class APIError(Exception):
    def __init__(self, code, retry_after=0, description=""):
        self.code, self.retry_after, self.description = code, retry_after, description
        super().__init__('Telegram API error %s' % code)


class APIInterrupted(Exception):
    """A rate-limit wait was interrupted during graceful shutdown."""


class API:
    def __init__(self, token, stop_event=None):
        self.base = 'https://api.telegram.org/bot' + token + '/'
        self.stop_event = stop_event

    def call(self, method, **params):
        return self._with_rate_limit_retry(method, lambda: self._call_once(method, params))

    def call_once(self, method, **params):
        """Make one attempt; durable outbox callers must persist 429 scheduling."""
        return self._call_once(method, params)

    def _call_once(self, method, params):
        request = Request(self.base + method, json.dumps(params).encode(), {'Content-Type': 'application/json'})
        try:
            with urlopen(request, timeout=40 if method == 'getUpdates' else 10) as response:
                result = json.load(response)
        except HTTPError as error:
            try:
                result = json.load(error)
            except (ValueError, OSError):
                raise APIError(error.code) from None
        except (URLError, OSError, ValueError):
            raise APIError(0) from None
        if not result.get('ok'):
            raise APIError(result.get('error_code', 0), result.get('parameters', {}).get('retry_after', 0), result.get('description', ''))
        return result['result']

    def _with_rate_limit_retry(self, method, request):
        while True:
            try:
                return request()
            except APIError as error:
                if error.code != 429:
                    raise
                delay = max(1, error.retry_after)
                logging.warning('Telegram %s rate-limited; retrying this request in %s seconds', method, delay)
                if self.stop_event and self.stop_event.wait(delay):
                    raise APIInterrupted() from None
                if not self.stop_event:
                    time.sleep(delay)

    def document(self, chat, filename, content):
        return self._with_rate_limit_retry('sendDocument', lambda: self.document_once(chat, filename, content))

    def document_once(self, chat, filename, content):
        import secrets
        boundary = secrets.token_hex(16)
        body = ('--%s\r\nContent-Disposition: form-data; name="chat_id"\r\n\r\n%s\r\n' % (boundary, chat)).encode()
        body += ('--%s\r\nContent-Disposition: form-data; name="document"; filename="%s"\r\nContent-Type: application/octet-stream\r\n\r\n' % (boundary, filename)).encode()
        body += content + ('\r\n--%s--\r\n' % boundary).encode()
        return self._document_once(body, boundary)

    def _document_once(self, body, boundary):
        try:
            with urlopen(Request(self.base + 'sendDocument', body, {'Content-Type': 'multipart/form-data; boundary=' + boundary}), timeout=60) as response:
                result = json.load(response)
        except HTTPError as error:
            try:result = json.load(error)
            except (ValueError, OSError):raise APIError(error.code) from None
        except (URLError, OSError, ValueError):
            raise APIError(0) from None
        if not result.get('ok'):
            raise APIError(result.get('error_code', 0), result.get('parameters', {}).get('retry_after', 0), result.get('description', ''))
        return result['result']

    def photo(self, content, **params):
        """Upload a local proof; also supports replacing withdrawn media."""
        return self._with_rate_limit_retry(params.get('_method', 'sendPhoto'),
                                           lambda: self.photo_once(content, **dict(params)))

    def photo_once(self, content, **params):
        """Make one upload attempt for a durable outbox operation."""
        import secrets
        method = params.pop('_method', 'sendPhoto')
        boundary = secrets.token_hex(16)
        if method == 'editMessageMedia':
            params['media'] = {'type':'photo','media':'attach://photo',
                               'caption':params.pop('caption',''),'parse_mode':'HTML'}
        body = bytearray()
        for key, value in params.items():
            value = json.dumps(value) if isinstance(value,(dict,list,bool)) else str(value)
            body.extend(('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (boundary,key,value)).encode())
        body.extend(('--%s\r\nContent-Disposition: form-data; name="photo"; filename="proof.jpg"\r\nContent-Type: application/octet-stream\r\n\r\n' % boundary).encode())
        body.extend(content)
        body.extend(('\r\n--%s--\r\n' % boundary).encode())
        def send():
            try:
                with urlopen(Request(self.base+method,bytes(body),{'Content-Type':'multipart/form-data; boundary='+boundary}),timeout=60) as response:
                    result=json.load(response)
            except HTTPError as error:
                try:result=json.load(error)
                except (ValueError,OSError):raise APIError(error.code) from None
            except (URLError,OSError,ValueError):
                raise APIError(0) from None
            if not result.get('ok'):
                raise APIError(result.get('error_code',0),result.get('parameters',{}).get('retry_after',0),result.get('description',''))
            return result['result']
        return send()

    def download_document(self, document, max_bytes):
        """Download only a Telegram file ID to bounded memory; never use supplied URLs."""
        from urllib.parse import quote
        size = document.get('file_size')
        if size is not None and (type(size) is not int or size < 0 or size > max_bytes):
            raise ValueError('文件超过大小限制（%s MB）。' % (max_bytes//1024//1024))
        info = self.call('getFile', file_id=document['file_id'])
        path = info.get('file_path', '')
        if not path or path.startswith('/') or any(part in ('..','.') for part in path.split('/')):
            raise ValueError('Telegram 文件位置无效，请重新上传。')
        url = self.base.replace('/bot', '/file/bot', 1).rsplit('/', 1)[0] + '/' + quote(path, safe='/')
        try:
            with urlopen(Request(url), timeout=60) as response:
                content = response.read(max_bytes + 1)
        except (HTTPError, URLError, OSError):
            raise APIError(0) from None
        if len(content) > max_bytes:
            raise ValueError('文件超过大小限制（%s MB）。' % (max_bytes//1024//1024))
        return content
