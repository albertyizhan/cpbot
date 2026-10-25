import json
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


class APIError(Exception):
    def __init__(self, code, retry_after=0):
        self.code, self.retry_after = code, retry_after
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
            raise APIError(result.get('error_code', 0), result.get('parameters', {}).get('retry_after', 0))
        return result['result']
