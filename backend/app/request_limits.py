"""Bound JSON bodies before parsing, including chunked requests without length.

Media uploads retain their streaming byte counter and project/disk budgets.
"""
from fastapi.responses import JSONResponse

from .errors import AppError

MAX_JSON_BODY = 1024 * 1024


class JsonBodyLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get('path', '')
        if (scope['type'] != 'http' or scope.get('method') not in ('POST', 'PUT', 'PATCH')
                or not path.startswith('/api/') or path.endswith('/upload')):
            return await self.app(scope, receive, send)
        messages, total = [], 0
        while True:
            message = await receive()
            total += len(message.get('body', b''))
            if total > MAX_JSON_BODY:
                response = JSONResponse(AppError('REQUEST_TOO_LARGE', 'JSON request exceeds the application limit', 413).envelope(), status_code=413)
                return await response(scope, receive, send)
            messages.append(message)
            if message['type'] == 'http.disconnect' or not message.get('more_body', False):
                break
        async def replay():
            return messages.pop(0) if messages else await receive()
        return await self.app(scope, replay, send)
