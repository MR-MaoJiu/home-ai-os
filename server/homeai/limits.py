from starlette.responses import JSONResponse


class BodyLimitMiddleware:
    """在解析 JSON/Multipart 之前限制实际收到的字节，不依赖 Content-Length。"""
    def __init__(self, app, limit=30 * 1024 * 1024):
        self.app,self.limit=app,limit

    async def __call__(self, scope, receive, send):
        if scope['type']!='http':
            return await self.app(scope,receive,send)
        body=bytearray()
        while True:
            message=await receive()
            if message['type']=='http.disconnect':return
            body.extend(message.get('body',b''))
            if len(body)>self.limit:
                return await JSONResponse({'detail':'请求正文超过限制'},status_code=413)(scope,receive,send)
            if not message.get('more_body',False):break
        used=False
        async def replay():
            nonlocal used
            if not used:
                used=True
                return {'type':'http.request','body':bytes(body),'more_body':False}
            return await receive()
        await self.app(scope,replay,send)
