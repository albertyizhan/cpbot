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
