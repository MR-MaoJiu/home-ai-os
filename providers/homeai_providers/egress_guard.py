"""已验证 SDK 的 Python 网络目的地门禁，不替代操作系统沙箱。"""
import sys

stats={'allowed_connections':0,'blocked_connections':0}
installed=False


def install_guard(allowed, label):
    global installed
    if installed:return
    def audit(event,args):
        if event=='socket.connect':
            address=args[1]
            if not isinstance(address,tuple) or tuple(address[:2]) not in allowed:
                stats['blocked_connections']+=1
                raise PermissionError(label + ' 出站目的地未授权')
            stats['allowed_connections']+=1
        elif event=='socket.getaddrinfo':
            host=args[0].decode('ascii') if isinstance(args[0],bytes) else args[0]
            if host not in {'127.0.0.1','::1',None}:
                stats['blocked_connections']+=1
                raise PermissionError(label + ' 不允许解析外部域名')
    sys.addaudithook(audit)
    installed=True


def install_mem0_guard():
    install_guard({('127.0.0.1',58080),('127.0.0.1',58081)}, 'Mem0')


def install_graphiti_guard():
    install_guard({('127.0.0.1',58082),('127.0.0.1',58081),('127.0.0.1',57687)}, 'Graphiti')
