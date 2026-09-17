"""阻止 Provider 直接导入核心，阻止任意 Shell 混入业务模块。"""
import ast
from pathlib import Path

errors=[]
for path in Path('providers').rglob('*.py'):
    tree=ast.parse(path.read_text())
    for node in ast.walk(tree):
        imports=[]
        if isinstance(node,ast.Import):imports=[n.name for n in node.names]
        if isinstance(node,ast.ImportFrom):imports=[node.module or '']
        if any(n=='homeai' or n.startswith('homeai.') for n in imports):errors.append(str(path)+': Provider 不得导入 Core')
for path in Path('server/homeai').glob('*.py'):
    tree=ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node,ast.Call) and any(k.arg=='shell' and isinstance(k.value,ast.Constant) and k.value.value is True for k in node.keywords):errors.append(str(path)+': 禁止 shell=True')
if errors:raise SystemExit('\n'.join(errors))
print('模块依赖与 Shell 边界检查通过')
