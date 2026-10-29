import json
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


class APIError(Exception):
    def __init__(self, code, retry_after=0, description=""):
        self.code, self.retry_after, self.description = code, retry_after, description
        super().__init__('Telegram API error %s' % code)


class API:
    def __init__(self, token):
        self.base = 'https://api.telegram.org/bot' + token + '/'

    def call(self, method, **params):
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

    def document(self, chat, filename, content):
        import secrets
        boundary = secrets.token_hex(16)
        body = ('--%s\r\nContent-Disposition: form-data; name="chat_id"\r\n\r\n%s\r\n' % (boundary, chat)).encode()
        body += ('--%s\r\nContent-Disposition: form-data; name="document"; filename="%s"\r\nContent-Type: application/octet-stream\r\n\r\n' % (boundary, filename)).encode()
        body += content + ('\r\n--%s--\r\n' % boundary).encode()
        try:
            with urlopen(Request(self.base + 'sendDocument', body, {'Content-Type': 'multipart/form-data; boundary=' + boundary}), timeout=60) as response:
                result = json.load(response)
        except (HTTPError, URLError, OSError, ValueError):
            raise APIError(0) from None
        if not result.get('ok'):
            raise APIError(result.get('error_code', 0), description=result.get('description', ''))
        return result['result']

    def photo(self, content, **params):
        """Upload a local proof; also supports replacing withdrawn media."""
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
